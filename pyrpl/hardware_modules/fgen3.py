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


class ComponentRegisters:
    """Manages registers for a single frequency component."""

    def __init__(self, module: 'Fgen3', index: int, base_addr: int):
        self.module = module
        self.index = index
        self.base_addr = base_addr
        self._create_registers()

    def _create_registers(self):
        """Dynamically create registers for this component."""
        # Create register attributes
        self.frequency = FrequencyRegister(
            self.base_addr + 0x00,
            bits=FgenConstants.PHASEBITS,
            doc=f"Base frequency for component {self.index} [Hz]."
        )

        self.phase_offset_a = PhaseRegister(
            self.base_addr + 0x04,
            bits=FgenConstants.PHASEBITS,
            doc=f"Phase offset for component {self.index} contribution to DAC A [degrees]."
        )

        self.amplitude_a = FloatRegister(
            self.base_addr + 0x08,
            bits=FgenConstants.GAINBITS,
            norm=2.0 ** (FgenConstants.GAINBITS - 1),
            signed=False,
            min=0.0,
            max=1.0,
            doc=f"Amplitude for component {self.index} contribution to DAC A (0.0 to 1.0)."
        )

        self.phase_offset_b = PhaseRegister(
            self.base_addr + 0x0C,
            bits=FgenConstants.PHASEBITS,
            doc=f"Phase offset for component {self.index} contribution to DAC B [degrees]."
        )

        self.amplitude_b = FloatRegister(
            self.base_addr + 0x10,
            bits=FgenConstants.GAINBITS,
            norm=2.0 ** (FgenConstants.GAINBITS - 1),
            signed=False,
            min=0.0,
            max=1.0,
            doc=f"Amplitude for component {self.index} contribution to DAC B (0.0 to 1.0)."
        )

        self.fm_enable = BoolRegister(
            self.base_addr + 0x14,
            bit=0,
            doc=f"Enable frequency modulation for component {self.index}."
        )

        self.fm_deviation_khz = IntRegister(
            self.base_addr + 0x18,
            bits=32,
            min=0,
            max=FgenConstants.MAX_FREQUENCY_KHZ,
            doc=f"Max frequency deviation for FM on component {self.index} [kHz]."
        )

        self.enable = BoolRegister(
            self.base_addr + 0x1C,
            bit=0,
            default=True,
            doc=f"Enable/disable component {self.index} contribution."
        )

    def setup(self, config: ComponentConfig):
        """Configure this component with the given settings."""
        if config.enable is not None:
            self.enable = config.enable
        if config.frequency is not None:
            self.frequency = config.frequency
        if config.phase_offset_a is not None:
            self.phase_offset_a = config.phase_offset_a
        if config.amplitude_a is not None:
            self.amplitude_a = config.amplitude_a
        if config.phase_offset_b is not None:
            self.phase_offset_b = config.phase_offset_b
        if config.amplitude_b is not None:
            self.amplitude_b = config.amplitude_b
        if config.fm_enable is not None:
            self.fm_enable = config.fm_enable
        if config.fm_deviation_khz is not None:
            self.fm_deviation_khz = config.fm_deviation_khz

    def get_register_names(self) -> List[str]:
        """Get list of register attribute names for this component."""
        return [
            f'enable{self.index}',
            f'frequency{self.index}',
            f'phase_offset_a{self.index}',
            f'amplitude_a{self.index}',
            f'phase_offset_b{self.index}',
            f'amplitude_b{self.index}',
            f'fm_enable{self.index}',
            f'fm_deviation_khz{self.index}'
        ]


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

    # Read-only FPGA Parameters
    _PHASEBITS_HW = ConstantIntRegister(0xFF00, bits=32, doc="Phase accumulator bits (read from HW).")
    _LUTSZ_HW =     ConstantIntRegister(0xFF04, bits=32, doc="LUT size (log2) (read from HW).")
    _LUTBITS_HW =   ConstantIntRegister(0xFF08, bits=32, doc="LUT data width (read from HW).")
    _DACBITS_HW =   ConstantIntRegister(0xFF0C, bits=32, doc="DAC data width (read from HW).")
    _GAINBITS_HW =  ConstantIntRegister(0xFF10, bits=32, doc="Component Amplitude register width (read from HW).")
    _FM_MOD_BITS_HW = ConstantIntRegister(0xFF14, bits=32, doc="FM modulator input width (read from HW).")
    _NUM_COMPONENTS_HW = ConstantIntRegister(0xFF18, bits=32, doc="Number of components implemented (read from HW).")

    def __init__(self, parent, name=None):
        super().__init__(parent, name=name)
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self._components = {}
        self._setup_ongoing = False
        self._initialize_components()

    def _initialize_components(self):
        """Initialize component registers and create attribute mappings."""
        # Create component register managers
        for idx, addr_offset in self._COMPONENT_ADDR_OFFSETS.items():
            comp = ComponentRegisters(self, idx, addr_offset)
            self._components[idx] = comp

            # Create attribute mappings on the main class
            for attr_name in ['enable', 'frequency', 'phase_offset_a', 'amplitude_a',
                              'phase_offset_b', 'amplitude_b', 'fm_enable', 'fm_deviation_khz']:
                # Create property that delegates to component register
                self._create_component_property(idx, attr_name)

    def _create_component_property(self, index: int, attr_name: str):
        """Create a property that delegates to a component register."""
        prop_name = f"{attr_name}{index}"

        def getter(self):
            return getattr(self._components[index], attr_name)

        def setter(self, value):
            setattr(self._components[index], attr_name, value)

        # Create the property and set it on the class
        prop = property(getter, setter)
        setattr(self.__class__, prop_name, prop)

    @property
    def _setup_attributes(self) -> List[str]:
        """Get list of all setup attributes."""
        attrs = [
            "gen_enable", "output_zero", "output_to_dsp_enable_o",
            "overall_dc_offset_a", "overall_dc_offset_b"
        ]

        # Add component attributes
        for idx in range(self.num_components):
            if idx in self._components:
                attrs.extend(self._components[idx].get_register_names())

        return attrs

    @property
    def _gui_attributes(self) -> List[str]:
        """Get list of attributes for GUI display."""
        return self._setup_attributes

    @property
    def num_components(self) -> int:
        """Get the number of components available."""
        hw_num = self._NUM_COMPONENTS_HW
        if hw_num is not None and hw_num > 0:
            return min(hw_num, FgenConstants.NUM_COMPONENTS)
        return FgenConstants.NUM_COMPONENTS

    @property
    def output_signal(self) -> float:
        """Get the current output signal amplitude estimate."""
        if self.gen_enable and not self.output_zero:
            # Return the sum of enabled component amplitudes for DAC A
            total = 0.0
            for idx in range(self.num_components):
                if idx in self._components:
                    comp = self._components[idx]
                    if comp.enable:
                        total += comp.amplitude_a
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

    def setup(self,
              gen_enable: Optional[bool] = None,
              output_zero: Optional[bool] = None,
              output_to_dsp_enable_o: Optional[bool] = None,
              overall_dc_offset_a: Optional[float] = None,
              overall_dc_offset_b: Optional[float] = None,
              # Component settings (accept lists or individual values)
              enables: Optional[Union[bool, List[bool]]] = None,
              frequencies: Optional[Union[float, List[float]]] = None,
              phase_offsets_a: Optional[Union[float, List[float]]] = None,
              amplitudes_a: Optional[Union[float, List[float]]] = None,
              phase_offsets_b: Optional[Union[float, List[float]]] = None,
              amplitudes_b: Optional[Union[float, List[float]]] = None,
              fm_enables: Optional[Union[bool, List[bool]]] = None,
              fm_deviations_khz: Optional[Union[int, List[int]]] = None) -> None:
        """
        Configure the 3-Component FM Sine Generator module.

        Parameters
        ----------
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
        """
        self._setup_ongoing = True
        try:
            # Set general parameters
            if gen_enable is not None:
                self.gen_enable = gen_enable
            if output_zero is not None:
                self.output_zero = output_zero
            if output_to_dsp_enable_o is not None:
                self.output_to_dsp_enable_o = output_to_dsp_enable_o
            if overall_dc_offset_a is not None:
                self.overall_dc_offset_a = overall_dc_offset_a
            if overall_dc_offset_b is not None:
                self.overall_dc_offset_b = overall_dc_offset_b

            # Process component settings
            num_comp = self.num_components

            # Convert all inputs to lists
            enables_list = self._ensure_list(enables, num_comp, default=True)
            frequencies_list = self._ensure_list(frequencies, num_comp)
            phase_offsets_a_list = self._ensure_list(phase_offsets_a, num_comp)
            amplitudes_a_list = self._ensure_list(amplitudes_a, num_comp)
            phase_offsets_b_list = self._ensure_list(phase_offsets_b, num_comp)
            amplitudes_b_list = self._ensure_list(amplitudes_b, num_comp)
            fm_enables_list = self._ensure_list(fm_enables, num_comp, default=False)
            fm_deviations_khz_list = self._ensure_list(fm_deviations_khz, num_comp)

            # Configure each component
            for i in range(num_comp):
                if i in self._components:
                    config = ComponentConfig(
                        enable=enables_list[i],
                        frequency=frequencies_list[i],
                        phase_offset_a=phase_offsets_a_list[i],
                        amplitude_a=amplitudes_a_list[i],
                        phase_offset_b=phase_offsets_b_list[i],
                        amplitude_b=amplitudes_b_list[i],
                        fm_enable=fm_enables_list[i],
                        fm_deviation_khz=fm_deviations_khz_list[i]
                    )
                    self._components[i].setup(config)

            # Call parent setup if it exists
            if hasattr(super(), '_setup'):
                super()._setup()

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

        if index not in self._components:
            raise ValueError(f"Component {index} not initialized")

        comp = self._components[index]
        return ComponentConfig(
            enable=comp.enable,
            frequency=comp.frequency,
            phase_offset_a=comp.phase_offset_a,
            amplitude_a=comp.amplitude_a,
            phase_offset_b=comp.phase_offset_b,
            amplitude_b=comp.amplitude_b,
            fm_enable=comp.fm_enable,
            fm_deviation_khz=comp.fm_deviation_khz
        )

    def __repr__(self) -> str:
        """Return a string representation of the module state."""
        enabled = "enabled" if self.gen_enable else "disabled"
        active_components = sum(
            1 for i in range(self.num_components)
            if i in self._components and self._components[i].enable
        )
        return (
            f"<Fgen3 {enabled}, "
            f"{active_components}/{self.num_components} components active>"
        )