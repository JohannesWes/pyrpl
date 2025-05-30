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

from typing import Optional, Union, List, Tuple
import numpy as np
from dataclasses import dataclass
from collections import OrderedDict
import logging

# Import necessary base classes and attribute types from pyrpl core
from ..attributes import (
    BoolRegister, FloatRegister, IntRegister, PhaseRegister,
    FrequencyRegister, ConstantIntRegister
)
from ..modules import HardwareModule, SignalModule
from ..widgets.module_widgets.fgen3_widget import Fgen3Widget


# Define constants based on Verilog parameters
# These should match the Verilog module `red_pitaya_3fgen.v`
class FgenConstants:
    """Constants for the FPGA module parameters."""
    PHASEBITS = 32
    LUTSZ = 11
    LUTBITS = 14
    DACBITS = 14
    GAINBITS = 14
    FM_MOD_BITS = 17
    NUM_COMPONENTS = 3
    MAX_FREQUENCY_KHZ = 125_000 // 2  # 62.5 MHz


@dataclass
class ComponentConfig:
    """Configuration for a single frequency component of the signal generator."""
    enable: bool = True
    frequency: Optional[float] = None
    phase_offset_a: Optional[float] = None
    amplitude_a: Optional[float] = None
    phase_offset_b: Optional[float] = None
    amplitude_b: Optional[float] = None
    fm_enable: bool = False
    fm_deviation_khz: Optional[int] = None


class Fgen3(HardwareModule, SignalModule):
    """
    Python interface for the 3-Component FM Sine Generator FPGA module.

    This module provides high-level control over a 3-component signal generator
    with independent frequency, phase, and amplitude control for each component's
    contribution to two DAC outputs (A and B).
    """

    _widget_class = Fgen3Widget
    addr_base = 0x40600000

    # Component base addresses
    _COMPONENT_ADDR_OFFSETS = {
        0: 0x0010,
        1: 0x0040,
        2: 0x0070
    }

    # General Control Registers
    gen_enable = BoolRegister(
        0x0000, bit=0,
        doc="Master enable for the generator. If False, phase accumulators are reset/held and AC sum is zero."
    )

    output_zero = BoolRegister(
        0x0000, bit=1,
        doc="If True, forces AC component of output to zero, outputting only the DC offset. "
            "Overrides gen_enable for AC part."
    )

    output_to_dsp_enable_o = BoolRegister(
        0x0000, bit=2,
        doc="If True, routes output of fgen3 to DSP, otherwise routes ASG output to DSP"
    )

    # Overall Output Settings
    overall_dc_offset_a = FloatRegister(
        0x0004,
        bits=FgenConstants.DACBITS,
        norm=2 ** (FgenConstants.DACBITS - 1),
        signed=True,
        min=-1.0,
        max=1.0,
        doc="DC offset added to the final DAC A output [V]."
    )

    overall_dc_offset_b = FloatRegister(
        0x0008,
        bits=FgenConstants.DACBITS,
        norm=2 ** (FgenConstants.DACBITS - 1),
        signed=True,
        min=-1.0,
        max=1.0,
        doc="DC offset added to the final DAC B output [V]."
    )

    # Component-specific registers
    # Component 0
    frequency0 = FrequencyRegister(
        _COMPONENT_ADDR_OFFSETS[0] + 0x00, bits=FgenConstants.PHASEBITS,
        doc="Base frequency for component 0 [Hz]."
    )
    phase_offset_a0 = PhaseRegister(
        _COMPONENT_ADDR_OFFSETS[0] + 0x04, bits=FgenConstants.PHASEBITS,
        doc="Phase offset for component 0 contribution to DAC A [degrees]."
    )
    amplitude_a0 = FloatRegister(
        _COMPONENT_ADDR_OFFSETS[0] + 0x08, bits=FgenConstants.GAINBITS,
        norm=2.0**(FgenConstants.GAINBITS - 1), signed=False,
        min=0.0, max=1.0, doc="Amplitude for component 0 contribution to DAC A (0.0 to 1.0)."
    )
    phase_offset_b0 = PhaseRegister(
        _COMPONENT_ADDR_OFFSETS[0] + 0x0C, bits=FgenConstants.PHASEBITS,
        doc="Phase offset for component 0 contribution to DAC B [degrees]."
    )
    amplitude_b0 = FloatRegister(
        _COMPONENT_ADDR_OFFSETS[0] + 0x10, bits=FgenConstants.GAINBITS,
        norm=2.0**(FgenConstants.GAINBITS - 1), signed=False,
        min=0.0, max=1.0, doc="Amplitude for component 0 contribution to DAC B (0.0 to 1.0)."
    )
    fm_enable0 = BoolRegister(
        _COMPONENT_ADDR_OFFSETS[0] + 0x14, bit=0,
        doc="Enable frequency modulation for component 0."
    )
    fm_deviation_khz0 = IntRegister(
        _COMPONENT_ADDR_OFFSETS[0] + 0x18, bits=32, min=0, max=FgenConstants.MAX_FREQUENCY_KHZ,
        doc="Max frequency deviation for FM on component 0 [kHz]."
    )
    enable0 = BoolRegister(
        _COMPONENT_ADDR_OFFSETS[0] + 0x1C, bit=0, default=True,
        doc="Enable/disable component 0 contribution."
    )

    # Component 1
    frequency1 = FrequencyRegister(
        _COMPONENT_ADDR_OFFSETS[1] + 0x00, bits=FgenConstants.PHASEBITS,
        doc="Base frequency for component 1 [Hz]."
    )
    phase_offset_a1 = PhaseRegister(
        _COMPONENT_ADDR_OFFSETS[1] + 0x04, bits=FgenConstants.PHASEBITS,
        doc="Phase offset for component 1 contribution to DAC A [degrees]."
    )
    amplitude_a1 = FloatRegister(
        _COMPONENT_ADDR_OFFSETS[1] + 0x08, bits=FgenConstants.GAINBITS,
        norm=2.0**(FgenConstants.GAINBITS - 1), signed=False,
        min=0.0, max=1.0, doc="Amplitude for component 1 contribution to DAC A (0.0 to 1.0)."
    )
    phase_offset_b1 = PhaseRegister(
        _COMPONENT_ADDR_OFFSETS[1] + 0x0C, bits=FgenConstants.PHASEBITS,
        doc="Phase offset for component 1 contribution to DAC B [degrees]."
    )
    amplitude_b1 = FloatRegister(
        _COMPONENT_ADDR_OFFSETS[1] + 0x10, bits=FgenConstants.GAINBITS,
        norm=2.0**(FgenConstants.GAINBITS - 1), signed=False,
        min=0.0, max=1.0, doc="Amplitude for component 1 contribution to DAC B (0.0 to 1.0)."
    )
    fm_enable1 = BoolRegister(
        _COMPONENT_ADDR_OFFSETS[1] + 0x14, bit=0,
        doc="Enable frequency modulation for component 1."
    )
    fm_deviation_khz1 = IntRegister(
        _COMPONENT_ADDR_OFFSETS[1] + 0x18, bits=32, min=0, max=FgenConstants.MAX_FREQUENCY_KHZ,
        doc="Max frequency deviation for FM on component 1 [kHz]."
    )
    enable1 = BoolRegister(
        _COMPONENT_ADDR_OFFSETS[1] + 0x1C, bit=0, default=True,
        doc="Enable/disable component 1 contribution."
    )

    # Component 2
    frequency2 = FrequencyRegister(
        _COMPONENT_ADDR_OFFSETS[2] + 0x00, bits=FgenConstants.PHASEBITS,
        doc="Base frequency for component 2 [Hz]."
    )
    phase_offset_a2 = PhaseRegister(
        _COMPONENT_ADDR_OFFSETS[2] + 0x04, bits=FgenConstants.PHASEBITS,
        doc="Phase offset for component 2 contribution to DAC A [degrees]."
    )
    amplitude_a2 = FloatRegister(
        _COMPONENT_ADDR_OFFSETS[2] + 0x08, bits=FgenConstants.GAINBITS,
        norm=2.0**(FgenConstants.GAINBITS - 1), signed=False,
        min=0.0, max=1.0, doc="Amplitude for component 2 contribution to DAC A (0.0 to 1.0)."
    )
    phase_offset_b2 = PhaseRegister(
        _COMPONENT_ADDR_OFFSETS[2] + 0x0C, bits=FgenConstants.PHASEBITS,
        doc="Phase offset for component 2 contribution to DAC B [degrees]."
    )
    amplitude_b2 = FloatRegister(
        _COMPONENT_ADDR_OFFSETS[2] + 0x10, bits=FgenConstants.GAINBITS,
        norm=2.0**(FgenConstants.GAINBITS - 1), signed=False,
        min=0.0, max=1.0, doc="Amplitude for component 2 contribution to DAC B (0.0 to 1.0)."
    )
    fm_enable2 = BoolRegister(
        _COMPONENT_ADDR_OFFSETS[2] + 0x14, bit=0,
        doc="Enable frequency modulation for component 2."
    )
    fm_deviation_khz2 = IntRegister(
        _COMPONENT_ADDR_OFFSETS[2] + 0x18, bits=32, min=0, max=FgenConstants.MAX_FREQUENCY_KHZ,
        doc="Max frequency deviation for FM on component 2 [kHz]."
    )
    enable2 = BoolRegister(
        _COMPONENT_ADDR_OFFSETS[2] + 0x1C, bit=0, default=True,
        doc="Enable/disable component 2 contribution."
    )

    # Read-only FPGA Parameters
    _PHASEBITS_HW = ConstantIntRegister(0xFF00, bits=32, doc="Phase accumulator bits (read from HW).")
    _LUTSZ_HW = ConstantIntRegister(0xFF04, bits=32, doc="LUT size (log2) (read from HW).")
    _LUTBITS_HW = ConstantIntRegister(0xFF08, bits=32, doc="LUT data width (read from HW).")
    _DACBITS_HW = ConstantIntRegister(0xFF0C, bits=32, doc="DAC data width (read from HW).")
    _GAINBITS_HW = ConstantIntRegister(0xFF10, bits=32, doc="Component Amplitude register width (read from HW).")
    _FM_MOD_BITS_HW = ConstantIntRegister(0xFF14, bits=32, doc="FM modulator input width (read from HW).")
    _NUM_COMPONENTS_HW = ConstantIntRegister(0xFF18, bits=32, doc="Number of components implemented (read from HW).")

    # Define setup and GUI attributes as class-level lists
    _setup_attributes = [
        "gen_enable", "output_zero", "output_to_dsp_enable_o",
        "overall_dc_offset_a", "overall_dc_offset_b",
        "enable0", "frequency0", "phase_offset_a0", "amplitude_a0",
        "phase_offset_b0", "amplitude_b0", "fm_enable0", "fm_deviation_khz0",
        "enable1", "frequency1", "phase_offset_a1", "amplitude_a1",
        "phase_offset_b1", "amplitude_b1", "fm_enable1", "fm_deviation_khz1",
        "enable2", "frequency2", "phase_offset_a2", "amplitude_a2",
        "phase_offset_b2", "amplitude_b2", "fm_enable2", "fm_deviation_khz2"
    ]
    _gui_attributes = list(_setup_attributes)

    def __init__(self, parent, name=None):
        super().__init__(parent, name=name)
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self._setup_ongoing = False

    @property
    def num_components(self) -> int:
        """Get the number of components available."""
        hw_num = self._NUM_COMPONENTS_HW
        if hw_num is not None and hw_num > 0:
            return min(hw_num, FgenConstants.NUM_COMPONENTS)
        return FgenConstants.NUM_COMPONENTS

    @property
    def output_signal(self) -> float:
        """Get the current output signal maximum amplitude estimate."""
        if self.gen_enable and not self.output_zero:
            # Return the sum of enabled component amplitudes for DAC A
            total = 0.0
            for i in range(self.num_components):
                if getattr(self, f'enable{i}', False):
                    total += getattr(self, f'amplitude_a{i}', 0.0)
            return total
        return 0.0

    def _validate_component_index(self, index: int) -> None:
        """Validate that a component index is valid."""
        if not (0 <= index < self.num_components):
            raise ValueError(f"Component index must be between 0 and {self.num_components - 1}")

    def _ensure_list(self, value: Union[None, float, List[float]],
                     size: int, default: Optional[float] = None) -> List[Optional[float]]:
        """Convert a value to a list of the specified size."""
        if value is None:
            return [default] * size

        if isinstance(value, (list, tuple, np.ndarray)):
            result = list(value) + [default] * (size - len(value))
            return result[:size]

        # Single value provided
        return [value] + [default] * (size - 1)

    def _component_setup(self, index: int, enable: Optional[bool] = None, frequency: Optional[float] = None,
                         phase_offset_a: Optional[float] = None, amplitude_a: Optional[float] = None,
                         phase_offset_b: Optional[float] = None, amplitude_b: Optional[float] = None,
                         fm_enable: Optional[bool] = None, fm_deviation_khz: Optional[int] = None):
        """Helper to set attributes for a specific component index."""
        if enable is not None:
            setattr(self, f'enable{index}', enable)
        if frequency is not None:
            setattr(self, f'frequency{index}', frequency)
        if phase_offset_a is not None:
            setattr(self, f'phase_offset_a{index}', phase_offset_a)
        if amplitude_a is not None:
            setattr(self, f'amplitude_a{index}', amplitude_a)
        if phase_offset_b is not None:
            setattr(self, f'phase_offset_b{index}', phase_offset_b)
        if amplitude_b is not None:
            setattr(self, f'amplitude_b{index}', amplitude_b)
        if fm_enable is not None:
            setattr(self, f'fm_enable{index}', fm_enable)
        if fm_deviation_khz is not None:
            setattr(self, f'fm_deviation_khz{index}', fm_deviation_khz)

    def setup(self, **kwargs) -> None:
        """
        Configure the 3-Component FM Sine Generator module.

        Accepts both list-based parameters (e.g., 'enables') and individual
        component parameters (e.g., 'enable0', 'enable1') for backward compatibility.

        General Parameters
        ------------------
        gen_enable : bool, optional
            Master enable for the generator.
        output_zero : bool, optional
            Force AC output to zero if True.
        output_to_dsp_enable_o : bool, optional
            Route output to DSP if True.
        overall_dc_offset_a : float, optional
            DC offset for DAC A output [-1.0, 1.0].
        overall_dc_offset_b : float, optional
            DC offset for DAC B output [-1.0, 1.0].

        Component Parameters (list-based)
        ---------------------------------
        enables : bool or list of bool, optional
            Enable state for each component.
        frequencies : float or list of float, optional
            Frequency in Hz for each component.
        phase_offsets_a : float or list of float, optional
            Phase offset in degrees for DAC A contribution.
        amplitudes_a : float or list of float, optional
            Amplitude [0.0, 1.0] for DAC A contribution.
        phase_offsets_b : float or list of float, optional
            Phase offset in degrees for DAC B contribution.
        amplitudes_b : float or list of float, optional
            Amplitude [0.0, 1.0] for DAC B contribution.
        fm_enables : bool or list of bool, optional
            Enable FM for each component.
        fm_deviations_khz : int or list of int, optional
            Maximum FM deviation in kHz for each component.

        Component Parameters (individual, for backward compatibility)
        ------------------------------------------------------------
        enable0, enable1, enable2 : bool, optional
        frequency0, frequency1, frequency2 : float, optional
        phase_offset_a0, phase_offset_a1, phase_offset_a2 : float, optional
        amplitude_a0, amplitude_a1, amplitude_a2 : float, optional
        phase_offset_b0, phase_offset_b1, phase_offset_b2 : float, optional
        amplitude_b0, amplitude_b1, amplitude_b2 : float, optional
        fm_enable0, fm_enable1, fm_enable2 : bool, optional
        fm_deviation_khz0, fm_deviation_khz1, fm_deviation_khz2 : int, optional
        """
        self._setup_ongoing = True
        try:
            # Extract general parameters
            general_params = ['gen_enable', 'output_zero', 'output_to_dsp_enable_o',
                              'overall_dc_offset_a', 'overall_dc_offset_b']

            for param in general_params:
                if param in kwargs:
                    setattr(self, param, kwargs.pop(param))

            # Component parameter configuration
            num_comp = self.num_components
            component_param_info = [
                # (list_param_name, individual_prefix, default_value)
                ('enables', 'enable', True),
                ('frequencies', 'frequency', None),
                ('phase_offsets_a', 'phase_offset_a', None),
                ('amplitudes_a', 'amplitude_a', None),
                ('phase_offsets_b', 'phase_offset_b', None),
                ('amplitudes_b', 'amplitude_b', None),
                ('fm_enables', 'fm_enable', False),
                ('fm_deviations_khz', 'fm_deviation_khz', None),
            ]

            # Process component parameters
            component_values = {}
            for list_name, prefix, default in component_param_info:
                # Check for list-based parameter
                if list_name in kwargs:
                    values = kwargs.pop(list_name)
                else:
                    # Check for individual parameters
                    values = []
                    for i in range(num_comp):
                        param_name = f'{prefix}{i}'
                        if param_name in kwargs:
                            values.append(kwargs.pop(param_name))

                    # If no individual params found, use None
                    values = values if values else None

                # Convert to properly sized list
                component_values[prefix] = self._ensure_list(values, num_comp, default=default)

            # Warn about unexpected parameters
            if kwargs:
                self._logger.warning(f"Unexpected parameters in setup(): {list(kwargs.keys())}")

            # Apply settings to each component
            for i in range(num_comp):
                self._component_setup(
                    index=i,
                    enable=component_values['enable'][i],
                    frequency=component_values['frequency'][i],
                    phase_offset_a=component_values['phase_offset_a'][i],
                    amplitude_a=component_values['amplitude_a'][i],
                    phase_offset_b=component_values['phase_offset_b'][i],
                    amplitude_b=component_values['amplitude_b'][i],
                    fm_enable=component_values['fm_enable'][i],
                    fm_deviation_khz=component_values['fm_deviation_khz'][i]
                )

            # Call parent setup if it exists
            if hasattr(self, '_setup'):
                self._setup()

        finally:
            self._setup_ongoing = False


    def reset_phases(self) -> None:
        """Reset all phase accumulators by toggling the generator enable."""
        was_enabled = self.gen_enable
        if was_enabled:
            self.gen_enable = False

        # Force a read to ensure the write has taken effect
        _ = self.gen_enable

        self.gen_enable = was_enabled
        self._logger.debug("Fgen3 phases reset by toggling gen_enable.")

    def off(self) -> None:
        """Disable the generator output."""
        self.gen_enable = False

    def on(self) -> None:
        """Enable the generator output."""
        self.gen_enable = True

    def set_iq(self,
               index: int,
               frequency: float,
               enable: bool = True,
               amplitude_iq: float = 0.5,
               phase_i: float = 0.0,
               amplitude_q: Optional[float] = None,
               phase_q_offset: float = 90.0,
               fm_enable: bool = False,
               fm_deviation_khz: int = 0) -> None:
        """
        Configure a component for I/Q signal generation.

        This is a convenience method for setting up a component with
        90-degree phase offset between DAC A (I) and DAC B (Q) outputs.

        Parameters
        ----------
        index : int
            Component index (0, 1, or 2).
        frequency : float
            Frequency in Hz.
        enable : bool, default=True
            Enable this component.
        amplitude_iq : float, default=0.5
            Amplitude for both I and Q channels [0.0, 1.0].
        phase_i : float, default=0.0
            Phase offset for I channel in degrees.
        amplitude_q : float, optional
            Amplitude for Q channel. If None, uses amplitude_iq.
        phase_q_offset : float, default=90.0
            Phase offset between I and Q channels in degrees.
        fm_enable : bool, default=False
            Enable frequency modulation.
        fm_deviation_khz : int, default=0
            Maximum FM deviation in kHz.

        Raises
        ------
        ValueError
            If component index is out of range.
        """
        self._validate_component_index(index)

        amplitude_i_val = amplitude_iq
        amplitude_q_val = amplitude_q if amplitude_q is not None else amplitude_iq
        phase_q_val = phase_i + phase_q_offset

        self._component_setup(
            index=index,
            enable=enable,
            frequency=frequency,
            phase_offset_a=phase_i,
            amplitude_a=amplitude_i_val,
            phase_offset_b=phase_q_val,
            amplitude_b=amplitude_q_val,
            fm_enable=fm_enable,
            fm_deviation_khz=fm_deviation_khz
        )

        self._logger.info(
            f"Component {index} set for I/Q: Freq={frequency} Hz, "
            f"Amp_I={amplitude_i_val}, Phase_I={phase_i} deg, "
            f"Amp_Q={amplitude_q_val}, Phase_Q={phase_q_val} deg."
        )

    def get_component_config(self, index: int) -> ComponentConfig:
        """
        Get the current configuration of a component.

        Parameters
        ----------
        index : int
            Component index.

        Returns
        -------
        ComponentConfig
            Current configuration of the component.
        """
        self._validate_component_index(index)

        return ComponentConfig(
            enable=getattr(self, f'enable{index}'),
            frequency=getattr(self, f'frequency{index}'),
            phase_offset_a=getattr(self, f'phase_offset_a{index}'),
            amplitude_a=getattr(self, f'amplitude_a{index}'),
            phase_offset_b=getattr(self, f'phase_offset_b{index}'),
            amplitude_b=getattr(self, f'amplitude_b{index}'),
            fm_enable=getattr(self, f'fm_enable{index}'),
            fm_deviation_khz=getattr(self, f'fm_deviation_khz{index}')
        )

    def __repr__(self) -> str:
        """Return a string representation of the module state."""
        enabled = "enabled" if self.gen_enable else "disabled"
        active_components = sum(
            1 for i in range(self.num_components)
            if getattr(self, f'enable{i}', False)
        )
        return (
            f"<Fgen3 {enabled}, "
            f"{active_components}/{self.num_components} components active>"
        )