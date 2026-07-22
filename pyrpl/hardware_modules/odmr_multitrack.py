# -*- coding: utf-8 -*-
"""
ODMR Multitrack oscillator + freeze controller (System Bus Region 9).

Per-channel phase-continuous modulation/demodulation oscillator for multi-resonance
ODMR tracking (Phase C). See docs/developer_guide/multi_resonance_tracking.md Section 5.1.

It owns N per-channel phase accumulators (all sharing one f_m frequency tuning word),
a single shared 17-bit quarter-wave LUT producing the active channel's
sin/cos/sin_shifted/cos_shifted references, and a per-hop physical-settle freeze
window. It drives the FM source, both lock-in chains' references, and their freeze
gates (aclken).

Backward compatibility: while ``enable`` is False the block is transparent — the top
level keeps the legacy IQ0 reference path and both lock-in chains free-run, so the
single-resonance system behaves exactly as before. Enable it (after setting
``frequency`` to f_m) to switch the signal path onto the per-channel oscillator and
activate freeze-and-resume.

Typical bring-up:
    >>> mt = p.rp.odmrmultitrack
    >>> mt.frequency = 15.25e3          # f_m (must match the lock-in modulation)
    >>> mt.demod_phase = 0.0            # calibrate 2*pi*f_m*tau on the bench
    >>> mt.settle_time = 200e-6         # physical-settle hold-off after each hop
    >>> mt.src = 'current_step'         # follow the scan hop index in hardware
    >>> mt.enable = True                # switch path onto the per-channel oscillator
"""

import logging

from ..modules import HardwareModule
from ..attributes import (BoolRegister, IntRegister, FloatProperty,
                          FrequencyRegister, PhaseRegister, ConstantIntRegister)

logger = logging.getLogger(__name__)

FPGA_CLK_HZ = 125e6
FPGA_CLK_PERIOD_S = 1.0 / FPGA_CLK_HZ


class OdmrMultitrack(HardwareModule):
    """Per-channel oscillator + freeze controller (Region 9)."""

    addr_base = 0x40900000  # Region 9

    _setup_attributes = ['enable', 'src', 'sw_channel', 'frequency',
                         'demod_phase', 'settle_time']
    _gui_attributes = _setup_attributes + ['nch', 'selected_channel',
                                           'in_settle']

    # ---- CTRL (0x00) ----
    enable = BoolRegister(0x00, bit=0,
                          doc="Enable the per-channel oscillator + freeze gating. "
                              "When False the block is transparent (legacy IQ0 path).")

    _src_sw = BoolRegister(0x00, bit=1,
                           doc="Channel source: 0 = hardware current_step (scan), "
                               "1 = software sw_channel.")

    sw_channel = BoolRegister(0x00, bit=8,
                              doc="Software-selected active channel (used when src='sw'). "
                                  "0/1 for NCH=2.")

    # ---- f_m frequency (0x04) ----
    frequency = FrequencyRegister(0x04, bits=32,
                                  doc="Modulation frequency f_m [Hz] (shared by all "
                                      "channels). Must match the lock-in modulation.")

    # ---- demod phase (0x08) ----
    # invert=True to MATCH the iq0 demod-phase convention. iq0.phase is a
    # PhaseRegister(invert=True), so iq0.phase=X writes the FPGA word for -X. The
    # FPGA applies the same offset operation here as for iq0 (demod_ref = LUT(phase +
    # offset_word); the LUT is bit-identical to the iq0 fgen LUT). Without invert,
    # demod_phase=X would write the word for +X, i.e. the OPPOSITE sign of iq0 -> the
    # demod reference would be rotated by -2X relative to the legacy path, breaking
    # the discriminator. With invert=True, demod_phase = <your tuned iq0 phase>
    # reproduces the iq0 demodulation EXACTLY. (Pure Python convention change; the
    # inversion is applied host-side in PhaseRegister.from_python, no FPGA rebuild.)
    demod_phase = PhaseRegister(0x08, bits=32, invert=True,
                                doc="Demodulation phase offset [degrees] for the "
                                    "sin_shifted/cos_shifted references (2*pi*f_m*tau). "
                                    "Same sign convention as iq0.phase: set it equal to "
                                    "the iq0 demod phase you tuned for the scan.")

    # ---- T_settle (0x0C) in clock cycles ----
    _t_settle_cycles = IntRegister(0x0C, bits=32, min=0,
                                   doc="Physical-settle hold-off after each hop [cycles].")

    # ---- STATUS (0x10) read-only ----
    _status = IntRegister(0x10, bits=32, doc="Status: bit0..=sel, in_settle, run mask.")

    # ---- NCH readback (0x14) ----
    _NCH_HW = ConstantIntRegister(0x14, bits=32,
                                  doc="Number of channels implemented (read from HW).")

    # ------------------------------------------------------------------
    # Friendly properties
    # ------------------------------------------------------------------
    @property
    def src(self):
        """Active-channel source: 'current_step' (hardware) or 'sw' (software)."""
        return 'sw' if self._src_sw else 'current_step'

    @src.setter
    def src(self, value):
        if value not in ('sw', 'current_step'):
            raise ValueError("src must be 'sw' or 'current_step'")
        self._src_sw = (value == 'sw')

    @property
    def settle_time(self):
        """Physical-settle hold-off after each hop [s]."""
        return self._t_settle_cycles * FPGA_CLK_PERIOD_S

    @settle_time.setter
    def settle_time(self, value_s):
        self._t_settle_cycles = max(0, int(round(float(value_s) / FPGA_CLK_PERIOD_S)))

    @property
    def nch(self):
        """Number of channels implemented in the FPGA bitstream."""
        return int(self._NCH_HW)

    @property
    def selected_channel(self):
        """Currently active channel index (sel)."""
        nbits = max(1, (self.nch - 1).bit_length())
        return int(self._status & ((1 << nbits) - 1))

    @property
    def in_settle(self):
        """True while inside the per-hop physical-settle freeze window."""
        nbits = max(1, (self.nch - 1).bit_length())
        return bool((self._status >> nbits) & 0x1)

    @property
    def run_mask(self):
        """Per-channel run-enable bitmask (bit i set = channel i currently clocking)."""
        return int((self._status >> 8) & ((1 << self.nch) - 1))

    def _setup(self):
        """Set sensible defaults (does not enable the oscillator)."""
        # Default f_m to the documented 15.25 kHz if unset, so enabling it is a
        # drop-in for the single-resonance modulation.
        if self.frequency == 0:
            self.frequency = 15.25e3

    def get_status(self):
        """Return a status dictionary."""
        return {
            'enable': self.enable,
            'src': self.src,
            'sw_channel': int(self.sw_channel),
            'frequency_hz': self.frequency,
            'demod_phase_deg': self.demod_phase,
            'settle_time_s': self.settle_time,
            'nch': self.nch,
            'selected_channel': self.selected_channel,
            'in_settle': self.in_settle,
            'run_mask': self.run_mask,
        }
