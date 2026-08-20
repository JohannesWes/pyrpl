"""DEFINITIVE Phase A test: cal-correct LO hopping with HW cal-slot switching.
Proper config timing (trigger 50us, settling 100us). LO hops 2.77<->2.97 (2-pt wrap),
cal slot follows scan.current_step in HW (active_slot_src=1). We measure image rejection
+ LO leakage at each step, for BOTH slot->cal assignments, to resolve the (k+1) offset and
show the MATCHED assignment gives good suppression at every step (=> cal-correct hopping)."""
import time, numpy as np, pandas as pd
from scipy.interpolate import griddata
import pyrpl, pyvisa
from RsInstrument import RsInstrument

HOST="10.203.129.28"; IF_HZ=21.58e6; A_IF=0.13; LO_LIST=[2.77e9,2.97e9]; LO_PWR=13.0
NUM_STEPS=6
CSV=r"C:/calibration_results/2026-01-21-10-23-24/IF_21.580MHz/calibration_redpitaya_all_results_IF_21.580MHz.csv"
df=pd.read_csv(CSV,sep='\t',index_col=0); pts=df[['lo_frequency_ghz','if_amplitude']].values
def ip(c,lo,a):
    v=griddata(pts,df[c].values,(lo,a),'linear'); return float(v if not np.isnan(v) else griddata(pts,df[c].values,(lo,a),'nearest'))
def cal(lo):
    lg=lo/1e9; g=ip('g',lg,A_IF); phi=ip('phi',lg,A_IF)
    return (A_IF*(1+g), A_IF*(1-g), (270+np.degrees(phi))%360, ip('I_offset',lg,A_IF), ip('Q_offset',lg,A_IF))
CAL={2.77e9:cal(2.77e9), 2.97e9:cal(2.97e9)}

p=pyrpl.Pyrpl(hostname=HOST,config="",reload_fpga=False,reload_server=True,gui=False); rp=p.rp; fg=rp.fgen3
fg.output_to_dsp_enable_o=True; rp.asg0.output_direct="out1"; rp.asg1.output_direct="out2"
fg.frequency0=IF_HZ; fg.enable0=True; fg.enable1=False; fg.enable2=False
fg.gen_enable=True; fg.output_zero=False; fg.active_slot_src=True
rp.hk.configure_pin('P7',direction='output',source='module',invert=True)

rm=pyvisa.ResourceManager()
wf=rm.open_resource("ASRL3::INSTR",baud_rate=9600,read_termination='\n',write_termination='\n'); wf.timeout=2000
def wfw(c): wf.write(c); time.sleep(0.08)
def program_list():
    wfw('g0'); wfw('Z0'); wfw(f'W{LO_PWR:2.3f}'); wfw('t1.0'); wfw('Ld'); time.sleep(0.2)
    for i,f in enumerate(LO_LIST): wfw(f'L{i}f{f/1e6:.6f}'); wfw(f'L{i}a{LO_PWR:2.3f}')
    wfw('X1'); wfw('y2'); wfw('Y0'); wfw('^1'); wfw('c1'); time.sleep(0.1); wfw('g1g0'); time.sleep(0.4)
program_list()

sc=RsInstrument('TCPIP0::10.203.129.15::inst0::INSTR',id_query=True,reset=False,options="SelectVisa='rs'")
sc.write_str_with_opc('CALC:MATH1 "FFTmag(Ch1)"'); sc.write_str_with_opc('CALC:MATH1:STATE ON')
sc.write_str_with_opc('CALC:MATH1:FFT:CFR 2870000000'); sc.write_str_with_opc('CALC:MATH1:FFT:SPAN 400000000')
def meas():
    sc.write_str_with_opc('SING')
    a=np.array(sc.query_bin_or_ascii_float_list('FORM REAL,32;CALC:MATH1:DATA?'))
    h=sc.query_bin_or_ascii_float_list('CALC:MATH1:DATA:HEAD?'); f=np.linspace(h[0],h[1],int(h[2]))
    peak=f[int(np.argmax(a))]; lo=LO_LIST[int(np.argmin([abs((peak-IF_HZ)-L) for L in LO_LIST]))]
    at=lambda t:float(a[int(np.argmin(np.abs(f-t)))])
    return lo, at(lo+IF_HZ)-at(lo-IF_HZ), at(lo+IF_HZ)-at(lo)

rp.scan.num_steps=NUM_STEPS; rp.scan.dwell_time=1.0; rp.scan.settling_time=100e-6; rp.scan.trigger_length=50e-6

def run(slot0_cal_lo, slot1_cal_lo):
    fg.active_slot_src=False
    fg.load_cal_slot(0,[CAL[slot0_cal_lo][:3]],CAL[slot0_cal_lo][3],CAL[slot0_cal_lo][4])
    fg.load_cal_slot(1,[CAL[slot1_cal_lo][:3]],CAL[slot1_cal_lo][3],CAL[slot1_cal_lo][4])
    fg.active_slot_src=True
    program_list()                       # re-arm to point 0
    rp.scan.start()
    seen={}; t0=time.time()
    while time.time()-t0 < NUM_STEPS*2.0+5:
        cs=int(rp.scan.current_step)
        if cs not in seen:
            time.sleep(0.25)
            if int(rp.scan.current_step)!=cs: continue
            lo,imgrej,losupp=meas(); seen[cs]=(lo,imgrej,losupp)
        if len(seen)>=NUM_STEPS: break
        time.sleep(0.05)
    return seen

for label,(s0,s1) in [("A: slot0<-cal2.77, slot1<-cal2.97",(2.77e9,2.97e9)),
                      ("B: slot0<-cal2.97, slot1<-cal2.77",(2.97e9,2.77e9))]:
    seen=run(s0,s1)
    print(f"\n=== assignment {label} ===")
    rejs=[]
    for cs in sorted(seen):
        lo,ir,ls=seen[cs]; rejs.append(ir)
        print(f"  step {cs} slot{cs%2}: LO {lo/1e9:.3f}  img_rej {ir:5.1f} dB  LO_supp {ls:5.1f} dB")
    los=[seen[cs][0] for cs in sorted(seen)]
    alt=all(los[i]!=los[i+1] for i in range(len(los)-1))
    print(f"  LO alternates 1->2->1->2: {alt}  seq={[round(l/1e9,3) for l in los]}")
    print(f"  min img_rej over steps = {min(rejs):.1f} dB  ({'ALL GOOD -> cal matched' if min(rejs)>27 else 'some poor -> mismatched'})")
fg.active_slot_src=True
print("\n(Done. Windfreak hopping-armed; fgen3 enabled; active_slot_src=1.)")
