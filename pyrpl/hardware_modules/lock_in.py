"""
Dual-channel lock-in demodulation module.

The lock-in module performs synchronous demodulation of an ADC input signal
with two independent reference signals from the IQ0 module. Each channel
multiplies the shared input signal with its selected reference, then performs
CIC decimation (by 4096) and FIR lowpass filtering to extract the DC component.

Each channel's reference signal can be independently selected from four options:
- sin: Normal sine from IQ0
- cos: Normal cosine from IQ0
- sin_shifted: Phase-shifted sine from IQ0
- cos_shifted: Phase-shifted cosine from IQ0

The 1f vs 2f frequency is controlled separately via IQ0's demodulation_at_2f flags.

Typical usage for I/Q demodulation:
- Channel 1: ref_select1 = 'sin' (in-phase component)
- Channel 2: ref_select2 = 'cos' (quadrature component)
"""

from ..attributes import SelectRegister, BoolRegister, IntRegister
from ..modules import HardwareModule


class LockIn(HardwareModule):
    """
    Dual-channel lock-in demodulation module with independently selectable reference signals.

    This module demodulates the ADC input (channel A) with two independent reference signals
    from the IQ0 module. Both channels share the same input but can use different references.
    Each output is independently decimated and lowpass filtered.
    """

    addr_base = 0x40700000

    _setup_attributes = ['ref_select1', 'ref_select2', 'fir_bypass_ch1', 'fir_bypass_ch2', '_filter_select_ch1_reg', '_filter_select_ch2_reg']
    _gui_attributes = ['ref_select1', 'ref_select2', 'fir_bypass_ch1', 'fir_bypass_ch2', '_filter_select_ch1_reg', '_filter_select_ch2_reg', 'overflow_ch1', 'overflow_ch2']

    # ========================================================================
    # CONTROL REGISTER (0x000)
    # ========================================================================
    
    # Reference signal selection for channel 1 (bits 1:0 of address 0x000)
    ref_select1 = SelectRegister(0x000,
                                 bitmask=0x3,  # Bits 1:0
                                 options={'sin': 0,
                                         'cos': 1,
                                         'sin_shifted': 2,
                                         'cos_shifted': 3},
                                 default='sin',
                                 doc="Channel 1 reference signal selection from IQ0 module. "
                                     "Choose which IQ0 output to use for demodulation. "
                                     "Use IQ0's demodulation_at_2f flags to control 1f vs 2f.")

    # Reference signal selection for channel 2 (bits 3:2 of address 0x000)
    ref_select2 = SelectRegister(0x000,
                                 bitmask=0x3 << 2,  # Bits 3:2
                                 options={'sin': 0 << 2,
                                         'cos': 1 << 2,
                                         'sin_shifted': 2 << 2,
                                         'cos_shifted': 3 << 2},
                                 default='cos',
                                 doc="Channel 2 reference signal selection from IQ0 module. "
                                     "Choose which IQ0 output to use for demodulation. "
                                     "Use IQ0's demodulation_at_2f flags to control 1f vs 2f.")

    # FIR bypass control for channel 1 (bit 4 of address 0x000)
    fir_bypass_ch1 = BoolRegister(0x000,
                                  bit=4,
                                  default=False,
                                  doc="Bypass FIR lowpass filter for channel 1. "
                                      "When True: bandwidth ~15 kHz (CIC only), latency ~160 µs. "
                                      "When False: bandwidth determined by filter_select_ch1 (CIC+FIR). "
                                      "Bypassing the FIR reduces latency for fast control loops.")

    # FIR bypass control for channel 2 (bit 5 of address 0x000)
    fir_bypass_ch2 = BoolRegister(0x000,
                                  bit=5,
                                  default=False,
                                  doc="Bypass FIR lowpass filter for channel 2. "
                                      "When True: bandwidth ~15 kHz (CIC only), latency ~160 µs. "
                                      "When False: bandwidth determined by filter_select_ch2 (CIC+FIR). "
                                      "Bypassing the FIR reduces latency for fast control loops.")

    # Filter selection for channel 1 (bits 7:6 of address 0x000)
    _filter_select_ch1_reg = SelectRegister(0x000,
                                       bitmask=0x3 << 6,  # Bits 7:6
                                       options={'500Hz': 0 << 6,
                                                '2kHz': 1 << 6,
                                                '5kHz': 2 << 6,
                                                '1kHz': 3 << 6},
                                       default='2kHz',
                                       doc="Channel 1 lowpass filter selection (active when fir_bypass_ch1 is False). "
                                           "500Hz: Bandwidth 500 Hz (CIC+FIR), latency ~9 ms. "
                                           "2kHz: Bandwidth 2 kHz (CIC+FIR). "
                                           "5kHz: Bandwidth 5 kHz (CIC+FIR). "
                                           "1kHz: Bandwidth 1 kHz (CIC+IIR).")

    # Filter selection for channel 2 (bits 9:8 of address 0x000)
    _filter_select_ch2_reg = SelectRegister(0x000,
                                       bitmask=0x3 << 8,  # Bits 9:8
                                       options={'500Hz': 0 << 8,
                                                '2kHz': 1 << 8,
                                                '5kHz': 2 << 8,
                                                '1kHz': 3 << 8},
                                       default='2kHz',
                                       doc="Channel 2 lowpass filter selection (active when fir_bypass_ch2 is False). "
                                           "500Hz: Bandwidth 500 Hz (CIC+FIR), latency ~9 ms. "
                                           "2kHz: Bandwidth 2 kHz (CIC+FIR). "
                                           "5kHz: Bandwidth 5 kHz (CIC+FIR). "
                                           "1kHz: Bandwidth 1 kHz (CIC+IIR).")
    
    @property
    def filter_select_ch1(self):
        """Get current filter selection for channel 1."""
        return self._filter_select_ch1_reg
    
    @filter_select_ch1.setter
    def filter_select_ch1(self, value):
        """Set filter selection for channel 1 and check overflow for IIR."""
        import time
        import threading
        
        # Clear overflow before switching
        self.clear_overflow('ch1')
        # Set the new filter
        self._filter_select_ch1_reg = value
        
        if value == '1kHz':
            print(f"Channel 1: IIR 1kHz filter selected")
            # Check overflow after filter settles (in background to not block)
            def check_after_delay():
                time.sleep(0.5)  # Wait for filter to process some data
                if self.overflow_ch1:
                    print(f"\n⚠️  IIR CH1 OVERFLOW: {self.overflow_count_ch1} events!")
                    print(f"    Reduce input gain or check signal levels.\n")
                else:
                    print(f"✅ Channel 1 IIR filter: No overflow detected")
            threading.Thread(target=check_after_delay, daemon=True).start()
    
    @property
    def filter_select_ch2(self):
        """Get current filter selection for channel 2."""
        return self._filter_select_ch2_reg
    
    @filter_select_ch2.setter
    def filter_select_ch2(self, value):
        """Set filter selection for channel 2 and check overflow for IIR."""
        import time
        import threading
        
        # Clear overflow before switching
        self.clear_overflow('ch2')
        # Set the new filter
        self._filter_select_ch2_reg = value
        
        if value == '1kHz':
            print(f"Channel 2: IIR 1kHz filter selected")
            # Check overflow after filter settles (in background to not block)
            def check_after_delay():
                time.sleep(0.5)  # Wait for filter to process some data
                if self.overflow_ch2:
                    print(f"\n⚠️  IIR CH2 OVERFLOW: {self.overflow_count_ch2} events!")
                    print(f"    Reduce input gain or check signal levels.\n")
                else:
                    print(f"✅ Channel 2 IIR filter: No overflow detected")
            threading.Thread(target=check_after_delay, daemon=True).start()

    # ========================================================================
    # OVERFLOW STATUS REGISTERS (0x004, 0x008)
    # ========================================================================
    
    # Overflow count register (0x004) - read-only
    # bits 15:0 = ch1 overflow count, bits 31:16 = ch2 overflow count
    _overflow_count_raw = IntRegister(0x004,
                                      doc="Raw overflow count register (internal use)")
    
    # Overflow flags register (0x008) - read-only
    # bit 0 = ch1 sticky flag, bit 1 = ch2 sticky flag
    overflow_ch1 = BoolRegister(0x008,
                                bit=0,
                                doc="IIR filter channel 1 overflow sticky flag. "
                                    "True if overflow occurred since last clear.")
    
    overflow_ch2 = BoolRegister(0x008,
                                bit=1,
                                doc="IIR filter channel 2 overflow sticky flag. "
                                    "True if overflow occurred since last clear.")

    def _setup(self):
        """
        Initialize the lock-in module with default settings.
        Called automatically when the module is created.
        """
        # Clear any previous overflow flags on startup
        self.clear_overflow('both')
        # Enable automatic overflow monitoring by default
        self._auto_monitor_overflow = True
        # Start background monitoring thread
        self._start_overflow_monitor()
        
    # ========================================================================
    # AUTOMATIC OVERFLOW MONITORING
    # ========================================================================
    
    _auto_monitor_overflow = True  # Class-level default
    _overflow_monitor_thread = None
    _overflow_monitor_running = False
    _overflow_check_interval = 2.0  # Check every 2 seconds
    
    def _start_overflow_monitor(self):
        """Start the background overflow monitoring thread."""
        import threading
        
        if self._overflow_monitor_thread is not None and self._overflow_monitor_thread.is_alive():
            return  # Already running
        
        self._overflow_monitor_running = True
        self._overflow_monitor_thread = threading.Thread(
            target=self._overflow_monitor_loop,
            daemon=True,  # Thread will stop when main program exits
            name="LockIn_OverflowMonitor"
        )
        self._overflow_monitor_thread.start()
    
    def _stop_overflow_monitor(self):
        """Stop the background overflow monitoring thread."""
        self._overflow_monitor_running = False
        if self._overflow_monitor_thread is not None:
            self._overflow_monitor_thread.join(timeout=1.0)
            self._overflow_monitor_thread = None
    
    def _overflow_monitor_loop(self):
        """Background thread loop that checks overflow periodically."""
        import time
        
        while self._overflow_monitor_running:
            try:
                if self._auto_monitor_overflow:
                    self._check_and_warn_overflow()
            except Exception:
                pass  # Ignore errors in background thread
            time.sleep(self._overflow_check_interval)
    
    @property
    def auto_monitor_overflow(self):
        """
        Enable/disable automatic overflow monitoring.
        When True, overflow warnings are printed automatically when detected.
        """
        return getattr(self, '_auto_monitor_overflow', True)
    
    @auto_monitor_overflow.setter
    def auto_monitor_overflow(self, value):
        self._auto_monitor_overflow = bool(value)
    
    def _check_and_warn_overflow(self):
        """
        Internal method to check overflow and print warning if detected.
        Called automatically by background monitor thread.
        Clears overflow after warning to avoid repeated warnings.
        """
        if not self.auto_monitor_overflow:
            return False
            
        ch1_overflow = self.overflow_ch1
        ch2_overflow = self.overflow_ch2
        
        if ch1_overflow or ch2_overflow:
            ch1_count = self.overflow_count_ch1
            ch2_count = self.overflow_count_ch2
            print(f"\n⚠️  IIR OVERFLOW WARNING!")
            if ch1_overflow:
                print(f"    Channel 1: {ch1_count} overflow events")
            if ch2_overflow:
                print(f"    Channel 2: {ch2_count} overflow events")
            print(f"    Consider reducing input gain or checking signal levels.\n")
            # Auto-clear to avoid repeated warnings for same overflow
            self.clear_overflow('both')
            return True
        return False
    
    def monitor_overflow_continuous(self, duration_sec=5, interval_sec=0.5):
        """
        Monitor overflow status continuously for a specified duration.
        Useful for checking overflow during a measurement.
        
        Args:
            duration_sec (float): Total monitoring duration in seconds
            interval_sec (float): Check interval in seconds
        """
        import time
        
        print(f"Monitoring IIR overflow for {duration_sec} seconds...")
        self.clear_overflow('both')
        
        start_time = time.time()
        max_ch1 = 0
        max_ch2 = 0
        
        while (time.time() - start_time) < duration_sec:
            time.sleep(interval_sec)
            ch1_count = self.overflow_count_ch1
            ch2_count = self.overflow_count_ch2
            max_ch1 = max(max_ch1, ch1_count)
            max_ch2 = max(max_ch2, ch2_count)
            
            if ch1_count > 0 or ch2_count > 0:
                elapsed = time.time() - start_time
                print(f"  [{elapsed:.1f}s] Overflow - Ch1: {ch1_count}, Ch2: {ch2_count}")
        
        print(f"Monitoring complete.")
        print(f"  Peak overflow count - Ch1: {max_ch1}, Ch2: {max_ch2}")
        
        if max_ch1 == 0 and max_ch2 == 0:
            print(f"  ✅ No overflow detected!")
        else:
            print(f"  ⚠️  Overflow detected during measurement!")
        
        return {'ch1_max': max_ch1, 'ch2_max': max_ch2}

    # ========================================================================
    # OVERFLOW MONITORING METHODS
    # ========================================================================
    
    @property
    def overflow_count_ch1(self):
        """
        Get the number of overflow events on channel 1 since last clear.
        
        Returns:
            int: Overflow count (0-65535, saturates at max)
        """
        return self._overflow_count_raw & 0xFFFF
    
    @property
    def overflow_count_ch2(self):
        """
        Get the number of overflow events on channel 2 since last clear.
        
        Returns:
            int: Overflow count (0-65535, saturates at max)
        """
        return (self._overflow_count_raw >> 16) & 0xFFFF
    
    def clear_overflow(self, channel='both'):
        """
        Clear the overflow sticky flag and counter for specified channel(s).
        
        Args:
            channel: 'ch1', 'ch2', or 'both' (default)
        """
        if channel == 'ch1':
            self._write(0x004, 0x01)  # Clear ch1 only
        elif channel == 'ch2':
            self._write(0x004, 0x02)  # Clear ch2 only
        else:  # 'both'
            self._write(0x004, 0x03)  # Clear both channels
    
    def get_overflow_status(self):
        """
        Get complete overflow status for both channels.
        
        Returns:
            dict: Overflow status with keys:
                - 'ch1_overflow': bool - True if ch1 has overflowed
                - 'ch2_overflow': bool - True if ch2 has overflowed  
                - 'ch1_count': int - Number of ch1 overflow events
                - 'ch2_count': int - Number of ch2 overflow events
        """
        return {
            'ch1_overflow': self.overflow_ch1,
            'ch2_overflow': self.overflow_ch2,
            'ch1_count': self.overflow_count_ch1,
            'ch2_count': self.overflow_count_ch2
        }
    
    def check_overflow(self, clear_after=False):
        """
        Check if any overflow has occurred and optionally clear flags.
        
        Args:
            clear_after (bool): If True, clear overflow flags after reading
            
        Returns:
            bool: True if any channel has overflowed
        """
        status = self.get_overflow_status()
        had_overflow = status['ch1_overflow'] or status['ch2_overflow']
        
        if had_overflow:
            print(f"IIR Overflow detected!")
            print(f"  Channel 1: {status['ch1_count']} events")
            print(f"  Channel 2: {status['ch2_count']} events")
        
        if clear_after:
            self.clear_overflow()
            
        return had_overflow

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
