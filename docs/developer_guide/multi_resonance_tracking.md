# Multi-Resonance ODMR Tracking — Design & Planning (Living Document)

> **Status:** DRAFT / planning. This is a *living* document — edit freely as the
> design firms up. Decisions that are settled go in the **Decision Log**; things
> we still need to resolve go in **Open Questions**. Date entries when you change
> something substantive.
>
> **Last updated:** 2026-06-23. **Current status:** Phase A (the FPGA cal-slot bank) is
> implemented and bench-verified, including cal-correct N=2 LO hopping — the cal slot
> follows `scan.current_step` in hardware (matched cal gives 31–32 dB image / 36–47 dB LO
> suppression on every hop). **Phase B (the N=2 multi-integrator in `odmr_freq_lock`) is
> IMPLEMENTED + BENCH-VERIFIED ON HARDWARE (2026-06-23):** `test_phaseB_multiint.py`
> passed 13/13 — `NSLOTS=2` live, slot-control reg, clear/disable zeros all slots, slot-0
> legacy aliases == per-slot bank, and the dynamic check (with a live demod error)
> confirmed only the active integrator moves, the idle slot stays at 0, and a slot switch
> **holds the previously-active integrator bumplessly** (slot 0 held at +1 MHz while slot 1
> integrated 0→~987 kHz). See Section 14 → Phase B "as implemented".
> **Phase B-stream (hop-boundary markers, Section 11) is IMPLEMENTED + BENCH-VERIFIED ON
> HARDWARE (2026-06-23):** `test_phaseB_stream.py` passed 15/15 — offline reconstruct math
> (6/6: fresh + ZOH-hold, transport-loss NaN kept, dead-time held, pre-first-dwell NaN),
> `STREAM_CONTROL[3]` round-trip, and the dynamic check ran an N=4 scan concurrently with
> `ftw_corr` streaming and captured clean hop markers `ticks=[0,178,243,309]
> steps=[0,1,2,3]` (concurrent scan+stream works; markers cover all steps with increasing
> ticks; no RF / no ARM push server needed). Internal `current_step`-edge marker capture in
> `scan_new.v` (tick→`ram_lsb`, step→`ram_msb`), `scan.py` `hop_stream_start` /
> `read_hop_markers` / `reconstruct_hop_series`.
> **Phase C (per-channel oscillator + 2nd lock-in chain + `aclken` freeze gating) is CODED
> (2026-06-23):** CIC/FIR IP regenerated with `Has_ACLKEN=true` (verified, 4/4 cores);
> new `odmr_multitrack.v` (region 9) = N=2 phase accumulators + shared 17-bit LUT
> (sin/cos/sin_shifted/cos_shifted of the active channel) + per-hop `T_settle` freeze
> window + per-channel `aclken`; `lock_in.v` gains an `aclken_i` freeze gate and is
> replicated to a 2nd chain (region 10); `odmr_multitrack.py` + `LockIn1` Python.
> Backward-safe: with the oscillator disabled the legacy `iq0` path is used and the
> single-resonance system is untouched. **Build #2 (after the 2 kHz prune + registered demod
> mux + audit fix) is DEPLOYED + REGISTER-VALIDATED on hardware (2026-06-23):
> `test_phaseC_regs.py` 10/10** — `NCH=2` live, multitrack regs round-trip, `lockin1`
> reachable, disabled→both chains free-run (legacy), enabled+sw→`run_mask` follows the
> selected resonance (per-channel freeze gating works in silicon). Resources now DSP 82.5 %
> / slices 92 % / BRAM 85 %. **Remaining (deferred): WNS −12.9 ns on the pre-existing odmr PI
> MAC** — genuinely single-cycle (not multicycle: the lock-in updates demod data coincident
> with the valid strobe), clean fix = pipeline the MAC, deferred per J.W. Build is
> functionally A/B-equivalent + Phase C features. **Next: bench-RF bring-up** (legacy lock →
> osc enable + re-tune `mu`/`kp`/`demod_phase` → 2-resonance freeze-and-resume, tune
> `settle_time`). See Section 14 → Phase C.
>
> **Scope:** Extend the existing single-resonance frequency-locked loop
> (`odmr_freq_lock`, see [odmr_freq_lock_implementation.md](odmr_freq_lock_implementation.md))
> to **track two or more NV resonances at once** on a single Red Pitaya + single
> LO + single IQ mixer, by **sequentially addressing** the resonances (hopping the
> external LO) and **frequency-multiplexing the lock-in modulation/demodulation**.

---

## 0. Current status & next step (session handoff)

**pyrpl / FPGA side is functionally complete for N=2 and bench-verified.** All four build
phases are implemented and on the board (live bitstream = **Phase C build #2**, 2026-06-23):
- **Phase A** — FPGA SSB cal-slot bank + cal-correct N=2 LO hopping (cal slot follows
  `scan.current_step` in HW; matched cal → 31–32 dB image / 35–47 dB LO suppression at every
  hop). `test_phaseA_*` verified. Section 14 → Phase A.
- **Phase B** — N=2 per-resonance integrators in `odmr_freq_lock` (bumpless hold of the
  parked slot). `test_phaseB_multiint.py` 13/13. Section 14 → Phase B.
- **Phase B-stream** — hop-boundary marker capture + per-resonance reconstruct
  (`scan.hop_stream_start` / `read_hop_markers` / `reconstruct_hop_series`).
  `test_phaseB_stream.py` 15/15. Sections 11, 14.
- **Phase C** — per-channel phase-continuous oscillator (`odmr_multitrack`, region 9) +
  2nd lock-in chain (region 10) + `aclken` freeze gating + `T_settle` window.
  `test_phaseC_regs.py` 10/10 (per-channel freeze selection verified in silicon).
  Section 14 → Phase C.

**Next: qudi integration** — wire the (done) tracker into qudi (hardware + logic + config +
GUI) so experiments run from qudi, not the standalone `test_phase*.py` scripts. This is also
where the **bench-RF bring-up** lives: re-tune `mu`/`kp`, calibrate `demod_phase`, tune
`settle_time`; acquire + fit each resonance, build the constant-IF LO `JUMP_LIST` + per-slot
SSB cal, run the hopping + hop-stream, reconstruct per-resonance drift.

**Remaining on the pyrpl / FPGA side (summary; details in Sections 10, 11, 14):**
1. **Continuous (indefinite) hopping — DONE + BENCH-VERIFIED (Option 1, 2026-06-24).**
   New bin ("Phase C build #2 + continuous mode") deployed + loaded; `test_continuous_hop.py`
   13/13 on hardware (continuous status latch; busy stays high / done never asserts over 2 s;
   `current_step` cycles 0..N-1; 1283 hop markers across many wraps with `Δstep≡1 (mod N)`
   incl. the wrap; clean stop), with `test_phaseB_stream.py` 15/15 + `test_phaseC_regs.py` 10/10
   unregressed on the same bin (finite scan still stops). Background: the `scan` FSM ran a
   *fixed* `num_steps` (≤4095, 12-bit)
   then stopped at `S_DONE`; it did **not** loop, so one `scan.start()` emitted only
   `num_steps` LO-hop triggers. (The Windfreak `c1` wraps its LO *table* indefinitely *per
   trigger*, but the RP only *emitted* `num_steps` triggers — different things; the
   "fixed-length list" limitation a colleague flagged, real.) **Fix shipped (decided over the
   Phase-D sequencer to unblock now; J.W. 2026-06-24):** a **continuous/loop mode** in
   `scan_new.v` — CONTROL **bit3** latched with START sets `reg_continuous`; in `S_FINISHING`
   the FSM then wraps `step_counter`→0 and re-enters `S_START_STEP` instead of `S_DONE`, so it
   keeps emitting LO-hop triggers + advancing `current_step` (0..`num_steps`-1) **forever**
   until stop/reset (busy stays high, done never asserts). STATUS **bit2** reads back
   `reg_continuous`. Pure RTL, no IP regen. Python: `Scan.start(continuous=True)`, the
   `Scan.continuous` status register, and `Scan.continuous_hop_start(nslots, ...)` /
   `continuous_hop_stop()` (one call: hop-marker push stream + indefinite loop). Test:
   `test_continuous_hop.py` (detects an old bitstream and exits cleanly). Reuses the entire
   bench-verified hop+freeze+stream+marker stack untouched (the stream engine + markers were
   already FSM-independent). The Phase-D on-FPGA sequencer remains the clean end-state but is
   no longer needed for indefinite operation. **Indefinite-run caveat (PC-side, not FPGA):**
   the 32-bit stream sample tick (`reg_stream_sample_cnt`) wraps at ~39 h — drain markers +
   stream frequently and reconstruct incrementally with wrap-safe deltas rather than one
   ever-growing array.
2. **odmr-MAC timing** — WNS −12.9 ns on the pre-existing odmr PI multiply-accumulate (it is
   genuinely single-cycle; clean fix = pipeline it). Deferred (J.W.); build #2 is
   functionally A/B-equivalent, so it doesn't block bench/qudi work.
3. **Phase D** — on-FPGA hop sequencer (co-drive LO trigger + slot index + valid gate +
   integrator hold). Subsumes item 1; generalizes N→4/8 (no room on this xc7z010 though).
4. **2D motor-scan composition** — **IMPLEMENTED (backend) 2026-07-01; NO 4th bank
   needed.** The original concern assumed the single-word hop-marker mode (which uses
   `ram_msb` for the resonance label). But the self-describing **marked** stream
   (`STREAM_CONTROL[5]`) carries the label INLINE in the `data3` triplets, leaving
   `ram_lsb`/`ram_msb` free — so KDC x/y position markers (`STREAM_CONTROL[2]`) coexist
   with the 4-trace stream in the SAME (unchanged) bitstream. Verified in `scan_new.v`:
   the marker-capture block and the lsb/msb write mux both key off `reg_marker_enable`
   independent of dual/marked, and `bram_wr_en` is gated off while streaming. Enabled by
   `Scan.hop_stream_start(input_source='marked', xy_markers=True)` (resets the stream so
   demod word 0 aligns with marker 0; markers are WORD indices → `//3` for the triplet
   index). qudi side: `MultiResonanceTrackingInterface.enable_position_markers` /
   `read_position_markers` / `read_stream_words` / `reconstruct_mapped_traces`, and the
   `ScanMode.KDC_HW_SYNC_MULTIRES` motor-scan mode (4 maps res{k}_err/res{k}_corr, binned
   by x-markers with nanmean fresh-only). Bench test pending.

**Operational notes (still current):** use the cfg LO-hop timing (RP trigger 50 µs / RP
settling 100 µs / Windfreak step-time `t≈1 ms` = 0.75×dwell, *not* the ~100 µs RF lock; §10);
long held-low triggers break the JUMP_LIST wrap (the level-low device re-reads the line and
double-steps). **Auto-verify the slot↔LO assignment at startup** (the `current_step↔LO`
offset can be 0 or 1).

---

## 1. Goal

Today we lock one NV resonance: an FM tone (~20 MHz IF) is generated by `fgen3`,
SSB-upconverted to 2.5–3.3 GHz by an external IQ mixer + LO, the NV fluorescence
is demodulated by the `lock_in` block, and `odmr_freq_lock` integrates the 1f-I
error into an FTW correction that is fed back into `fgen3` (it shifts the IF, and
therefore the microwave, to sit on the resonance zero-crossing).

We want to **track ≥2 resonances concurrently**. An NV ensemble has 8 electronic
resonances (4 axes × 2 spin transitions), each split into 3 hyperfine lines
spaced **±2.158 MHz**. The 8 lines move semi-independently with B-field and
temperature; the hyperfine triplets are rigid relative to their parent line.

We have **one** Red Pitaya, **one** LO (Windfreak SynthNV Pro), **one** IQ mixer.
So we cannot address multiple widely-separated microwave frequencies
simultaneously. The plan is to **time-share** the hardware: park the LO on
resonance A, track it for a dwell, hop the LO to resonance B, track it, hop back,
etc. — while keeping the per-resonance loop state alive across the gaps.

---

## 2. Current architecture (what we are extending)

Signal chain (verified against `red_pitaya_top.v`, `fgen3.py`,
`red_pitaya_3fgen.v`, `lock_in.py`, `odmr_freq_lock_1f.v`):

```
            f_m oscillator (IQ0)
                  │  iq0_sin  ────────────────┐ (FM source)
                  │  iq0_sin/cos/shifted ──┐  │ (demod refs)
                  ▼                        │  ▼
   ┌─────────────────────────┐             │ ┌────────────────────────────┐
   │ fgen3 (3-comp FM DDS)   │   DAC A/B   │ │ external IQ mixer + LO     │
   │  3 IF tones (~20 MHz),  │────I/Q─────►│ │ SSB upconvert → 2.5-3.3GHz │
   │  per-comp phase/amp/DC  │             │ └─────────────┬──────────────┘
   │  + ftw_correction (all) │             │               ▼  microwave
   └───────────▲─────────────┘             │            NV ensemble
               │ ftw_correction            │               │ fluorescence
   ┌───────────┴────────────┐              │               ▼
   │ odmr_freq_lock (I/PI)  │◄── err ──────┼──── ADC A ─► photodiode
   │  ±15 MHz integrator    │  (1f-I)      │               │
   └────────────────────────┘              └─ lock_in (CIC R=4096 + FIR) ◄┘
                                              ch1 = I (error), ch2 = Q
                                              out @ ~30.5 kS/s
```

**Key facts that shape the multi-resonance design:**

- **Two frequency knobs, two ranges.** Coarse = external **LO** (sets *which*
  resonance, hundreds of MHz of reach). Fine = **`fgen3` IF / `ftw_correction`**
  (tracks drift, but only up to ~15 MHz — see `ftw_lim`/`max_correction_hz`).
- **The 3 components of `fgen3` are the hyperfine triplet.** Three IF tones
  summed on DAC A/B, with the FTW correction applied to *all three* at once
  (`red_pitaya_3fgen.v` lines ~287). So one `odmr_freq_lock` loop already drives a
  full hyperfine-triplet drive for a single parent resonance. Good — a "resonance"
  for us = one parent line driven as a triplet.
- **One modulation oscillator today.** `fm_mod_in = iq0_sin` and *all* lock-in
  references come from IQ0. There is a second IQ (`iq2`) instantiated, currently
  used elsewhere; `lock_in` does **not** reference it.
- **`ftw_correction` is global to `fgen3`.** There is exactly one correction bus;
  it shifts every component. There is currently no notion of "which resonance".
- **A hardware trigger output already exists.** `scan_trigger_o` → `exp_p_dat`
  bit 7 → **DIO7_P** (`red_pitaya_top.v` line 458). The scan block
  (`scan_new.v`) already knows how to emit programmable-width trigger pulses with
  programmable dwell — the *same mechanism* we want for hopping the LO table.
- **Lock-in timescales.** CIC R=4096 → 30,517.6 S/s (Ts ≈ 32.768 µs). FIR adds
  ~1 ms group delay (`filter_select` 500 Hz ≈ 9 ms latency; 2 kHz / 5 kHz faster).
  The SynthNV Pro settles in **100 µs** (Section 10). The freeze-and-resume scheme
  (Section 5) **holds** the filter state across a hop, so the per-hop dead time is
  the ~100–300 µs physical settle, *not* a filter refill — which frees the lock-in
  filter choice from the hop rate (Section 7).

---

## 3. Physics constraints we must respect

| Quantity | Value | Implication |
|---|---|---|
| Parent resonances | up to 8, in 2.5–3.3 GHz | hundreds of MHz spread ⇒ must move LO |
| Hyperfine splitting | ±2.158 MHz (rigid) | handled by the 3 `fgen3` components at fixed IF spacing |
| IF tracking range | typically ±15 MHz (set by `ftw_lim`/`max_correction_hz`; ±1 MHz is only the register default) | long-term drift may eventually need coarse LO re-centering |
| Lock-in output rate | 30.5 kS/s | one error sample per 32.768 µs |
| Lock-in FIR group delay | ~1 ms (2 kHz) … ~9 ms (500 Hz) | *not* a per-hop cost under freeze-and-resume (filter state is held, Section 5) |
| LO re-lock + pipeline τ | ~100–300 µs | the actual per-hop dead time (Section 7) |

---

## 4. Core architectural decision: keep the IF constant, hop the LO

**Proposal:** For every resonance *i*, choose the LO table entry `f_LO,i` so that
the parent line lands at the **same IF center** `f_IF` (e.g. ~20 MHz) on the
Red Pitaya. The microwave is `f_MW,i = f_LO,i ± f_IF`; we move `f_LO,i`, not the
IF, to bring each resonance to the same Red Pitaya working point. The `ftw_correction` (typically ±15 MHz range) then only ever absorbs *drift within a dwell* plus the slow accumulated offset since the last coarse
re-centering.

**Consequence / management layer:** different resonances drift differently, so
their required IF centers diverge over time. The ±15 MHz IF range is generous, but
if a resonance's accumulated correction ever approaches the configured `ftw_lim`,
we could think about nudging `f_LO,i` in the table and re-zero that resonance's integrator (bumpless
transfer). This "coarse recapture" is slow and could be done in software. See Section 12. This is something to keep in mind but not a near-term implementation.

---

## 5. Multiplexing strategy: freeze-and-resume

When resonance *i* is parked out, **stop clocking its entire
signal-processing chain** — its modulation oscillator, its FM drive into `fgen3`, and
its lock-in (CIC + FIR) — i.e. drive every stage's clock-enable low and **hold the
register contents**. Nothing decays; the demodulator keeps its converged, locked
state. On return, re-enable the chain and it continues exactly where it left off.

> **Why freezing helps:** it changes the per-hop dead time from
> "lock-in filter refill" (bandwidth-dependent, up to ~9 ms for the 500 Hz FIR) to
> "physical settle" (LO re-lock ~100 µs + analog/optical pipeline delay τ,
> filter-independent). The narrow, high-SNR filter no longer costs a refill on every
> hop — its long memory becomes a *feature* (it retains resonance-*i* history across
> visits). This **decouples the lock-in bandwidth choice from the hop rate**.

Why the freeze is physically valid (the coherence argument):

- **Modulation/demodulation stay coherent** because, for each channel, modulation and
  demodulation are driven by the **same per-channel phase** (that channel's phase
  accumulator feeds both the FM drive and the lock-in references — Section 5.1; today a
  single `iq0` does this for one channel). Freezing that channel's phase accumulator
  freezes mod and demod *together* and holds `φ_i`, so their relative phase — and hence
  the demod phase calibration `2π·f_m·τ` — is preserved across the gap, and the channel
  resumes at exactly `φ_i` (phase-continuous). This also stops the channel's FM drive
  cleanly. (A single *free-running* oscillator shared across channels is rejected — it
  would resume a parked channel at an advanced phase; Section 5.1.)
- **The held filter state does not decay** — it is digital register contents, held
  exactly while frozen. On resume the output is immediately a *valid* (if slightly stale)
  estimate of resonance-*i*'s error, and it ramps to the current value quickly again.
  So the loop never goes blind; it resumes acting at once.
- **Absolute LO phase doesn't matter** (P-7): fluorescence is an incoherent
  intensity response to MW *frequency*; the lock-in detects amplitude modulation at
  `f_m`, not MW phase. So the LO hopping away and back (non-phase-continuous) is
  benign — we only wait out its ~100 µs frequency re-lock.

What freezing does **not** remove (the residual dead time):

1. **A physical-settle / valid window on resume.** For ~(LO lock 100 µs + pipeline
   delay τ) after re-enabling, the ADC still carries the LO transient and the tail
   of the previous resonance's fluorescence. **Gate** the chain's clock-enable to
   stay frozen through this window, then resume clocking. Small (~100–300 µs) and
   filter-independent.
2. **Genuine staleness.** The held error reflects `f_r,i` from a gap ago; the
   resonance drifted by (drift rate × gap) meanwhile. On resume the loop sees that
   real step and corrects it. This is fundamental to time-sharing (you can't track a
   line faster than you visit it) — not a filter artifact.
3. **Implementation care:** the CIC integrator/comb stages *and* the decimation
   counter must freeze and resume as one unit (gate the whole chain's clock-enable),
   so the decimation window is continuous in *clocked* samples across the wall-clock
   gap.

Mapping back to the original design questions:

- **Parked modulation phase:** *held per channel* — freeze that channel's phase
  accumulator at `φ_i` and resume from `φ_i` (Section 5.1). Keeps mod+demod coherent for
  the channel and cleanly stops its FM drive — no off-resonant tone emitted.
- **Demodulation during the gap:** *paused* (freeze the lock-in). Freezing avoids
  both contamination from the other resonance *and* any filter refill on return.
- **Frequency tracker during the gap:** **holds** its integrator (the existing
  `hold` bit), resuming from the held value. This needs **N independent integrator
  states** (Section 8).

### 5.1 Per-channel phase-continuous modulation/demodulation

**Requirement: freeze/resume must be phase-continuous *per channel*.** When
channel *i* is parked at modulation phase `φ_i` (to service another resonance), it must
resume at **exactly `φ_i`**. Each channel therefore carries its own modulation phase across the gap, and
modulation+demodulation stay coherent for that channel because they are driven by the
*same* per-channel phase.

**Efficient implementation: N phase accumulators + ONE shared sine LUT (no LUT replication).**
The expensive block (the quarter-wave sin/cos LUT) is **not** replicated. Instead:
- Keep **N per-channel phase accumulators** (one ~32-bit accumulator per resonance), all
  using the *same* FTW. Cheap — a register + adder each.
- **Clock-enable per accumulator:** only the *active* channel's accumulator increments;
  the parked ones **hold** their phase `φ_i`.
- The **active** accumulator's phase feeds the **single shared** quarter-wave LUT →
  `sin/cos` (+ `sin_shifted/cos_shifted` for the demod phase offset), which drive *both*
  the FM tone into `fgen3` *and* the demod references of the active lock-in chain.

This makes each channel behave **exactly as if it were the only channel** — its
modulation timeline is gap-free in *clocked phase*, so there is **no resume transient**.
The mod↔demod relative phase stays `2π·f_m·τ` (fixed path delay τ), so the single
shared demod-phase-offset (Section 6) still covers all channels.

**One clock-enable domain per channel.** A hop freezes, as one coherent unit gated by the
channel's active/valid signal: the channel's **phase accumulator** + its `fgen3` FM drive+ its lock-in (CIC/FIR, Section 13) + its integrator (Section 8). They freeze and resume together (P-3).

---

## 6. Modulation frequency: keep `f_m = 15.25 kHz` for all resonances

Because the freeze-and-resume scheme (Section 5) only ever clocks one demod chain at
a time, there is no simultaneous cross-talk to suppress — so **every resonance uses
the same, proven `f_m ≈ 15.25 kHz`.** This also means the demodulation phase
calibration `2π·f_m·τ` is identical for all resonances, so one phase setting covers
everything.


---

## 7. Interleaving & timing budget

Per-resonance effective sample rate and loop bandwidth are reduced by the duty
cycle. For N resonances with dwell `T_dwell` and hop overhead `T_hop`:

```
cycle period        T_cyc   = N · (T_dwell + T_hop)
per-resonance rate  f_eff,i = 1 / T_cyc
useful samples/dwell        = max(0, (T_dwell − T_settle) / Ts)
```

where `T_hop` = LO settling + cal-slot apply, and `T_settle` = the dead time before
clocked samples are usable again. Under freeze-and-resume `T_settle` is just the
**physical settle** = LO re-lock (~100 µs) + analog/optical pipeline delay τ,
**independent of the lock-in filter** (the filter held its state, so there is no
refill). So the whole per-hop dead time `T_settle + T_hop` is ≈ **200–550 µs**
regardless of FIR choice.

Design tensions:
- **`T_dwell` large** ⇒ more fresh samples per visit, but lower `f_eff,i`. Each
  resonance's *closed-loop* bandwidth is capped by both `f_eff,i` *and* the lock-in
  bandwidth, well below the single-resonance 150–300 Hz.
- **`T_dwell` small** ⇒ faster revisit, but the fixed `T_settle + T_hop` dead time
  eats the duty cycle.
- **Narrow filter + short dwell:** if `T_dwell` ≪ one FIR window (~1 ms), each visit
  only adds a few fresh samples to a window that still spans previous visits — so the
  per-resonance estimate is effectively averaged over *many* visits. Good for SNR but
  lengthens the effective tracking time constant. Same SNR ↔ bandwidth trade as
  always, just spread across visits; size `T_dwell` and filter against the real drift
  rates (OQ-5).



**Both `T_dwell` (dwell / hop rate) and `T_settle` (dead time before the demod valid
window opens) must be runtime-configurable registers** (#2, 2026-06-21). `T_dwell` is
the scan `dwell_time`; `T_settle` is a new register that holds the chain frozen
(`aclken`/`tvalid` low) for a programmable count after each hop before clocking resumes.
The right values are best found empirically — sweep a few and watch lock quality vs duty
cycle — so they stay parameters, not hard-coded constants. (Maximize `f_eff,i` per OQ-5:
shrink `T_dwell` toward `T_settle`, then back off until lock SNR is acceptable.)

---

## 8. Per-resonance loop state (the integrator problem)

Today `odmr_freq_lock_1f.v` has **one** integrator (`ftw_corr`) and emits **one**
`ftw_correction` that `fgen3` adds to all components. For N resonances we need:

- **N integrator states**, one per resonance, each holding its accumulated IF
  offset while the others are serviced.
- A **slot selector** synchronized with the LO hop: when resonance *i* is active
  and its valid window is open, route the demod error to integrator *i*, update it,
  and drive `fgen3`'s `ftw_correction` from integrator *i*. When the window is
  closed, hold all integrators.
- **Bumpless behavior**: resuming integrator *i* must continue from its stored
  value — never reset on a hop (only on explicit coarse-recapture, Section 12).

(8a) FPGA-resident N-integrator module ("`odmr_multitrack`"): replicate the
  control datapath ×N (or time-share one datapath across N state registers,
  cheaper since only one is active at a time) plus the slot/valid logic. This is
  the deterministic end-state.x

The existing `hold` bit, `clear`, and `ftw_int` readback are the primitives we
build on.

Because each integrator changes **only while its resonance is live** and **holds**
otherwise, the active integrator's value — which already drives `ftw_correction`,
and hence the existing `ftw_corr` stream — *is* the per-tick fresh correction the
monitoring stream needs (Section 11). The PC reconstructs each parked resonance's
held value by zero-order hold, so Phase B need only (a) drive `ftw_correction` from
the `current_step`-selected integrator and (b) make `current_step` available for the
hop-boundary markers; it does **not** need to stream all N integrators continuously.



---

## 9. SSB calibration when hopping (apply correct corrections fast)

Each working point needs its own SSB output correction: per-component
`phase_offset_a/b`, per-component `amplitude_a/b`, and the global
`overall_dc_offset_a/b` carrier-null terms. **These differ per resonance** because the
SSB calibration depends strongly on the LO frequency (observed empirically), not just
the IF (Section 4, corrected 2026-06-19). Constant IF removes the IF-axis variation,
but each `f_LO,i` still has its own distinct SSB correction set. So this is a real
per-hop problem, and the active correction set must switch within the hop's settling
window — making **(9b) the necessary end-state, not an optimization.**

**Selected architecture: 9b, FPGA cal-slot bank.** Pre-load N calibration sets into
an on-FPGA register bank once; a single **active-slot index** muxes the right set into
the `fgen3` datapath. The hop then changes only the slot index (one write, or
hardware-sequenced alongside the LO trigger), applied in one clock with no
software-timing jitter. This is implemented in `red_pitaya_3fgen.v` as slotted
register arrays plus an active-slot mux.

Per-`f_m` **demod phase stays global** in the N=2 constant-IF design: with shared
`f_m = 15.25 kHz`, the demod phase is identical for all resonances (Section 6), so it
does not belong in the cal-slot bank.

**MW amplitude is *not* a per-resonance variable:** MW power is fixed at **13 dBm for
every hop entry** (decided 2026-06-21). The SynthNV Pro hop table carries a per-point
amplitude field, but we set it identically for all entries. (Per-resonance *IF*
amplitude `A` still lives inside the cal slot's `amp_a/amp_b`.)

### What the calibration data actually contains

We already produce SSB calibration tables (e.g.
`C:\calibration_results\2026-01-21-10-23-24\`). Each row is keyed by
**(sideband, IF, LO, IF-amplitude)** and stores the four correction parameters:

| CSV column | Meaning | Maps to `fgen3` |
|---|---|---|
| `g` | I/Q amplitude-imbalance correction | per-component `amplitude_a/b` ratio |
| `phi` | I/Q phase-imbalance correction (rad) | per-component `phase_offset_a/b` |
| `I_offset` | carrier-null DC offset, I | `overall_dc_offset_a` |
| `Q_offset` | carrier-null DC offset, Q | `overall_dc_offset_b` |

plus achieved `lo_leakage_dbm`, `image_power_dbm`, `sfdr_db` (quality metrics for
picking the best row). Two important structural facts:

- **The table is explicitly a function of LO** (each IF folder sweeps LO 2.62–3.12
  GHz) — direct confirmation of P-4b. A working point is a *single row* selected by
  (sideband, IF, LO, amplitude).
- **The IF grid IS the hyperfine triplet.** The IF folders (19.422 / 21.580 /
  23.738 MHz) are spaced exactly 2.158 MHz, i.e. one IF per hyperfine line. So each
  resonance's three `fgen3` components draw from the *three* IF rows at that LO —
  the calibration already covers the triplet.

**Final per-resonance setup:** the PC selects three CSV rows (one per hyperfine
component) plus the corresponding LO hop-list entry. The LO/power entry stays in the
SynthNV hop table; the FPGA cal slot contains only the converted `fgen3` SSB-correction
words. The PC converts each component with the resolved mapping below and writes the
pre-converted words into the slot. The conversion is deliberately kept in the existing
qudi calibration-apply layer; the FPGA only stores and muxes the already-converted
words.

### Calibration-register mapping — `(g, phi)` → `fgen3` words

The single-resonance calibration-apply path lives in the qudi `RedPitayaIFSource` /
`IFSourceBase` (`qudi-iqo-modules/.../microwave/redpitaya/redpitaya_if_source.py`,
`if_source_base.py`), **not** in pyrpl. Two entry points apply the *same* formula:
`set_iq_correction()` (single component) and `set_multi_frequency_signal()` →
`configure_signal()` (interpolates the CSV per component, then applies it). Per
frequency component *i*, given calibration `(g, phi)` (`phi` in **radians**), the IF
amplitude `A in [0,1]`, and the sideband:

```
amplitude_a{i}    = A * (1 + g)                       # I-channel amplitude (DAC A)
amplitude_b{i}    = A * (1 - g)                       # Q-channel amplitude (DAC B)
phase_offset_a{i} = 0.0 deg                           # I phase reference (always 0)
phase_offset_b{i} = (q_base + degrees(phi)) mod 360  # I/Q quadrature + phase corr.
                    q_base = 270 deg (USB/'upper')  or  90 deg (LSB/'lower')
```

**DC offsets are global, not per-component.** `set_dc_offsets()` writes
`overall_dc_offset_a = mean(I_offset)` and `overall_dc_offset_b = mean(Q_offset)`,
averaged over the active components (they are physical channel-voltage offsets =
carrier null), into the two global `overall_dc_offset_a/b` registers.

**Register semantics (`fgen3.py`) — the on-FPGA words a cal slot must hold:**

| `fgen3` register | type | width | hardware word |
|---|---|---|---|
| `amplitude_a{i}`, `amplitude_b{i}` | FloatRegister, **unsigned**, norm 2^13, [0,1] | 14 bit | `round(amp * 2^13)` |
| `phase_offset_b{i}` | PhaseRegister | 32 bit | `round((deg/360)*2^32) mod 2^32` |
| `phase_offset_a{i}` | (constant 0) | 32 bit | `0` |
| `overall_dc_offset_a/b` | FloatRegister, **signed**, norm 2^13, [-1,1] | 14 bit | `round(off * 2^13)` |

**So a cal slot (per resonance) is:**
- 3 x { `amplitude_a` (14b), `amplitude_b` (14b), `phase_offset_b` (32b) } — AC SSB
  correction, one triple per hyperfine component (`phase_offset_a == 0`, can be
  hardwired);
- 1 x { `overall_dc_offset_a` (14b), `overall_dc_offset_b` (14b) } — shared carrier null.

= 3*60 + 28 = **208 bits ~ 26 bytes/slot**. For N=8 that is ~208 bytes total —
**small enough to live in FPGA registers, with no BRAM needed.**

**What is NOT in the slot** (constant across resonances under constant-IF, Section 4):
the three component **frequencies** (the hyperfine IF triplet), **FM deviation**, the
**FM mod frequency**, and the **demod phase** (shared `f_m = 15.25 kHz` => identical
`2*pi*f_m*tau` for all). These stay global. *Only the SSB-correction quantities above
vary per resonance* — which is exactly the cal-slot bank's contents.

**Implication for the slot mux (9b):** the PC pre-computes each resonance's 208-bit
slot once (interpolate `(g, phi, I_off, Q_off)` at that resonance's LO + per-component
IF + amplitude from the CSVs, apply the formulas above to get the register words),
writes all N slots into the FPGA bank, and thereafter the hop only advances the
**active-slot index**; the mux drives `amplitude_a/b{0..2}`, `phase_offset_b{0..2}`,
`overall_dc_offset_a/b` from `slot[idx]` into the `fgen3` datapath in one clock.
Switching `phase_offset_b` is a *static* I/Q quadrature realignment (added before the
LUT) — independent of the running phase-accumulator value, so it is benign across a hop
and consistent with freeze-and-resume.

### Phase A — concrete RTL sketch for the cal-slot bank (`red_pitaya_3fgen.v`)

Grounded in the current module (read 2026-06-20). **Design finalized 2026-06-21** after
review with J.W. — decisions: (1) **clean packed cal-bank** (a dedicated contiguous
region; *all* slots incl. slot 0 live there — **not** aliased onto the legacy comp
addresses; this deliberately breaks the legacy single-resonance register map, handled at
the Python layer); (2) keep **both** a software `active_slot` and an `active_slot_src`
selector; (3) **`comp_enable` stays global** (every resonance is a full triplet);
(4) **no cal-change output gate in Phase A** — see "phase-step" below.

**What is slotted.** Only the SSB-cal quantities, and `phase_offset_a` is *dropped*
(OQ-8: I-phase reference ≡ 0 always → hardwire `phase_eff_a = phase_acc`, i.e. no
I-offset). So a slot holds exactly the 208-bit OQ-8 set: per component `{amp_a, amp_b,
phase_b}` ×3, plus `{dc_a, dc_b}`. The non-cal per-component regs (`comp_freq_step`,
`fm_enable`, `fm_deviation_kHz`, `comp_enable`) and `ftw_correction_reg` (frequency only)
stay single/global and untouched — slotting amp/phase is orthogonal to the freq-lock.

1. **Slotted storage** (`NSLOTS=2`, generalizes to 8):
   `cal_amplitude_a[NSLOTS][3]`, `cal_amplitude_b[NSLOTS][3]`,
   `cal_phase_offset_b[NSLOTS][3]`, `cal_dc_offset_a[NSLOTS]`, `cal_dc_offset_b[NSLOTS]`
   (11 regs/slot).
2. **Addressing — dedicated cal-bank at `0x0200`, slot stride `0x40`** (slot `s` at
   `0x200 + s*0x40`). Within a slot: `+0x00` dc_a, `+0x04` dc_b, then per component `c`
   at `+0x08 + c*0x0C`: `+0x00` amp_a, `+0x04` amp_b, `+0x08` phase_b. (11 regs ⇒ 0x2C
   used of the 0x40 stride; N=8 ⇒ 0x200..0x400, inside the 64 KB region.) The legacy
   cal addresses (`0x04/0x08` DC and the per-comp `+0x04/+0x08/+0x0C/+0x10`) are
   **removed**; legacy comp blocks keep only the non-cal regs (`0x10` freq, `0x24` fm_en,
   `0x28` fm_dev, `0x2C` comp_en).
3. **Active-slot select** — control reg at free global address `0x000C`:
   bit0 = `active_slot` (sw), bit[…] = `active_slot_src` (0 = sw `active_slot`;
   1 = hardware `current_step` from the scan block). Reset = 0 ⇒ slot 0, sw-driven.
4. **Mux into the datapath (registered read-path swap).** `sel = active_slot_src ?
   current_step_i[BITS-1:0] : active_slot`; `eff_amp_a[i]=cal_amplitude_a[sel][i]` etc.;
   `eff_phase_a[i]=0` (hardwired); feed `eff_*` into stage 3 (`phase_eff_*`, ~L304),
   stage 5 (`prod_*` amplitudes, ~L339), stage 7 (DC add, ~L377). Register `sel` so the
   swap is glitch-free; a value change still blends old/new slot across the ~few-cycle
   pipeline depth (tens of ns) — bounded and inside any blanking.
5. **New top-level input** `current_step_i` (`ceil(log2 NSLOTS)` bits; 1 for N=2) routed
   from the scan block via `red_pitaya_dsp.v`/`red_pitaya_top.v` (the cross-module wire,
   Section 14). Until it exists, `active_slot_src=0` and drive the slot from software.

**Independently testable** with no other phase present: load slot 0 and slot 1 with
deliberately different SSB cal, toggle `active_slot`, and scope the image / LO-leakage —
each slot should null *its own* working point (judge steady state, not the transition).

**Python (`fgen3.py`).** The legacy map breaks, so: replace the per-comp
`amplitude_a/b{i}`/`phase_offset_b{i}` and `overall_dc_offset_a/b` descriptors with
cal-bank descriptors (or keep the old *attribute names* as thin proxies onto slot 0 for
single-resonance convenience), add `active_slot` + `active_slot_src`, and add
`load_cal_slot(slot, comps, dc_a, dc_b)` taking *already-converted* register words. The
`(g,phi)`→words conversion stays in the qudi `RedPitayaIFSource` layer (OQ-8), and that
layer is updated to call `load_cal_slot` (its current direct `setattr` of
`amplitude_a{i}` etc. is what the map change touches). Slot cal regs go in
`_setup_attributes` so states persist.

---

## 10. LO source: Windfreak SynthNV Pro integration

Intended mechanism (matches the brief): pre-load a N-**frequency/amplitude hop
table** in the SynthNV Pro and **advance it with an external trigger pulse** from
the Red Pitaya — similar to how `scan_new.v` steps a sweep. The existing
`scan_trigger_o` on **DIO7_P** is the trigger source (it already emits programmable-width pulses 
on a programmable schedule).

**Confirmed from the datasheet** (`docs/manuals/synthnvpro-rf-signal-generator-detector.pdf`;
extracted text in `synthnvpro_extracted.txt`):

- **RF lock time: 100 µs (standard); 250 µs per step typical sweep speed.** This
  is the whole reason the LO is *not* the timing bottleneck (Section 7).
- **500-point frequency *and amplitude* hop table.** So we *could* pre-load a distinct
  MW power per resonance on-device; in practice power is fixed at **13 dBm for all
  entries** (decided 2026-06-21), so we use only the per-point frequency and leave the
  amplitude field constant.

### The hop mechanism already exists in our stack

The qudi driver `mw_source_windfreak_synthnvpro_redpitaya.py`
 already implements "software configures, FPGA triggers" — exactly
the pattern we need. Key serial commands it uses (resolves most of OQ-2):

- **`y2` = single-step trigger mode** — each external trigger pulse advances the
  sweep/table by one step. (`y0` = software trigger.) This *is* the hop-advance
  command.
- **Equidistant sweep:** `l`/`u`/`s` = lower/upper/step (MHz), `^` direction,
  `c0` non-continuous, `g1g0` arm + go.
- **Jump list (arbitrary freq + power per entry):** `L{i}f{MHz}L{i}a{dBm}` per
  entry, `Ld` to clear, `X1` tabular mode. This is the per-resonance LO+power table.
- **Trigger is level-low (not edge), and the step-time `t` sizes the low window.** The
  SynthNV Pro is **level-sensitive and low-active** (`Y0`): it acts while the trigger line
  is held *low*. The driver sets the device **step-time** with the serial `t{ms}` command
  to **0.75 × the per-step period** (`step_time_ms = 1000 * 0.75 / sample_rate`, in
  `mw_source_windfreak_synthnvpro_redpitaya.py::_configure_windfreak_list`), so the low
  window is 75 % of the dwell and **releases (goes high) inside the LO's settling
  dead-time**. The LO finishes its jump + re-lock with the line already high, so one low
  pulse steps exactly once — hold it low too long and the device re-reads the level and
  double-steps (this is why "long triggers break the wrap").

> ✅ **Arbitrary `JUMP_LIST` works (bench-verified 2026-06-20, FW 2.05).** Although
> *undocumented* in both the SynthNV Pro and SynthHD serial manuals (the `?` help
> lists no `L` command), the `L` table commands are real and functional. Arbitrary,
> **non-equidistant** frequency+power tables hop one point per Red Pitaya trigger.
> So the "equidistant-only" worry is moot — full multi-resonance hopping is
> supported. Verified command sequence and quirks:
>
> - **Program:** `Ld` (clear), then per point `L{i}f{MHz}` and `L{i}a{dBm}`.
> - **Mode:** `X1` (tabular), `y2` (one point per trigger), `c1` (wrap), `^1`.
> - **Arm:** `g1g0` → sits at point 0.
> - **`Z0`** (mandatory): temp-comp bug otherwise forces sweep power → CW power every
>   ~10 s (±10 dB jumps between hops).
> - **`Y0`** trigger polarity active-low — matches the RP driving DIO7_P as an
>   *inverted* module output (idle high, active-low pulse).
> - **No priming trigger:** after `g1g0` the output sits on point 0 and **each trigger
>   advances exactly one point** (scope-verified at the antenna/circulator port). The
>   `f?` register reports the *next* (pre-loaded) point, so serial-only testing can
>   look like the first trigger is redundant — it is not.
> - **Indefinite hardware-only cycling:** with `c1`, the table **wraps last→first and
>   repeats forever on triggers alone** — no software resets, no padded long list. A
>   2-point table gives `f1→f2→f1→f2→…` indefinitely; the wrap costs exactly one
>   trigger like any other step (verified over 24 triggers / 12 wraps, scope ground
>   truth). This is the mechanism for the sequential-hop tracking.
> - **One autonomous scan drives the whole cycle, and `scan.current_step` is
>   frequency-synced.** A single `rp.scan.start()` (`num_steps=N`, `dwell_time` per
>   point) emits the entire trigger sequence in hardware; scope-verified that the
>   actual LO frequency at each step matched `freqs[(step+1) mod N]` exactly (3 cycles
>   of a 3-point list). The FPGA `current_step` register tracks 1:1 with the active LO
>   frequency — a **hardware index of "which resonance is live"** for binning demod
>   samples per resonance (cf. the MODE-3 marker allocation). `num_steps` is 12-bit
>   (≤4095/scan); for unbounded running, restart per scan or drive triggers from the
>   freeze-and-resume sequencer.
>
> **Verification method:** RP `scan` emits the trigger on DIO7_P; an R&S RTO6
> (`TCPIP0::10.203.129.15`, `RsInstrument`, FFT on Ch1) measured the actual peak
> frequency at each step = ground truth.
>
> Implemented in `mw_source_windfreak_synthnvpro_redpitaya.py::_configure_windfreak_list`
> (+ `arm_list()`). Still nice-to-have: trigger→RF latency/jitter number
> (phase-continuity across entries is benign for the `f_m` lock-in, P-7).

---

## 11. PC data streaming & sample → (resonance, time-bin) allocation

> **Ring-architecture decision for indefinite operation (J.W. 2026-06-24): keep scheme
> (a) — ONE data3 push ring (live resonance value) + hop-boundary markers — and make the
> robustness improvements PC-side.** Considered: (a) one ring + markers (current); (b) extend
> the ARM `stream_server.c` to also drain the marker rings into one coherent pushed stream;
> (c) per-resonance data rings. Chose (a): the marker tick is literally
> `reg_stream_sample_cnt` — the *same* hardware counter the data ring uses — and the ARM
> server guarantees frame continuity + explicit NaN-gap counts, so the PC's contiguous array
> index *is* the absolute tick; markers and data **cannot desync by construction** (the only
> failure modes are marker-ring overflow, already detected/warned, and the 32-bit tick wrap).
> So robust *indefinite* multi-resonance operation needs **no new ring architecture** — just
> drain markers + stream frequently and reconstruct **incrementally/windowed** with wrap-safe
> deltas (don't hold one ever-growing array; tick wraps at ~39 h). (b) is reserved as an
> *ARM-only* upgrade (no FPGA build, no BRAM) if the dual transport ever proves operationally
> awkward. (c) is rejected: BRAM cost on an 85%-full xc7z010, an FPGA rebuild, ARM multi-ring
> support, and it's hard-blocked for N>2. This refines OQ-11 (live-value+markers) for the
> continuous case.

How the per-resonance frequency-**correction** (or error) signals are conveyed to
the PC and placed on a common time axis. Two use-cases drive the design:

- **(A) Live time monitoring (build now).** Watch how *all* tracked resonances'
  corrections / errors evolve over time — the multi-resonance generalization of
  today's single-quantity `demod` / `ftw_corr` push stream (`scan_new.v`,
  `scan.py`; `docs/developer_guide/scan_data_streaming.md`).
- **(B) 2D motor-scan mapping (design-for now, build later).** During a
  hardware-synced 2D stage scan, allocate *each resonance's* correction to each
  spatial pixel — the multi-resonance generalization of the single-quantity marker
  scan (`docs/developer_guide/motor_position_sync_scan.md`). We do **not** implement
  2D now, but the stream format below is chosen so the 2D map is a later *slice* of
  the very same stream.

### The invariant (hard requirement, J.W. 2026-06-22): deterministic (time, resonance) → value

The one thing that must hold, no matter the transport: the PC must reconstruct,
**absolutely deterministically, at what wall-clock time which resonance's
correction/error was applied/measured.** Those values *are* the measurement — each
encodes a B-field / temperature sample of resonance *i* at a specific time — so a
wrong or ambiguous time/resonance assignment corrupts the physics.

Everything else (whether we stream continuously, how we pack words) is free to
choose, *subject to* this invariant. Concretely, the time base is a **single
free-running wall-clock tick counter** at the demod cadence (one increment per
4096-cycle period, `Ts ≈ 32.768 µs`), **never frozen** (only the demod *chains*
freeze). `tick · Ts` is absolute time; the push stream's existing gap-true sample
index already realises it (NaN only for transport loss).

### Recommended transport: live-value stream + hop-boundary markers

One ring carries the fresh values; a small marker ring carries the resonance
schedule. Both share the free-running tick counter, exactly mirroring the proven
motor-scan marker mechanism (`motor_position_sync_scan.md`) — but the boundary
event here is an **internal hop edge** (`current_step` change) instead of an
external KDC pulse:

- **Data ring (`data3`):** the live resonance's fresh value, one per tick (= today's
  `ftw_corr`/`demod` stream, unchanged). Absolute time of sample `k` = its gap-true
  push index `k`; NaN means *transport loss only*.
- **Hop-boundary marker ring:** on each `current_step` change, record the pair
  **(tick, new `current_step`)**. Since `current_step` is piecewise-constant, only
  its *changes* need recording — a handful of entries per cycle, not per tick.
- **PC reconstruction (deterministic):** the hop markers partition the tick axis into
  segments, each labelled by the resonance live in it; every data sample `k` belongs
  to the resonance of the segment containing `k`, at time `k·Ts`. Per resonance *i*:
  gather its segments' samples at their ticks → fresh series; **ZOH** the correction
  between dwells (the held value), leave the error as bursts. No tag is carried in the
  value word, and no held constant is transmitted.

Dead-time vs. loss stay distinguishable: dead-time (a parked resonance) is
**structural** — known from the hop markers, never streamed — so **NaN is reserved
exclusively for transport loss**. This removes the earlier two-meanings-of-NaN
ambiguity.

### 2D motor-scan forward-compatibility (design-for, build later)

The same primitive composes with the 2D scan. There the PC needs, per spatial
pixel, *both* resonances' shift (the physics: two lines → B-projection +
temperature / vector sensing). Reconstruction is a **double slice**:

1. Reconstruct each `c_i(t)` as above (live stream + hop markers).
2. Slice each `c_i` by the **x/y position markers** (fast-axis bin / slow-axis line),
   which already record the tick index at each KDC encoder pulse.

Per pixel, per resonance = average resonance *i*'s fresh samples whose ticks fall in
that pixel's bin (within one bin the LO still hops through every resonance several
times, revisit cadence `f_eff,i`, Section 7, so each gets several fresh updates).

This needs **three marker channels** — x position, y position, **and** hop
boundaries — but the scan block has only two free banks (`ram_lsb`/`ram_msb`) today.
So the 2D extension must add a **dedicated hop-boundary marker ring** (a 4th BRAM
bank; one job per bank, the established pattern). For the *now* build (time
monitoring, no 2D) the hop markers can live in one of the currently-free banks; 2D
later promotes them to their own bank. Keep the per-tick counter and `current_step`
exported so this drops in without touching the data path.

### Alternative considered: positional N-column interleave

Stream N words per tick (the live value in its resonance's column, a NaN sentinel —
*not* the held value — in the others); resonance is then **positional** (column),
time is **positional** (row), so no hop-marker channel is needed and both position
banks stay free for x/y — the leanest 2D marker budget. Cost: N×(mostly-NaN) words
per tick (throughput still trivial, ~244 kB/s at N=2) and the ARM loss-NaN must be
**record-aligned** (whole N-word ticks) so a PC `reshape(-1, N)` cannot shear the
columns. **Recommendation:** prefer the live-value + hop-marker scheme above (leaner,
zero data-path change, no NaN placeholders); fall back to positional interleave only
if adding the 4th marker bank for 2D proves more trouble than streaming the NaN
columns. Either way the invariant and the "fresh-only / ZOH-on-PC" rule are identical
— only the `(tick, resonance)` *encoding* differs (markers vs. column position).

### Open sub-decisions for the implementation pass

> **STATUS (2026-06-23): the pyrpl side of all of these is IMPLEMENTED + bench-verified in
> Phase B-stream** (`test_phaseB_stream.py` 15/15; `scan.hop_stream_start` /
> `read_hop_markers` / `reconstruct_hop_series`). What remains is the **qudi side**
> (`redpitaya_data_instream` consumption + per-resonance reconstruct in a qudi logic module)
> and the **2D 4th marker bank** (design-only). The bullets below record how each was
> resolved.

- **Hop-marker capture (must-build).** ✅ DONE (2026-06-23, Phase B-stream). Internal
  edge detector on `current_step` in `scan_new.v` records (tick, `current_step`) into a
  marker ring — analogous to the external-pulse MODE-3 capture but internally triggered.
  **Bank decision (resolved):** monitoring reuses the two free position-marker banks
  (tick→`ram_lsb`, step→`ram_msb`); a dedicated 4th bank is added only when the 2D motor
  scan needs x/y *and* hop markers together.
- **Time base.** Add the free-running per-tick counter (one per demod period) as the
  shared index for the data ring and all marker rings; expose it as the absolute time
  reference. (Today's stream sample counter already increments once per streamed
  sample = once per tick in the 1-value-per-tick scheme, so it can serve directly.)
- **NaN = transport loss only.** Dead-time is structural (hop markers), never a NaN.
  Keep the existing push NaN-on-loss semantics untouched.
- **Phase dependency.** The data path already streams the active correction once
  Phase B drives `ftw_correction` from the active integrator, so multi-resonance
  *monitoring* is essentially "Phase B + hop-marker capture." Error streaming needs
  the live error routed similarly. Both testable with N=2 and the existing push path.
- **`scan.py` API.** A `current_step`-marker read (wrap-aware, like
  `read_x_markers`), a reconstruct helper that returns per-resonance ZOH'd
  correction traces on the common tick axis (NaN-safe), and `ftw_to_hz` per
  resonance. The single-resonance `demod`/`ftw_corr` modes stay unchanged (N=1).
- Touches `scan_new.v` (internal hop-edge marker capture + tick counter),
  `odmr_freq_lock` (active integrator already drives `ftw_correction`; expose
  `current_step`-driven selection), `scan.py` (hop-marker read + reconstruct), and
  `redpitaya_data_instream.py` (qudi side). Marker rings reuse the MODE-3 primitive.

---

## 12. Coarse recapture / long-term management (software layer) - deferred to later point

A slow supervisory loop (PC / qudi logic) that:
- Watches each resonance's `correction_hz`; if it ever nears the configured
  `ftw_lim` (typically ±15 MHz), adjusts that resonance's `f_LO,i` table entry to
  re-center and **bumplessly** re-zeros its integrator.
- Handles initial **acquisition** per resonance (sweep once, find the
  zero-crossing, pre-position LO + IF, then close the loop).
- Detects lock loss / saturation per resonance (reuse `locked`, `saturated_*`).
- Logs each resonance's drift (this is the physics output: differential shifts of
  the 8 lines → B-field + temperature).

This layer is naturally a new **qudi logic module** (see
`qudi-iqo-modules`/qudi-core patterns) coordinating the pyrpl hardware module,
mirroring the existing scan/streaming integration.

---

## 13. FPGA resources & reuse

- **Modulation oscillator — N per-channel phase accumulators + ONE shared LUT (Section 5.1).**
  Phase continuity is required per channel (resume at the paused phase `φ_i`), so the
  modulation oscillator becomes **N ~32-bit phase accumulators** (one per resonance, all
  with the same FTW for `f_m = 15.25 kHz`), each clock-enabled by its channel's
  active/valid signal (parked ones hold `φ_i`), feeding a **single shared quarter-wave
  sin/cos LUT** (like `fgen3`'s) that produces the FM tone + demod references for the
  active channel. Do **not** replicate the LUT, and do **not** use one free-running
  oscillator (that loses phase continuity). The existing `iq0` (single free-running
  oscillator) is the starting point to refactor; `iq2` is not needed for this.
- **Demodulators:** CIC+FIR are the expensive blocks. The clean form is **N
  replicated chains, each with a clock-enable** so the inactive ones freeze in place;
  only one is clocked at a time. Replication costs area but is logically simple and
  preserves per-resonance state for free. The alternative — one datapath whose full
  state (CIC integrators/combs + FIR tap line) is saved/restored to N banks per hop —
  saves area but the swap must be atomic and is fiddly. Replicate for small N (2–4);
  revisit for N=8. Either way, add **clock-enable gating** to the lock-in (and to the
  modulation oscillator and the per-resonance `fgen3` FM drive) — this is the core new
  RTL primitive for freeze-and-resume.
  - **Replication count (verified against `lock_in.v` 2026-06-21).** Today's `lock_in`
    is *one* instance with **two channels = I and Q of one resonance** (`ch1` = 1f-I
    error, `ch2` = Q), 2× `cic_decimate_by_4096` + 6× `fir_lowpass_{500,2000,5000}Hz`
    (3 selectable cutoffs × 2 channels). For N=2 *resonances* we replicate the whole
    block → 2 lock-in instances (4 CIC + 12 FIR). Resource/timing check needed but
    should fit; the 3 FIR-cutoff variants per channel could be pruned to the chosen
    2 kHz to save area if needed (OQ-5 keeps 2 kHz).
  - **Native multi-channel IP mode — investigated, REJECTED across resonances, KEPT
    for I/Q (PG140/PG149, read 2026-06-22).** Both cores support native TDM
    multi-channel (FIR up to 1024 ch, CIC up to 128 ch), which time-shares the
    arithmetic across channels. Tempting as a replacement for replication, but it is
    the wrong fit for freeze-and-resume for **two independent reasons**:
    1. **Control semantics (decisive, both cores).** Multi-channel = strict *lockstep*
       TDM: every channel is processed every frame, with **no per-channel freeze/hold**.
       `aclken` low halts the *whole* core ("the core state and outputs are halted",
       PG149 p.13); there is one `s_axis_data_tvalid` for the entire interleaved stream
       and back-pressure keeps all channels aligned (PG149 p.14–15; PG140 p.20–22); the
       FIR "Advanced channel pattern" only sets *fixed* fractional rates and **switching
       patterns clears every channel's data vector** (PG149 p.47). Our scheme needs the
       opposite — advance exactly *one* resonance while holding the other N−1 frozen at
       their converged state for an *indefinite, asynchronous* gap, then resume bumplessly.
       That is per-resonance `aclken` across *separate* freeze domains, which a single
       multi-channel core cannot do. (Same root cause as rejecting *simultaneous* demod,
       Section 6: one LO + one mixer ⇒ only one resonance is ever physically interrogated.)
    2. **No CIC resource win anyway.** Sharing in these cores comes from *hardware
       oversampling* = (clock / per-channel rate) ÷ channels, i.e. spare clock cycles to
       fold arithmetic onto shared adders. The CIC input is **1 sample/clock** (verified:
       both CICs tie `.s_axis_data_tvalid(1'b1)`, `lock_in.v` L119/129 ⇒ 125 MHz in,
       oversampling = 1) — **zero spare cycles**, so multi-channel folds nothing and the
       per-channel integrator/comb state must be replicated regardless (PG140 p.21, p.29).
       The CIC (125 MHz integrators) is the genuinely expensive, timing-critical block,
       and it is replicated 4× for N=2 either way.
  - **FIR optimization we DO take — fold each resonance's I+Q into one 2-channel FIR.**
    A resonance's I and Q **share one freeze domain** (same `aclken`, frozen/resumed
    together), so they are a valid 2-channel TDM pair, and they apply the *same* low-pass
    (Configuration Method = "Single", one coefficient set for both channels). At ~30.5 kHz
    in / 125 MHz clock (ratio ~4096) the FIR MAC is ~99.98 % idle, so one 2-channel core
    serves I+Q for ≈ the cost of a single-channel core. **What is saved vs two separate
    single-channel FIRs:** the **DSP/MAC arithmetic**, the **control logic**, *and* the
    **coefficient storage** — the coefficient memory (BRAM or LUT-RAM depending on tap
    count) is held **once** and time-shared across the I and Q slots instead of duplicated.
    Per PG149 p.47 a multi-channel filter uses "a similar amount of logic resources to a
    single-channel version … with proportionate increase in **data memory**": i.e. logic +
    coefficients stay ~constant, and *only* the data/delay-line (per-channel sample
    history) scales with channel count — which is a wash, since two separate cores hold
    that same total anyway. **Net:** with the 3 cutoffs pruned to 2 kHz (above), N=2 goes
    from "12 FIR" → **2 two-channel FIR cores** (one per resonance, gated by that
    resonance's `aclken`). Absolute saving is modest (each FIR is already ~1 DSP), but it
    halves FIR instance/coefficient count for free. The CIC cannot do this (I+Q each at
    1 sample/clock = 250 MHz aggregate > clock), so the 4 CIC instances stand.
- **Pausing the IP cores — how freeze-and-resume is actually wired (PG140/PG149,
  searched 2026-06-21).** Both Xilinx cores support a true hold-all-state freeze:
  - **`aclken` (clock enable), the unambiguous freeze.** Optional port enabled at IP
    generation by **`has_aclken = True`** (CIC: PG140 p30/Table 4-1; FIR: PG149
    p86/Table 4-1). Deasserted → "core state and outputs are halted" (FIR PG149 p13,
    verbatim); for CIC it gates the whole datapath incl. integrators/combs **and the
    decimation counter**, subordinate only to `aresetn` (PG140 p8/p30). No decay, exact
    resume. **Must regenerate** the CIC/FIR IP with `has_aclken=True` (cannot be added
    post-hoc).
  - **AXIS `s_axis_data_tvalid` stall, the no-regen alternative.** Holding input
    `tvalid` low consumes no sample, so the delay line and the *per-accepted-sample*
    decimation counter do not advance → state held ("no data is dropped", PG149 p15;
    CIC counter advances per accepted sample, PG140 p21–22). The FIR is downstream of
    the CIC's `m_axis_data_tvalid` so it gates automatically.
  - **Current instantiation blocks neither path yet:** the CICs are tied
    `.s_axis_data_tvalid(1'b1)` (always clocking) with **no `aclken`** port, and the
    cores were generated without it. So Phase C must either (a) **regenerate** the CIC
    and FIR cores with `has_aclken=True` and drive `aclken = resonance_active &
    valid_window` per chain (cleanest, recommended), or (b) drive the per-chain run
    enable onto `s_axis_data_tvalid` (no IP regen, but the documented hold relies on
    "counter advances per accepted sample" rather than an explicit register-freeze
    sentence). **Never pulse `aresetn`** (≥2-cycle synchronous *clear* wipes state; FIR
    default `reset_data_vector=True` clears the delay line — build with it `False` as a
    safety net).
  - **`.xci` config verified (2026-06-21).** `cic_decimate_by_4096.xci`:
    `HAS_ACLKEN=false`, `HAS_ARESETN=false`, `HAS_DOUT_TREADY=false` (input
    `s_axis_data_tvalid`/`tready` ports present). `fir_lowpass_2000Hz.xci`:
    `Has_ACLKEN=false`, `Has_ARESETn=false`, `Reset_Data_Vector=true` (moot — no reset
    port), `M_DATA_Has_TREADY=false` (input `tvalid`/`tready` present).
  - **DECISION (OQ-12, J.W. 2026-06-21): use `aclken`, regenerate the IP** — the clean
    solution, no short-cut. **Concrete recipe for Phase C:**
    1. **FIRs:** in `pyrpl/fpga/ip/generate_single_fir_ip.tcl`, set
       `CONFIG.Has_ACLKEN {true}` (currently line 95) — applies to all three
       `fir_lowpass_{500,2000,5000}Hz` (built from the `.coe` files). Keep
       `CONFIG.Has_ARESETn {false}` (no reset port ⇒ no accidental clear;
       `Reset_Data_Vector` stays moot).
    2. **CIC:** in `pyrpl/fpga/ip/cic_decimate_by_4096/cic_decimate_by_4096.xci`, set
       `"HAS_ACLKEN"` → `"true"` (keep `HAS_ARESETN=false`). It is regenerated from this
       `.xci` via `regenerate_xci_ip.tcl` (listed in `generate_fir_ips.py`'s
       `extra_ips`).
    3. **Regenerate IP:** run `pyrpl/fpga/ip/generate_fir_ips.py` (regens all FIRs + the
       CIC into their `ip/` subdirs).
    4. **RTL:** in `lock_in.v` connect `.aclken(<per-chain run-enable>)` on each CIC and
       FIR instance; `aclken` low fully freezes that chain regardless of `tvalid` (leave
       `tvalid` as-is). The run-enable = this resonance's `valid` signal (Section 11).
    5. **Build + install:** run `pyrpl/fpga/copy_and_make.bat` (builds the `.bin` in the
       generation dir), then copy the `.bin` into the live pyrpl fpga directory (J.W.'s
       usual flow).
  - **Never enable `aresetn`** (stays `false`); it is a ≥2-cycle synchronous *clear*
    that would wipe state (FIR `Reset_Data_Vector` default `true` would clear the delay
    line).
- **Trigger out:** reuse `scan_trigger_o`/`scan_new.v` pulse machinery, or add a
  dedicated hop-sequencer that co-drives the LO trigger, the cal/`f_m` slot index,
  the demod valid-window gate, and the integrator hold — the eventual
  "`odmr_multitrack` sequencer".
- **DIO budget:** DIO7_P = MW/scan trigger out (candidate LO trigger), DIO5/6_P =
  KDC101 position triggers (motor-scan feature — don't clobber), DIO0 = ext
  trigger in. Confirm a free pin for the LO trigger if DIO7_P is contended.

---

## 14. Proposed phased roadmap

**FPGA-only build (decided 2026-06-20).** No software-only prototype — every new
stage is RTL, mirroring the existing on-FPGA lock-in / freq-lock / scan. Target **N=2,
replicated** demod (OQ-4/6). The hardware **"which resonance is live" index** is
`scan.current_step` (Section 10) — it is the common selector that drives the slot mux,
the integrator selector, and the per-chain clock-enables. Cross-module wiring (DONE):
`current_step` is routed from the scan block (region 5) to `fgen3` (region 6),
`odmr_freq_lock` (region 8), and `odmr_multitrack` (region 9) via `red_pitaya_top.v`.
**The "settled/valid" strobe is NOT routed from scan** (the original plan): instead the
per-hop physical-settle/valid window is generated *inside* `odmr_multitrack` by the
`T_settle` counter (it restarts on every `current_step` change), which drives the
per-channel `aclken` freeze gates. Simpler and self-contained — superseded the strobe-routing idea.

**Phase 0 — De-risk (DONE / bench).** `JUMP_LIST` + `y2` single-step trigger verified
(OQ-2′ resolved). Still nice-to-have: a measured **physical settle** τ + LO re-lock
number (the real `T_settle`) to size the valid-window gate. Keep `f_m = 15.25 kHz`,
2 kHz FIR.

**Phase A — FPGA cal-slot bank + slot mux in `red_pitaya_3fgen.v` (was Phase 2; 9b).**
✅ **IMPLEMENTED + BENCH-VERIFIED 2026-06-21.** New bitstream built, loaded, and tested
on hardware: (a) **register test 15/15** (`NSLOTS=2` readback confirms the new bitstream;
slot 0/1 independent; signed DC round-trips; `active_slot`/`active_slot_src`/`load_cal_slot`
work) via `test_phaseA_calbank.py`; (b) **end-to-end scope test CONFIRMED** via
`test_phaseA_scope.py` — single IF tone, LO CW 2.87 GHz, slot 0 `phase_b=270°` vs slot 1
`phase_b=90°` (equal amps): toggling `active_slot` **flips the dominant sideband**
2.850↔2.890 GHz with a **~23 dB** antenna-independent swing at each sideband → the slot
mux routes the selected slot's cal into the DAC datapath. **Bug found + fixed during the
test:** the signed DC registers read back wrong for negatives (FPGA read path
sign-extends to 32 bits, but `FloatRegister.to_python` expects a `bits`-wide value) →
fixed in Python with `bitmask=2^DACBITS-1` on the four DC registers (works with the *current*
bitstream, no rebuild) and the RTL DC reads switched to zero-extend for the *next* build.
Add an N=2 slotted store of the 208-bit cal set (3×{amp_a, amp_b, phase_b} + dc_a/dc_b,
per OQ-8/Section 9) and an active-slot mux feeding the `fgen3` datapath. Index from a
software-writable register first, then from `current_step`. **Independently
bench-testable**: load two slots, toggle the index, scope the SSB null/image per slot.
The most self-contained piece, and a good first concrete step.

> **Phase A — as implemented (2026-06-21).** Files changed:
> - `pyrpl/fpga/rtl/red_pitaya_3fgen.v`: `NSLOTS=2` param + `current_step_i[7:0]` port;
>   packed cal bank at `0x0200` stride `0x40` (per slot: `+0x00` dc_a, `+0x04` dc_b,
>   comp c at `+0x08+0x0C*c` = amp_a/amp_b/phase_b); `active_slot`+`active_slot_src` at
>   `0x000C`; registered `slot_sel_r` mux into stages 3/5/7; `phase_offset_a` dropped
>   (`phase_eff_a = phase_acc`); legacy cal addresses (`0x04/0x08`, per-comp
>   `0x14/0x18/0x1C/0x20`) removed; `NSLOTS` readback at `0xFF1C`.
> - `pyrpl/fpga/rtl/red_pitaya_top.v`: `.current_step_i(8'd0)` (hw slot select unrouted
>   in Phase A; software drives `active_slot`).
> - `pyrpl/hardware_modules/fgen3.py`: cal-bank descriptors (slot 0 = backward-compat
>   names `amplitude_a/b{0..2}`, `phase_offset_b{0..2}`, `overall_dc_offset_a/b`; slot 1
>   = `cal1_*`), `active_slot`/`active_slot_src`, `nslots`, `load_cal_slot(slot, comps,
>   dc_a, dc_b)`; `phase_offset_a*` removed; `_setup_attributes` updated.
> - `qudi-iqo-modules/.../redpitaya/redpitaya_if_source.py`: dropped the `phase_offset_a`
>   writes/reads (now hardwired 0).
>
> **Build & install (J.W.'s flow):** no IP regen needed (pure RTL). Run
> `pyrpl/fpga/copy_and_make.bat` → build the `.bin`, then copy it into the live pyrpl
> fpga directory. **Bench test:** `rp.fgen3.load_cal_slot(0, comps0, dc_a0, dc_b0)` and
> `load_cal_slot(1, comps1, ...)`; toggle `rp.fgen3.active_slot` (with
> `active_slot_src=False`); scope the image/LO-leakage per slot (judge steady state,
> ignore the switch glitch).

**Phase B — Multi-integrator (N=2) in `odmr_freq_lock` (part of 8a).**
Replicate the integrator state ×2; select the active one by `current_step`; **hold**
the inactive one (extend the existing `hold` primitive); drive `ftw_correction` from
the active integrator. Bumpless — never reset on a hop. Add per-slot readback for the
supervisor. Driving `ftw_correction` from the `current_step`-selected integrator
means the existing `ftw_corr` stream already carries "the live resonance's
correction, one per tick" — the data path needs no change for monitoring (Section 11).

> **Phase B — as implemented (2026-06-23).** IMPLEMENTED + BENCH-VERIFIED on hardware
> (`test_phaseB_multiint.py` → 13/13; new bitstream built in
> `C:\Users\aj92uwef\Documents\fpga_compilation\fpga\out\red_pitaya.bin`, deployed to the
> live `pyrpl/fpga/red_pitaya.bin`, Phase A bin backed up `.bak_20260621_phaseA`). The
> dynamic check ran with a real demod error (531 LSB on ADC A): slot 0 integrated to the
> +1 MHz saturation while slot 1 stayed at 0; switching the active slot then **held slot 0
> at +1 MHz (bumpless) while slot 1 integrated 0→~987 kHz** — confirming per-slot
> independence, active-integrates/idle-holds, and bumpless switching. (The dynamic test
> used the software slot select `active_slot_src=0`; the hardware `current_step` path is the
> same registered mux and its control bit round-trips — full hopping-while-tracking is the
> next phase's integration test.) Files changed:
> - `pyrpl/fpga/rtl/odmr_freq_lock_1f.v`: added `NSLOTS=2` param + `current_step_i[7:0]`
>   port. The integrator state and all per-resonance status (`ftw_corr`, applied-output
>   `ftw_out_r`, `err_latch`, lock counter, saturation flags) became **per-slot arrays**.
>   A `slot_sel` selector (`active_slot` sw / `current_step_i` hw via `active_slot_src`,
>   registered `slot_sel_r` — same idiom as Phase A fgen3) picks the active slot; **only it
>   integrates**, the others **hold** (bumpless; never reset on a hop). The datapath is
>   **time-shared** (one multiplier) since only one slot is live at a time (8a). The output
>   `ftw_correction_o` is driven **continuously** from the active slot's stored
>   `ftw_out_r` (fgen3 samples that bus every clock — it does *not* gate on
>   `ftw_correction_valid`, verified at `red_pitaya_3fgen.v` L307), so it follows the slot
>   the instant `current_step` changes. The loop **gains/deadband/ftw_lim stay global**
>   (shared by all slots) — only the integrator *state* is per-slot. `clr` / `enable=0`
>   zero **all** slots. New register map: `0x0024` SLOT_CTRL (bit0=`active_slot`,
>   bit8=`active_slot_src`), `0x0028` NSLOTS readback, packed per-slot readback bank at
>   `0x0040` stride `0x10` (`+0x0` status, `+0x4` err, `+0x8` ftw_int, `+0xC` ftw_out);
>   the legacy `0x10/0x14/0x18/0x20` are kept as **slot-0 aliases** (existing
>   single-resonance Python works unchanged; with `NSLOTS=1` behaviour is identical to the
>   old loop).
> - `pyrpl/fpga/rtl/red_pitaya_top.v`: route `scan_current_step[7:0]` into the new
>   `odmr_freq_lock_1f.current_step_i` (mirrors the fgen3 wire) and pass `.NSLOTS(2)`.
> - `pyrpl/hardware_modules/odmr_freq_lock.py`: added `active_slot`, `active_slot_src`,
>   `nslots`/`_NSLOTS_HW`, and per-slot accessors (`status_slot`, `locked_slot`,
>   `error_lsb_slot`, `integrator_ftw/hz_slot`, `correction_ftw/hz_slot`,
>   `get_status_all`) reading the packed bank via `_read`. Slot-0 still reported by the
>   existing single-resonance properties (legacy aliases). `_setup_attributes` extended.
>
> **Build & install (J.W.'s flow):** no IP regen (pure RTL). Run
> `pyrpl/fpga/copy_and_make.bat` → build the `.bin`, then copy it into the live pyrpl fpga
> directory. **Bench test:** `test_phaseB_multiint.py` — register-level checks (NSLOTS,
> slot control, clear/disable zeros all slots, slot-0 alias == bank) always run; a
> best-effort **dynamic** section confirms only the active integrator moves and the
> inactive one holds bumplessly across a software slot switch (needs a live demod error on
> ADC A — skips, not fails, if none is present). **Still to do after the run:** Phase
> B-stream hop-boundary markers (Section 11), then Phase C freeze gating.

**Phase B-stream — Deterministic multi-resonance monitoring (Section 11).** Add the
**hop-boundary marker capture** (an internal edge detector on `current_step` writing
(tick, `current_step`) into a marker ring, analogous to the MODE-3 external-pulse
capture) plus the `scan.py` hop-marker read + per-resonance reconstruct (ZOH the
held correction between dwells; NaN = transport loss only). No held constants are
streamed. Testable with N=2 over the existing push path. Keep `ram_lsb`/`ram_msb`
free for the x/y position markers so the 2D motor scan composes later (which then
adds a dedicated 4th bank for the hop markers).

> **Phase B-stream — as implemented (2026-06-23).** IMPLEMENTED + BENCH-VERIFIED on hardware
> (`test_phaseB_stream.py` → 15/15; new bitstream built in
> `C:\Users\aj92uwef\Documents\fpga_compilation\fpga\out\red_pitaya.bin`, deployed to the
> live `pyrpl/fpga/red_pitaya.bin`, Phase B bin backed up `.bak_20260623_phaseB`). The
> dynamic check ran an N=4 scan concurrently with `ftw_corr` streaming and captured
> `ticks=[0,178,243,309] steps=[0,1,2,3]` (clean per-step markers; concurrent scan+stream
> confirmed; no RF / no ARM push server needed). Files changed:
> - `pyrpl/fpga/rtl/scan_new.v`: internal `current_step`-edge detector (`hop_event`; fires on
>   each change *and* once at stream start for the initial segment) records the pair
>   **(tick = `reg_stream_sample_cnt`, step = `reg_current_step`)** into a hop-marker ring —
>   tick→`ram_lsb`, step→`ram_msb` — gated by new `STREAM_CONTROL[3]` (hop enable), with
>   new regs `0x40` HOP_WR_PTR / `0x44` HOP_COUNT (wrap-aware, same idiom as the MODE-3
>   x/y markers). **Concurrent scan-trigger + stream enabled**: the FSM start gate no longer
>   blocks on `reg_stream_enable`, `STREAM_CONTROL` is writable while the scan is busy, and
>   `bram_wr_en` is suppressed while streaming — so the scan FSM drives the LO-hop trigger +
>   advances `current_step` while the push stream owns `data3` and hop markers own
>   `ram_lsb/ram_msb`. **Bank choice (resolves the Section-11 sub-decision):** monitoring
>   reuses the two otherwise-free position-marker banks (tick+step); the dedicated 4th bank
>   is added only when the 2D motor scan needs x/y *and* hop markers together (deferred).
>   Existing scan-only and stream-only/MODE-3 paths are unchanged when streaming is off /
>   hop is off.
> - `pyrpl/hardware_modules/scan.py`: `_stream_ctrl_write(..., hop=)` (bit3, RMW),
>   `hop_stream_start` / `hop_stream_read` / `hop_stream_stop`, `read_hop_markers()`
>   (returns `(ticks, steps)`, both rings read in lockstep, wrap-aware), and
>   `reconstruct_hop_series(values, ticks, steps, nslots, to_hz)` — the deterministic
>   **fresh-only + zero-order-hold** reconstruction: each resonance carries its fresh
>   samples while live and the ZOH-held value while parked; **NaN is reserved for transport
>   loss only** (a dropped sample inside a live dwell stays NaN; dead-time is held; pre-first-
>   dwell is NaN). New regs `ADDR_HOP_WR_PTR`/`ADDR_HOP_COUNT`.
>
> **Operational model (interim):** hops are driven by the existing scan FSM running
> concurrently with the stream (configure `num_steps`/`dwell`/… and `scan.start()`); Phase D
> replaces this with the dedicated on-FPGA hop sequencer. **Test:** `test_phaseB_stream.py`
> — Part 0 offline reconstruct unit checks (always run, 6/6); Part 1/2 on hardware confirm
> `STREAM_CONTROL[3]` round-trips and that running an N-step scan while hop-streaming
> captures `(tick, step)` markers covering steps 0..N-1 (no RF / no ARM push server needed —
> markers live in BRAM, fed by the scan FSM advancing `current_step`).

**Phase C — Per-channel phase-continuous oscillator + 2nd demod chain + freeze gating
(rest of 8a / Sections 5.1, 12).**
Build the **N-phase-accumulator + shared-LUT** modulation oscillator (Section 5.1) so each
channel resumes at its paused phase `φ_i`; replicate the lock-in (CIC+FIR) into N chains;
add **clock-enable gating** (via `aclken`, OQ-12) so only the active channel's phase
accumulator + lock-in chain + per-resonance `fgen3` FM drive is clocked, freezing the
inactive ones in place. Add the **physical-settle valid-window gate** on resume (stay
frozen through LO lock + pipeline τ). All gated by the per-channel active/valid signal as
one unit (P-3). This gating is what freeze-and-resume is built on.

> **Phase C — as implemented (2026-06-23).** IMPLEMENTED; build #2 DEPLOYED +
> REGISTER-VALIDATED on hardware (`test_phaseC_regs.py` 10/10 — `NCH=2` live, per-channel
> freeze selection confirmed in silicon, legacy-transparent when disabled). Two follow-ups
> noted at the end of this block (the 2 kHz FIR prune + registered demod mux that built #2,
> and the deferred odmr-MAC timing). Bench-RF tuning still ahead (moves to qudi). Files changed:
> - **IP regen (OQ-12):** `pyrpl/fpga/ip/generate_single_fir_ip.tcl` `Has_ACLKEN {true}`
>   and `cic_decimate_by_4096.xci` `HAS_ACLKEN:true` (both keep `Has_ARESETn=false`); ran
>   `generate_fir_ips.py` → all 4 cores (cic + fir_500/2000/5000) regenerated with an
>   `aclken` port (verified). NOTE: `generate_fir_ips.py` prints ✅/❌ — run it with
>   `PYTHONUTF8=1` in a non-UTF-8 console or it crashes on the emoji (the Vivado build
>   itself is fine).
> - **`pyrpl/fpga/rtl/red_pitaya_quarter_wave_lut17.v` (new):** 17-bit quarter-wave sine
>   LUT (16-bit magnitudes, `lut[k]=round((2^16-1)·sin(π/2·(k+0.5)/2048))`), same
>   addressing/sign logic as the 14-bit `red_pitaya_quarter_wave_lut.v`. 17-bit so the
>   reference amplitude matches the lock-in demod refs (preserves demod gain K).
> - **`pyrpl/fpga/rtl/odmr_multitrack.v` (new, region 9):** NCH=2 per-channel phase
>   accumulators (shared f_m FTW; only the active+in-window channel increments, parked
>   ones hold φ_i) + two LUT17 instances → active channel `sin/cos/sin_shifted/cos_shifted`
>   + per-hop `T_settle` freeze window (frozen `settle_cnt` cycles after each
>   `current_step` change) + per-channel `aclken_o`. Regs: `0x00` CTRL(enable, src,
>   sw_channel), `0x04` FTW, `0x08` demod_phase, `0x0C` T_settle, `0x10` STATUS, `0x14` NCH.
>   **Transparent when disabled** (`enable=0` → `aclken_o`=all-ones, `osc_active_o`=0).
> - **`pyrpl/fpga/rtl/lock_in.v`:** added `aclken_i` → all CIC/FIR `.aclken()`; output
>   valids gated by `aclken_i` (a frozen FIR can't emit a stuck valid → odmr holds).
> - **`pyrpl/fpga/rtl/red_pitaya_top.v`:** instantiate `odmr_multitrack` (region 9) +
>   2nd `lock_in` (region 10); mux fgen3 `fm_mod_in` and both chains' refs between legacy
>   `iq0` and the oscillator on `osc_active`; gate chain 0/1 with `osc_aclken[0/1]`; mux
>   the odmr error + scan demod-stream from the active chain (`use_ch1=osc_active&osc_sel[0]`);
>   freed region 9/10 stubs. `red_pitaya_vivado.tcl` lists the two new RTL files.
> - **Python:** `odmr_multitrack.py` (`OdmrMultitrack`, region 9: `enable`, `src`,
>   `sw_channel`, `frequency`=f_m, `demod_phase`, `settle_time`, `nch`, `selected_channel`,
>   `in_settle`, `run_mask`); `LockIn1` (region 10); registered in `redpitaya.py`.
>
> **Backward compatibility:** the new bitstream defaults to `osc_active=0` → the proven
> single-resonance path (iq0 refs, free-running lock-in) is byte-for-byte unchanged until
> `rp.odmrmultitrack.enable=True`. **Bench bring-up order:** (1) `test_phaseC_regs.py`
> (registers, no RF); (2) osc disabled → confirm legacy single-resonance ODMR still locks;
> (3) osc enabled, `src='sw'`, `frequency`=f_m, calibrate `demod_phase` → confirm a single
> resonance still locks (re-tune `mu`/`kp` if the 17-bit LUT shifted the gain); (4) osc
> enabled, `src='current_step'`, scan hopping → 2-resonance freeze-and-resume (tune
> `settle_time`). **Resource risk (xc7z010):** the 2nd chain adds ~2 CIC + 6 FIR; if the
> build overflows DSP/LUT or fails timing, prune the lock-in FIR cutoffs to 2 kHz only
> (drop the 500 Hz/5 kHz instances — OQ-5 keeps 2 kHz), halving the FIR count.
>
> **Build #1 (2026-06-23) — failed timing + resource ceiling; fixes applied, rebuild
> pending.** First Phase-C build (2 chains × 3 FIR cutoffs): **DSP 78/80 (97.5 %), slices
> 96 %, BRAM 56/60 (93 %)**, and **WNS = −13.227 ns** on `adc_clk` — worst path
> `i_odmr_multitrack/reg_sw_sel → i_odmr_freq_lock_1f/ftw_out_r` (the cross-module demod
> mux Phase C added, feeding the long odmr MAC, inflated by congestion). PWM-250 MHz and
> ADC-hold violations are pre-existing. The sine ROMs correctly inferred as BRAM. **Key
> point (J.W. confirmed):** the odmr loop updates at 30.5 kHz (`err_valid` every 4096
> cycles), so its long combinational MAC is genuinely **multicycle** — it never closed
> single-cycle in Phase A/B either, but functioned. **Three fixes applied for the next
> build:** (1) audit fix — `odmr_freq_lock_1f.v` disable-zeroing pulled out of the
> `err_valid` gate (unconditional); (2) **prune `lock_in.v` to 2 kHz only** (removed the
> 500 Hz/5 kHz FIR instances + filter-select mux; `filter_select` regs kept but ignored —
> frees DSP/BRAM/slices + relieves congestion); (3) **register the cross-module demod mux**
> in `red_pitaya_top.v` (removes `reg_sw_sel` from the odmr combinational cone → odmr
> datapath back to its Phase-A/B form). Plan: rebuild, re-check WNS; if a path still fails
> notably, add a targeted `set_multicycle_path` on the 30.5 kHz odmr loop (deferred until
> the data shows it's needed). **No room for N=4 on this chip even after the prune.**

**Phase D — On-FPGA hop sequencer + qudi supervisory logic (Section 12).**
Fold hop timing fully on-FPGA (one sequencer co-driving the LO trigger, slot index,
valid gate, and integrator hold — the "`odmr_multitrack` sequencer"). Add the qudi
logic module for per-resonance acquisition, coarse recapture, lock management, and
drift logging. Generalize N → 4 (→ 8; revisit replicate-vs-time-share there).

> **Continuous/indefinite-hopping fix (Section 0, remaining item 1) — DONE via option (a),
> 2026-06-24.** The `scan` FSM was finite (`num_steps` then `S_DONE`). Of the two routes —
> (a) a **continuous/loop mode** in `scan_new.v` (a control bit that, in `S_FINISHING`, wraps
> `step_counter`→0 and re-enters `S_START_STEP` instead of `S_DONE`), or (b) the full Phase-D
> sequencer that owns hop timing directly — **(a) was implemented** (CONTROL bit3 / STATUS
> bit2; `Scan.start(continuous=True)`; see Section 0 item 1) because it unblocks indefinite
> tracking with a ~10-line, low-risk RTL change that reuses the whole verified
> hop+freeze+stream+marker stack. The Phase-D sequencer (b) is no longer required for
> indefinite operation; it remains the *clean end-state* (co-locating hop generation with the
> `odmr_multitrack` freeze/settle/`current_step` logic it already owns, and freeing `scan` to
> stream only) and the path to N→4/8. Build (b) only as a deliberate refactor once the
> continuous tracker is validated end-to-end.

---

## 15. Pitfalls / risk register

- **P-1 IF tracking range (~±15 MHz, set by `ftw_lim`) vs. long drift.** The range
  is generous, but without coarse recapture (Section 12) a resonance could in
  principle walk out of the IF window and unlock over long runs. Build the
  supervisory re-centering as a safety net; confirm `ftw_lim` is set to the
  intended ±15 MHz, not the ±1 MHz register default.
- **P-2 Dead time vs. dwell.** If `T_settle + T_hop` ≳ `T_dwell`, useful duty cycle
  collapses. Freeze-and-resume keeps `T_settle` at the physical settle
  (~100–300 µs, filter-independent), so this is comfortable. See Section 7.
- **P-3 Resume coherence.** Freeze-and-resume is only valid if (a) modulation and
  demodulation are driven by the **same per-channel phase** (the active channel's phase
  accumulator feeds both FM drive and demod refs, Section 5.1), (b) the whole per-channel
  chain (**that channel's phase accumulator** + CIC integrators/combs + decimation counter
  + FIR taps + integrator) freezes and resumes as **one clock-enable unit**, holding and
  resuming at `φ_i`, and (c) the physical-settle window (LO lock + pipeline τ) is gated out
  on resume. Get any of these wrong and you reintroduce a transient. (Never free-run a
  shared oscillator across the gap, and never stop/restart *only* the oscillator — either
  injects a phase step and breaks per-channel continuity.)
- **P-4 Per-`f_m` demod phase.** Only an issue if channels use *distinct* `f_m`
  (each then needs its own `2π·f_m·τ` compensation). The recommended single shared
  `f_m = 15.25 kHz` makes the demod phase identical for every resonance — one phase
  setting works for all. (Section 6.)
- **P-4b SSB calibration is LO-dependent (not IF-only).** Empirically the I/Q
  phase/amplitude balance and carrier-null DC offsets vary strongly with LO
  frequency, so every resonance needs its own full calibration set applied on each
  hop. This makes the FPGA cal-slot bank (9b) mandatory and means cal cannot be
  shared across resonances. Calibration may also drift with temperature/time → the
  supervisory layer (Section 12) should support periodic re-calibration per slot.
- **P-5 (n/a) Inter-channel `f_m` cross-talk.** Not a concern: all resonances share
  `f_m = 15.25 kHz` and only one chain is ever clocked, so there is no simultaneous
  demodulation to cross-couple. Would only return if a future overlap/simultaneous
  mode were adopted (Section 6, out of scope).
- **P-6 Global `ftw_correction`.** Until the multi-integrator block (Phase B) exists,
  the one correction bus drives *all* `fgen3` components — fine while only one resonance
  is active, but any per-resonance save/restore during bring-up must be airtight until
  Phase B replaces it.
- **P-7 LO phase/coherence across hops.** If the SynthNV Pro is not phase-coherent
  across table entries, the absolute MW phase jumps each hop. The lock-in measures
  amplitude at `f_m` so this is likely benign, but confirm it doesn't perturb the
  SSB null or demod phase.
- **P-8 DIO contention.** LO trigger pin must not collide with the KDC101
  motor-scan triggers (DIO5/6_P) or PWM sources; check `exp_p_src_sel`. The LO
  trigger itself needs no level shifter (3.3 V, Section 10) — but if `scan_trigger_o`
  on DIO7_P is already committed to another use, allocate a free DIO.
- **P-9 Reduced per-resonance bandwidth.** Each loop's bandwidth is capped by
  `f_eff,i = 1/(N·(T_dwell+T_hop))`. Make sure that still exceeds the real drift
  rates of the lines, or tracking falls behind.

---

## 16. Resolved questions (decision record)

Every question below is resolved; this table is the per-topic record. The dated
chronology of how each was settled lives in the Decision Log (Section 17).

| ID | Question | Owner | Status |
|---|---|---|---|
| OQ-1 | SynthNV Pro frequency settling time? | — | **RESOLVED: 100 µs RF lock, 250 µs/step; LO is not the bottleneck** |
| OQ-2 | Trigger spec + step command? | — | **RESOLVED via qudi driver:** `y2` = single-step-on-trigger; sweep via `l/u/s`, list via `L{i}f..L{i}a..`; trigger low-active, FPGA `scan` trigger drives it. 3.3 V, no level shift |
| OQ-2′ | Does arbitrary `JUMP_LIST` work on the SynthNV Pro? | — | **RESOLVED — YES (scope-verified 2026-06-20).** Undocumented `L` commands (`Ld`, `L{i}f{MHz}`, `L{i}a{dBm}`) program an arbitrary, non-equidistant table; `y2` + RP trigger (DIO7_P active-low → `Y0`) steps **exactly one point per trigger from armed point 0** (no priming step — that earlier belief was an `f?`-readback artifact; the scope shows clean stepping). `c1` wraps; `Z0` for the temp-comp power bug. Implemented in `mw_source_windfreak_synthnvpro_redpitaya.py::_configure_windfreak_list` |
| OQ-3 | Modulation frequency | — | **RESOLVED:** keep `f_m = 15.25 kHz` for all resonances (CIC M=2 → first null at `f_m`; only one chain demodulated at a time, so no distinct tones needed). FIR cutoff is just an SNR↔latency choice |
| OQ-4 | Start N=2, but design for 4 or 8? Affects replicate-vs-time-share demod | user | **RESOLVED (2026-06-20, J.W.):** **N=2** is enough for the foreseeable future; N=8 is a *later* end-state. → build for N=2 now (replicate, OQ-6), keep generalization in mind but don't pay for it yet |
| OQ-5 | Acceptable per-resonance tracking bandwidth vs. measured line drift rates? | user | **RESOLVED (2026-06-20, J.W.):** keep the **2 kHz FIR** in use today; aim for the **highest per-resonance bandwidth achievable**. Future goal (not now): a few kHz tracking BW on *all* channels. So size dwell/hop to maximize `f_eff,i`, not to chase a specific slow drift rate |
| OQ-6 | Replicate demod hardware ×N or time-share one datapath? | — | **RESOLVED (2026-06-20):** **replicate** for N=2 (two clock-gated lock-in chains; only the active one clocked). Time-share is the N=8 problem, deferred (Section 13) |
| OQ-7 | Hop sequencing: software vs. on-FPGA sequencer acceptance criteria | — | **Direction set (2026-06-20):** **all on FPGA** — no software prototype. Stage it (software may *write* the active-slot index during early bring-up) but the end-state hop timing is the on-FPGA sequencer co-driving LO trigger + slot + gate + hold |
| OQ-8 | (g, phi) → (`amplitude_a/b`, `phase_offset_a/b`) conversion + where the current single-resonance path applies cal | — | **RESOLVED (2026-06-20):** cal applied in qudi `RedPitayaIFSource`/`IFSourceBase` (`set_iq_correction` / `set_multi_frequency_signal`), not pyrpl. `amp_a=A(1+g)`, `amp_b=A(1-g)`, `phase_a=0`, `phase_b=(q_base+deg(phi))%360` (q_base 270/90 USB/LSB); DC offsets **global** (mean over comps). Slot = 3×(amp_a 14b, amp_b 14b, phase_b 32b) + 1×(dc_a 14b, dc_b 14b) = 208 bits; freqs/FM/demod-phase stay global. Section 9 |
| OQ-9 | `f_m` value: 15.25 kHz or 30.5 kHz? | user | **RESOLVED (2026-06-21, J.W.): 15.25 kHz** (`=125 MHz/(4096·2)=f_dec/2`, CIC M=2 first null). The earlier "~30.5 kHz" was a slip (that is `f_dec`). No design change |
| OQ-10 | First-iteration scope vs. deferring per-resonance SSB cal (#4) | user | **RESOLVED (2026-06-21, J.W.): keep Phase A first** — build the cal-slot-bank *infrastructure* now; only the per-resonance cal *values* are deferred ("tomorrow"). Iteration 1 loads the same cal into both slots (or uses slot 0); distinct per-resonance cal loading (qudi `RedPitayaIFSource` per slot) wired up later |
| OQ-11 | Multi-resonance sample→(resonance,time-bin) allocation + how it's streamed to the PC | user | **RESOLVED (2026-06-22, J.W.).** Invariant: deterministic `(absolute tick, resonance) → value` on a single free-running wall-clock tick base (the values *are* the B/temperature measurement). Key: info exists only during a resonance's live dwell — the correction is piecewise-constant (holds while parked), so **stream only fresh samples and ZOH the held value on the PC; never transmit the held constant**. Recommended transport: **live-value stream** (one fresh sample/tick = the active resonance's correction/error — the existing data path, unchanged) **+ hop-boundary markers** recording (tick, `current_step`) at each hop; PC segments the stream by markers → per-resonance series at known ticks. NaN reserved for transport loss only (dead-time is structural). 2D = double slice (per-resonance ZOH series, then x/y position markers) and needs a dedicated 4th marker bank for hop boundaries. Alternative: positional N-column interleave (resonance=column) frees the marker channel at the cost of NaN-column words — fallback only. Earlier 06-22 "interleave all N held integrators every tick" rejected as redundant (Section 11) |
| OQ-12 | Freeze mechanism: regenerate CIC/FIR with `aclken` vs. drive per-chain `s_axis_data_tvalid` (no IP regen) | user | **RESOLVED (2026-06-21, J.W.): `aclken`, regenerate the IP** ("take the cleaner solution, no short-cuts"). Recipe + build flow captured in Section 13. Drive `aclken` per chain in `lock_in.v` |
| OQ-13 | Windfreak 2-point JUMP_LIST clamp? | user | **RESOLVED (2026-06-21) — NOT a real limit; it was a TIMING ARTIFACT.** The clamp only happened with my over-long trigger (5 ms) + `t4`. With the **config timing (trigger 50 µs, settling 100 µs, Windfreak `t≈1 ms`)**, **all** list sizes wrap cleanly **including N=2** (`2.77,2.97,2.77,2.97,…`). So `1→2→1→2` works for N=2; no `NSLOTS≥3` workaround needed. (Aside: with the bad timing the LO ordering was also scrambled — another artifact.) |

## 17. Decision log

| Date | Decision | Rationale |
|---|---|---|
| 2026-06-17 | Keep IF constant; hop LO to bring each resonance to the same Red Pitaya working point | Pins the demod/IF working point and reuses the hyperfine triplet layout (Section 4). *(Original rationale "makes SSB cal ~resonance-independent" retracted 2026-06-19 — see below.)* |
| 2026-06-17 | *(Superseded 2026-06-18)* Initially considered run-and-mask (continuous demod + masking) and stop-and-go schemes | Both replaced by freeze-and-resume — see 2026-06-18 entry |
| 2026-06-17 | Reuse `scan_trigger_o`/`scan_new.v` pulse machinery as LO trigger source | Already emits programmable triggers; proven path |
| 2026-06-17 | *(Superseded 2026-06-21 — power fixed 13 dBm, not per-resonance; see that row)* Hold per-resonance MW power in the SynthNV Pro hop table, not in `fgen3` | Datasheet confirms a 500-pt freq+amplitude hop table; keeps power switching on the LO with the frequency hop |
| 2026-06-17 | *(Superseded by freeze-and-resume)* Lock-in filter choice was thought to be the primary dead-time lever | True only for a refilling filter; freeze-and-resume holds the filter, so the FIR choice no longer sets the per-hop dead time (Sections 5, 7). LO settling (100 µs) is indeed negligible |
| 2026-06-18 | **Core scheme = freeze-and-resume** (DECIDED): clock-gate and hold the entire inactive per-resonance chain (oscillator + FM drive + lock-in) | Holding the converged filter state turns per-hop dead time into *physical settle* (~100–300 µs, filter-independent) instead of *filter refill* (up to ~9 ms). Decouples lock-in bandwidth from hop rate → keep high-SNR narrow filter *and* hop fast (Sections 5, 7). Alternative schemes removed from the doc 2026-06-19 |
| 2026-06-18 | Single shared `f_m`; no distinct/orthogonal tones | Only one chain is clocked at a time, so there is no simultaneous cross-talk to suppress (Sections 5, 6) |
| 2026-06-19 | **SSB calibration is strongly LO-dependent, not IF-only** → per-resonance full cal sets required; FPGA cal-slot bank (9b) is mandatory, not an optimization | Empirical finding from J.W.'s IQ-cal experiments; physically expected (mixer I/Q imbalance + LO→RF leakage vary across the GHz LO range). Retracts the 2026-06-17 "keep IF constant ⇒ cal ~resonance-independent" rationale (Sections 4, 9, P-4b) |
| 2026-06-19 | IF tracking range is ~±15 MHz (set via `ftw_lim`/`max_correction_hz`), not ±1 MHz | ±1 MHz is only the register default; coarse LO recapture (Section 12) is a long-run safety net, not on the critical path |
| 2026-06-19 | `f_m,i` must be **above** the FIR cutoff (corrected from "below") | Post-mixer residuals sit at `f_m, 2f_m, …` and must fall in the FIR stopband (Section 6) |
| 2026-06-19 | **CIC differential delay is M=2** → nulls at multiples of `f_dec/2 = 15.26 kHz`; the **first null sits at `f_m`**, so `f_m = 15.25 kHz` nulls the 1f residual (and all harmonics) exactly | Corrects two earlier mistakes: the assumed M=1 (first null at `f_dec`, 2f on it) and the over-conservative `f_m < f_dec/4` rule + ~7 kHz suggestion. 15.25 kHz is the unique low null-aligned sweet spot (J.W.) (Section 6) |
| 2026-06-19 | **Keep the proven `f_m = 15.25 kHz` for *every* resonance** | Only one chain is demodulated at a time, so no spectral separation is required; bonus, the demod phase `2π·f_m·τ` is then identical for all channels (one phase cal). FIR cutoff becomes a pure SNR↔latency choice — keep 2 kHz, or 500 Hz for more SNR (free under freeze-and-resume) (Sections 6, P-4) |
| 2026-06-19 | **Committed to freeze-and-resume; removed the alternative multiplexing schemes** (run-and-mask, stop-and-go) from the doc | Decision settled a while ago; doc cleaned up to present freeze-and-resume as *the* approach rather than one of three options |
| 2026-06-20 | **Point-0 / continuity convention:** ODMR `JUMP_LIST` uses `c0` (clamp) to **mirror the linear sweep exactly** (drop-in: scope-verified identical freq↔sample alignment under the existing `generate_pulse`+`acquire_frame`+`reset_scan` flow); tracking mode uses `c1` (wrap) + aligns data by FPGA `scan.current_step`, not trigger counting | (a) Existing ODMR logic is shared across modes, so matching the sweep means zero logic changes. (b) Tracking is continuous and must be robust to first-trigger ambiguity → use the hardware step index. `_configure_windfreak_list(..., continuous=...)` param; default `False`=`c0` for ODMR |
| 2026-06-20 | **OQ-8 resolved — concrete `(g,phi)`→register mapping + slot size.** Cal is applied in qudi `RedPitayaIFSource`/`IFSourceBase` (not pyrpl): `amp_a=A(1+g)`, `amp_b=A(1-g)`, `phase_a=0`, `phase_b=(q_base+deg(phi))%360`; DC offsets are **global** (averaged over components), not per-component. On-FPGA slot = 3×(amp_a 14b, amp_b 14b, phase_b 32b) + 1×(dc_a/dc_b 14b) = **208 bits ≈ 26 B**; frequencies, FM, and demod phase stay global. So the cal-slot bank is tiny (registers, no BRAM) and the hop is a single slot-index advance | Read `redpitaya_if_source.py` / `if_source_base.py` (apply path) + `fgen3.py` (register widths/norms). Confirms the 9b slot mux is cheap and well-defined (Section 9) |
| 2026-06-20 | **N=2 is the working target** (N=8 deferred); **replicate** the demod (two clock-gated lock-in chains), don't time-share (OQ-4, OQ-6) | J.W.: N=2 covers the foreseeable need (one axis = 1 B-projection + temperature); replicate is the simplest, lowest-risk form for small N and preserves per-resonance state for free. Time-share is revisited only for N=8 |
| 2026-06-20 | **Keep the 2 kHz FIR; maximize per-resonance bandwidth** (OQ-5) | J.W.: stick with the proven 2 kHz FIR, push `f_eff,i` as high as the hop/dwell budget allows. Future aim (not now): few-kHz tracking BW on all channels. Sizing is driven by *maximizing bandwidth*, not by tracking a known slow drift |
| 2026-06-20 | **FPGA-only — no software-sequenced prototype** (kills the old Phase 1) | J.W.: the lock-in, freq tracking, and scanning already run on the FPGA; the multi-resonance additions (cal-slot bank, multi-integrator, freeze-and-resume, hop sequencer) will too. Roadmap rewritten FPGA-first (Section 14). Software stays only as a thin bring-up/supervisory layer |
| 2026-06-21 | **Phase A design finalized** (Section 9, "Phase A — concrete RTL sketch"): clean **packed cal-bank** at `0x0200`, slot stride `0x40` (legacy cal addresses removed, map break handled in Python); slot = OQ-8 208-bit set with **`phase_offset_a` dropped** (I-phase ≡ 0 hardwired); keep both `active_slot` + `active_slot_src`; `comp_enable` global; **no cal-change output gate in Phase A** (blanking is Phase C's job — avoids two competing gates); registered `sel` mux | J.W. chose the clean/symmetric address scheme over the legacy-aliased one (accepting the backward-compat break) and left the phase-step handling to me; the glitch lands inside Phase-C blanking so a separate Phase-A gate is redundant. *(Was plan-only when written; now IMPLEMENTED + bench-verified — see Section 14 → Phase A "as implemented".)* |
| 2026-06-21 | *(SUPERSEDED 2026-06-22 — see next row)* One shared free-running `f_m` oscillator; per-channel state = demod+integrator only | Had argued the resume AC ripple was negligible so a shared oscillator suffices. J.W. instead requires exact per-channel phase continuity → superseded |
| 2026-06-22 | **Per-channel phase-continuous modulation/demodulation REQUIRED** (Section 5.1): each channel resumes at the phase `φ_i` where it was paused. Implement as **N per-channel phase accumulators (same `f_m`) + ONE shared sin/cos LUT**, each accumulator clock-enabled by its channel's active/valid signal (parked ones hold `φ_i`), active phase feeds FM drive + demod refs. NOT a single free-running oscillator (loses continuity); NOT replicated LUTs (wasteful) | J.W.: phase continuity per channel is important. Accumulator-per-channel is cheap (N registers+adders, LUT stays shared) and gives exact continuity + zero resume transient. `fgen3` IF carrier phase still not per-channel (P-7). Affects Phase C / Sections 5, 5.1, 12, P-3. *(Now IMPLEMENTED as `odmr_multitrack.v` region 9 — N=2 accumulators + shared 17-bit LUT; see Section 14 → Phase C "as implemented".)* |
| 2026-06-21 | **`T_dwell` (hop rate) and `T_settle` (dead time) are runtime registers, tuned empirically** (#2, Section 7) | Best values depend on measured LO settle + pipeline τ + lock SNR; keep them parameters and sweep. `T_settle` gates the freeze window before clocking resumes |
| 2026-06-21 | **Windfreak MW power fixed at 13 dBm for every hop entry** (#5) | J.W.: 13 dBm works well for the IQ mixing at all LO frequencies. So MW power is *not* a per-resonance variable — the hop table's per-entry amplitude is constant; simplifies the table and removes power from the per-resonance state. (Per-resonance *IF* amplitude `A` is still captured inside the cal slot's `amp_a/amp_b`.) |
| 2026-06-21 | *(SUPERSEDED 2026-06-22 — see final row)* Streaming scheme = continuous wall-clock stream + per-entry value/NaN + `current_step` tag + valid bit; per-resonance `valid` register reused for freeze/integrator gates (OQ-11) | Right instincts (sample index = absolute time; one signal serves stream+gates) but the tag-per-sample encoding and the NaN-means-two-things model were reworked into the determinism-invariant framing of the final row |
| 2026-06-22 | *(SUPERSEDED same day — see final row)* Stream all resonances simultaneously, interleaved N-per-tick (snapshot all N held integrators every tick → N gap-free columns; `ftw_corr_all`) | My intermediate proposal. **Rejected by J.W.: the parked integrator is constant, so re-streaming the held value every tick is pure redundancy.** Determinism does not require continuous streaming — see final row |
| 2026-06-22 | **Streaming scheme RESOLVED (OQ-11, Section 11): deterministic time-base + fresh-only + ZOH-on-PC.** Invariant: PC must reconstruct `(absolute tick, resonance) → value` with certainty (the values are the B/temperature measurement); time base = one free-running wall-clock tick (never frozen), `tick·Ts` = absolute time. Because the correction is **piecewise-constant** (integrator changes only while live, holds while parked) and the error only exists while live, **stream only the fresh per-tick value of the *live* resonance (the existing data path, unchanged) + hop-boundary markers (tick, `current_step`); reconstruct the held correction by ZOH on the PC — never transmit the constant.** NaN = transport loss only (dead-time is structural via markers). 2D = double slice (per-resonance ZOH series, then x/y position markers) + a dedicated 4th marker bank for hop boundaries. Positional N-column interleave kept as a fallback only | J.W.: "I don't mind how we stream it, but we must absolutely deterministically reconstruct at what time which correction/error was applied/measured." The piecewise-constant correction makes continuous streaming of held values unnecessary; the lean live-value+marker scheme reuses the data path with no change, drops all redundant/placeholder words, and keeps the determinism guarantee. Builds on the proven motor-scan marker primitive (Section 11) |
| 2026-06-21 | **Freeze via `aclken` + IP regen** (OQ-12 resolved): regenerate CIC + 3 FIRs with `Has_ACLKEN=true`, keep `Has_ARESETn=false`; drive `aclken` per chain from the resonance `valid`. Recipe (FIR TCL line 95; CIC `.xci`; `generate_fir_ips.py`; `copy_and_make.bat`; copy `.bin`) in Section 13 | J.W.: take the cleaner solution, regen is no problem. `aclken` is the manual's unambiguous halt-all-state signal; avoids relying on the `tvalid`-stall inference |
| 2026-06-23 | **Phase B coded** (Section 14 → Phase B "as implemented"): `odmr_freq_lock_1f.v` → `NSLOTS` per-slot integrator/output/status arrays, time-shared datapath, `active_slot`/`active_slot_src` slot mux (same idiom as Phase A fgen3) from `current_step`; bumpless hold of inactive slots; `ftw_correction_o` driven **continuously** from the active slot (fgen3 samples it every clock, not valid-gated); **loop gains/deadband/ftw_lim stay global**, only integrator *state* is per-slot; `clr`/`enable=0` zero all slots. New regs `0x24` SLOT_CTRL, `0x28` NSLOTS, per-slot bank `0x40` stride `0x10`; legacy `0x10/14/18/20` kept as slot-0 aliases (full back-compat, N=1 identical to old loop). `current_step[7:0]` routed to region 8 in `red_pitaya_top.v`. **BENCH-VERIFIED 2026-06-23: `test_phaseB_multiint.py` 13/13** (NSLOTS live, slot ctrl, clear/disable zeros all, slot-0 alias==bank, and dynamic per-slot integrate/hold with bumpless switch — slot0 held +1 MHz while slot1 integrated 0→~987 kHz) | Generalizes the single integrator to N=2 per the Phase B plan, reusing the proven Phase A slot-mux idiom and the existing anti-windup/PI datapath verbatim (only the operand/target became slot-indexed). Global gains chosen for simplicity (per-slot gains deferred — N=2 bring-up doesn't need them); back-compat via slot-0 aliases avoids touching the single-resonance Python |
| 2026-06-23 | **Phase C coded** (Section 14 → Phase C "as implemented"): IP regen with `aclken` (OQ-12); new 17-bit shared-LUT per-channel oscillator `odmr_multitrack.v` (region 9, N=2 accumulators + `T_settle` freeze window + per-channel `aclken`); `lock_in.v` `aclken_i` + 2nd chain (region 10); top-level ref/FM/aclken/err muxing on `osc_active`; `OdmrMultitrack`+`LockIn1` Python. Transparent when disabled (legacy iq0 path untouched). Awaiting rebuild + bench validation + `mu`/`kp` re-tune | Oscillator approach B (new lean 17-bit LUT) chosen by J.W. over reusing iq_block; 17-bit preserves demod gain (re-tunable). Combined oscillator+freeze controller in one region-9 module = the start of the Phase D "odmr_multitrack sequencer". Backward-safe via the iq0/osc mux so the proven single-resonance system is unchanged until enabled. Resource risk on xc7z010 (2nd chain) flagged with the 2 kHz-prune fallback (OQ-5) |
| 2026-06-23 | **Phase B-stream coded** (Section 14 → Phase B-stream "as implemented"): hop-boundary marker capture in `scan_new.v` (internal `current_step` edge → tick→`ram_lsb`, step→`ram_msb`, `STREAM_CONTROL[3]`, regs `0x40/0x44`); **concurrent scan-trigger + stream** enabled (relax FSM start gate, `STREAM_CONTROL` writable while busy, suppress scan `bram_wr_en` while streaming) so the scan FSM drives hops while the push stream runs. `scan.py`: `hop_stream_start/read/stop`, `read_hop_markers` (tick+step, wrap-aware), `reconstruct_hop_series` (fresh-only + ZOH; NaN = transport loss only). **BENCH-VERIFIED 2026-06-23: `test_phaseB_stream.py` 15/15** (offline reconstruct 6/6; STREAM_CONTROL[3] round-trip; dynamic N=4 scan concurrent with ftw_corr stream → markers `ticks=[0,178,243,309] steps=[0,1,2,3]`) | Implements the OQ-11 live-value + hop-marker transport with the proven MODE-3 marker idiom; the concurrency enabling is the minimal interim way to make hops happen during streaming (Phase D's sequencer replaces it). **Bank sub-decision resolved:** reuse the two free position-marker banks for monitoring (tick+step); dedicated 4th bank deferred to the 2D motor-scan composition. Backward-safe: scan-only / stream-only / MODE-3 paths unchanged when hop off |

---

## Appendix A. Using the oscilloscope (bench verification)

A Rohde & Schwarz **RTO6** is available for objective, automated bench checks
(it was the ground truth for verifying the LO-hopping / JUMP_LIST work).

- **Access:** VISA `TCPIP0::10.203.129.15::inst0::INSTR`, via the `RsInstrument`
  library (installed in the qudi venv). There is a qudi hardware module
  `qudi/hardware/oscilloscope/rohde_schwarz_rto6.py` (FFT-based spectrum +
  markers), or talk to it directly with `RsInstrument`/SCPI for scripts. Connect
  with `reset=False` so you don't wipe the user's current scope setup.
- **Amplitudes are not absolute.** The scope is **not** at the IQ-mixer
  output — it sits on **circulator port 3 (reflections)** off a *badly-matched*
  loop antenna. So measured power is shaped by the antenna's frequency-dependent
  reflection coefficient to an extent. **Use the scope only for the frequency content** of the
  signal (which is reliable); do **not** use it to judge absolute MW power.
  

- **Frequency measurement recipe (FFT peak = the live MW/LO frequency):**
  ```python
  from RsInstrument import RsInstrument
  import numpy as np
  sc = RsInstrument('TCPIP0::10.203.129.15::inst0::INSTR', id_query=True,
                    reset=False, options="SelectVisa='rs'")
  sc.write_str_with_opc('CALC:MATH1 "FFTmag(Ch1)"'); sc.write_str_with_opc('CALC:MATH1:STATE ON')
  sc.write_str_with_opc('CALC:MATH1:FFT:CFR 2870000000')  # center (Hz), cover the hop span
  sc.write_str_with_opc('CALC:MATH1:FFT:SPAN 800000000')
  sc.write_str_with_opc('SING')                            # single acquisition
  a = np.array(sc.query_bin_or_ascii_float_list('FORM REAL,32;CALC:MATH1:DATA?'))
  h = sc.query_bin_or_ascii_float_list('CALC:MATH1:DATA:HEAD?')   # [f_start, f_stop, n]
  f = np.linspace(h[0], h[1], int(h[2]))
  peak_hz = f[int(np.argmax(a))]                           # dominant peak = live LO/RF
  ```
  The dominant FFT peak tracks the LO (leakage) / RF, so it is a reliable readout
  of *which frequency is being output* — e.g. used to confirm each Red Pitaya
  trigger steps the Windfreak to the expected table point. Channel 1 carries the
  signal; widen `SPAN`/`CFR` to cover all hop frequencies in one view.
- **Why it matters here:** the Windfreak `f?` register lies in tabular trigger mode
  (reports the *next/pre-loaded* point, not the live output), so serial readback is
  not ground truth — the scope is. For commissioning the tracker, the scope is the
  go-to for "did the LO actually land where we think it did".

---

## 18. References

- [odmr_freq_lock_implementation.md](odmr_freq_lock_implementation.md) — single-resonance loop (control theory, register map, tuning).
- `pyrpl/fpga/rtl/odmr_freq_lock_1f.v`, `pyrpl/hardware_modules/odmr_freq_lock.py` — current loop.
- `pyrpl/fpga/rtl/red_pitaya_3fgen.v`, `pyrpl/hardware_modules/fgen3.py` — 3-component FM DDS + FTW correction injection.
- `pyrpl/fpga/rtl/lock_in.v`, `pyrpl/hardware_modules/lock_in.py` — dual-channel demod, filter/bypass options.
- `pyrpl/hardware_modules/iq.py` — IQ0/IQ2 modulation+reference oscillators.
- `pyrpl/fpga/rtl/scan_new.v` — trigger-pulse / dwell machinery (model for LO hopping).
- `pyrpl/fpga/rtl/red_pitaya_top.v` (lines ~700–840) — instantiation & wiring of all of the above; DIO routing (line ~458).
- `docs/manuals/synthnvpro-rf-signal-generator-detector.pdf` — SynthNV Pro hardware datasheet (owner-password only; readable via PyMuPDF). Extracted text: `docs/manuals/synthnvpro_extracted.txt`. Gives lock time (100 µs), 500-pt freq+amp hop table, 3.3 V trigger.
- **qudi-iqo-modules** `src/qudi/hardware/microwave/mw_source_windfreak_synthnvpro.py` — `MicrowaveSynthNVPro` driver; serial command set, `y2` single-step trigger, sweep (`l/u/s`) and experimental list (`L…`) modes — the existing software-controls-LO / FPGA-triggers path.
- **qudi-iqo-modules** `src/qudi/hardware/redpitaya/redpitaya_data_instream.py` — `RedPitayaDataInStream`; pyrpl scan push-streaming, lock-in (`rp.lockin`) filter config, the "one streamed quantity at a time" constraint, KDC_HW_SYNC stream handover.
- `C:\calibration_results\2026-01-21-10-23-24\` — SSB calibration CSVs: columns `g, phi, I_offset, Q_offset` (+ quality metrics) per `(sideband, IF, LO, IF-amplitude)`; IF folders = hyperfine triplet (Δ=2.158 MHz). Source data for the cal-slot bank (Section 9).
- **qudi-iqo-modules** `src/qudi/hardware/oscilloscope/rohde_schwarz_rto6.py` — R&S RTO6 FFT/marker module (`RsInstrument`); bench ground-truth scope, `TCPIP0::10.203.129.15::inst0::INSTR`. See Appendix A (amplitudes not trustworthy — antenna-reflection tap; frequency only).
