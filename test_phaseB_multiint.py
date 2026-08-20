"""Phase B bench test: N=2 multi-integrator in odmr_freq_lock (region 8).

Connects to the real Red Pitaya (FPGA already loaded with the new Phase-B
bitstream), then checks two things:

REGISTER LEVEL (deterministic, no RF needed):
  1. NSLOTS readback (0x0028) == 2  -> confirms the new bitstream is live
  2. slot-control register (active_slot / active_slot_src) round-trips
  3. clear() / disable zero ALL slots
  4. slot-0 legacy aliases (0x10/0x14/0x18/0x20) agree with the per-slot bank
     slot-0 readback (0x40..0x4C)

DYNAMIC (best-effort — needs a live lock-in error on ADC A):
  5. With the loop enabled and software slot select:
       - only the ACTIVE slot's integrator moves; the inactive one stays ~0
       - switching the active slot HOLDS the previously-active integrator
         (bumpless) while the newly-active one starts to move
  This section is skipped (not failed) if no demod error is present (err==0),
  since with no signal nothing integrates.

Build/install first (J.W.'s flow): pure-RTL change, no IP regen.
  cd pyrpl/fpga && copy_and_make.bat   -> build .bin, then copy into the live
  pyrpl fpga dir, then run this with reload_fpga=False.
"""
import sys
import time
import pyrpl

HOST = "10.203.129.28"


def approx(a, b, tol=1e-3):
    return abs(a - b) <= tol


print(f"Connecting to {HOST} (FPGA already loaded with Phase-B bitstream) ...")
p = pyrpl.Pyrpl(hostname=HOST, config="", reload_fpga=False, reload_server=True, gui=False)
rp = p.rp
odm = rp.odmrfreqlock
print("Connected. odmr_freq_lock:", odm)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")


# ---------------------------------------------------------------------------
# 1. New bitstream live?
# ---------------------------------------------------------------------------
nslots = odm.nslots
check("NSLOTS readback == 2 (new bitstream)", nslots == 2, f"got {nslots}")

# ---------------------------------------------------------------------------
# 2. Slot-control register round-trip
# ---------------------------------------------------------------------------
odm.active_slot_src = False
odm.active_slot = False
check("active_slot reads 0 after clear", odm.active_slot == 0 or odm.active_slot is False)
odm.active_slot = True
check("active_slot reads 1 after set", bool(odm.active_slot) is True, f"got {odm.active_slot}")
odm.active_slot_src = True
check("active_slot_src reads True after set", bool(odm.active_slot_src) is True,
      f"got {odm.active_slot_src}")
# leave deterministic: software-driven, slot 0
odm.active_slot_src = False
odm.active_slot = False

# ---------------------------------------------------------------------------
# 3. clear() / disable zero all slots
# ---------------------------------------------------------------------------
odm.enable = False
odm.clear()
time.sleep(0.02)
all_zero = all(approx(odm.correction_hz_slot(s), 0.0, 1.0) and
               approx(odm.integrator_hz_slot(s), 0.0, 1.0) for s in range(nslots))
check("all slots zeroed after disable+clear", all_zero,
      detail="; ".join(f"s{s}={odm.correction_hz_slot(s):.1f}Hz" for s in range(nslots)))

# ---------------------------------------------------------------------------
# 4. Slot-0 legacy aliases agree with the per-slot bank slot-0 readback
# ---------------------------------------------------------------------------
legacy_int = odm.integrator_ftw                # 0x18
legacy_out = odm.correction_ftw                # 0x20
legacy_err = odm.error_lsb                      # 0x14
legacy_lock = odm.locked                        # 0x10 bit0
bank_int = odm.integrator_ftw_slot(0)          # 0x48
bank_out = odm.correction_ftw_slot(0)          # 0x4C
bank_err = odm.error_lsb_slot(0)               # 0x44
bank_lock = odm.locked_slot(0)                  # 0x40 bit0
check("slot-0 integrator alias == bank", legacy_int == bank_int, f"{legacy_int} vs {bank_int}")
check("slot-0 output alias == bank", legacy_out == bank_out, f"{legacy_out} vs {bank_out}")
check("slot-0 error alias == bank", legacy_err == bank_err, f"{legacy_err} vs {bank_err}")
check("slot-0 locked alias == bank", legacy_lock == bank_lock, f"{legacy_lock} vs {bank_lock}")

# ---------------------------------------------------------------------------
# 5. Dynamic: per-slot integrate / hold (needs a live lock-in error)
# ---------------------------------------------------------------------------
print("\n--- dynamic per-slot integrate/hold (needs ADC A demod error) ---")
odm.enable = False
odm.clear()
odm.active_slot_src = False
odm.deadband_enable = False
odm.invert = False
odm.prop_enable = False           # integral-only, so the integrator state == output
odm.max_correction_hz = 1e6
odm.set_bandwidth(2000.0)         # large mu so even a small error ramps fast

# Activate slot 0 and let it integrate
odm.active_slot = False           # slot 0
odm.enable = True
time.sleep(0.10)
err0 = odm.error_lsb_slot(0)
c0_active = odm.correction_hz_slot(0)
c1_idle = odm.correction_hz_slot(1)

if abs(c0_active) < 1.0 and err0 == 0:
    print(f"  [SKIP] no demod error present (err=0); cannot exercise integration."
          f"  Connect a signal to ADC A and re-run the dynamic part.")
else:
    print(f"  signal present: err(slot0)={err0} LSB, slot0 corr={c0_active:.1f} Hz")
    # While slot 0 was active, slot 1 must NOT have moved
    check("slot 1 idle while slot 0 active (~0)", abs(c1_idle) < max(50.0, 1e-3*abs(c0_active)),
          f"slot1={c1_idle:.1f} Hz")
    check("slot 0 integrated while active (moved off 0)", abs(c0_active) > 1.0,
          f"slot0={c0_active:.1f} Hz")

    # Switch active slot to 1: slot 0 must HOLD (bumpless); slot 1 must start moving
    odm.active_slot = True        # slot 1
    time.sleep(0.001)
    c0_just_after = odm.correction_hz_slot(0)
    time.sleep(0.10)
    c0_held = odm.correction_hz_slot(0)
    c1_active = odm.correction_hz_slot(1)

    hold_tol = max(50.0, 0.02 * abs(c0_active))   # 2% or 50 Hz
    check("slot 0 HELD after switching away (bumpless)",
          approx(c0_held, c0_active, hold_tol),
          f"was {c0_active:.1f} -> now {c0_held:.1f} Hz (tol {hold_tol:.1f})")
    check("slot 1 integrated while active (moved off 0)", abs(c1_active) > abs(c1_idle) + 1.0,
          f"slot1 {c1_idle:.1f} -> {c1_active:.1f} Hz")
    print(f"  (immediately after switch slot0={c0_just_after:.1f} Hz)")

# Restore a safe, deterministic state
odm.enable = False
odm.clear()
odm.active_slot_src = False
odm.active_slot = False

npass = sum(1 for _, ok in results if ok)
print(f"\n=== {npass}/{len(results)} checks passed ===")
sys.exit(0 if npass == len(results) else 1)
