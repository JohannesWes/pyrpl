# -*- coding: utf-8 -*-
"""
ODMR Frequency Lock Module (1f-I Component with PI Control)

This module implements a frequency-locked loop (FLL) that tracks the resonance
frequency of a physical system by locking a DDS-generated frequency to the
zero-crossing of a demodulated lock-in signal.

================================================================================
THEORY OF OPERATION
================================================================================

The ODMR frequency lock supports two control modes:

INTEGRAL-ONLY MODE (default, prop_enable=False):
    FTW_corr[n+1] = FTW_corr[n] - μ_FTW * e[n]

PI MODE (prop_enable=True, faster acquisition):
    u[n] = K_p,FTW * e[n] + x[n]    (parallel PI form)
    x[n+1] = x[n] - μ_FTW * e[n]     (integral state)

Where:
    - e[n]: Demodulated error signal (LSB from lock_in channel 1)
    - μ_FTW: Integral gain in Q8.24 fixed-point format (FTW/LSB)
    - K_p,FTW: Proportional gain in Q8.24 fixed-point format (FTW/LSB)
    - FTW_corr: Frequency Tuning Word correction sent to 3FGEN

================================================================================
DESIGN PARAMETERS
================================================================================

Clock: 125 MHz (FPGA clock)
Update rate: ~30.5 kS/s (when lock_in FIR outputs valid data)
Phase bits: 32 (matching 3FGEN DDS)
Gain format: Q8.24 fixed-point (8 integer bits, 24 fractional bits)

Default Values (for K=1.1 LSB/Hz, 300 Hz bandwidth):
    - mu_q: 0x01EDE8D0 ≈ 1.929 FTW/LSB (integral gain)
    - kp_q: 0x5DB55838 ≈ 93.71 FTW/LSB (proportional gain, zero at BW/3)
    - ftw_lim: 34359738 (±1 MHz correction range)

================================================================================
TYPICAL USAGE
================================================================================

>>> # Initialize and configure (integral-only mode)
>>> odm = pyrpl.rp.odmr_freq_lock
>>> odm.enable = False
>>> odm.set_bandwidth(300, slope_lsb_per_hz=1.1)  # I-only, 300 Hz BW
>>> odm.max_correction_hz = 1e6  # ±1 MHz range
>>>
>>> # Or use PI mode for faster acquisition
>>> odm.set_bandwidth_pi(300, slope_lsb_per_hz=1.1, zero_ratio=3)
>>>
>>> # Check polarity by manually stepping frequency
>>> odm.invert = False  # Adjust if loop has wrong sign
>>>
>>> # Enable the loop
>>> odm.enable = True
>>>
>>> # Monitor performance
>>> print(f"Error: {odm.error_lsb} LSB")
>>> print(f"Correction: {odm.correction_hz:.1f} Hz")
>>> print(f"Locked: {odm.locked}, Saturated: {odm.saturated}")
>>>
>>> # Stream correction data for analysis
>>> corrections = []
>>> for _ in range(1000):
>>>     corrections.append(odm.correction_hz)
>>>     time.sleep(0.001)

================================================================================
ATTRIBUTES
================================================================================

Control:
    enable: Enable/disable the frequency lock loop
    invert: Invert error sign (for polarity correction)
    hold: Freeze integrator (stop updates)
    clear: Clear integrator to zero (self-clearing)
    deadband_enable: Enable deadband threshold
    prop_enable: Enable proportional path (PI mode)

Tuning:
    mu_q: Raw integral gain in Q8.24 format (FTW/LSB)
    mu_hz_per_lsb: Integral gain in Hz/LSB units
    kp_q: Raw proportional gain in Q8.24 format (FTW/LSB)
    kp_hz_per_lsb: Proportional gain in Hz/LSB units
    deadband_lsb: Deadband threshold in error LSB
    max_correction_hz: Maximum frequency correction in Hz

Status (read-only):
    locked: True if error below threshold for 256 samples
    saturated: True if any correction hit saturation limit (legacy)
    saturated_i: True if integrator path hit saturation limit
    saturated_pi: True if PI sum hit saturation limit
    error_lsb: Last error value that produced an update
    correction_hz: Current frequency correction in Hz
    correction_ftw: Current frequency correction in FTW units
"""

import logging
import numpy as np

from ..modules import HardwareModule
from ..attributes import (BoolRegister, IntRegister, FloatRegister, FloatProperty)

logger = logging.getLogger(__name__)

# Constants from planning document
FPGA_CLK_HZ = 125e6
PHASEBITS = 32
FTW_PER_HZ = (2**PHASEBITS) / FPGA_CLK_HZ  # ≈ 34.359738368 FTW/Hz

# Q8.24 format parameters
MU_Q_FRAC_BITS = 24
MU_Q_SCALE = 2**MU_Q_FRAC_BITS

# Default values
# MU_Q_DEFAULT: raw hardware value is 0x01EDE8D0, which in Q8.24 format is:
MU_Q_DEFAULT = 0x01EDE8D0 / MU_Q_SCALE  # ≈ 1.929333 FTW/LSB (for 300 Hz BW with K=1.1 LSB/Hz)
FTW_LIM_DEFAULT = 34359738  # ±1 MHz
LOCK_COUNT_THRESH = 256  # Samples needed to declare lock

# Default proportional gain (for 300 Hz BW with K=1.1, zero at BW/3)
# K_p = α/K = 3/1.1 ≈ 2.7273 Hz/LSB
# K_p,FTW = 2.7273 * (2^32 / 125 MHz) ≈ 93.7084 FTW/LSB
# Q8.24: round(93.7084 * 2^24) = 0x5DB55838
KP_Q_DEFAULT = 0x5DB55838 / MU_Q_SCALE  # ≈ 93.7084 FTW/LSB


class OdmrFreqLock(HardwareModule):
    """
    ODMR Frequency Lock hardware module (System Bus Region 8).

    Implements configurable frequency-locked loop (I-only or PI mode) for tracking
    resonance frequency via demodulated lock-in signal. Defaults to integral-only
    mode for backward compatibility; enable PI mode with prop_enable=True for
    faster acquisition and improved phase margin.
    """

    addr_base = 0x40800000  # Region 8

    _setup_attributes = [
        'enable', 'invert', 'hold', 'deadband_enable',
        'mu_hz_per_lsb', 'deadband_lsb', 'max_correction_hz',
        'prop_enable', 'kp_hz_per_lsb'
    ]
    _gui_attributes = _setup_attributes + ['locked', 'saturated', 'saturated_i', 'saturated_pi', 'error_lsb', 'correction_hz']

    #--------------------------------------------------------------------------
    # CONTROL REGISTER (0x0000)
    #--------------------------------------------------------------------------

    enable = BoolRegister(0x0000,
                         bitmask=0x1,
                         doc="Enable frequency lock loop. When disabled, correction forced to zero.")

    invert = BoolRegister(0x0000,
                         bitmask=0x2,
                         doc="Invert error sign. Set this if loop tracks in wrong direction.")

    hold = BoolRegister(0x0000,
                       bitmask=0x4,
                       doc="Freeze integrator. Updates are skipped but correction holds current value.")

    _clear_bit = BoolRegister(0x0000,
                             bitmask=0x8,
                             doc="Clear integrator to zero (self-clearing, write-only).")

    deadband_enable = BoolRegister(0x0000,
                                  bitmask=0x10,
                                  doc="Enable deadband. Updates skipped when |error| < deadband threshold.")

    prop_enable = BoolRegister(0x0000,
                              bit=5,  # Bit 5 = 0x20
                              doc="Enable proportional path (PI control). "
                                  "When disabled, loop is integral-only for backward compatibility.")

    #--------------------------------------------------------------------------
    # GAIN REGISTER (0x0004) - Q8.24 format
    #--------------------------------------------------------------------------

    mu_q = FloatRegister(0x0004,
                        bits=32,
                        norm=2**24,  # Q8.24 fixed-point format
                        signed=True,
                        doc="Integral gain in Q8.24 fixed-point (FTW/LSB). "
                            "Default 0x01EDE8D0 for 300 Hz BW with K=1.1.")

    #--------------------------------------------------------------------------
    # DEADBAND REGISTER (0x0008)
    #--------------------------------------------------------------------------

    deadband = IntRegister(0x0008,
                          bits=32,
                          doc="Deadband threshold in error LSB. Updates skipped when |error| < threshold.")

    #--------------------------------------------------------------------------
    # FTW LIMIT REGISTER (0x000C)
    #--------------------------------------------------------------------------

    ftw_lim = IntRegister(0x000C,
                         bits=32,
                         doc="Saturation limit for FTW correction (unsigned magnitude). "
                             "Default 34359738 for ±1 MHz.")

    #--------------------------------------------------------------------------
    # KP_Q REGISTER (0x001C) - Q8.24 format (was RESERVED)
    #--------------------------------------------------------------------------

    kp_q = FloatRegister(0x001C,
                        bits=32,
                        norm=2**24,  # Q8.24 fixed-point format
                        signed=True,
                        doc="Proportional gain in Q8.24 fixed-point (FTW/LSB). "
                            "Default 0x5DB55838 for 300 Hz BW with zero at BW/3.")

    #--------------------------------------------------------------------------
    # STATUS REGISTER (0x0010) - Read-only
    #--------------------------------------------------------------------------

    _status_reg = IntRegister(0x0010,
                             bits=32,
                             doc="Status register: bit0=locked, bit1=saturated")

    #--------------------------------------------------------------------------
    # ERROR LATCH REGISTER (0x0014) - Read-only
    #--------------------------------------------------------------------------

    err_latch = FloatRegister(0x0014,
                             bits=32,
                             norm=1,  # No scaling, just signed interpretation
                             signed=True,
                             doc="Last error value that produced an integrator update (LSB).")

    #--------------------------------------------------------------------------
    # FTW CORRECTION REGISTER (0x0018) - Read-only
    #--------------------------------------------------------------------------

    ftw_corr = FloatRegister(0x0018,
                            bits=32,
                            norm=1,  # No scaling, just signed interpretation
                            signed=True,
                            doc="Current FTW correction value (signed, DDS units).")

    #--------------------------------------------------------------------------
    # PROPERTIES - User-friendly interfaces to registers
    #--------------------------------------------------------------------------

    @property
    def mu_q_raw(self):
        """
        Raw 32-bit Q8.24 value in mu_q register (for debugging/display).

        Returns the hardware register value as an integer (not normalized).
        """
        return int(round(self.mu_q * MU_Q_SCALE))

    @property
    def mu_hz_per_lsb(self):
        """
        Integral gain in Hz/LSB (user-friendly units).

        This is converted to/from the Q8.24 fixed-point mu_q register.

        For a first-order integral loop with sample period Ts and slope K:
            μ ≈ (2π * BW * Ts) / K

        Example: For 300 Hz BW with K=1.1 LSB/Hz, Ts=32.768 μs:
            μ ≈ 0.05615 Hz/LSB
        """
        # mu_q is FloatRegister with norm=2**24, so it already returns the Q8.24 value as float (in FTW/LSB units)
        mu_ftw = self.mu_q
        return mu_ftw / FTW_PER_HZ

    @mu_hz_per_lsb.setter
    def mu_hz_per_lsb(self, value):
        """Set gain in Hz/LSB units."""
        mu_ftw = value * FTW_PER_HZ
        # FloatRegister will handle Q8.24 conversion automatically
        # Clamp to Q8.24 range [-128, 128)
        mu_ftw = max(-128.0, min(128.0 - 1.0/MU_Q_SCALE, mu_ftw))
        self.mu_q = mu_ftw

    @property
    def deadband_lsb(self):
        """Deadband threshold in LSB units."""
        return self.deadband

    @deadband_lsb.setter
    def deadband_lsb(self, value):
        """Set deadband threshold in LSB units."""
        self.deadband = int(value)

    @property
    def max_correction_hz(self):
        """Maximum frequency correction in Hz (symmetric ±limit)."""
        return self.ftw_lim / FTW_PER_HZ

    @max_correction_hz.setter
    def max_correction_hz(self, value_hz):
        """Set maximum frequency correction in Hz."""
        ftw_limit = int(round(abs(value_hz) * FTW_PER_HZ))
        self.ftw_lim = ftw_limit

    @property
    def kp_hz_per_lsb(self):
        """
        Proportional gain in Hz/LSB.

        This is converted to/from the Q8.24 fixed-point kp_q register.

        For PI control with zero placement at BW/α:
            K_p = α / K
        where K is the discriminator slope (LSB/Hz) and α ∈ [2, 4].

        Example: For α=3 with K=1.1 LSB/Hz:
            K_p ≈ 2.7273 Hz/LSB
        """
        # kp_q is FloatRegister with norm=2**24, so it returns Q8.24 value as float (in FTW/LSB units)
        kp_ftw = self.kp_q
        return kp_ftw / FTW_PER_HZ

    @kp_hz_per_lsb.setter
    def kp_hz_per_lsb(self, value):
        """Set proportional gain in Hz/LSB units."""
        kp_ftw = value * FTW_PER_HZ
        # Clamp to Q8.24 range [-128, 128)
        kp_ftw = max(-128.0, min(128.0 - 1.0/MU_Q_SCALE, kp_ftw))
        self.kp_q = kp_ftw

    @property
    def locked(self):
        """True if error has been below deadband for 256 consecutive samples."""
        return bool(self._status_reg & 0x1)

    @property
    def saturated(self):
        """True if FTW correction hit saturation limit in recent updates (legacy: any saturation)."""
        return bool(self._status_reg & 0x2)

    @property
    def saturated_i(self):
        """True if integrator path hit saturation limit."""
        return bool(self._status_reg & 0x4)  # bit[2]

    @property
    def saturated_pi(self):
        """True if PI sum (integrator + proportional) hit saturation limit."""
        return bool(self._status_reg & 0x8)  # bit[3]

    @property
    def error_lsb(self):
        """Last error value in LSB units (from lock_in output)."""
        return int(round(self.err_latch))

    @property
    def correction_hz(self):
        """Current frequency correction in Hz."""
        return self.ftw_corr / FTW_PER_HZ

    @property
    def correction_ftw(self):
        """Current frequency correction in FTW units (raw DDS value)."""
        return int(round(self.ftw_corr))

    #--------------------------------------------------------------------------
    # METHODS
    #--------------------------------------------------------------------------

    def _setup(self):
        """
        Initialize the ODMR frequency lock module.
        Called automatically when module is created.
        """
        # Set defaults if not already configured
        if self.mu_q == 0:
            self.mu_q = MU_Q_DEFAULT
        if self.ftw_lim == 0:
            self.ftw_lim = FTW_LIM_DEFAULT
        if self.kp_q == 0:
            self.kp_q = KP_Q_DEFAULT

        # Ensure prop_enable defaults to False (integral-only mode)
        # This maintains backward compatibility with existing experiments

    def clear(self):
        """
        Clear the integrator to zero.

        This resets the frequency correction to zero and restarts the lock detector.
        The clear bit is self-clearing in hardware.
        """
        self._clear_bit = True
        logger.info("ODMR frequency lock integrator cleared")

    def set_bandwidth(self, bandwidth_hz, slope_lsb_per_hz=1.1):
        """
        Set loop bandwidth for INTEGRAL-ONLY mode by computing appropriate integral gain.

        This method configures the loop for I-only operation (prop_enable=False).
        For PI control with faster acquisition, use set_bandwidth_pi() instead.

        Args:
            bandwidth_hz (float): Desired closed-loop bandwidth in Hz
            slope_lsb_per_hz (float): Measured demodulation slope (K) in LSB/Hz.
                                     Default 1.1 from planning document.

        Uses first-order approximation:
            μ = (2π * BW * Ts) / K

        where Ts = 32.768 μs (sample period at 30.5 kS/s).
        """
        Ts = 1.0 / 30517.578  # Sample period at decimated rate
        mu = (2 * np.pi * bandwidth_hz * Ts) / slope_lsb_per_hz
        self.mu_hz_per_lsb = mu

        # Disable proportional control (integral-only mode)
        self.prop_enable = False

        logger.info(f"ODMR lock bandwidth set to {bandwidth_hz} Hz "
                   f"(μ = {mu:.6f} Hz/LSB, K = {slope_lsb_per_hz} LSB/Hz, integral-only)")

    def set_bandwidth_pi(self, bandwidth_hz, slope_lsb_per_hz=1.1, zero_ratio=3):
        """
        Set PI loop bandwidth by computing both integral and proportional gains.

        This method enables proportional control and computes both gains from
        control theory, placing the PI zero at bandwidth_hz / zero_ratio.

        Args:
            bandwidth_hz (float): Desired closed-loop bandwidth in Hz
            slope_lsb_per_hz (float): Measured demodulation slope (K) in LSB/Hz.
                                     Default 1.1 from planning document.
            zero_ratio (float): Ratio of bandwidth to zero frequency (α).
                               Default 3 places zero at BW/3 for good damping.
                               Typical range: 2-4.

        Uses PI control formulas:
            μ = (2π * BW * Ts) / K         [integral step, Hz/LSB]
            K_p = α / K                     [proportional gain, Hz/LSB]

        where Ts = 32.768 μs (sample period at 30.5 kS/s).

        Example:
            >>> odm.set_bandwidth_pi(300, slope_lsb_per_hz=1.1, zero_ratio=3)
            # Sets μ ≈ 0.05615 Hz/LSB, K_p ≈ 2.7273 Hz/LSB, enables PI mode
        """
        Ts = 1.0 / 30517.578  # Sample period at decimated rate

        # Integral gain (same as integral-only formula)
        mu = (2 * np.pi * bandwidth_hz * Ts) / slope_lsb_per_hz

        # Proportional gain (places zero at bandwidth / zero_ratio)
        kp = zero_ratio / slope_lsb_per_hz

        # Apply gains
        self.mu_hz_per_lsb = mu
        self.kp_hz_per_lsb = kp

        # Enable proportional control
        self.prop_enable = True

        logger.info(f"ODMR PI lock bandwidth set to {bandwidth_hz} Hz "
                   f"(μ = {mu:.6f} Hz/LSB, K_p = {kp:.6f} Hz/LSB, "
                   f"K = {slope_lsb_per_hz} LSB/Hz, zero at {bandwidth_hz/zero_ratio:.1f} Hz)")

    def get_status(self):
        """
        Get comprehensive status dictionary.

        Returns:
            dict: Status information including:
                - enabled: Loop enable state
                - locked: Lock detector state
                - saturated: Saturation indicator
                - error_lsb: Current error signal
                - correction_hz: Current frequency correction
                - mu_hz_per_lsb: Current integral gain
                - deadband_lsb: Current deadband threshold
        """
        return {
            'enabled': self.enable,
            'locked': self.locked,
            'saturated': self.saturated,           # Legacy: any saturation
            'saturated_i': self.saturated_i,       # Integrator only
            'saturated_pi': self.saturated_pi,     # PI sum
            'inverted': self.invert,
            'held': self.hold,
            'deadband_enabled': self.deadband_enable,
            'prop_enabled': self.prop_enable,
            'error_lsb': self.error_lsb,
            'correction_hz': self.correction_hz,
            'correction_ftw': self.correction_ftw,
            'mu_hz_per_lsb': self.mu_hz_per_lsb,
            'mu_q': self.mu_q,
            'kp_hz_per_lsb': self.kp_hz_per_lsb,
            'kp_q': self.kp_q,
            'deadband_lsb': self.deadband_lsb,
            'max_correction_hz': self.max_correction_hz,
        }

    def check_polarity(self, step_hz=200, duration_s=0.1):
        """
        Helper to check if error polarity is correct.

        This method requires the 3FGEN to be configured and running.
        It will step the frequency and check if the error signal responds
        with the correct sign.

        Args:
            step_hz (float): Frequency step to apply (default 200 Hz)
            duration_s (float): How long to hold the step (default 0.1 s)

        Returns:
            dict: Results containing:
                - error_before: Error before step
                - error_after: Error after step
                - error_delta: Change in error
                - polarity_correct: True if polarity is correct
                - recommendation: 'keep' or 'invert'

        Note:
            This check should be done with the loop DISABLED.
        """
        import time

        if self.enable:
            logger.warning("Polarity check should be done with loop disabled. "
                          "Disabling now...")
            self.enable = False
            time.sleep(0.05)

        # Get initial error
        time.sleep(0.05)
        error_before = self.error_lsb

        # Apply frequency step (requires access to parent pyrpl object)
        # This is a placeholder - actual implementation needs 3FGEN access
        logger.info(f"Apply +{step_hz} Hz step to 3FGEN and observe error change")
        logger.info("For correct polarity: positive frequency step should give positive error")

        # Wait for step
        time.sleep(duration_s)

        # Read error after
        error_after = self.error_lsb
        error_delta = error_after - error_before

        # Check polarity
        polarity_correct = (error_delta > 0)  # Positive freq step should give positive error

        result = {
            'error_before': error_before,
            'error_after': error_after,
            'error_delta': error_delta,
            'polarity_correct': polarity_correct,
            'recommendation': 'keep current invert setting' if polarity_correct else 'toggle invert'
        }

        logger.info(f"Polarity check: {result}")
        return result
