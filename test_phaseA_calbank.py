"""Phase A bench test: cal-slot bank register-level verification (no RF needed).

Connects to the real Red Pitaya, RELOADS the new FPGA bitstream, then checks:
  1. NSLOTS readback (0xFF1C) == 2  -> confirms the new bitstream is live
  2. slot 0 vs slot 1 cal registers are independent (write different, read back)
  3. active_slot / active_slot_src control register works
  4. load_cal_slot() helper round-trips
"""
import sys
import pyrpl

HOST = "10.203.129.28"

def approx(a, b, tol=1e-3):
    return abs(a - b) <= tol

print(f"Connecting to {HOST} (FPGA already loaded with new bitstream) ...")
p = pyrpl.Pyrpl(hostname=HOST, config="", reload_fpga=False, reload_server=True, gui=False)
rp = p.rp
fg = rp.fgen3
print("Connected. fgen3:", fg)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")

# 1. New bitstream live?
nslots = fg._NSLOTS_HW
check("NSLOTS readback == 2 (new bitstream)", nslots == 2, f"got {nslots}")
print("  HW params: NUM_COMPONENTS=%s GAINBITS=%s PHASEBITS=%s DACBITS=%s" % (
    fg._NUM_COMPONENTS_HW, fg._GAINBITS_HW, fg._PHASEBITS_HW, fg._DACBITS_HW))

# 2. Slot independence: amplitudes
fg.amplitude_a0 = 0.5            # slot 0, comp 0, I amp
fg.cal1_amplitude_a0 = 0.2       # slot 1, comp 0, I amp
ra0, ra0_s1 = fg.amplitude_a0, fg.cal1_amplitude_a0
check("slot0 amplitude_a0 round-trip ~0.5", approx(ra0, 0.5, 2e-3), f"got {ra0:.4f}")
check("slot1 cal1_amplitude_a0 round-trip ~0.2", approx(ra0_s1, 0.2, 2e-3), f"got {ra0_s1:.4f}")
check("slots are independent (0.5 != 0.2)", not approx(ra0, ra0_s1, 1e-2))

# phase_offset_b independence
fg.phase_offset_b0 = 270.0
fg.cal1_phase_offset_b0 = 90.0
pb0, pb0_s1 = fg.phase_offset_b0, fg.cal1_phase_offset_b0
check("slot0 phase_offset_b0 ~270", approx(pb0, 270.0, 0.1), f"got {pb0:.3f}")
check("slot1 cal1_phase_offset_b0 ~90", approx(pb0_s1, 90.0, 0.1), f"got {pb0_s1:.3f}")

# DC offset independence (signed)
fg.overall_dc_offset_a = 0.10
fg.cal1_dc_offset_a = -0.10
dca, dca_s1 = fg.overall_dc_offset_a, fg.cal1_dc_offset_a
check("slot0 dc_offset_a ~+0.10", approx(dca, 0.10, 2e-3), f"got {dca:.4f}")
check("slot1 cal1_dc_offset_a ~-0.10 (signed)", approx(dca_s1, -0.10, 2e-3), f"got {dca_s1:.4f}")

# 3. active_slot / active_slot_src
fg.active_slot_src = False
fg.active_slot = False
check("active_slot reads False", fg.active_slot is False or fg.active_slot == 0)
fg.active_slot = True
check("active_slot reads True after set", bool(fg.active_slot) is True, f"got {fg.active_slot}")
fg.active_slot_src = True
check("active_slot_src reads True after set", bool(fg.active_slot_src) is True, f"got {fg.active_slot_src}")
# leave deterministic: software-driven, slot 0
fg.active_slot_src = False
fg.active_slot = False

# 4. load_cal_slot round-trip
fg.load_cal_slot(0, [(0.30, 0.31, 270.0), (0.32, 0.33, 271.0), (0.34, 0.35, 272.0)], 0.01, -0.02)
fg.load_cal_slot(1, [(0.10, 0.11, 90.0),  (0.12, 0.13, 91.0),  (0.14, 0.15, 92.0)],  -0.01, 0.02)
check("load_cal_slot0 amp_b1 ~0.33", approx(fg.amplitude_b1, 0.33, 2e-3), f"got {fg.amplitude_b1:.4f}")
check("load_cal_slot0 phase_b2 ~272", approx(fg.phase_offset_b2, 272.0, 0.1), f"got {fg.phase_offset_b2:.3f}")
check("load_cal_slot1 amp_a2 ~0.14", approx(fg.cal1_amplitude_a2, 0.14, 2e-3), f"got {fg.cal1_amplitude_a2:.4f}")
check("load_cal_slot1 dc_b ~+0.02", approx(fg.cal1_dc_offset_b, 0.02, 2e-3), f"got {fg.cal1_dc_offset_b:.4f}")

npass = sum(1 for _, ok in results if ok)
print(f"\n=== {npass}/{len(results)} checks passed ===")
sys.exit(0 if npass == len(results) else 1)
