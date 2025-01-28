import pyvisa as visa
import time

class Windfreak_MW_control:
    """
    A class to control a Windfreak microwave source via serial communication.

    This class provides methods for configuring the output mode (CW or sweep),
    setting frequencies and power levels, and controlling the output state.

    Attributes:
        serial_port (str): The serial port address (e.g., 'COM3').
        serial_timeout (int, optional): Timeout for serial communication in seconds (default: 10).

    """

    serial_timeout = 10

    def __init__(self, com):
        """
        Initializes the Windfreak_MW_control object.

        Args:
            com (str): The serial port address (e.g., 'COM3').
        """
        self.serial_port = com
        self._conn = None  # Initialize the connection object

    def activate(self):
        """
        Establishes a connection to the microwave source.
        Queries for basic instrument information upon successful connection.

        Raises:
            IOError: If there is an error connecting to the instrument.
        """
        try:
            self.rm = visa.ResourceManager()
            self._conn = self.rm.open_resource(
                self.serial_port,
                baud_rate=9600,
                read_termination='\n',
                write_termination='\n',
                timeout=self.serial_timeout * 1000
            )

            # Query instrument information (optional)
            self.model = self._conn.query('+').strip()  # Model number
            self.sernr = self._conn.query('-').strip()  # Serial number
            self.mod_hw = self._conn.query('v1').strip()  # Hardware version
            self.mod_fw = self._conn.query('v0').strip()  # Software version
            self.temperature = float(self._conn.query('z').strip())  # Temperature in Celsius

        except visa.VisaIOError as e:
            raise IOError(f"Error connecting to microwave source at {self.serial_port}: {e}")

    def deactivate(self):
        """
        Closes the connection to the microwave source.
        """
        if self._conn is not None:
            self._conn.close()
            self.rm.close()
            self._conn = None

    def _send_command(self, command, check_error=True):
        """
        Sends a command to the instrument and optionally checks for errors.

        Args:
            command (str): The command string to send.
            check_error (bool, optional): If True, checks the instrument's
                response for errors (default: True).

        Raises:
            IOError: If there is an error communicating with the instrument
                or if an error response is received.
        """
        try:
            self._conn.write(command)
            if check_error:
                # Implement error checking based on your instrument's response
                # For example:
                # error_code = int(self._conn.query('SYST:ERR?'))
                # if error_code != 0:
                #     raise IOError(f"Instrument error: {error_code}")
                pass  # Replace with actual error checking logic
        except visa.VisaIOError as e:
            raise IOError(f"Error communicating with microwave source: {e}")

    def off(self):
        """
        Turns the microwave output OFF.

        Raises:
            IOError: If there is an error communicating with the instrument.
        """
        self._send_command('g0')
        self._send_command('y0')
        self._send_command('E0h0')

    def _off(self):
        """Internal method to turn the RF output off."""
        self._send_command('E0h0')
        return self._stat()

    def _on(self):
        """Internal method to turn the RF output on."""
        self._send_command('E1h1')
        return self._stat()

    def _stat(self):
        """
        Gets the current output enable status of both channels.

        Returns:
            tuple: A tuple (E, h) where:
                E (int):  Channel 0 enable status (1: ON, 0: OFF).
                h (int):  Channel 1 enable status (1: ON, 0: OFF).
        """
        E = int(self._conn.query('E?'))
        h = int(self._conn.query('h?'))
        return E, h

    def cw_on(self):
        """
        Turns the microwave output ON in continuous wave (CW) mode.

        Raises:
            IOError: If there is an error communicating with the instrument.
        """
        self._on()
        self._send_command('g1')

    # ... (add parameter validation and error handling to the following methods)

    def set_cw(self, frequency=None, power=None):
        """
        Configures the device for CW mode and optionally sets frequency and/or power.

        Args:
            frequency (float, optional): Frequency to set in Hz.
            power (float, optional): Power to set in dBm.

        Returns:
            tuple: A tuple (frequency, power, mode), where:
                frequency (float): The set CW frequency in Hz.
                power (float): The set CW power in dBm.
                mode (str): The string "cw" indicating CW mode.

        Raises:
            ValueError: If the frequency or power is out of range.
            IOError: If there is an error communicating with the instrument.
        """
        self._send_command('X0')
        self._send_command('c1')
        self._send_command('y0')

        if frequency is not None:
            self._send_command(f'f{frequency/1e6:5.7f}')
            self._send_command(f'l{frequency/1e6:5.7f}')
            self._send_command(f'u{frequency/1e6:5.7f}')
        if power is not None:
            self._send_command(f'W{power:2.3f}')
            self._send_command(f'[{power:2.3f}')
            self._send_command(f']{power:2.3f}')

        self.mw_cw_freq = float(self._conn.query('f?')) * 1e6
        self.mw_cw_power = float(self._conn.query('W?'))
        return self.mw_cw_freq, self.mw_cw_power, 'cw'

    def sweep_on(self):
        """
        Turns ON the sweep mode.

        Returns:
            int: 0 if successful.

        Raises:
            IOError: If there is an error communicating with the instrument.
        """
        self._on()
        self._send_command('g1')
        return 0

    def set_sweep(self, start=None, stop=None, step=None, power=None):
        """
        Configures the device for sweep-mode and optionally sets frequency start/stop/step
        and/or power.

        Returns:
            tuple: A tuple (start_frequency, stop_frequency, step_frequency, power, 'sweep'), where:
                start_frequency (float): The set sweep start frequency in Hz.
                stop_frequency (float): The set sweep stop frequency in Hz.
                step_frequency (float): The set sweep step frequency in Hz.
                power (float): The set sweep power in dBm.
                'sweep' (str): A string indicating sweep mode.

        Raises:
            ValueError: If any of the input parameters are out of range.
            IOError: If there is an error communicating with the instrument.
        """
        if (start is not None) and (stop is not None) and (step is not None):
            self._send_command('X0')
            self._send_command('c1')
            self._send_command('y2')
            self._send_command('t37.5')
            if stop >= start:
                self._send_command('^1')
            else:
                self._send_command('^0')
            self._send_command(f'l{start/1e6:5.7f}')
            self._send_command(f'u{stop/1e6:5.7f}')
            self._send_command(f's{step/1e6:5.7f}')
        if power is not None:
            self._send_command(f'W{power:2.3f}')
            self._send_command(f'[{power:2.3f}')
            self._send_command(f']{power:2.3f}')

        mw_start_freq = float(self._conn.query('l?')) * 1e6
        mw_stop_freq = float(self._conn.query('u?')) * 1e6
        mw_step_freq = float(self._conn.query('s?')) * 1e6
        mw_power = float(self._conn.query('W?'))
        return mw_start_freq, mw_stop_freq, mw_step_freq, mw_power, 'sweep'

    def reset_sweeppos(self):
        """
        Resets the MW sweep mode position to the start frequency.

        Returns:
            int: 0 if successful.

        Raises:
            IOError: If there is an error communicating with the instrument.
        """
        self._send_command('g1')
        return 0

    def set_ext_trigger(self, dwelltime):
        """
        Sets the external trigger for this device.

        Args:
            dwelltime (float): Minimum dwell time.

        Returns:
            float: The set dwell time.

        Raises:
            ValueError: If the dwell time is out of range.
            IOError: If there is an error communicating with the instrument.
        """
        self._send_command(f't{0.75 * dwelltime:f}')
        newtime = float(self._conn.query('t?'))
        return newtime

    def trigger(self):
        """
        Triggers the next element in the list or sweep mode programmatically.
        This method is currently not implemented.

        Raises:
            NotImplementedError: This method is not yet implemented.
        """
        raise NotImplementedError("The 'trigger' method is not yet implemented.")

    def start_MW(self, MW_freq, MW_pwr):
        """
        Starts the microwave output with the specified frequency and power in CW mode.

        Args:
            MW_freq (float): Microwave frequency in Hz.
            MW_pwr (float): Microwave power in dBm.

        Returns:
            None

        Raises:
            ValueError: If the frequency or power is out of range.
            IOError: If there is an error communicating with the instrument.
        """
        self.activate()
        print('MW Settings:')
        print(self.set_cw(frequency=MW_freq, power=MW_pwr))
        print('Power up LO...')
        self.cw_on()
        print("Microwave source started successfully!")

    def end_MW(self):
        """
        Stops the microwave output and deactivates the device.

        Returns:
            None

        Raises:
            IOError: If there is an error communicating with the instrument.
        """
        print('\nShutting down MW...')
        self.off()
        print('Deactivating MW...')
        self.deactivate()
        print("Microwave source shut down successfully!")