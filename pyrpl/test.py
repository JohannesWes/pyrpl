
import pyrpl

#define hostname
HOSTNAME = "10.203.129.28"

p = pyrpl.Pyrpl(config="",  # do not use a config file
                hostname=HOSTNAME)
rp = p.redpitaya  # shortcut for the the redpitaya handler

iq = rp.iq0

# modulation/demodulation frequency 25 MHz
# two lowpass filters with 10 and 20 kHz bandwidth
# input signal is analog input 1
# input AC-coupled with cutoff frequency near 50 kHz
# modulation amplitude 0.1 V
# modulation goes to out1
# output_signal is the demodulated quadrature 1
# quadrature_1 is amplified by 10
iq.setup(frequency=25e3, bandwidth=[10e3,20e3], gain=0.0,
         phase=0, acbandwidth=50000, amplitude=0.5,
         input='in1', output_direct='off',
         output_signal='quadrature', quadrature_factor=10)
iq = p.rp.iq2
print('Retrieved iq module "%s"' % iq.name)

# setup the iq module iq2 so that both demodulation quadratures are visible on the scope
iq.setup(input='in1',
         amplitude=0.5,
         output_direct='off',
         output_signal='quadrature',
         gain=0.0,
         quadrature_factor=10,
         frequency=5e3, # set the frequency to half the demodulation
         phase=0, #tune the phase as necessary
         modulation_at_2f='off',
         demodulation_at_2f='off',
         acbandwidth=5000)

scope = p.rp.scope
scope.setup(input1='in1', input2='in2',
            duration=0.002,
            average=True,
            trigger_source='ch2_positive_edge',
            rolling_mode=True)

# now you can view the measurement on the scope

asg0 = p.rp.asg0

asg0.amplitude = 0.2