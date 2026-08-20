"""Phase C register-level bench test (no RF needed).

Confirms the Phase C bitstream is live and the new control surface works:
  1. odmr_multitrack (region 9): NCH readback == 2 (new bitstream); frequency,
     demod_phase, settle_time, src, sw_channel round-trip; STATUS sel/in_settle/run.
  2. lock_in chain 1 (region 10): present and its control register round-trips.
  3. Oscillator transparent when disabled: with enable=False, run_mask = all-ones
     (both chains free-run) and osc_active is implied off -> legacy single-resonance
     path (iq0) is untouched.
  4. Software channel select drives sel + run_mask; a sw_channel change starts the
     settle window (best-effort, may be too fast to catch).

Dynamic freeze / 2-resonance tracking is bench-RF work (run after this passes):
  - osc disabled: confirm the existing single-resonance ODMR still locks (legacy).
  - osc enabled, src='sw': set frequency=f_m, demod_phase calibrated; confirm a
    single resonance still locks via the new oscillator (gain re-tune if needed).
  - osc enabled, src='current_step', scan hopping: 2-resonance freeze-and-resume.

Build first (IP already regen'd with aclken): cd pyrpl/fpga && copy_and_make.bat,
copy the .bin into the live pyrpl fpga dir.
"""
import sys
import time
import pyrpl

HOST = "10.203.129.28"

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")

def approx(a, b, tol):
    return abs(a - b) <= tol

print(f"Connecting to {HOST} ...")
p = pyrpl.Pyrpl(hostname=HOST, config="", reload_fpga=False, reload_server=True, gui=False)
rp = p.rp
mt = rp.odmrmultitrack
li1 = rp.lockin1
print("Connected. multitrack:", mt, " lockin1:", li1)

# 1. New bitstream live?
nch = mt.nch
check("multitrack NCH readback == 2 (new bitstream)", nch == 2, f"got {nch}")

# 2. Register round-trips
mt.enable = False
mt.frequency = 15.25e3
check("frequency ~15.25 kHz round-trip", approx(mt.frequency, 15.25e3, 5.0), f"got {mt.frequency:.2f}")
mt.demod_phase = 123.0
check("demod_phase ~123 deg round-trip", approx(mt.demod_phase, 123.0, 0.1), f"got {mt.demod_phase:.3f}")
mt.settle_time = 200e-6
check("settle_time ~200 us round-trip", approx(mt.settle_time, 200e-6, 1e-6), f"got {mt.settle_time*1e6:.1f} us")
mt.src = 'sw'
check("src='sw' round-trip", mt.src == 'sw', f"got {mt.src}")
mt.src = 'current_step'
check("src='current_step' round-trip", mt.src == 'current_step', f"got {mt.src}")

# 3. lock_in chain 1 present + control round-trip (region 10)
li1.ref_select1 = 'cos'
rt_cos = (li1.ref_select1 == 'cos')
li1.ref_select1 = 'sin'
rt_sin = (li1.ref_select1 == 'sin')
check("lockin1 reachable + ref_select1 round-trip (region 10)", rt_cos and rt_sin,
      f"now={li1.ref_select1}")

# 4. Transparent when disabled: both channels run (legacy free-run), not in settle
mt.enable = False
time.sleep(0.01)
rm_off = mt.run_mask
check("disabled -> run_mask all-ones (both chains free-run, legacy iq0 path)",
      rm_off == (1 << nch) - 1, f"run_mask=0b{rm_off:0{nch}b}")

# 5. Software channel select drives sel + run_mask when enabled
mt.src = 'sw'
mt.enable = True
mt.sw_channel = False
time.sleep(0.01)
sel0 = mt.selected_channel
rm0 = mt.run_mask
mt.sw_channel = True
time.sleep(0.01)
sel1 = mt.selected_channel
check("enabled+sw: sel follows sw_channel (0->1)", sel0 == 0 and sel1 == 1,
      f"sel0={sel0} sel1={sel1}")
# with a long settle_time the just-changed channel may still be in its hold-off;
# accept either the active bit set or in-settle (both are valid post-hop states).
rm1 = mt.run_mask
check("enabled: only one channel runs at a time (or in settle)",
      bin(rm0).count('1') <= 1 and bin(rm1).count('1') <= 1,
      f"rm0=0b{rm0:0{nch}b} rm1=0b{rm1:0{nch}b} in_settle={mt.in_settle}")

# restore safe/legacy state
mt.enable = False
mt.src = 'current_step'
mt.sw_channel = False

npass = sum(1 for _, ok in results if ok)
print(f"\n=== {npass}/{len(results)} checks passed ===")
sys.exit(0 if npass == len(results) else 1)
