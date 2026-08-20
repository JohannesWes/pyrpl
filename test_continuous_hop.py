"""Continuous-loop hopping bench test (Option 1: scan_new.v continuous/loop mode).

Validates the indefinite, hardware-driven LO-hop source that unblocks continuous
multi-resonance tracking: CONTROL bit3 (latched with START) puts the scan FSM in
continuous/loop mode so it wraps current_step 0..num_steps-1 FOREVER (one LO-hop
trigger per step) until stop()/reset(), instead of stopping at S_DONE after
num_steps. The push stream + hop markers run concurrently off current_step.

Requires the NEW bitstream (rebuilt scan_new.v). Pure RTL change, no IP regen:
  cd pyrpl/fpga && copy_and_make.bat ; copy the .bin into the live pyrpl fpga dir.
Then run this. NO RF and NO ARM push server needed for the core checks — the hop
markers are captured in FPGA BRAM purely from the FSM advancing current_step while
ftw_correction_valid strobes at ~30.5 kHz (regardless of lock).

If the loaded bitstream is OLD (no continuous bit), PART 1 reports it and the
script exits early with a clear message rather than producing misleading results.
"""
import sys
import time
import numpy as np

HOST = "10.203.129.28"

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")

import pyrpl
reload_fpga = "--reload-fpga" in sys.argv  # pass on the first run after a rebuild
print(f"Connecting to {HOST} (reload_fpga={reload_fpga}) ...")
p = pyrpl.Pyrpl(hostname=HOST, config="", reload_fpga=reload_fpga, reload_server=True, gui=False)
rp = p.rp
scan = rp.scan
print("Connected. scan:", scan)

# Clean slate
scan._stream_ctrl_write(enable=False, hop=False)
scan._write_control_bit(2, 1)  # reset
time.sleep(0.05)

# ===========================================================================
# PART 1: continuous mode latches + status read-back (new-bitstream probe)
# ===========================================================================
print("\n=== PART 1: continuous/loop control + status ===")
N = 2
scan.input_select = 'ftw_corr'
scan.num_steps = N
scan.dwell_time = 1e-3        # ~30 stream samples/resonance
scan.settling_time = 1e-4
scan.trigger_length = 5e-5

scan.start(continuous=True)
time.sleep(0.05)
cont = scan.continuous
busy = scan.busy
done = scan.done
check("STATUS bit2 (continuous) reads True after start(continuous=True)", cont,
      f"continuous={cont} busy={busy} done={done}")

if not cont:
    print("\n  >>> continuous read-back is False: the loaded bitstream does NOT have "
          "the continuous/loop mode. Rebuild scan_new.v and reload the .bin, then "
          "re-run. (On an old bitstream bit3 is ignored and this is a finite scan.)")
    scan.stop(); scan._write_control_bit(2, 1)
    sys.exit(2)

check("busy True in continuous mode", busy, f"busy={busy}")
check("done False in continuous mode", not done, f"done={done}")

# ===========================================================================
# PART 2: it really runs indefinitely (does NOT stop at S_DONE after N steps)
# ===========================================================================
print("\n=== PART 2: indefinite run (no S_DONE) ===")
# A finite scan of N=2 tiny steps would finish in a few ms. Watch for ~2 s: the
# FSM must stay busy and never assert done, and current_step must keep cycling.
seen_steps = set()
t0 = time.time()
ever_done = False
while time.time() - t0 < 2.0:
    seen_steps.add(int(scan.current_step))
    if scan.done:
        ever_done = True
    time.sleep(0.005)
still_busy = scan.busy
check("still busy after 2 s (did not stop at S_DONE)", still_busy,
      f"busy={still_busy}")
check("done never asserted over 2 s", not ever_done)
check("current_step cycled through all N resonances", seen_steps >= set(range(N)),
      f"seen={sorted(seen_steps)}")

# ===========================================================================
# PART 3: hop markers across many wraps (continuous + concurrent marker capture)
# ===========================================================================
print("\n=== PART 3: hop markers across multiple wraps ===")
# restart cleanly with hop-marker capture enabled (no ARM push server needed)
scan.stop(); scan._write_control_bit(2, 1); time.sleep(0.05)
scan.input_select = 'ftw_corr'
scan.num_steps = N
scan._stream_ctrl_write(enable=True, reset=True, hop=True, marker=False)
scan._hop_active = True
scan._hop_rd_ptr = 0
scan._hop_total_read = 0
scan.start(continuous=True)

# accumulate markers over ~1.5 s, draining periodically (indefinite-run pattern)
all_ticks, all_steps = [], []
t0 = time.time()
while time.time() - t0 < 1.5:
    tk, st = scan.read_hop_markers()
    if tk.size:
        all_ticks.append(tk); all_steps.append(st)
    time.sleep(0.05)
# stop the loop + marker stream
scan.stop()
scan._stream_ctrl_write(enable=False, hop=False)
scan._hop_active = False

ticks = np.concatenate(all_ticks) if all_ticks else np.array([], dtype=np.int64)
steps = np.concatenate(all_steps) if all_steps else np.array([], dtype=np.int64)
print(f"  captured {ticks.size} hop markers over 1.5 s")
check("captured many markers (>> N => multiple wraps)", ticks.size > 3 * N,
      f"got {ticks.size}")
if ticks.size:
    check("all steps in [0, N-1]", bool(np.all((steps >= 0) & (steps < N))),
          f"unique={sorted(set(int(s) for s in steps))}")
    check("ticks non-decreasing (single tick base across wraps)",
          bool(np.all(np.diff(ticks) >= 0)))
    check("steps cover all N resonances", set(int(s) for s in steps) >= set(range(N)))
    # consecutive markers should advance the step by +1 mod N (the wrap N-1 -> 0
    # is a legitimate hop, recorded like any other)
    dsteps = (np.diff(steps) % N)
    check("each hop advances current_step by 1 (mod N), incl. the wrap",
          bool(np.all(dsteps == 1)), f"diffs(mod N)={dsteps.tolist()[:12]}...")

# ===========================================================================
# PART 4: stop() leaves continuous mode cleanly
# ===========================================================================
print("\n=== PART 4: clean stop ===")
scan._write_control_bit(2, 1); time.sleep(0.05)
check("continuous clears after stop/reset", not scan.continuous)
check("busy clears after stop/reset", not scan.busy)

npass = sum(1 for _, ok in results if ok)
print(f"\n=== {npass}/{len(results)} checks passed ===")
sys.exit(0 if npass == len(results) else 1)
