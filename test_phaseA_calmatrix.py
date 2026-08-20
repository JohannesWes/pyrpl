"""Phase A cal-correctness 2x2 matrix: each LO x each cal slot.
Confirms the MATCHED cal (slot for that LO) gives good image rejection + LO leakage
suppression, and the MISMATCHED cal is clearly worse -- at BOTH LO frequencies.
(Hardware slot-follows-current_step already proven in test_phaseA_hop.py; here we use
software active_slot to map out all four LOxcal combinations cleanly.)"""
import time, numpy as np, pandas as pd
from scipy.interpolate import griddata
import pyrpl, pyvisa
from RsInstrument import RsInstrument

HOST="10.203.129.28"; IF_HZ=21.58e6; A_IF=0.13; LO_LIST=[2.77e9,2.97e9]; LO_PWR=13.0
CSV=r"C:/calibration_results/2026-01-21-10-23-24/IF_21.580MHz/calibration_redpitaya_all_results_IF_21.580MHz.csv"
df=pd.read_csv(CSV,sep='\t',index_col=0); pts=df[['lo_frequency_ghz','if_amplitude']].values
def ip(col,lo,a):
    v=griddata(pts,df[col].values,(lo,a),method='linear')
    return float(v if not np.isnan(v) else griddata(pts,df[col].values,(lo,a),method='nearest'))
def cal(lo):
    lg=lo/1e9; g=ip('g',lg,A_IF); phi=ip('phi',lg,A_IF)
    return dict(amp_a=A_IF*(1+g),amp_b=A_IF*(1-g),phase_b=(270+np.degrees(phi))%360,
               dc_a=ip('I_offset',lg,A_IF),dc_b=ip('Q_offset',lg,A_IF))
cals={0:cal(LO_LIST[0]),1:cal(LO_LIST[1])}   # slot0<-2.77 cal, slot1<-2.97 cal

p=pyrpl.Pyrpl(hostname=HOST,config="",reload_fpga=False,reload_server=True,gui=False); rp=p.rp; fg=rp.fgen3
fg.output_to_dsp_enable_o=True; rp.asg0.output_direct="out1"; rp.asg1.output_direct="out2"
fg.frequency0=IF_HZ; fg.enable0=True; fg.enable1=False; fg.enable2=False
fg.active_slot_src=False                       # software slot control for this characterization
for s in (0,1):
    c=cals[s]; fg.load_cal_slot(s,[(c['amp_a'],c['amp_b'],c['phase_b'])],c['dc_a'],c['dc_b'])
fg.gen_enable=True; fg.output_zero=False
rp.hk.configure_pin('P7',direction='output',source='module',invert=True)

rm=pyvisa.ResourceManager()
wf=rm.open_resource("ASRL3::INSTR",baud_rate=9600,read_termination='\n',write_termination='\n'); wf.timeout=2000
def wfw(c): wf.write(c); time.sleep(0.08)
wfw('g0'); wfw('Z0'); wfw(f'W{LO_PWR:2.3f}'); wfw('t4.0'); wfw('Ld'); time.sleep(0.2)
for i,f in enumerate(LO_LIST): wfw(f'L{i}f{f/1e6:.6f}'); wfw(f'L{i}a{LO_PWR:2.3f}')
wfw('X1'); wfw('y2'); wfw('Y0'); wfw('^1'); wfw('c1'); time.sleep(0.1); wfw('g1g0'); time.sleep(0.5)

sc=RsInstrument('TCPIP0::10.203.129.15::inst0::INSTR',id_query=True,reset=False,options="SelectVisa='rs'")
sc.write_str_with_opc('CALC:MATH1 "FFTmag(Ch1)"'); sc.write_str_with_opc('CALC:MATH1:STATE ON')
sc.write_str_with_opc('CALC:MATH1:FFT:CFR 2870000000'); sc.write_str_with_opc('CALC:MATH1:FFT:SPAN 400000000')
def fft():
    sc.write_str_with_opc('SING')
    a=np.array(sc.query_bin_or_ascii_float_list('FORM REAL,32;CALC:MATH1:DATA?'))
    h=sc.query_bin_or_ascii_float_list('CALC:MATH1:DATA:HEAD?'); return np.linspace(h[0],h[1],int(h[2])),a
def at(f,a,t): return float(a[int(np.argmin(np.abs(f-t)))])
def measure():
    f,a=fft(); peak=f[int(np.argmax(a))]; lo=LO_LIST[int(np.argmin([abs((peak-IF_HZ)-L) for L in LO_LIST]))]
    w=at(f,a,lo+IF_HZ); im=at(f,a,lo-IF_HZ); lk=at(f,a,lo)
    return lo, w-im, w-lk

rp.scan.num_steps=1; rp.scan.dwell_time=0.02; rp.scan.settling_time=0.005; rp.scan.trigger_length=0.005
results={}
# Armed -> LO 2.77 ; one trigger -> LO 2.97 (clamps)
for label in ('armed(2.77)','trig->2.97'):
    if label=='trig->2.97':
        rp.scan.start(); rp.scan.wait_done(timeout=3); time.sleep(0.3)
    for slot in (0,1):
        fg.active_slot=bool(slot); time.sleep(0.2)
        lo,imgrej,losupp=measure()
        results[(round(lo/1e9,3),slot)]=(imgrej,losupp)
        print(f"{label}: LO~{lo/1e9:.3f} GHz, slot {slot} (cal {LO_LIST[slot]/1e9:.2f}) -> img_rej {imgrej:5.1f} dB  LO_supp {losupp:5.1f} dB")

print("\n--- 2x2 matrix (rows=LO, cols=slot/cal); diagonal=MATCHED ---")
print("            slot0(cal2.77)      slot1(cal2.97)")
for lo in (2.77,2.97):
    row=[]
    for slot in (0,1):
        k=(lo,slot); v=results.get(k)
        row.append(f"img{v[0]:.0f}/LO{v[1]:.0f}" if v else "   -    ")
    print(f"  LO {lo}:   "+"      ".join(f"{r:>16}" for r in row))
fg.active_slot=False
print("\nMATCHED cal should beat MISMATCHED at each LO (diagonal > off-diagonal).")
