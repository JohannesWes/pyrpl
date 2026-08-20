"""Phase A hop test: FPGA-triggered LO hopping with HARDWARE cal-slot switching.

  - fgen3 single IF tone (21.580 MHz), real per-LO SSB cal loaded into slots 0/1.
  - Windfreak JUMP_LIST hops LO between 2.77 and 2.97 GHz, advanced by the RP scan
    trigger (DIO7_P). active_slot_src=1 -> cal slot follows scan.current_step in HW.
  - scan steps 0..N-1 (num_steps>2) exercise 1->2->1->2 back-and-forth (list wraps, c1).
  - For each step we read current_step and FFT the scope: measure wanted sideband,
    image (LO-IF), and LO leakage (LO) -> image rejection + LO suppression per step.

Pass the new (current_step-routed) bitstream by reloading the FPGA on connect.
"""
import sys, time
import numpy as np
import pandas as pd
from scipy.interpolate import griddata
import pyrpl, pyvisa
from RsInstrument import RsInstrument

HOST   = "10.203.129.28"
IF_HZ  = 21.580e6
A_IF   = 0.13                      # IF amplitude (within calibrated 0.0005..0.3)
LO_LIST = [2.77e9, 2.97e9]         # JUMP_LIST point 0, point 1
LO_PWR = 13.0
NUM_STEPS = 6                      # 0..5 -> LO alternates -> 1->2->1->2->1->2
CSV = r"C:/calibration_results/2026-01-21-10-23-24/IF_21.580MHz/calibration_redpitaya_all_results_IF_21.580MHz.csv"

# ---------- calibration: (g,phi,I_off,Q_off) -> fgen3 words (USB, q_base=270) ----------
df = pd.read_csv(CSV, sep='\t', index_col=0)
pts = df[['lo_frequency_ghz', 'if_amplitude']].values
def interp(col, lo_ghz, a):
    v = griddata(pts, df[col].values, (lo_ghz, a), method='linear')
    if np.isnan(v):
        v = griddata(pts, df[col].values, (lo_ghz, a), method='nearest')
    return float(v)
def cal_for_lo(lo_hz):
    lg = lo_hz / 1e9
    g   = interp('g', lg, A_IF); phi = interp('phi', lg, A_IF)
    iof = interp('I_offset', lg, A_IF); qof = interp('Q_offset', lg, A_IF)
    amp_a = A_IF * (1 + g); amp_b = A_IF * (1 - g)
    phase_b = (270.0 + np.degrees(phi)) % 360.0
    return dict(amp_a=amp_a, amp_b=amp_b, phase_b=phase_b, dc_a=iof, dc_b=qof,
                g=g, phi=phi)
cal0 = cal_for_lo(LO_LIST[0]); cal1 = cal_for_lo(LO_LIST[1])
print("cal LO0=%.3f GHz: g=%.4f phi=%.3f -> amp_a=%.4f amp_b=%.4f phase_b=%.2f dc=(%.4f,%.4f)"
      % (LO_LIST[0]/1e9, cal0['g'], cal0['phi'], cal0['amp_a'], cal0['amp_b'], cal0['phase_b'], cal0['dc_a'], cal0['dc_b']))
print("cal LO1=%.3f GHz: g=%.4f phi=%.3f -> amp_a=%.4f amp_b=%.4f phase_b=%.2f dc=(%.4f,%.4f)"
      % (LO_LIST[1]/1e9, cal1['g'], cal1['phi'], cal1['amp_a'], cal1['amp_b'], cal1['phase_b'], cal1['dc_a'], cal1['dc_b']))

# ---------- Red Pitaya: load new bitstream + configure ----------
print(f"\nConnecting to {HOST} with FPGA reload (new current_step-routed bitstream) ...")
p = pyrpl.Pyrpl(hostname=HOST, config="", reload_fpga=True, reload_server=True, gui=False)
rp = p.rp; fg = rp.fgen3
print("NSLOTS readback:", fg._NSLOTS_HW)

fg.output_to_dsp_enable_o = True
rp.asg0.output_direct = "out1"; rp.asg1.output_direct = "out2"
fg.frequency0 = IF_HZ; fg.enable0 = True; fg.enable1 = False; fg.enable2 = False

def load(slot, c):
    fg.load_cal_slot(slot, [(c['amp_a'], c['amp_b'], c['phase_b'])], c['dc_a'], c['dc_b'])

# Initial guess: slot 0 <- cal for LO point 0 ; slot 1 <- cal for LO point 1
SLOT_CAL = {0: cal0, 1: cal1}
load(0, SLOT_CAL[0]); load(1, SLOT_CAL[1])
fg.gen_enable = True; fg.output_zero = False
fg.active_slot_src = True          # HARDWARE slot select (follows scan.current_step)

# trigger pin: DIO7_P driven by scan module, inverted (idle-high, active-low -> Windfreak Y0)
rp.hk.configure_pin('P7', direction='output', source='module', invert=True)

# ---------- Windfreak JUMP_LIST ----------
rm = pyvisa.ResourceManager()
wf = rm.open_resource("ASRL3::INSTR", baud_rate=9600, read_termination='\n',
                      write_termination='\n'); wf.timeout = 2000
def wfw(c): wf.write(c); time.sleep(0.05)
wfw('g0'); wfw('Z0'); wfw(f'W{LO_PWR:2.3f}'); wfw('t4.0'); wfw('Ld')
for i, f in enumerate(LO_LIST):
    wfw(f'L{i}f{f/1e6:.6f}'); wfw(f'L{i}a{LO_PWR:2.3f}')
wfw('X1'); wfw('y2'); wfw('Y0'); wfw('^1'); wfw('c1'); wfw('g1g0')
time.sleep(0.5)

# ---------- Scope ----------
sc = RsInstrument('TCPIP0::10.203.129.15::inst0::INSTR', id_query=True, reset=False,
                  options="SelectVisa='rs'")
sc.write_str_with_opc('CALC:MATH1 "FFTmag(Ch1)"'); sc.write_str_with_opc('CALC:MATH1:STATE ON')
sc.write_str_with_opc('CALC:MATH1:FFT:CFR 2870000000')
sc.write_str_with_opc('CALC:MATH1:FFT:SPAN 400000000')
def fft():
    sc.write_str_with_opc('SING')
    a = np.array(sc.query_bin_or_ascii_float_list('FORM REAL,32;CALC:MATH1:DATA?'))
    h = sc.query_bin_or_ascii_float_list('CALC:MATH1:DATA:HEAD?')
    return np.linspace(h[0], h[1], int(h[2])), a
def amp_at(f, a, tgt): return float(a[int(np.argmin(np.abs(f - tgt)))])

# ---------- Run scan, measure per step ----------
rp.scan.num_steps = NUM_STEPS
rp.scan.settling_time = 0.1
rp.scan.dwell_time = 1.2
rp.scan.trigger_length = 0.005
print("\nStarting scan (num_steps=%d, dwell=1.2s) ...\n" % NUM_STEPS)
rp.scan.start()

seen = {}
t0 = time.time()
while time.time() - t0 < NUM_STEPS * 2.0 + 5:
    cs = int(rp.scan.current_step)
    if cs not in seen:
        time.sleep(0.25)                      # let the step settle
        if int(rp.scan.current_step) != cs:   # changed mid-read, retry
            continue
        f, a = fft()
        peak = f[int(np.argmax(a))]
        lo_guess = LO_LIST[int(np.argmin([abs((peak - IF_HZ) - L) for L in LO_LIST]))]
        wanted = amp_at(f, a, lo_guess + IF_HZ)
        image  = amp_at(f, a, lo_guess - IF_HZ)
        leak   = amp_at(f, a, lo_guess)
        seen[cs] = (lo_guess, peak, wanted, image, leak)
        print(f"step {cs} (slot {cs%2}): LO~{lo_guess/1e9:.3f} peak={peak/1e9:.4f} GHz | "
              f"wanted={wanted:.1f} image={image:.1f} leak={leak:.1f} dB | "
              f"img_rej={wanted-image:.1f} dB  LO_supp={wanted-leak:.1f} dB")
    if len(seen) >= NUM_STEPS:
        break
    time.sleep(0.05)

# ---------- Summary ----------
print("\n--- summary (slot -> LO mapping & suppression) ---")
for cs in sorted(seen):
    lo, peak, w, im, lk = seen[cs]
    print(f"  step {cs} slot {cs%2}: LO {lo/1e9:.3f} GHz  img_rej {w-im:5.1f} dB  LO_supp {w-lk:5.1f} dB")
los = [seen[cs][0] for cs in sorted(seen)]
alternates = all(los[i] != los[i+1] for i in range(len(los)-1))
print(f"\n  LO alternates step-to-step (1->2->1->2): {alternates}  seq={[round(l/1e9,3) for l in los]}")
print("(Windfreak left hopping-armed; RP scan finished. fgen3 enabled.)")
