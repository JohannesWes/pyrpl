# Two-Resonance ODMR Tracking — ASD Debugging Log & Handoff

> **Status (2026-06-30):** in progress. Freq-lock timing + current_step bugs fixed;
> one remaining bug (MAC-delay slot-straddle) fixed in RTL, **rebuild #2 + re-test pending**.
> This doc is the working context to continue from in the next session.

Companion design docs (read these for the architecture; this doc only covers the
debugging + test workflow):
- Control loop / integral FLL: [`odmr_freq_lock_implementation.md`](odmr_freq_lock_implementation.md)
- Multi-resonance design & planning: [`multi_resonance_tracking.md`](multi_resonance_tracking.md)
- Streaming / reconstruction: [`scan_data_streaming.md`](scan_data_streaming.md),
  [`qudi_redpitaya_streaming_integration.md`](qudi_redpitaya_streaming_integration.md)

---

## 1. Goal of 2-resonance tracking

Track how the frequencies of **two different ODMR resonances** drift **simultaneously**,
on one Red Pitaya + one Windfreak LO + one IQ mixer, by **hardware-driven LO hopping**:

- One shared modulation frequency `f_m`; **one phase accumulator, lock-in chain, and
  integral controller (slot) per resonance**.
- While parked on resonance *i*: only chain *i*'s phase accumulator advances, only chain
  *i* demodulates, only controller slot *i* integrates. The others **hold** (bumpless).
- On a hop to resonance *j*: chain/controller *i* **pause**; chain/controller *j* resume
  **only after** the microwave source has settled — so resonance *i*'s signal chain is
  never contaminated with samples that belong to *j*.
- Stream both resonances so that **every demod sample is unambiguously labelled** (which
  resonance, or dead-time), enabling a **deterministic per-resonance time series** for
  noise/sensitivity (ASD) analysis on the PC.

**Sensitivity metric:** amplitude spectral density (ASD, nT/√Hz) of the per-resonance
frequency estimate, vs the single-resonance baseline. 1-res baseline ≈ **1.4–1.8 nT/√Hz**
(5–50 Hz). The 2-res question: how close can we get, and what does hopping cost?

## 2. Signal path & the hop/pause challenge

```
ADC → lock-in chain 0 ─┐                      ┌→ slot 0 integrator ─┐
                       ├ active_demod (mux) → │  (odmr_freq_lock_1f)├→ ftw_correction → fgen3 (FM + SSB cal)
ADC → lock-in chain 1 ─┘   by current_step    └→ slot 1 integrator ─┘
        ▲ aclken freeze (per chain)                ▲ slot = current_step
        │                                          │
   odmr_multitrack (shared f_m osc, per-channel phase park, T_SETTLE freeze)
        ▲ sel = current_step
        │
   scan (scan_new.v): emits LO-hop trigger, advances current_step, streams [err,corr,state]
```

Everything keys off **`current_step`** (the live resonance index from `scan_new.v`,
routed in `red_pitaya_top.v` to `odmr_multitrack`, `odmr_freq_lock_1f`, `fgen3`, and the
stream tag). Correct operation requires `current_step`, the demod freeze, the integrator
slot, the cal slot, and the stream label to **all switch in lockstep with the physical
MW hop**.

**Challenges:**
- **Hopping:** the external MW source needs a physical settle time after each hop trigger;
  the demod/controller must be frozen across exactly that window.
- **Pausing:** parked chains hold filter state and phase (phase-continuous resume); parked
  integrators hold their converged correction (applied to fgen3 so the resonance stays put).
- **Two settle timers** exist (see §7): `odmr_multitrack` `T_SETTLE` (the real demod/
  controller freeze, GUI "Settle time") and the scan FSM `SETTLING_TIME` (legacy, now
  redundant in tracking mode).
- **Deterministic streaming:** marked-continuous mode (`STREAM_CONTROL[5]`) emits one
  `[err, corr, state]` triplet **every** demod period on a free-running /4096 tick, tagging
  dead-time explicitly → exact uniform 30.5 kHz time grid.
- **Fragmented ASD (see §5).**

## 3. Test suite

### HW-driving ASD tests — `qudi-core/my_software/freq_tracking_tests/noise_test.py`
Attaches to a **running qudi** and drives the live modules. Entry points:
- `run_test1(rf_min,rf_max,…)` — **1-res**: open-loop (err→B, lock off) vs closed-loop
  (corr→B). Saves traces/ASD/plots.
- `run_test2_full(ranges,…)` (CLI `--test2-full`) — the **main 2-res study**, drift-bracketed:
  (A) fit both features; (B) 1-res references per resonance — *start* bracket; (C) sweep
  2-res params (`sweep_2res`, grid `_2res_grid` / `_2res_grid_lowbw`); (D) 1-res references —
  *end* bracket; then optimal-2res-vs-1res ASD overlay + per-axis trends + within-dwell
  profile + crosstalk.
- `record_traces` (1-res) / `record_traces_multi` (2-res) — drain `scan.hop_stream_read()`
  and reconstruct (`reconstruct_dual` / `reconstruct_dual_fresh` / `reconstruct_marked_series`).
- Analysis: `compute_asd` (Welch, NaN-interpolated), `asd_noise_floor`, `_fresh_metrics`
  (servo bump/floor/Nyquist), `_crosstalk`, `within_dwell_profile`.
- Pure-math self-test: `--dry` (no hardware) — run after editing the reconstruction/ASD code.

CLI (run from `qudi-core/`):
```bash
MPLBACKEND=Agg PYTHONPATH=C:/Users/aj92uwef/PycharmProjects/qudi-core \
  ./venv/Scripts/python.exe -u -m my_software.freq_tracking_tests.noise_test \
  --test2-full --lowbw \
  --rf-min 2.72e9 --rf-max 2.76e9 --rf-min2 2.97e9 --rf-max2 3.01e9 \
  --closed-duration 12 --two-res-duration 12
```
Other flags: `--power`(-10), `--points`(1000), `--bandwidth`, `--dwell`, `--pi`,
`--no-baselines`, `--outdir`, `--two-res-duration`. Runtime ≈ 4–6 min; output dir printed
as `[out] …` (see §9).

### Bench regression tests — direct PyRPL, **no qudi** (run from `pyrpl_new/`)
Connect with `pyrpl.Pyrpl(hostname='10.203.129.28', reload_fpga=True, reload_server=True, gui=False)`.
**qudi must be closed** for these.
- `pyrpl/test/test_marked_stream.py` — marked vs dual: asserts dead-time present, ~30.5 kHz
  uniform rate, true ~436 Hz visit rate.
- `test_dual_stream.py`, `test_continuous_hop.py` — streaming + hop sequencing (12/12, 13/13).
These verify the **digital** mechanism only (sequencing, labels, rates) — they do **not**
catch analog/closed-loop faults (that is what the ASD tests above are for).

## 4. Controlling qudi for the ASD tests

The ASD tests run **outside** qudi but drive the **live** qudi modules over rpyc:

- **Attach:** rpyc namespace server on **port 18861**, reusing
  `qudi.core.qudikernel.QudiKernelClient` → `get_active_modules()`. Wrapped by the
  `QudiHandles` class in `noise_test.py`. Handles: `h.odmr` (tracking logic), `h.scan`,
  `h.lock_hw` (odmr_lock HW), `h.lock` (pyrpl freq-lock), `h.mw` (Windfreak).
- **Localize netrefs:** numpy arrays come back as rpyc netrefs → wrap with
  `h.obtain(x)` (`rpyc.utils.classic.obtain`) before using locally.
- **Environment:** venv python `qudi-core/venv/Scripts/python.exe`; **`PYTHONPATH=…/qudi-core`**
  is required when running by `-m`; `MPLBACKEND=Agg` for headless plots.
- **Gotcha — we are the sole stream drainer:** over rpyc the logic's `_multi_status_timer`
  lives on the rpyc service thread, so `QTimer.start` silently fails and the logic never
  drains the push stream. The driver drains `scan.hop_stream_read()` itself — no race, but
  it means the GUI's live trace plots stay empty while a driver run is in progress.
- **Manual drive sequence** (what `run_test2_full` does, for ad-hoc work):
  `odmr.set_scan_region` → `start_odmr_scan` → `_wait_idle` → `fit_resonance_n(i,…)` ×N →
  `configure_multi_tracking` → `start_multi_tracking` → drain `scan.hop_stream_read()` →
  `stop_multi_tracking`. 1-res: `setup_single_resonance` → `park_cw` → `ensure_lock_polarity`
  → `record_traces(closed_loop=…)`.
- **Deploying a new bitstream:** copy `pyrpl/fpga/red_pitaya.bin`, then **restart qudi** —
  `resource_manager.py` connects PyRPL with `reload_fpga=True`, so a fresh qudi *process*
  reflashes the board on startup (a module reactivation does **not**, the instance is cached).
  qudi must be **running** for the ASD tests but **restarted** to pick up a new bin.

## 5. Correct ASD from the two fragmented series

Each resonance is observed **only during its visits** (~436 Hz visit rate for dwell 1 ms,
N=2; Nyquist ≈ 218 Hz). The integrator is a sample-and-hold; the ~30 within-visit samples
at 30.5 kHz are loop settling dynamics, **not** independent drift samples.

- **Correct:** decimate to **one value per visit** (last live sample of each contiguous
  live run) → uniform series at the true visit rate → Welch ASD. The marked-continuous
  stream gives the exact uniform grid + DEAD mask, so the visit spacing is exact.
- **Wrong:** naive ZOH ASD at 30.5 kHz → sinc² sample-and-hold rolloff + a comb at the hop
  rate (the "periodic noise" artifact). Only useful to *display* the artifact.
- The fixes in §7 make the DEAD/step labels **correct**; the ASD method itself is unchanged
  and already implemented (`reconstruct_marked_series`, `reconstruct_dual_fresh`).

## 6. What to watch for in ASD tests (diagnostic checklist)

1. **Demod vs correction health (the key split).** The **err (demod)** column must always
   roll off at the 2 kHz FIR — high/mid ASD ratio (5–10 kHz vs 100–300 Hz) ≈ **0.02**. If
   the **corr** ASD is **flat to Nyquist** (ratio → 1), the demod is fine but the
   **freq-lock/correction is broken** (this was bug #3). Compute both before concluding.
2. **Railing / saturation.** Check per-visit corr `min/max`: hitting **exactly
   ±max_correction (±1 MHz)** = instability or corruption (bug #4). Healthy `std` is a few
   **k**Hz (slow drift); `std` of hundreds of **k**Hz = railing.
3. **Transport loss** (`loss_frac`) must be ≈ 0 %; high loss → NaN-interpolation artifacts.
4. **Drift bracketing.** `run_test2_full` brackets the 2-res sweep with 1-res references
   before/after. Compare 2-res to the **contemporaneous 1-res bracket**, not absolute
   numbers. A start→end closed-floor change >~10 %, or a single anomalous reference (e.g.
   `212317` res0 = 4.23 nT/√Hz), means the run is confounded — **repeat it**.
5. **Per-visit, not ZOH** (see §5) for the honest floor.
6. **Visit rate** ≈ **436 Hz** (dwell 1 ms, N=2). A wrong rate shifts spectral lines (e.g.
   50 Hz mains → ~60 Hz). The marked stream makes it exact.
7. **Servo bump near visit-Nyquist (~218 Hz):** grows with loop BW; the dominant 2-res cost.
   Suppressed by lower BW / faster hop (fast-hop bump <1). Report it alongside the floor.
8. **Crosstalk:** `_crosstalk` returns `corr_raw` and `corr_resid`; the **residual** (after
   removing common-mode drift) is the cleaner cross-resonance-coupling metric.
9. **Run-to-run variation:** one run is noisy — use the bracket, and repeat before strong
   claims. Save raw `diag_words.npz` so the per-visit/ZOH/comb analysis can be redone offline.

## 7. Bug log

| # | Bug | Symptom / evidence | Root cause | Fix | Status |
|---|-----|--------------------|------------|-----|--------|
| 1 | **current_step latched late** (`scan_new.v`) | parked resonance contaminated during MW settle; stream samples mislabelled | `reg_current_step` latched in `S_ACQUIRING` (after settle) while `step_counter++` in `S_FINISHING` → `current_step` updated ~`trigger+settle` **after** the hop trigger | latch in **`S_START_STEP`** (one state before the trigger); removed redundant `S_ACQUIRING` assignment | **FIXED + HW-verified (digital).** Also *beneficial* in closed loop (bump/crosstalk improved) |
| 2 | **PC reconstruction time-axis** | per-visit rate overestimated (~526 vs true 436 Hz); mains line shifted | dual stream is demod-strobe-gated → dead-time was zero-width in the word stream | **marked-continuous** stream (`STREAM_CONTROL[5]`) tags dead-time explicitly; `reconstruct_marked_series` (exact uniform grid) | **FIXED** (prior session) |
| 3 | **Freq-lock MAC timing violation** | **all** closed-loop tracking broken: correction = broadband noise **above** the 2 kHz FIR (std ~4.3 kHz, changes every sample, flat-to-Nyquist ASD) | `odmr_freq_lock_1f` MAC (32×32 mult + 64-bit accumulate + saturate) is **single-cycle** but needs ~21 ns → post-route **WNS −13.05 ns, 11896 endpoints, no xdc constraint**. Latent on all builds; the rebuild that added fix #1 re-placed it and it tipped into **real silicon failure** | **delay the integrator capture** `MAC_DELAY=4` clocks (operands held stable 4096 clk) + `set_multicycle_path -setup 4 -hold 3` on `ftw_corr/ftw_out_r/flag_*` in `red_pitaya.xdc` | **FIXED + HW-confirmed (1-res).** Freq-lock gone from failing timing; 1-res correction rolls off |
| 4 | **MAC-delay slot-straddle** (incomplete fix #3) | **2-res only:** one slot (res1) **rails to exactly −1 MHz** intermittently (visit std 327 kHz); erratic floors 6–109 nT/√Hz | a delayed capture launched just before a hop **lands after** `slot_sel_r`/`err_i` switch → writes the **new** slot with the **old** slot's stale off-resonance error → saturates to ±`ftw_lim` | **snapshot operands at `err_valid`** (`err_upd<=err_i`, `slot_upd<=slot_sel_r`) and run the whole MAC + capture off the snapshot; output bus still follows live `slot_sel_r` | **RTL done, synced to build tree. REBUILD #2 + RE-TEST PENDING** |

Bug #3 was *not* caused by fix #1 — 1-res does not use `current_step` (oscillator runs
`sw_src=1`), yet 1-res was broken; the rebuild merely exposed the latent violation.

## 8. Settle-time configuration (consequence of fix #1)

After fix #1 the GUI **"Settle time"** spinbox (→ `_osc_settle_time` → `configure_oscillator`
→ `odmr_multitrack` `T_SETTLE`) is the **single authoritative** post-hop freeze: it starts
exactly at the hop, gates both demod chains + both integrators, and defines the DEAD window
in the stream. **Set it = the MW source's physical settle time.** The GUI-hidden
`_scan_settling_time` (scan FSM `SETTLING_TIME`) is now redundant for demod protection →
recommend **0** in tracking mode (scan accumulation is suppressed during streaming anyway).

## 9. Data & plots (under `~/qudi/Data/freq_tracking_tests/`)

| dir | build / state | what it shows |
|-----|---------------|---------------|
| `20260629-151442/` | pre-current_step, working freq-lock (15:00 bin) | clean 1-res closed-loop rolloff (reference for "healthy") |
| `20260629-153009/` | pre-current_step, working freq-lock | **clean 2-res baseline**: floors 1.46/2.57/3.45/4.15/4.70 (bw10–50), bump 4.75→9.69, fast-hop bump 0.86/2.57, crosstalk corr_raw 0.766 resid 0.064 |
| `20260629-212317/` | current_step fix + **broken** freq-lock (20:56 bin) | **INVALID** — corrupted by bug #3 (res0 1-res ref anomaly 4.23, flat 1-res closed ASD). Discard |
| `20260630-080657/` | current_step + freq-lock fix#1 (broken by #4) | 1-res baselines clean (1.41/1.44, drift 1–2 %); **bump improved ×3.31**, **crosstalk resid improved 0.016**; **res1 rails −1 MHz**, floors 78.6/17.2/109/18.6/6.21 |
| `diag_1res_live/` | broken 20:56 bin | bug #3 raw: corr std 4300, change-frac 0.999, ASD hi/mid **0.737** (flat) |
| `diag_1res_postfix/` | freq-lock fix#1 bin | bug #3 fixed: corr rolls off, ASD hi/mid **0.000**, closed floor 0.36 ≈ open 0.38 |
| `diag_column_spectra/` | 151442/153009/212317 | err (demod) column rolls off in **all** builds (ratio ~0.024) → demod always healthy; isolates the fault to the correction path |
| `fix_compare_153009_vs_212317/` | pre vs broken | floor/bump/crosstalk comparison `.png/.npz/.json` |
| `fix_compare_2res_visits/` | 153009 vs 080657 | **per-visit corr traces**: shows res1 railing to −1 MHz on the new build (bug #4) vs smooth on 153009 |

Each run dir also has `bracket_compare.png` (2-res vs 1-res ASD overlay), `sweep_axes/metrics.png`,
`within_dwell.png`, `summary_2res_full.json`, and the raw `diag_words.npz`.

## 10. Next session — continue here

1. **Rebuild #2** (`Documents/fpga_compilation/fpga`, `cmd /c '.\make.bat'`, ~40 min) with
   the bug-#4 snapshot fix already in `rtl/odmr_freq_lock_1f.v` + build tree.
   - Verify: `i_odmr_freq_lock_1f` still **absent** from the failing-timing list (snapshot
     doesn't deepen the MAC path). The `i_dsp/iq_2_outputs` −4.5 ns path remains — **benign**,
     long-standing, demod always clean on silicon; do not chase it.
2. Copy `out/red_pitaya.bin` → `pyrpl/fpga/red_pitaya.bin`; **restart qudi** to reflash.
3. Re-run `diag_1res_postfix`-style 1-res check (must stay clean, hi/mid ≈ 0).
4. Re-run `--test2-full --lowbw` (ranges: res0 2.72–2.76, res1 2.97–3.01 GHz).
   - **Success = res1 no longer rails**, per-visit floors back to the `153009` range
     (~1.5–5 nT/√Hz) **with** the current_step-fix gains (lower bump, lower crosstalk).
5. Then evaluate the real current_step-fix benefit and the bump-vs-BW / fast-hop tradeoff;
   optionally reduce `T_SETTLE` (now correctly timed) to raise duty / usable bandwidth.

## 11. Source files touched

- FPGA RTL: `pyrpl/fpga/rtl/scan_new.v` (fix #1), `pyrpl/fpga/rtl/odmr_freq_lock_1f.v`
  (fixes #3, #4), `pyrpl/fpga/sdc/red_pitaya.xdc` (fix #3 constraint). Read for analysis:
  `odmr_multitrack.v`, `lock_in.v`, `red_pitaya_top.v`.
  **Build tree copy (what Vivado compiles): `C:/Users/aj92uwef/Documents/fpga_compilation/fpga/{rtl,sdc}/` — keep in sync.**
- PC / pyrpl: `pyrpl/hardware_modules/scan.py` (`reconstruct_marked_series`).
- qudi: `logic/multi_resonance_odmr_tracking_logic.py`, `gui/odmr_tracking/multi_resonance_odmr_tracking_gui.py`,
  `hardware/redpitaya/redpitaya_odmr_lock.py`, `hardware/redpitaya/resource_manager.py`.
- Test driver: `my_software/freq_tracking_tests/noise_test.py`.
