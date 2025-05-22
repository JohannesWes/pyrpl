# pyrpl/hardware_modules/fgen3.py

"""
Module for controlling the 3-Component FM Sine Generator (Fgen3) FPGA module.

This module generates a signal composed of the sum of three independent,
potentially frequency-modulated sine waves. It uses memory-efficient
quarter-sine LUTs. Each component's contribution to DAC A and DAC B
can have independent phase and amplitude settings, allowing for precise
I/Q signal generation for applications like SSB mixing.

Key functionalities:
- Independent control of frequency for 3 components.
- Independent phase and amplitude for each component's contribution to DAC A.
- Independent phase and amplitude for each component's contribution to DAC B.
- Optional frequency modulation (FM) for each component using an external signal.
- Overall DC offset control for the two DAC outputs.
"""

import numpy as np
from collections import OrderedDict

# Import necessary base classes and attribute types from pyrpl core
from ..attributes import (BoolRegister, FloatRegister, IntRegister, PhaseRegister,
                          FrequencyRegister, SelectRegister, ConstantIntRegister) # Removed GainRegister as we use FloatRegister for amplitude
from ..modules import HardwareModule, SignalModule
from ..widgets.module_widgets.fgen3_widget import Fgen3Widget # Assuming this widget will be created/updated

# Define constants based on Verilog parameters
# These should match the Verilog module `red_pitaya_3fgen.v`
_PHASEBITS_PY = 32
_LUTSZ_PY = 11
_LUTBITS_PY = 14
_DACBITS_PY = 14
_GAINBITS_PY = 14
_FM_MOD_BITS_PY = 17
_NUM_COMPONENTS_PY = 3


class Fgen3(HardwareModule, SignalModule):
    """
    Python interface for the 3-Component FM Sine Generator FPGA module.
    """
    _widget_class = Fgen3Widget # TODO: Create/Update Fgen3Widget
    addr_base = 0x40600000

    # General Control Registers
    gen_enable = BoolRegister(0x0000, bit=0,
                              doc="Master enable for the generator. If False, phase accumulators are reset/held and AC sum is zero.")
    output_zero = BoolRegister(0x0000, bit=1,
                               doc="If True, forces AC component of output to zero, outputting only the DC offset. Overrides gen_enable for AC part.")

    output_to_dsp_enable_o = BoolRegister(0x0000, bit=2,
                               doc="If True, routes output of fgen3 to DSP, otherwise routes ASG output to DSP")

    # Overall Output Settings
    overall_dc_offset_a = FloatRegister(0x0004, bits=_DACBITS_PY, norm=2**(_DACBITS_PY - 1), signed=True,
                                        min=-1.0, max=1.0,
                                        doc="DC offset added to the final DAC A output [V].")
    overall_dc_offset_b = FloatRegister(0x0008, bits=_DACBITS_PY, norm=2**(_DACBITS_PY - 1), signed=True,
                                        min=-1.0, max=1.0,
                                        doc="DC offset added to the final DAC B output [V].")

    # --- Component-Specific Settings ---
    # Component 0
    _comp0_addr_base = 0x0010
    frequency0 = FrequencyRegister(_comp0_addr_base + 0x00, bits=_PHASEBITS_PY,
                                   doc="Base frequency for component 0 [Hz].")
    phase_offset_a0 = PhaseRegister(_comp0_addr_base + 0x04, bits=_PHASEBITS_PY,
                                  doc="Phase offset for component 0 contribution to DAC A [degrees].")
    amplitude_a0 = FloatRegister(_comp0_addr_base + 0x08, bits=_GAINBITS_PY, norm=2.0**(_GAINBITS_PY - 1), signed=False,
                              min=0.0, max=1.0,
                              doc="Amplitude for component 0 contribution to DAC A (0.0 to 1.0).")
    phase_offset_b0 = PhaseRegister(_comp0_addr_base + 0x0C, bits=_PHASEBITS_PY,
                                  doc="Phase offset for component 0 contribution to DAC B [degrees].")
    amplitude_b0 = FloatRegister(_comp0_addr_base + 0x10, bits=_GAINBITS_PY, norm=2.0**(_GAINBITS_PY - 1), signed=False,
                              min=0.0, max=1.0,
                              doc="Amplitude for component 0 contribution to DAC B (0.0 to 1.0).")
    fm_enable0 = BoolRegister(_comp0_addr_base + 0x14, bit=0,
                              doc="Enable frequency modulation for component 0.")
    fm_deviation_khz0 = IntRegister(_comp0_addr_base + 0x18, bits=32, min=0, max=int(125e6 / 2 / 1000),
                                    doc="Max frequency deviation for FM on component 0 [kHz].")

    # Component 1
    _comp1_addr_base = 0x0040
    frequency1 = FrequencyRegister(_comp1_addr_base + 0x00, bits=_PHASEBITS_PY,
                                   doc="Base frequency for component 1 [Hz].")
    phase_offset_a1 = PhaseRegister(_comp1_addr_base + 0x04, bits=_PHASEBITS_PY,
                                  doc="Phase offset for component 1 contribution to DAC A [degrees].")
    amplitude_a1 = FloatRegister(_comp1_addr_base + 0x08, bits=_GAINBITS_PY, norm=2.0**(_GAINBITS_PY - 1), signed=False,
                              min=0.0, max=1.0,
                              doc="Amplitude for component 1 contribution to DAC A (0.0 to 1.0).")
    phase_offset_b1 = PhaseRegister(_comp1_addr_base + 0x0C, bits=_PHASEBITS_PY,
                                  doc="Phase offset for component 1 contribution to DAC B [degrees].")
    amplitude_b1 = FloatRegister(_comp1_addr_base + 0x10, bits=_GAINBITS_PY, norm=2.0**(_GAINBITS_PY - 1), signed=False,
                              min=0.0, max=1.0,
                              doc="Amplitude for component 1 contribution to DAC B (0.0 to 1.0).")
    fm_enable1 = BoolRegister(_comp1_addr_base + 0x14, bit=0,
                              doc="Enable frequency modulation for component 1.")
    fm_deviation_khz1 = IntRegister(_comp1_addr_base + 0x18, bits=32, min=0, max=int(125e6 / 2 / 1000),
                                    doc="Max frequency deviation for FM on component 1 [kHz].")

    # Component 2
    _comp2_addr_base = 0x0070
    frequency2 = FrequencyRegister(_comp2_addr_base + 0x00, bits=_PHASEBITS_PY,
                                   doc="Base frequency for component 2 [Hz].")
    phase_offset_a2 = PhaseRegister(_comp2_addr_base + 0x04, bits=_PHASEBITS_PY,
                                  doc="Phase offset for component 2 contribution to DAC A [degrees].")
    amplitude_a2 = FloatRegister(_comp2_addr_base + 0x08, bits=_GAINBITS_PY, norm=2.0**(_GAINBITS_PY - 1), signed=False,
                              min=0.0, max=1.0,
                              doc="Amplitude for component 2 contribution to DAC A (0.0 to 1.0).")
    phase_offset_b2 = PhaseRegister(_comp2_addr_base + 0x0C, bits=_PHASEBITS_PY,
                                  doc="Phase offset for component 2 contribution to DAC B [degrees].")
    amplitude_b2 = FloatRegister(_comp2_addr_base + 0x10, bits=_GAINBITS_PY, norm=2.0**(_GAINBITS_PY - 1), signed=False,
                              min=0.0, max=1.0,
                              doc="Amplitude for component 2 contribution to DAC B (0.0 to 1.0).")
    fm_enable2 = BoolRegister(_comp2_addr_base + 0x14, bit=0,
                              doc="Enable frequency modulation for component 2.")
    fm_deviation_khz2 = IntRegister(_comp2_addr_base + 0x18, bits=32, min=0, max=int(125e6 / 2 / 1000),
                                    doc="Max frequency deviation for FM on component 2 [kHz].")


    # --- Read-only FPGA Parameters ---
    _PHASEBITS_HW = ConstantIntRegister(0xFF00, bits=32, doc="Phase accumulator bits (read from HW).")
    _LUTSZ_HW = ConstantIntRegister(0xFF04, bits=32, doc="LUT size (log2) (read from HW).")
    _LUTBITS_HW = ConstantIntRegister(0xFF08, bits=32, doc="LUT data width (read from HW).")
    _DACBITS_HW = ConstantIntRegister(0xFF0C, bits=32, doc="DAC data width (read from HW).")
    _GAINBITS_HW = ConstantIntRegister(0xFF10, bits=32, doc="Component Amplitude register width (read from HW).")
    _FM_MOD_BITS_HW = ConstantIntRegister(0xFF14, bits=32, doc="FM modulator input width (read from HW).")
    _NUM_COMPONENTS_HW = ConstantIntRegister(0xFF18, bits=32, doc="Number of components implemented (read from HW).")

    _setup_attributes = ["gen_enable", "output_zero", "output_to_dsp_enable_o",
                         "overall_dc_offset_a", "overall_dc_offset_b",
                         "frequency0", "phase_offset_a0", "amplitude_a0",
                         "phase_offset_b0", "amplitude_b0", "fm_enable0", "fm_deviation_khz0",
                         "frequency1", "phase_offset_a1", "amplitude_a1",
                         "phase_offset_b1", "amplitude_b1", "fm_enable1", "fm_deviation_khz1",
                         "frequency2", "phase_offset_a2", "amplitude_a2",
                         "phase_offset_b2", "amplitude_b2", "fm_enable2", "fm_deviation_khz2"
                        ]
    _gui_attributes = list(_setup_attributes)



    @property
    def output_signal(self):
        if self.gen_enable and not self.output_zero:
            return self.amplitude_a0 if hasattr(self, 'amplitude_a0') else 1.0
        else:
            return 0.0

    def __init__(self, parent, name=None):
        super(Fgen3, self).__init__(parent, name=name)

    def _component_setup(self, index, frequency=None,
                         phase_offset_a=None, amplitude_a=None,
                         phase_offset_b=None, amplitude_b=None,
                         fm_enable=None, fm_deviation_khz=None):
        """Helper to set attributes for a specific component index."""
        if frequency is not None: setattr(self, f'frequency{index}', frequency)
        if phase_offset_a is not None: setattr(self, f'phase_offset_a{index}', phase_offset_a)
        if amplitude_a is not None: setattr(self, f'amplitude_a{index}', amplitude_a)
        if phase_offset_b is not None: setattr(self, f'phase_offset_b{index}', phase_offset_b)
        if amplitude_b is not None: setattr(self, f'amplitude_b{index}', amplitude_b)
        if fm_enable is not None: setattr(self, f'fm_enable{index}', fm_enable)
        if fm_deviation_khz is not None: setattr(self, f'fm_deviation_khz{index}', fm_deviation_khz)

    def setup(self,
              gen_enable=None,
              output_zero=None,
              output_to_dsp_enable_o=None,
              overall_dc_offset_a=None,
              overall_dc_offset_b=None,
              # Component settings (accept lists or individual values)
              frequencies=None,         # List for 3 components
              phase_offsets_a=None,     # List for 3 components, DAC A
              amplitudes_a=None,        # List for 3 components, DAC A
              phase_offsets_b=None,     # List for 3 components, DAC B
              amplitudes_b=None,        # List for 3 components, DAC B
              fm_enables=None,          # List for 3 components
              fm_deviations_khz=None):  # List for 3 components
        """
        Configures the 3-Component FM Sine Generator module.
        (Docstring remains the same)
        """
        self._setup_ongoing = True
        try:
            # Set general parameters
            if gen_enable is not None: self.gen_enable = gen_enable
            if output_zero is not None: self.output_zero = output_zero
            if output_to_dsp_enable_o is not None: self.output_to_dsp_enable_o = output_to_dsp_enable_o
            if overall_dc_offset_a is not None: self.overall_dc_offset_a = overall_dc_offset_a
            if overall_dc_offset_b is not None: self.overall_dc_offset_b = overall_dc_offset_b

            def _ensure_list(val, size, default_val=None):
                if val is None:
                    return [default_val] * size
                if not isinstance(val, (list, tuple, np.ndarray)):
                    res = [val] + [default_val] * (size - 1)
                    return res
                else:
                    res = list(val) + [default_val] * (size - len(val))
                    return res[:size]

            num_comp = self._NUM_COMPONENTS_HW
            if num_comp is None or num_comp < 1 : num_comp = _NUM_COMPONENTS_PY

            frequencies_list = _ensure_list(frequencies, num_comp)
            phase_offsets_a_list = _ensure_list(phase_offsets_a, num_comp)
            amplitudes_a_list = _ensure_list(amplitudes_a, num_comp)
            phase_offsets_b_list = _ensure_list(phase_offsets_b, num_comp)
            amplitudes_b_list = _ensure_list(amplitudes_b, num_comp)
            fm_enables_list = _ensure_list(fm_enables, num_comp)
            fm_deviations_khz_list = _ensure_list(fm_deviations_khz, num_comp)

            for i in range(num_comp):
                self._component_setup(index=i,
                                      frequency=frequencies_list[i],
                                      phase_offset_a=phase_offsets_a_list[i],
                                      amplitude_a=amplitudes_a_list[i],
                                      phase_offset_b=phase_offsets_b_list[i],
                                      amplitude_b=amplitudes_b_list[i],
                                      fm_enable=fm_enables_list[i],
                                      fm_deviation_khz=fm_deviations_khz_list[i])
            if hasattr(self, '_setup'):
                self._setup()
        finally:
            self._setup_ongoing = False

    def reset_phases(self):
        was_enabled = self.gen_enable
        if was_enabled:
            self.gen_enable = False
        _ = self.gen_enable
        self.gen_enable = was_enabled
        self._logger.debug("Fgen3 phases reset by toggling gen_enable.")

    def off(self):
        self.gen_enable = False

    def on(self):
        self.gen_enable = True

    def set_iq(self, index, frequency, amplitude_iq=0.5, phase_i=0.0, amplitude_q=None, phase_q_offset=90.0,
               fm_enable=False, fm_deviation_khz=0):
        if not (0 <= index < _NUM_COMPONENTS_PY): # Use Python constant for safety before HW read
            num_hw_comp = self._NUM_COMPONENTS_HW
            max_idx = (num_hw_comp -1) if (num_hw_comp is not None and num_hw_comp > 0) else (_NUM_COMPONENTS_PY -1)
            raise ValueError(f"Component index must be between 0 and {max_idx}")


        amplitude_i_val = amplitude_iq
        amplitude_q_val = amplitude_q if amplitude_q is not None else amplitude_iq
        phase_q_val = phase_i + phase_q_offset

        self._component_setup(index=index,
                              frequency=frequency,
                              phase_offset_a=phase_i,
                              amplitude_a=amplitude_i_val,
                              phase_offset_b=phase_q_val,
                              amplitude_b=amplitude_q_val,
                              fm_enable=fm_enable,
                              fm_deviation_khz=fm_deviation_khz)
        self._logger.info(f"Component {index} set for I/Q: Freq={frequency} Hz, Amp_I={amplitude_i_val}, Phase_I={phase_i} deg, Amp_Q={amplitude_q_val}, Phase_Q={phase_q_val} deg.")