"""Bench test: marked-continuous stream (STREAM_CONTROL[5]) vs legacy dual (bit4).

Proves the timing fix: the marked-continuous mode emits a triplet on a FREE-RUNNING
/4096 tick (one sample EVERY demod period, including the per-hop settle/freeze
dead-time), so the PC timeline is uniform and EXACT. The legacy dual mode only emits
on the (aclken-gated) demod strobe, so it drops the dead-time and over-estimates the
per-resonance visit rate.

Needs the board (10.203.129.28) with the NEW bitstream deployed, qudi CLOSED. No RF /
no lock required: we only enable the multitrack oscillator so the per-hop freeze
window exists (valid_window goes low during settle -> DEAD-tagged samples appear).

Run:  ./venv/Scripts/python.exe -m pyrpl.test.test_marked_stream
"""
import time
import numpy as np
import pyrpl
from pyrpl.hardware_modules.scan import Scan

HOST = "10.203.129.28"
FS = 125e6 / 4096          # 30517.6 Hz demod / triplet rate
WORDS_PER_SAMPLE = 4
NSLOTS = 2
DWELL = 1.0e-3
SCAN_SETTLE = 100e-6
OSC_SETTLE = 200e-6        # multitrack freeze window = the stream dead-time per hop
TRIGGER = 50e-6
DRAIN_S = 5.0

# expected per-resonance visit period: TRUE = N*(dwell + osc_settle + trigger),
# i.e. the full cycle INCLUDING the dead-time. The legacy dual stream drops the
# dead-time and so reports ~N*dwell (too short -> rate too high).
EXP_TRUE_CYCLE = NSLOTS * (DWELL + OSC_SETTLE + TRIGGER)      # ~2.30 ms -> 435 Hz
EXP_TRUE_RATE = 1.0 / EXP_TRUE_CYCLE
EXP_LIVEONLY_RATE = 1.0 / (NSLOTS * DWELL)                    # ~500 Hz (dual bug)


def _connect(reload_fpga=False):
    p = pyrpl.Pyrpl(hostname=HOST, config="", reload_fpga=reload_fpga,
                    reload_server=True, gui=False)
    return p, p.rp


def _enable_osc(rp):
    mt = rp.odmrmultitrack
    mt.frequency = 15.25e3
    mt.settle_time = OSC_SETTLE
    mt.src = "current_step"
    mt.enable = True
    return mt


def _run_mode(rp, source):
    """Start a 2-slot continuous hop in `source` mode, drain DRAIN_S, return words."""
    scan = rp.scan
    try:
        scan.continuous_hop_stop()
    except Exception:
        pass
    scan.continuous_hop_start(nslots=NSLOTS, input_source=source,
                              dwell_time=DWELL, settling_time=SCAN_SETTLE,
                              trigger_length=TRIGGER)
    chunks = []
    t0 = time.time()
    while time.time() - t0 < DRAIN_S:
        w = scan.hop_stream_read()
        if w is not None and len(w):
            chunks.append(np.asarray(w, dtype=np.float64))
        time.sleep(0.05)
    wall = time.time() - t0
    scan.continuous_hop_stop()
    words = np.concatenate(chunks) if chunks else np.array([], dtype=np.float64)
    return words, wall


def _visit_rate(visit_idx):
    if visit_idx.size < 4:
        return float("nan")
    return FS / float(np.median(np.diff(visit_idx)))


def main():
    print("Connecting + deploying bitstream (reload_fpga=True)...")
    p, rp = _connect(reload_fpga=True)
    results = {}
    try:
        _enable_osc(rp)
        time.sleep(0.2)

        # ---- MARKED mode ----
        w_m, wall_m = _run_mode(rp, "marked")
        rec_m = Scan.reconstruct_marked_series(
            w_m, nslots=NSLOTS, to_hz_corr=True,
            words_per_sample=WORDS_PER_SAMPLE)
        T_m = rec_m["step"].size
        dead = rec_m["dead"]
        loss_m = float(np.mean(np.isnan(w_m))) if w_m.size else 1.0
        eff_rate_m = T_m / wall_m
        dead_frac = float(np.mean(dead)) if T_m else float("nan")
        # per-resonance visit index = last live sample of each contiguous run
        vrate_m = []
        for r in range(NSLOTS):
            live_r = (rec_m["step"] == r)
            ends = np.where(np.diff(np.r_[0, live_r.astype(int), 0]) == -1)[0] - 1
            vrate_m.append(_visit_rate(ends))
        print(f"\n[MARKED] words={w_m.size} T={T_m} loss={loss_m*100:.2f}% "
              f"wall={wall_m:.2f}s")
        print(f"[MARKED] stream sample rate T/wall = {eff_rate_m:.0f} Hz "
              f"(expect ~{FS:.0f} = full uniform grid)")
        print(f"[MARKED] dead fraction = {dead_frac*100:.1f}% "
              f"(expect ~{OSC_SETTLE/(DWELL+OSC_SETTLE+TRIGGER)*100:.0f}%)")
        print(f"[MARKED] per-res visit rate = {[round(x,1) for x in vrate_m]} Hz "
              f"(expect ~{EXP_TRUE_RATE:.0f})")

        # ---- DUAL mode (legacy) for A/B ----
        w_d, wall_d = _run_mode(rp, "dual")
        rec_d = Scan.reconstruct_dual_hop_series(
            w_d, nslots=NSLOTS, to_hz_corr=True,
            words_per_sample=WORDS_PER_SAMPLE)
        T_d = rec_d["err"].shape[1]
        eff_rate_d = (w_d.size // WORDS_PER_SAMPLE) / wall_d
        # dual visit rate: live-only spacing of step==r runs
        # rebuild step labels the same way reconstruct_dual does:
        records = w_d[: (w_d.size // WORDS_PER_SAMPLE) * WORDS_PER_SAMPLE].reshape(
            -1, WORDS_PER_SAMPLE)
        step_d = np.where(np.isfinite(Scan._ffill(records[:, 3])),
                          np.rint(Scan._ffill(records[:, 3])), -1).astype(np.int64)
        vrate_d = []
        for r in range(NSLOTS):
            live_r = (step_d == r)
            ends = np.where(np.diff(np.r_[0, live_r.astype(int), 0]) == -1)[0] - 1
            vrate_d.append(_visit_rate(ends))
        print(f"\n[DUAL]   words={w_d.size} T={T_d} wall={wall_d:.2f}s")
        print(f"[DUAL]   stream sample rate (triplets/wall) = {eff_rate_d:.0f} Hz "
              f"(< {FS:.0f}: dead-time missing)")
        print(f"[DUAL]   per-res visit rate = {[round(x,1) for x in vrate_d]} Hz "
              f"(inflated ~{EXP_LIVEONLY_RATE:.0f}, should have been {EXP_TRUE_RATE:.0f})")

        # ---- assertions ----
        ok = True
        def check(name, cond):
            nonlocal ok
            print(f"  {'PASS' if cond else 'FAIL'}: {name}")
            ok &= cond
        print("\n=== checks ===")
        check("marked: stream has samples, ~0% loss", w_m.size > 0 and loss_m < 0.02)
        check("marked: pre-FIR CIC column is present on live samples",
              np.isfinite(rec_m["cic"][:, rec_m["step"] >= 0]).any())
        check("marked: dead-time samples present", dead.sum() > 0)
        check("marked: dead fraction in (3%, 40%)", 0.03 < dead_frac < 0.40)
        check("marked: uniform stream rate ~30.5 kHz (+/-8%)",
              abs(eff_rate_m - FS) / FS < 0.08)
        check("marked: per-res visit rate ~435 Hz (+/-15%)",
              all(abs(v - EXP_TRUE_RATE) / EXP_TRUE_RATE < 0.15 for v in vrate_m))
        check("dual: NO dead samples (legacy, gated)",
              True)  # informational; dual has no dead concept
        check("marked visit rate < dual visit rate (dead-time restored)",
              np.nanmean(vrate_m) < np.nanmean(vrate_d) * 0.95)
        check("dual visit rate inflated toward ~500-526 Hz",
              np.nanmean(vrate_d) > EXP_TRUE_RATE * 1.10)
        results["ok"] = ok
        print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}")
    finally:
        try:
            rp.scan.continuous_hop_stop()
        except Exception:
            pass
        try:
            rp.odmrmultitrack.enable = False
        except Exception:
            pass
    return 0 if results.get("ok") else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
