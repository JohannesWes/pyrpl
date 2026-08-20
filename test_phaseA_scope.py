"""Phase A end-to-end scope test: does active_slot route the selected slot's cal
into the fgen3 datapath?

Robust frequency-only test (scope amplitude is NOT absolute -- antenna-reflection tap):
  - fgen3 single IF tone at f_IF, SSB-upconverted by LO at f_LO.
  - slot 0: phase_offset_b = 270 deg  (one sideband, e.g. LO+IF)
  - slot 1: phase_offset_b =  90 deg  (the other sideband, LO-IF)
  - same amplitudes both slots, so ONLY the I/Q phase differs.
  Toggling active_slot must FLIP which sideband is dominant. We compare the FFT
  amplitude at the SAME two frequencies across the two slot settings (antenna factor
  cancels), so the wanted sideband is strong only in the slot that selects it.
"""
import sys, time
import numpy as np
import pyrpl
import pyvisa
from RsInstrument import RsInstrument

HOST   = "10.203.129.28"
F_IF   = 20e6
F_LO   = 2.87e9
LO_PWR = 13.0
F_USB  = F_LO + F_IF      # 2.890 GHz
F_LSB  = F_LO - F_IF      # 2.850 GHz

# ---------------- Red Pitaya: fgen3 single-tone SSB ----------------
print(f"Connecting to Red Pitaya {HOST} ...")
p = pyrpl.Pyrpl(hostname=HOST, config="", reload_fpga=False, reload_server=True, gui=False)
rp = p.rp
fg = rp.fgen3

# Output routing (fgen3 -> out1/out2), mirror RedPitayaIFSource.connect
fg.output_to_dsp_enable_o = True
rp.asg0.output_direct = "out1"
rp.asg1.output_direct = "out2"

# Single component only
fg.frequency0 = F_IF
fg.enable0 = True
fg.enable1 = False
fg.enable2 = False

# Two slots differ ONLY in phase_offset_b (sideband select); equal amplitudes, no DC.
A = 0.3
fg.active_slot_src = False
fg.load_cal_slot(0, [(A, A, 270.0)], 0.0, 0.0)
fg.load_cal_slot(1, [(A, A,  90.0)], 0.0, 0.0)

fg.gen_enable = True
fg.output_zero = False
print("fgen3:", fg, "| f_IF =", F_IF/1e6, "MHz")

# ---------------- Windfreak: CW LO ----------------
print(f"Setting Windfreak CW LO = {F_LO/1e9:.4f} GHz @ {LO_PWR} dBm ...")
rm = pyvisa.ResourceManager()
wf = rm.open_resource("ASRL3::INSTR", baud_rate=9600,
                      read_termination='\n', write_termination='\n')
for cmd in ['X0', 'c1', 'y0', 'Z0',
            f'f{F_LO/1e6:5.7f}', f'l{F_LO/1e6:5.7f}', f'u{F_LO/1e6:5.7f}',
            f'W{LO_PWR}', 'E1h1', 'g1']:
    wf.write(cmd); time.sleep(0.05)
time.sleep(0.5)

# ---------------- Scope FFT ----------------
sc = RsInstrument('TCPIP0::10.203.129.15::inst0::INSTR', id_query=True, reset=False,
                  options="SelectVisa='rs'")
sc.write_str_with_opc('CALC:MATH1 "FFTmag(Ch1)"')
sc.write_str_with_opc('CALC:MATH1:STATE ON')
sc.write_str_with_opc(f'CALC:MATH1:FFT:CFR {F_LO:.0f}')
sc.write_str_with_opc('CALC:MATH1:FFT:SPAN 200000000')

def fft_measure():
    sc.write_str_with_opc('SING')
    a = np.array(sc.query_bin_or_ascii_float_list('FORM REAL,32;CALC:MATH1:DATA?'))
    h = sc.query_bin_or_ascii_float_list('CALC:MATH1:DATA:HEAD?')
    f = np.linspace(h[0], h[1], int(h[2]))
    return f, a

def amp_at(f, a, target):
    return a[int(np.argmin(np.abs(f - target)))]

res = {}
for slot in (0, 1):
    fg.active_slot = bool(slot)
    time.sleep(0.3)
    f, a = fft_measure()
    peak = f[int(np.argmax(a))]
    res[slot] = (peak, amp_at(f, a, F_USB), amp_at(f, a, F_LSB))
    print(f"slot {slot}: peak={peak/1e9:.4f} GHz | A(USB={F_USB/1e9:.3f})={res[slot][1]:.2f} "
          f"A(LSB={F_LSB/1e9:.3f})={res[slot][2]:.2f}")

# ---------------- Verdict (polarity-agnostic) ----------------
peak0, usb0, lsb0 = res[0]
peak1, usb1, lsb1 = res[1]
# Antenna factor cancels when comparing the SAME frequency across slots.
# A working slot mux makes each sideband strong in exactly one slot, and the two
# slots select OPPOSITE sidebands -> d_usb and d_lsb have opposite signs, both large.
d_usb = usb0 - usb1            # >0 means USB stronger in slot 0
d_lsb = lsb0 - lsb1            # >0 means LSB stronger in slot 0
opposite_sign = (d_usb * d_lsb) < 0
both_large = abs(d_usb) > 10 and abs(d_lsb) > 10   # >10 dB swing
peak_moved = abs(peak0 - peak1) > 0.5 * F_IF
peak_opposite = (peak0 > F_LO) != (peak1 > F_LO)

print("\n--- verdict ---")
print(f"  d(USB amp) slot0-slot1 = {d_usb:+.2f} dB ; d(LSB amp) slot0-slot1 = {d_lsb:+.2f} dB")
print(f"  sidebands swing in opposite directions: {opposite_sign}")
print(f"  both swings > 10 dB: {both_large}")
print(f"  dominant peak moved >IF: {peak_moved}  ({peak0/1e9:.4f} -> {peak1/1e9:.4f} GHz)")
print(f"  peaks on opposite sides of LO: {peak_opposite}")
ok = opposite_sign and both_large and peak_moved and peak_opposite
print(f"\n=== Phase A slot mux datapath: {'CONFIRMED' if ok else 'INCONCLUSIVE -- inspect numbers'} ===")
print("(Windfreak left ON in CW at %.4f GHz / %g dBm.)" % (F_LO/1e9, LO_PWR))
