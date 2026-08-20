"""Bench validation of the FPGA-side sequence the qudi multi-resonance HW module
(RedPitayaOdmrLockHardware multi-res surface) drives, exercised directly via pyrpl
(no qudi, no RF, no Windfreak).

Mirrors RedPitayaOdmrLockHardware.{configure_oscillator, set_integrator_source,
set_bandwidth, start_tracking(stream_traces=True), get_all_status, read_traces,
stop_tracking}. The hop markers + ftw_corr stream come from the scan FSM advancing
current_step while ftw_correction_valid strobes at ~30.5 kHz, independent of RF, so
this validates the full configure -> continuous-hop -> stream -> reconstruct path
on the board. Requires the continuous-mode bitstream (test_continuous_hop.py green).

Run:  python test_multitrack_qudi_path.py [--reload-fpga]
"""
import sys
import time
import numpy as np

HOST = "10.203.129.28"
N = 2

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")

import pyrpl
reload_fpga = "--reload-fpga" in sys.argv
print(f"Connecting to {HOST} (reload_fpga={reload_fpga}) ...")
p = pyrpl.Pyrpl(hostname=HOST, config="", reload_fpga=reload_fpga, reload_server=True, gui=False)
rp = p.rp

mt = getattr(rp, 'odmrmultitrack', None)
lock = rp.odmrfreqlock
fgen3 = rp.fgen3
scan = rp.scan
check("rp.odmrmultitrack present (Phase C bitstream)", mt is not None)
if mt is None:
    print("  >>> old bitstream; cannot test multi-resonance path."); sys.exit(2)

# clean slate
scan._stream_ctrl_write(enable=False, hop=False)
scan._write_control_bit(2, 1); time.sleep(0.05)
lock.enable = False
mt.enable = False

# ---- 1. configure_oscillator + sources (mirror HW module) ----
print("\n=== 1. configure oscillator + per-slot/cal sources ===")
mt.frequency = 15258.789
mt.demod_phase = 123.0
mt.settle_time = 200e-6
mt.src = 'current_step'
lock.active_slot_src = True       # integrators follow current_step
fgen3.active_slot_src = True      # cal slot follows current_step
check("multitrack f_m round-trip", abs(mt.frequency - 15258.789) < 1.0, f"{mt.frequency:.3f}")
check("multitrack src=current_step", mt.src == 'current_step', f"{mt.src}")
check("lock.active_slot_src True", bool(lock.active_slot_src))
check("fgen3.active_slot_src True", bool(fgen3.active_slot_src))

# ---- 2. set_bandwidth + max correction (global) ----
print("\n=== 2. loop gains ===")
lock.set_bandwidth(300, 1.1)
lock.max_correction_hz = 1.0e6
check("nslots >= 2", lock.nslots >= N, f"nslots={lock.nslots}")

# ---- 3. start_tracking(stream_traces=True): enable osc + lock + continuous hop ----
print("\n=== 3. start tracking (osc + lock + continuous hop + stream) ===")
mt.enable = True
lock.clear()
lock.enable = True
rx = scan.continuous_hop_start(nslots=N, input_source='ftw_corr',
                               dwell_time=1.0e-3, settling_time=100e-6,
                               trigger_length=50e-6)
check("scan in continuous mode", bool(scan.continuous), f"continuous={scan.continuous}")
check("scan busy (loop running)", bool(scan.busy))

# ---- 4. poll per-slot status + accumulate stream/markers (mirror read_traces) ----
print("\n=== 4. poll per-slot status + reconstruct traces ===")
values = np.array([], dtype=np.float64)
ticks = np.array([], dtype=np.int64)
steps = np.array([], dtype=np.int64)
last_status = None
t0 = time.time()
while time.time() - t0 < 1.5:
    last_status = lock.get_status_all()
    v = scan.hop_stream_read()
    if v is not None and len(v):
        values = np.concatenate([values, np.asarray(v, dtype=np.float64)])
    tk, st = scan.read_hop_markers()
    if tk is not None and len(tk):
        ticks = np.concatenate([ticks, np.asarray(tk, dtype=np.int64)])
        steps = np.concatenate([steps, np.asarray(st, dtype=np.int64)])
    time.sleep(0.1)

check("per-slot status list length == N", last_status is not None and len(last_status) == N,
      f"got {None if last_status is None else len(last_status)}")
if last_status is not None:
    for s in range(N):
        _ = lock.correction_hz_slot(s)  # per-slot correction readable
    check("per-slot correction_hz readable", True)

check("stream produced samples", values.size > 0, f"{values.size} samples")
check("hop markers captured across wraps", ticks.size > 2 * N, f"{ticks.size} markers")
if values.size and ticks.size:
    traces = scan.reconstruct_hop_series(values, ticks, steps, nslots=N, to_hz=True)
    check("reconstruct shape (N, T)", traces.shape == (N, values.size), f"{traces.shape}")
    finite_frac = np.isfinite(traces).mean()
    check("traces mostly finite (>50%)", finite_frac > 0.5, f"finite={finite_frac:.2f}")
    print(f"  trace0 last finite ~ {np.nanmean(traces[0][-200:]):.1f} Hz, "
          f"trace1 last finite ~ {np.nanmean(traces[1][-200:]):.1f} Hz")

# ---- 5. stop_tracking + cleanup ----
print("\n=== 5. stop + cleanup ===")
scan.continuous_hop_stop()
lock.enable = False
mt.enable = False
scan._write_control_bit(2, 1); time.sleep(0.05)
check("scan stopped (not busy)", not scan.busy)
check("scan continuous cleared", not scan.continuous)

npass = sum(1 for _, ok in results if ok)
print(f"\n=== {npass}/{len(results)} checks passed ===")
sys.exit(0 if npass == len(results) else 1)
