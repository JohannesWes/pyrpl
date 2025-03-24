from ..modules import HardwareModule
from ..attributes import PWMRegister, IntRegister


class AMS(HardwareModule):
    """mostly deprecated module (redpitaya has removed adc support).
    only here for dac2 and dac3 and PWM frequency control"""
    addr_base = 0x40400000

    # attention: writing to dac0 and dac1 has no effect
    # only write to dac2 and 3 to set output voltages
    # to modify dac0 and dac1, connect a r.pwm0.input='pid0'
    # and let the pid module determine the voltage
    dac0 = PWMRegister(0x20, doc="PWM output 0 [V]")
    dac1 = PWMRegister(0x24, doc="PWM output 1 [V]")
    dac2 = PWMRegister(0x28, doc="PWM output 2 [V]")
    dac3 = PWMRegister(0x2C, doc="PWM output 3 [V]")

    # Add frequency control registers (32-bit)
    pwm0_freq_div = IntRegister(0x30, bits=16, doc="PWM0 frequency divider", default=1)
    pwm1_freq_div = IntRegister(0x34, bits=16, doc="PWM1 frequency divider", default=1)
    pwm2_freq_div = IntRegister(0x38, bits=16, doc="PWM2 frequency divider", default=1)
    pwm3_freq_div = IntRegister(0x3C, bits=16, doc="PWM3 frequency divider", default=1)

    pwm_mode = IntRegister(0x40, bits=4, doc="PWM mode (0=normal, 1=dithered)", default=0x0000)

    def _setup(self):  # the function is here for its docstring to be used by the metaclass.
        """
        sets up the AMS (just setting the attributes is OK)
        """
        pass

    def set_pwm_mode(self, channel, mode):
        """
        Set the PWM mode for the specified channel

        Parameters:
        -----------
        channel : int
            PWM channel (0-3)
        mode : str or int
            'normal' or 0: Direct PWM frequency
            'dithered' or 1: Duty cycle modulated over 16 cycles
        """
        if isinstance(mode, str):
            if mode.lower() == 'normal':
                mode_val = 0
            elif mode.lower() == 'dithered':
                mode_val = 1
            else:
                raise ValueError("Mode must be 'normal' or 'dithered'")
        else:
            mode_val = 1 if mode else 0

        # Store the current frequency before changing mode
        current_freq = self.get_pwm_frequency(channel)

        # Get current mode register value
        current_mode = self.pwm_mode

        # Modify only the bit for the specified channel
        if mode_val:
            new_mode = current_mode | (1 << channel)  # Set bit
        else:
            new_mode = current_mode & ~(1 << channel)  # Clear bit

        self.pwm_mode = new_mode
        self._logger.debug(f"Setting PWM{channel} mode to {'dithered' if mode_val else 'normal'}")

        # Recalculate and set the frequency to maintain the same output frequency
        if current_freq > 0:
            self.set_pwm_frequency(channel, current_freq)

    def get_pwm_mode(self, channel):
        """
        Get the PWM mode for the specified channel

        Parameters:
        -----------
        channel : int
            PWM channel (0-3)

        Returns:
        --------
        str
            'normal' or 'dithered'
        """
        mode_bit = (self.pwm_mode >> channel) & 0x1
        return 'dithered' if mode_bit else 'normal'

    def set_pwm_frequency(self, channel, freq_hz):
        """
        Set the PWM frequency for the specified channel

        Parameters:
        -----------
        channel : int
            PWM channel (0-3)
        freq_hz : float
            Frequency in Hz

        Notes:
        ------
        The base PWM clock runs at 125 MHz divided by 256 steps per cycle.
        In normal mode, there is a 4x frequency multiplication effect in the hardware.
        """
        if freq_hz <= 0:
            divider = 0xFFFFFFFF  # Essentially disable PWM
            self._logger.debug(f"Disabling PWM{channel}, as frequency is <= 0 Hz")
        else:
            # Check the current PWM mode
            mode = self.get_pwm_mode(channel)

            # Base PWM clock: 125 MHz
            # Each PWM cycle takes 256 steps
            base_freq = 125e6 / 256  # ~488 kHz for normal mode

            if mode == 'dithered':
                # Each full sequence takes 16 repetitions in dithered mode
                base_freq = base_freq / 16  # ~30.5 kHz
            else:
                # In normal mode, no dithering
                freq_hz = freq_hz / 2

            divider = int(round(base_freq / freq_hz))

            # Ensure divider is within valid range (32 bits)
            divider = max(1, min(divider, 0xFFFFFFFF))

        self._logger.debug(
            f"Setting PWM{channel} frequency to {freq_hz:.2f} Hz (hardware freq: {freq_hz * 4 if self.get_pwm_mode(channel) == 'normal' else freq_hz:.2f} Hz, divider={divider})")

        if channel == 0:
            self.pwm0_freq_div = divider
        elif channel == 1:
            self.pwm1_freq_div = divider
        elif channel == 2:
            self.pwm2_freq_div = divider
        elif channel == 3:
            self.pwm3_freq_div = divider
        else:
            raise ValueError(f"Invalid PWM channel: {channel}")

    def get_pwm_frequency(self, channel):
        """
        Get the current PWM frequency for the specified channel

        Parameters:
        -----------
        channel : int
            PWM channel (0-3)

        Returns:
        --------
        float
            Frequency in Hz (the actual output frequency)
        """
        # Check the current PWM mode
        mode = self.get_pwm_mode(channel)

        # Base frequency depends on mode
        base_freq = 125e6 / 256  # ~488 kHz for normal mode
        if mode == 'dithered':
            # Each full sequence takes 16 repetitions in dithered mode
            base_freq = base_freq / 16  # ~30.5 kHz

        if channel == 0:
            divider = self.pwm0_freq_div
        elif channel == 1:
            divider = self.pwm1_freq_div
        elif channel == 2:
            divider = self.pwm2_freq_div
        elif channel == 3:
            divider = self.pwm3_freq_div
        else:
            raise ValueError(f"Invalid PWM channel: {channel}")

        if divider == 0:
            return 0

        frequency = base_freq / divider

        # In normal mode, adjust reported frequency to match what was requested
        if mode == 'normal':
            frequency = frequency * 2

        return frequency

    def set_current_pwm(self, channel, current_ma, error_state=False):
        """
        Set the PWM output to represent a current value according to specifications:

        1. Current within ±250mA: 10 Hz, duty cycle 10-90% (50%=0mA)
        2. Current out of range: 20 Hz, duty cycle 10-90%
        3. Error state: 30 Hz

        Parameters:
        -----------
        channel : int
            PWM channel (0-3)
        current_ma : float
            Current in mA
        error_state : bool
            If True, set PWM to error mode (30 Hz)
        """
        # Set the frequency based on conditions
        if error_state:
            # Error mode: 30 Hz
            self.set_pwm_frequency(channel, 30)
            # Use 50% duty cycle in error state
            duty = 0.5
        elif abs(current_ma) <= 250:
            # Normal mode: 10 Hz
            self.set_pwm_frequency(channel, 10)
            # Map current to duty cycle: -250mA -> 10%, 0mA -> 50%, +250mA -> 90%
            duty = 0.5 + (current_ma / 500)
        else:
            # Out of range mode: 20 Hz
            self.set_pwm_frequency(channel, 20)
            # Clamp current to ±250mA for duty cycle calculation
            clamped_current = max(-250, min(current_ma, 250))
            duty = 0.5 + (clamped_current / 500)

        # Ensure duty cycle is within bounds
        duty = max(0.1, min(0.9, duty))

        # Set duty cycle by setting the voltage (PWM configuration)
        # Convert duty cycle (0-1) to voltage (0-1.8V)
        voltage = duty * 1.8

        self._logger.debug(f"Setting PWM{channel} duty cycle to {duty:.2f} (voltage={voltage:.2f}V)")

        # Set the appropriate channel
        if channel == 0:
            self.dac0 = voltage
        elif channel == 1:
            self.dac1 = voltage
        elif channel == 2:
            self.dac2 = voltage
        elif channel == 3:
            self.dac3 = voltage
        else:
            raise ValueError(f"Invalid PWM channel: {channel}")