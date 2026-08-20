"""Phase B-stream bench test: hop-boundary marker capture + reconstruct (Section 11).

Two parts:

PART 0 - OFFLINE (no hardware): unit-checks Scan.reconstruct_hop_series — the
  deterministic fresh-only + zero-order-hold reconstruction (per-resonance traces
  on the common tick axis; NaN reserved for transport loss only). Always runs.

PART 1/2 - HARDWARE (FPGA already loaded with the Phase B-stream bitstream):
  1. register level: STREAM_CONTROL[3] (hop enable) round-trips; HOP_WR_PTR/COUNT
     read 0 after a stream reset.
  2. dynamic capture: enable ftw_corr streaming + hop markers, run the scan FSM
     (num_steps=N) so current_step advances and drives hops, then read the captured
     (tick, step) markers and check they cover steps 0..N-1 with increasing ticks.
     This needs NO RF and NO ARM push server — the markers are captured in FPGA
     BRAM purely from the scan FSM advancing current_step while the stream's sample
     counter ticks (ftw_correction_valid strobes at ~30.5 kHz regardless of lock).

Build/install first (pure RTL, no IP regen): cd pyrpl/fpga && copy_and_make.bat,
copy the .bin into the live pyrpl fpga dir, then run this (reload_fpga handled below).
"""
import sys
import time
import numpy as np

HOST = "10.203.129.28"

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")

def arr_eq_nan(a, b):
    a = np.asarray(a, float); b = np.asarray(b, float)
    return a.shape == b.shape and np.all((a == b) | (np.isnan(a) & np.isnan(b)))

# ===========================================================================
# PART 0 - OFFLINE reconstruct unit checks
# ===========================================================================
print("=== PART 0: offline reconstruct_hop_series unit checks ===")
from pyrpl.hardware_modules.scan import Scan

# N=2, four segments [0,5)=res0, [5,10)=res1, [10,15)=res0, [15,20)=res1.
# Inject a transport-loss NaN at index 12 (inside res0's 2nd live dwell).
values = np.array([10,10,10,10,10, 20,20,20,20,20,
                   11,11,11,11,11, 21,21,21,21,21], dtype=float)
values[12] = np.nan
ticks = [0, 5, 10, 15]
steps = [0, 1, 0, 1]
rec = Scan.reconstruct_hop_series(values, ticks, steps, nslots=2)

exp0 = np.array([10,10,10,10,10, 10,10,10,10,10,
                 11,11,np.nan,11,11, 11,11,11,11,11], dtype=float)
exp1 = np.array([np.nan]*5 + [20,20,20,20,20] + [20,20,20,20,20] + [21,21,21,21,21], dtype=float)
check("reconstruct shape (2, 20)", rec.shape == (2, 20), f"got {rec.shape}")
check("res0: fresh + ZOH-hold, transport-loss NaN kept", arr_eq_nan(rec[0], exp0))
check("res1: NaN before first dwell, then fresh + ZOH-hold", arr_eq_nan(rec[1], exp1))
# transport loss stays NaN (not held); dead-time is held (not NaN)
check("transport-loss NaN preserved at idx 12", np.isnan(rec[0][12]))
check("dead-time is ZOH-held, not NaN (res0 @ idx 7)", rec[0][7] == 10)
check("pre-first-dwell is NaN (res1 @ idx 0)", np.isnan(rec[1][0]))

if "--offline" in sys.argv:
    npass = sum(1 for _, ok in results if ok)
    print(f"\n=== {npass}/{len(results)} checks passed (offline only) ===")
    sys.exit(0 if npass == len(results) else 1)

# ===========================================================================
# HARDWARE
# ===========================================================================
import pyrpl
print(f"\nConnecting to {HOST} ...")
p = pyrpl.Pyrpl(hostname=HOST, config="", reload_fpga=False, reload_server=True, gui=False)
rp = p.rp
scan = rp.scan
print("Connected. scan:", scan)

# ---- PART 1: register level ----
print("\n=== PART 1: hop-marker registers ===")
ADDR_STREAM_CONTROL = 0x20
ADDR_HOP_WR_PTR = 0x40
ADDR_HOP_COUNT = 0x44

# enable+reset+hop, then read back STREAM_CONTROL bit3
scan.input_select = 'ftw_corr'
scan._stream_ctrl_write(enable=True, reset=True, hop=True, marker=False)
ctrl = scan._read(ADDR_STREAM_CONTROL)
check("STREAM_CONTROL bit3 (hop) set", bool(ctrl & 0x8), f"ctrl=0x{ctrl:08x}")
check("STREAM_CONTROL bit2 (x/y) clear", not (ctrl & 0x4), f"ctrl=0x{ctrl:08x}")
# disable hop, confirm it clears
scan._stream_ctrl_write(enable=False, hop=False)
ctrl = scan._read(ADDR_STREAM_CONTROL)
check("STREAM_CONTROL bit3 clears", not (ctrl & 0x8), f"ctrl=0x{ctrl:08x}")
# after a reset with hop off, hop counters are 0
scan._stream_ctrl_write(enable=True, reset=True, hop=False)
hc = scan._read(ADDR_HOP_COUNT)
check("HOP_COUNT == 0 after reset", hc == 0, f"got {hc}")
scan._stream_ctrl_write(enable=False)

# ---- PART 2: dynamic hop-marker capture (scan FSM drives current_step) ----
print("\n=== PART 2: dynamic hop-marker capture ===")
N = 4
# configure the scan FSM (writes allowed while idle)
scan.input_select = 'ftw_corr'
scan.num_steps = N
scan.dwell_time = 2e-3       # ~61 stream samples/step
scan.settling_time = 1e-4
scan.trigger_length = 5e-5
# enable streaming + hop markers (no ARM push server needed; markers live in BRAM)
scan._stream_ctrl_write(enable=True, reset=True, hop=True, marker=False)
scan._hop_active = True
scan._hop_rd_ptr = 0
scan._hop_total_read = 0
# run the scan so current_step advances 0..N-1 (concurrent with streaming)
scan.start()
t0 = time.time()
while scan.busy and (time.time() - t0) < 3.0:
    time.sleep(0.01)
time.sleep(0.05)
ran = not scan.busy
check("scan FSM ran while streaming (concurrent scan+stream)", ran,
      f"busy={scan.busy}")

ticks, steps = scan.read_hop_markers()
print(f"  captured {ticks.size} hop markers: ticks={ticks.tolist()} steps={steps.tolist()}")
check("captured >= N hop markers", ticks.size >= N, f"got {ticks.size}")
if ticks.size:
    check("all steps in [0, N-1]", bool(np.all((steps >= 0) & (steps < N))),
          f"steps={steps.tolist()}")
    check("ticks non-decreasing", bool(np.all(np.diff(ticks) >= 0)),
          f"ticks={ticks.tolist()}")
    check("steps cover all N resonances", set(int(s) for s in steps) >= set(range(N)),
          f"unique={sorted(set(int(s) for s in steps))}")

# cleanup: stop streaming + reset scan
scan._stream_ctrl_write(enable=False, hop=False)
scan._hop_active = False
scan._write_control_bit(2, 1)  # reset

npass = sum(1 for _, ok in results if ok)
print(f"\n=== {npass}/{len(results)} checks passed ===")
sys.exit(0 if npass == len(results) else 1)
