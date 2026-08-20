"""Diagnostic: does each RP scan trigger advance the Windfreak LO, and does c1 wrap?
Manual single-step scans (1 trigger each), scope the LO after each."""
import time, numpy as np
import pyrpl, pyvisa
from RsInstrument import RsInstrument

import sys
HOST="10.203.129.28"; IF_HZ=21.58e6; LO_PWR=13.0
# config from argv: list points (comma GHz) and t (ms)
LO_LIST=[float(x)*1e9 for x in (sys.argv[1].split(',') if len(sys.argv)>1 else ['2.77','2.87','2.97'])]
T_MS=float(sys.argv[2]) if len(sys.argv)>2 else 4.0
NTRIG=int(sys.argv[3]) if len(sys.argv)>3 else 9
print(f"LIST={[l/1e9 for l in LO_LIST]} GHz  t={T_MS}ms  ntrig={NTRIG}")

p=pyrpl.Pyrpl(hostname=HOST,config="",reload_fpga=False,reload_server=True,gui=False)
rp=p.rp; fg=rp.fgen3
# fgen3 single tone so we get a strong USB peak that tracks LO+IF
fg.output_to_dsp_enable_o=True; rp.asg0.output_direct="out1"; rp.asg1.output_direct="out2"
fg.frequency0=IF_HZ; fg.enable0=True; fg.enable1=False; fg.enable2=False
fg.active_slot_src=False; fg.active_slot=False
fg.load_cal_slot(0,[(0.13,0.13,110.0)],0.0,0.0)
fg.gen_enable=True; fg.output_zero=False
rp.hk.configure_pin('P7',direction='output',source='module',invert=True)

# Windfreak clean reprogram
rm=pyvisa.ResourceManager()
wf=rm.open_resource("ASRL3::INSTR",baud_rate=9600,read_termination='\n',write_termination='\n'); wf.timeout=2000
def wfw(c): wf.write(c); time.sleep(0.08)
wfw('g0'); wfw('Z0'); wfw(f'W{LO_PWR:2.3f}'); wfw(f't{T_MS}')
wfw('Ld'); time.sleep(0.2)
for i,f in enumerate(LO_LIST):
    wfw(f'L{i}f{f/1e6:.6f}'); wfw(f'L{i}a{LO_PWR:2.3f}')
wfw('X1'); wfw('y2'); wfw('Y0'); wfw('^1'); wfw('c1'); time.sleep(0.1)
wfw('g1g0'); time.sleep(0.5)

sc=RsInstrument('TCPIP0::10.203.129.15::inst0::INSTR',id_query=True,reset=False,options="SelectVisa='rs'")
sc.write_str_with_opc('CALC:MATH1 "FFTmag(Ch1)"'); sc.write_str_with_opc('CALC:MATH1:STATE ON')
sc.write_str_with_opc('CALC:MATH1:FFT:CFR 2870000000'); sc.write_str_with_opc('CALC:MATH1:FFT:SPAN 400000000')
def peak():
    sc.write_str_with_opc('SING')
    a=np.array(sc.query_bin_or_ascii_float_list('FORM REAL,32;CALC:MATH1:DATA?'))
    h=sc.query_bin_or_ascii_float_list('CALC:MATH1:DATA:HEAD?')
    f=np.linspace(h[0],h[1],int(h[2])); return f[int(np.argmax(a))]

rp.scan.num_steps=1; rp.scan.dwell_time=0.02; rp.scan.settling_time=0.005; rp.scan.trigger_length=0.005
print("armed (point0). peak now:", round(peak()/1e9,4),"GHz -> LO~", round((peak()-IF_HZ)/1e9,3))
for i in range(NTRIG):
    rp.scan.start(); rp.scan.wait_done(timeout=3); time.sleep(0.3)
    pk=peak(); lo=pk-IF_HZ
    print(f"trigger {i+1}: peak={pk/1e9:.4f} GHz -> LO~{lo/1e9:.3f} GHz")
print("\nWrap works if the LO cycles through all list points; clamp if it sticks at the last.")
