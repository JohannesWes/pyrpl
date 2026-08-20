"""Dual-quantity (self-describing) high-rate streaming bench test.

Validates STREAM_CONTROL[4] dual-quantity mode: every demod strobe the FPGA writes
THREE words [err, corr, step] into the data3 ring (over three consecutive clocks),
so the PC can reconstruct FOUR simultaneous per-resonance traces (both errors AND
both corrections) at the full ~30.5 kHz aggregate rate. The step label travels
inline with each sample, so value<->resonance can never desync (no marker ring).

Requires the NEW bitstream (rebuilt scan_new.v with the dual-quantity writer) and
the ARM push server. Pure RTL change, no IP regen:
  cd pyrpl/fpga && copy_and_make.bat ; copy out/red_pitaya.bin -> live fpga dir.
NO RF / lock needed for the core checks: ftw_correction_valid strobes at ~30.5 kHz
regardless of lock, and the FSM advances current_step on its own.

If the loaded bitstream is OLD (no dual bit), PART 1 reports it and exits early.
"""
import sys
import time
import numpy as np

HOST = "10.203.129.28"
N = 2  # resonances

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")

import pyrpl
from pyrpl.hardware_modules.scan import Scan, ADDR_STREAM_CONTROL, FTW_PER_HZ

reload_fpga = "--reload-fpga" in sys.argv  # pass on the first run after a rebuild
print(f"Connecting to {HOST} (reload_fpga={reload_fpga}) ...")
p = pyrpl.Pyrpl(hostname=HOST, config="", reload_fpga=reload_fpga, reload_server=True, gui=False)
rp = p.rp
scan = rp.scan
print("Connected. scan:", scan)

# Clean slate
scan.continuous_hop_stop(stop_server=False)
scan._stream_ctrl_write(enable=False, hop=False, dual=False)
scan._write_control_bit(2, 1)  # reset
time.sleep(0.05)

# ===========================================================================
# PART 1: dual-quantity bit latches + reads back (new-bitstream probe)
# ===========================================================================
print("\n=== PART 1: dual-quantity control bit ===")
scan._stream_ctrl_write(enable=True, reset=True, marker=False, hop=False, dual=True)
time.sleep(0.02)
ctrl = int(scan._read(ADDR_STREAM_CONTROL))
dual_bit = bool(ctrl & 0x10)
check("STREAM_CONTROL bit4 (dual) reads back True", dual_bit, f"ctrl=0x{ctrl:02x}")
# park it again before the real run
scan._stream_ctrl_write(enable=False, hop=False, dual=False)
scan._write_control_bit(2, 1); time.sleep(0.05)

if not dual_bit:
    print("\n  >>> dual bit read-back is False: the loaded bitstream does NOT have "
          "dual-quantity mode. Rebuild scan_new.v and reload the .bin, then re-run.")
    sys.exit(2)

# ===========================================================================
# PART 2: stream dual triplets continuously, drain + reconstruct 4 traces
# ===========================================================================
print("\n=== PART 2: dual streaming + 4-trace reconstruction ===")
rx = scan.continuous_hop_start(nslots=N, input_source='dual',
                               dwell_time=1e-3, settling_time=1e-4,
                               trigger_length=5e-5)
check("continuous mode active (loop running)", bool(scan.continuous))

all_words = []
t0 = time.time()
while time.time() - t0 < 2.5:
    w = scan.hop_stream_read()  # float64, NaN = transport loss
    if w is not None and len(w):
        all_words.append(np.asarray(w, dtype=np.float64))
    time.sleep(0.05)
# stop_server=True so the single-client ARM push server is restarted fresh for the
# legacy-path check in PART 3 (one client per server instance).
scan.continuous_hop_stop(stop_server=True)

words = np.concatenate(all_words) if all_words else np.array([], dtype=np.float64)
print(f"  drained {words.size} words (~{words.size/3:.0f} triplets) over 2.5 s")
check("streamed many words (>> 0)", words.size > 3 * 1000, f"got {words.size}")

# raw alignment / step-label sanity on the contiguous word stream
n3 = (words.size // 3) * 3
trip = words[:n3].reshape(-1, 3)
step_raw = trip[:, 2]
finite_steps = step_raw[np.isfinite(step_raw)]
in_range = np.all((finite_steps >= 0) & (finite_steps < N) &
                  (np.rint(finite_steps) == finite_steps))
check("all inline step labels are valid ints in [0, N-1] (no misalignment)",
      bool(in_range),
      f"unique={sorted(set(int(s) for s in finite_steps[:5000]))}")
loss_frac = np.mean(np.isnan(words)) if words.size else 1.0
check("transport loss fraction is small (< 1%)", loss_frac < 0.01,
      f"loss={loss_frac*100:.3f}%")

# reconstruct the 4 traces
rec = Scan.reconstruct_dual_hop_series(words, nslots=N, to_hz_corr=True)
err, corr = rec['err'], rec['corr']
check("reconstructed shapes (N, T) match", err.shape == corr.shape and err.shape[0] == N,
      f"err{err.shape} corr{corr.shape}")
for r in range(N):
    e_fin = np.isfinite(err[r]).sum()
    c_fin = np.isfinite(corr[r]).sum()
    check(f"res{r}: error trace populated", e_fin > 100, f"finite={e_fin}/{err.shape[1]}")
    check(f"res{r}: correction trace populated", c_fin > 100, f"finite={c_fin}/{corr.shape[1]}")
    # each resonance must actually be live for a meaningful fraction (fresh dwell)
    print(f"    res{r}: err mean={np.nanmean(err[r]):.1f} LSB, "
          f"corr mean={np.nanmean(corr[r]):.1f} Hz")

# both quantities present at the SAME sample times (the whole point) for some index
both = np.isfinite(err[0]) & np.isfinite(corr[0])
check("err AND corr available simultaneously (res0)", bool(np.any(both)),
      f"co-finite samples={int(both.sum())}")

# ===========================================================================
# PART 3: legacy single-word path still works (regression)
# ===========================================================================
# Use 'demod' (keyed off the always-on demod strobe) rather than 'ftw_corr'
# (which only strobes when the ODMR oscillator is producing corrections, not set
# up in this bare bench test) so the regression check is self-contained.
print("\n=== PART 3: legacy single-word stream unregressed ===")
scan._write_control_bit(2, 1); time.sleep(0.05)
rx = scan.continuous_hop_start(nslots=N, input_source='demod',
                               dwell_time=1e-3, settling_time=1e-4,
                               trigger_length=5e-5)
time.sleep(0.5)
legacy = []
t0 = time.time()
while time.time() - t0 < 1.0:
    w = scan.hop_stream_read()
    if w is not None and len(w):
        legacy.append(np.asarray(w, dtype=np.float64))
    tk, st = scan.read_hop_markers()
    time.sleep(0.05)
scan.continuous_hop_stop(stop_server=True)
lw = np.concatenate(legacy) if legacy else np.array([])
check("legacy single-word (demod) stream still produces samples", lw.size > 1000,
      f"got {lw.size}")

npass = sum(1 for _, ok in results if ok)
print(f"\n=== {npass}/{len(results)} checks passed ===")
sys.exit(0 if npass == len(results) else 1)
