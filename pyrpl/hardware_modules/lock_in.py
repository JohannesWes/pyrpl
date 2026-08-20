"""
Dual-channel lock-in demodulation module.

The lock-in module performs synchronous demodulation of an ADC input signal
with two reference signals. Each channel multiplies the input signal with
its selected reference, then performs CIC decimation (by 4096) and FIR
lowpass filtering to extract the DC component.

Each channel's reference signal can be independently selected from four options:
- sin: Normal sine
- cos: Normal cosine
- sin_shifted: Phase-shifted sine
- cos_shifted: Phase-shifted cosine

Typical usage for I/Q demodulation:
- Channel 1: ref_select1 = 'sin' (in-phase component)
- Channel 2: ref_select2 = 'cos' (quadrature component)
"""

from ..attributes import SelectRegister, BoolRegister
from ..modules import HardwareModule


class LockIn(HardwareModule):
    """
    Dual-channel lock-in demodulation module with independently selectable reference signals.

    This module demodulates the ADC input (channel A) with two independent reference signals.
    Both channels share the same data input but can use different references.
    Each output is independently decimated and lowpass filtered.
    """

    addr_base = 0x40700000 # Top-Level Region 7

    _setup_attributes = ['ref_select1', 'ref_select2', 'fir_bypass_ch1', 'fir_bypass_ch2',
                         'filter_select_ch1', 'filter_select_ch2',
                         'demod_bypass_ch1', 'demod_bypass_ch2']
    _gui_attributes = list(_setup_attributes)

    # Reference signal selection for channel 1 (bits 1:0 of address 0x000)
    ref_select1 = SelectRegister(0x000,
                                 bitmask=0x3,  # Bits 1:0
                                 options={'sin': 0,
                                         'cos': 1,
                                         'sin_shifted': 2,
                                         'cos_shifted': 3},
                                 default='sin',
                                 doc="Channel 1 reference signal selection.")

    # Reference signal selection for channel 2 (bits 3:2 of address 0x000)
    ref_select2 = SelectRegister(0x000,
                                 bitmask=0x3 << 2,  # Bits 3:2
                                 options={'sin': 0 << 2,
                                         'cos': 1 << 2,
                                         'sin_shifted': 2 << 2,
                                         'cos_shifted': 3 << 2},
                                 default='cos',
                                 doc="Channel 2 reference signal selection.")

    # FIR bypass control for channel 1 (bit 4 of address 0x000)
    fir_bypass_ch1 = BoolRegister(0x000,
                                  bit=4,
                                  default=False,
                                  doc="Bypass FIR lowpass filter for channel 1.")

    # FIR bypass control for channel 2 (bit 5 of address 0x000)
    fir_bypass_ch2 = BoolRegister(0x000,
                                  bit=5,
                                  default=False,
                                  doc="Bypass FIR lowpass filter for channel 2.")

    # Filter selection for channel 1 (bits 7:6 of address 0x000).
    # Raw value 0 is the default 2 kHz minimum-phase path. The legacy "2kHz"
    # spelling remains an alias for that filter.
    filter_select_ch1 = SelectRegister(0x000,
                                       bitmask=0x3 << 6,  # Bits 7:6
                                       options={'2kHz_minphase': 0 << 6,
                                                '2kHz': 0 << 6,
                                                '2kHz_linear': 1 << 6},
                                       default='2kHz_minphase',
                                       doc="Channel 1 CIC-compensated 2 kHz FIR selection. "
                                           "2kHz_linear has constant group delay; "
                                           "2kHz_minphase has the established low-latency phase response. "
                                           "2kHz is a compatibility alias for 2kHz_minphase.")

    # Filter selection for channel 2 (bits 9:8 of address 0x000)
    filter_select_ch2 = SelectRegister(0x000,
                                       bitmask=0x3 << 8,  # Bits 9:8
                                       options={'2kHz_minphase': 0 << 8,
                                                '2kHz': 0 << 8,
                                                '2kHz_linear': 1 << 8},
                                       default='2kHz_minphase',
                                       doc="Channel 2 CIC-compensated 2 kHz FIR selection. "
                                           "2kHz_linear has constant group delay; "
                                           "2kHz_minphase has the established low-latency phase response. "
                                           "2kHz is a compatibility alias for 2kHz_minphase.")

    # Demodulation bypass for channel 1 (bit 10 of address 0x000)
    demod_bypass_ch1 = BoolRegister(0x000,
                                     bit=10,
                                     default=False,
                                     doc="Bypass demodulation for channel 1"
                                         "When True: reference signal replaced by fixed constant, "
                                         "ADC passes through CIC/FIR as a lowpass decimation filter "
                                         "without frequency mixing. Same gain scaling as demodulated path. "
                                         "When False: normal lock-in demodulation with selected reference.")

    # Demodulation bypass for channel 2 (bit 11 of address 0x000)
    demod_bypass_ch2 = BoolRegister(0x000,
                                     bit=11,
                                     default=False,
                                     doc="Bypass demodulation for channel 2"
                                         "When True: reference signal replaced by fixed constant, "
                                         "ADC passes through CIC/FIR as a lowpass decimation filter "
                                         "without frequency mixing. Same gain scaling as demodulated path. "
                                         "When False: normal lock-in demodulation with selected reference.")

    def _setup(self):
        """
        Initialize the lock-in module with default settings.
        Called automatically when the module is created.
        """
        pass

    # TODO: possibly not used anymore / was used for old single resonance tracking mode. Check before removing
    def get_iq_reference_info(self, channel=1):
        """
        Get information about which IQ0 reference signal is currently selected
        and its frequency mode (1f or 2f) for a specific channel.

        Args:
            channel (int): Channel number (1 or 2). Default is 1.

        Returns:
            dict: Information about current reference selection for the specified channel

        Raises:
            ValueError: If channel is not 1 or 2
        """
        if channel not in [1, 2]:
            raise ValueError("Channel must be 1 or 2")

        # Get the reference selection for the specified channel
        ref_name = self.ref_select1 if channel == 1 else self.ref_select2

        # Get the IQ0 module to check its at_2f settings
        iq0 = self.pyrpl.rp.iq0

        # Determine frequency based on which reference is selected
        if ref_name == 'sin':
            at_2f = iq0._demodulation_sin_at_2f
        elif ref_name == 'cos':
            at_2f = iq0._demodulation_cos_at_2f
        elif ref_name == 'sin_shifted':
            at_2f = iq0._demodulation_sin_at_2f  # shifted follows sin flag
        elif ref_name == 'cos_shifted':
            at_2f = iq0._demodulation_cos_at_2f  # shifted follows cos flag
        else:
            at_2f = False

        return {
            'channel': channel,
            'reference': ref_name,
            'frequency_mode': '2f' if at_2f else '1f',
            'iq0_frequency': iq0.frequency,
            'demodulation_frequency': iq0.frequency * (2 if at_2f else 1)
        }


class LockIn1(LockIn):
    """Second lock-in instance. Identical to :class:`LockIn` but mapped to System Bus Region 10.
    """
    addr_base = 0x40A00000  # Top-Level Region 10
