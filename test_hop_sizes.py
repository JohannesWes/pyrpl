"""Characterize Windfreak JUMP_LIST wrap vs clamp as a function of list SIZE,
using DISTINCT frequencies and the config timing (trigger 50us, settling 100us).
Answers: is it even/odd, or 2-specific, and does proper timing change it?"""
import sys, time, numpy as np
import pyrpl, pyvisa
from RsInstrument import RsInstrument

HOST="10.203.129.28"; IF_HZ=21.58e6; LO_PWR=13.0
T_MS=float(sys.argv[1]) if len(sys.argv)>1 else 1.0     # Windfreak step-time fallback
TRIG_S=50e-6; SETTLE_S=100e-6                            # from motor_scan_freq_tracking_hwsync.cfg
SIZES=[2,3,4,5]

p=pyrpl.Pyrpl(hostname=HOST,config="",reload_fpga=False,reload_server=True,gui=False); rp=p.rp; fg=rp.fgen3
fg.output_to_dsp_enable_o=True; rp.asg0.output_direct="out1"; rp.asg1.output_direct="out2"
fg.frequency0=IF_HZ; fg.enable0=True; fg.enable1=False; fg.enable2=False
fg.active_slot_src=False; fg.active_slot=False
fg.load_cal_slot(0,[(0.13,0.13,110.0)],0.0,0.0)
fg.gen_enable=True; fg.output_zero=False
rp.hk.configure_pin('P7',direction='output',source='module',invert=True)

rm=pyvisa.ResourceManager()
wf=rm.open_resource("ASRL3::INSTR",baud_rate=9600,read_termination='\n',write_termination='\n'); wf.timeout=2000
def wfw(c): wf.write(c); time.sleep(0.08)

sc=RsInstrument('TCPIP0::10.203.129.15::inst0::INSTR',id_query=True,reset=False,options="SelectVisa='rs'")
sc.write_str_with_opc('CALC:MATH1 "FFTmag(Ch1)"'); sc.write_str_with_opc('CALC:MATH1:STATE ON')
sc.write_str_with_opc('CALC:MATH1:FFT:CFR 2870000000'); sc.write_str_with_opc('CALC:MATH1:FFT:SPAN 400000000')
def lo_now():
    sc.write_str_with_opc('SING')
    a=np.array(sc.query_bin_or_ascii_float_list('FORM REAL,32;CALC:MATH1:DATA?'))
    h=sc.query_bin_or_ascii_float_list('CALC:MATH1:DATA:HEAD?'); f=np.linspace(h[0],h[1],int(h[2]))
    return f[int(np.argmax(a))]-IF_HZ

rp.scan.num_steps=1; rp.scan.dwell_time=0.001; rp.scan.settling_time=SETTLE_S; rp.scan.trigger_length=TRIG_S
print(f"timing: trigger={TRIG_S*1e6:.0f}us settling={SETTLE_S*1e6:.0f}us  windfreak t={T_MS}ms\n")

for N in SIZES:
    freqs=list(np.linspace(2.77e9, 2.97e9, N))
    wfw('g0'); wfw('Z0'); wfw(f'W{LO_PWR:2.3f}'); wfw(f't{T_MS}'); wfw('Ld'); time.sleep(0.2)
    for i,f in enumerate(freqs): wfw(f'L{i}f{f/1e6:.6f}'); wfw(f'L{i}a{LO_PWR:2.3f}')
    wfw('X1'); wfw('y2'); wfw('Y0'); wfw('^1'); wfw('c1'); time.sleep(0.1); wfw('g1g0'); time.sleep(0.4)
    ntrig=2*N+2
    seq=[]
    for i in range(ntrig):
        rp.scan.start(); rp.scan.wait_done(timeout=3); time.sleep(0.25)
        seq.append(round(lo_now()/1e9,3))
    distinct=len(set(seq))
    # clamp if the tail is stuck on one value; wrap if it keeps cycling through all points
    tail_stuck = len(set(seq[-N:]))==1 and N>1
    verdict = "CLAMP" if (distinct<N or tail_stuck) else "WRAP"
    print(f"N={N} list={[round(x/1e9,3) for x in freqs]}")
    print(f"   LO seq after triggers: {seq}")
    print(f"   distinct visited={distinct}/{N}  -> {verdict}\n")
