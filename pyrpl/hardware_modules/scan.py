# -*- coding: utf-8 -*-
"""
Scan Module for Pyrpl.

This module controls the FPGA scan block, enabling automated sweeps.
For each step:
1. Outputs a trigger pulse on a *fixed* digital output pin (exp_p_io[7]).
2. Waits for a settling time.
3. Acquires and accumulates data from a *fixed* input source (adc_a)
   for a dwell time.
4. Stores the 64-bit accumulated sum in BRAM.

The accumulated data can then be read back and averaged in Python.

**Important Limitations (Current FPGA Design):**
*   **Input Source:** The input signal for the scan is currently HARDWIRED
    to 'adc_a' in the FPGA design (`red_pitaya_top.v`). Changing the
    `input_select` attribute in this Python module will NOT change the
    physical input source without modifying and recompiling the FPGA design.
*   **Trigger Output Pin:** The trigger output pulse is currently HARDWIRED
    to the most significant bit pin of the expansion connector ('exp_p_io[7]')
    in the FPGA design (`red_pitaya_hk.v`). Changing the
    `trigger_pin_select` attribute will write to the corresponding FPGA register,
    but it will NOT change the physical output pin without modifications
    to the FPGA design (`red_pitaya_hk.v`).

"""
import time
import numpy as np
import logging

from ..modules import HardwareModule
from ..widgets.module_widgets.scan_widget import ScanWidget
from ..attributes import (IntRegister, FloatProperty, BoolRegister,
                          SelectRegister, BaseRegister)
# from ..dsp import InputSelectRegister # Cannot use this due to FPGA limitation
# from ..pyrpl_utils import time

logger = logging.getLogger(__name__)

# Define constants based on scan_new.v
MAX_STEPS_BITS = 12
DATA_WIDTH = 14
ACCUM_WIDTH = 64
BUS_DATA_WIDTH = 32
FPGA_CLK_PERIOD_S = 8e-9 # 1/125MHz

# Address Map (relative to module base)
ADDR_CONTROL = 0x00
ADDR_STATUS = 0x00 # Same as control, different bits
ADDR_NUM_STEPS = 0x04
ADDR_DWELL_TIME = 0x08
ADDR_SETTLING_TIME = 0x0C
ADDR_TRIGGER_LENGTH = 0x10
ADDR_TRIGGER_PIN_SEL = 0x14
ADDR_CURRENT_STEP = 0x18
# ADDR_INPUT_SELECT = 0x20 # Not implemented in scan_new.v
BRAM_LSB_BASE_ADDR = 0x10000 # As per Verilog: Module Base + 0x10000
BRAM_MSB_BASE_ADDR = 0x20000 # As per Verilog: Module Base + 0x20000

# Control Register Bits
CONTROL_START_BIT = 0
CONTROL_STOP_BIT = 1
CONTROL_RESET_BIT = 2

# Status Register Bits
STATUS_BUSY_BIT = 0
STATUS_DONE_BIT = 1


class CyclesProperty(FloatProperty):
    """Converts time in seconds to FPGA clock cycles."""
    def from_python(self, obj, value_s):
        # Convert seconds to integer number of cycles
        cycles = int(round(float(value_s) / FPGA_CLK_PERIOD_S))
        # Ensure minimum of 1 cycle if time > 0, otherwise 0
        if value_s > 0 and cycles == 0:
            cycles = 1
        elif value_s <= 0:
            cycles = 0
        # Clamp to 32-bit unsigned integer max
        if cycles > (2**32 - 1):
            cycles = 2**32 - 1
            logger.warning("%s: Requested time %f s exceeds maximum representable "
                           "cycles. Clamping to maximum.", self.name, value_s)
        return cycles

    def to_python(self, obj, cycles):
        # Convert cycles back to seconds
        return float(cycles) * FPGA_CLK_PERIOD_S

    def __init__(self, register_address, **kwargs):
        self.register_address = register_address
        # Initialize FloatProperty with reasonable bounds, doc comes from Reg below
        super(CyclesProperty, self).__init__(min=0,
                                              max=(2**32 - 1) * FPGA_CLK_PERIOD_S,
                                              **kwargs)

    # We need to override __get__ and __set__ to interact with the IntRegister
    def __get__(self, instance, owner):
        if instance is None:
            return self
        cycles = getattr(instance, f"_{self.name}_cycles")
        return self.to_python(instance, cycles)

    def __set__(self, instance, value_s):
        # validate and clamp the user-provided float value.
        validated_value_s = self.validate_and_normalize(instance, value_s)  # Uses parent clamping
        if value_s != validated_value_s:
            logger.warning("%s: Requested value %f s is out of bounds [%f, %f]. Clamping to %f s.",
                           self.name, value_s, self.min, self.max, validated_value_s)

        # convert the validated value to clock cycles.
        cycles = self.from_python(instance, validated_value_s)

        # set the value of the underlying IntRegister - automatically updates value on FPGA
        setattr(instance, f"_{self.name}_cycles", cycles)


class Scan(HardwareModule):
    """
    Pyrpl module for controlling the FPGA Scan block.

    Performs automated sweeps, triggering an external device and accumulating
    input data at each step.
    """
    _widget_class = ScanWidget
    addr_base = 0x40500000 # Corresponds to system bus port 5
    name = 'scan'
    #_widget_class = ScanWidget # TODO: Create a widget later if needed

    _setup_attributes = ["num_steps", "dwell_time", "settling_time",
                         "trigger_length", "trigger_pin_select"] # input_select removed

    # Status Registers (Read-Only)
    busy = BoolRegister(ADDR_STATUS, bit=STATUS_BUSY_BIT,
                        doc="Read-only: True if a sweep is currently running.")

    done = BoolRegister(ADDR_STATUS, bit=STATUS_DONE_BIT,
                        doc="Read-only: True if a sweep has finished.")

    current_step = IntRegister(ADDR_CURRENT_STEP, bits=MAX_STEPS_BITS,
                               doc="Read-only: The index of the step currently "
                                   "being processed (or the last step processed).")

    # Configuration Registers (Read/Write)
    num_steps = IntRegister(ADDR_NUM_STEPS, bits=MAX_STEPS_BITS, min=1, max=2**MAX_STEPS_BITS,
                            doc="Number of steps in the sweep (1 to {}).".format(2**MAX_STEPS_BITS))

    # Use helper FloatProperties that manage conversion to/from cycles for the user
    # The actual storage is in the IntRegister attributes below (_dwell_time_cycles etc.)
    dwell_time = CyclesProperty(ADDR_DWELL_TIME,
                                doc="Duration to acquire data at each step [s].")
    _dwell_time_cycles = IntRegister(ADDR_DWELL_TIME, bits=32, min=0,
                                     doc="Internal: Duration to acquire data at each step [cycles].")

    settling_time = CyclesProperty(ADDR_SETTLING_TIME,
                                   doc="Delay after trigger before acquisition starts [s].")
    _settling_time_cycles = IntRegister(ADDR_SETTLING_TIME, bits=32, min=0,
                                        doc="Internal: Delay after trigger before acquisition starts [cycles].")

    trigger_length = CyclesProperty(ADDR_TRIGGER_LENGTH,
                                    doc="Duration of the trigger pulse [s].")
    _trigger_length_cycles = IntRegister(ADDR_TRIGGER_LENGTH, bits=32, min=0,
                                         doc="Internal: Duration of the trigger pulse [cycles].")

    # Trigger Pin Selection
    _trigger_pin_options = {f"DOUT{i}": i for i in range(8)} # Map names to values 0-7
    trigger_pin_select = SelectRegister(ADDR_TRIGGER_PIN_SEL, options=_trigger_pin_options,
                                        default="DOUT7", # Default matches FPGA hardwiring
                                        doc="Selects trigger output pin (0-7). "
                                            "WARNING: Currently hardwired to DOUT7 (exp_p_io[7]) in FPGA!")

    # Input Selection - Not implemented in FPGA, commented out
    # input_select = InputSelectRegister(ADDR_INPUT_SELECT, # Address doesn't exist
    #                                    doc="Selects the input signal source. "
    #                                        "WARNING: Currently hardwired to 'adc_a' in FPGA!")


    # --- Control Methods ---
    def _write_control_bit(self, bit_position, value):
        """
        Write a self-clearing command bit to the control register.

        The control register (0x00000) uses a dual-function pattern where writes
        trigger one-cycle command pulses (start/stop/reset) that auto-clear, while
        reads return status flags (busy/done). This requires special handling since
        standard IntRegister assumes persistent read/write values.
        """
        if value:
            control_val = 1 << bit_position
        else:
            # Writing 0 usually has no effect for command bits, but safer to be explicit
            # However, writing 0 might interfere if other commands are active?
            # Let's just write the single bit assert command.
             control_val = 1 << bit_position
        self._write(ADDR_CONTROL, control_val)

    def start(self):
        """
        Starts the sweep sequence.

        Checks for potential accumulator overflow before starting.
        """
        if self.busy:
            logger.warning("Scan module is already busy. Ignoring start command.")
            return

        # If we're in DONE state, reset first to ensure clean start
        if self.done:
            self.reset()

        self._check_overflow()
        logger.info("Starting scan sweep...")
        self._write_control_bit(CONTROL_START_BIT, True)

    def stop(self):
        """
        Stops the current sweep sequence immediately.
        The 'done' flag will not be set. Data might be incomplete.
        """
        logger.info("Stopping scan sweep...")
        self._write_control_bit(CONTROL_STOP_BIT, True)

    def reset(self):
        """
        Resets the scan module state machine and clears flags.
        """
        logger.info("Resetting scan module...")
        self._write_control_bit(CONTROL_RESET_BIT, True)
        # Give FPGA time to process reset
        time.sleep(0.01)


    # --- Data Retrieval ---
    def wait_done(self, timeout=10.0, poll_interval=0.1):
        """
        Waits until the sweep is finished or timeout occurs.

        Args:
            timeout (float): Maximum time to wait in seconds.
            poll_interval (float): Time between status checks in seconds.

        Returns:
            bool: True if the sweep finished, False if timed out.
        """
        start_time = time.time()
        while not self.done:
            if self.busy: # Only log waiting message if it's actually running
                logger.debug("Scan sweep running, step %d / %d. Waiting...",
                            self.current_step, self.num_steps)
            if time.time() - start_time > timeout:
                logger.error("Timeout waiting for scan sweep to finish.")
                return False
            time.sleep(poll_interval)
        logger.info("Scan sweep finished.")
        return True

    def get_data(self, average=True):
        """
        Reads the accumulated data from the FPGA BRAM after a sweep.

        This method uses two efficient block-reads to retrieve the LSB and MSB
        data banks separately.

        Args:
            average (bool): If True (default), divides the accumulated sums
                            by the dwell time in cycles to return averages.
                            If False, returns the raw 64-bit accumulated sums.

        Returns:
            np.ndarray: A numpy array containing the (averaged) data for each step.
                        The dtype is float64 if average=True, otherwise int64.

        Raises:
            RuntimeError: If the sweep is still busy, hasn't finished correctly,
                          or if there is a data readout error.
        """
        if self.busy:
            raise RuntimeError("Scan module is busy. Cannot read data.")
        if not self.done:
             # Check if it was stopped prematurely vs never started
            if self.current_step > 0:
                 logger.warning("Sweep was stopped before completion or did not run. "
                                "Reading potentially incomplete data.")
            else:
                 raise RuntimeError("Sweep has not finished or was reset. "
                                   "Run a sweep before getting data.")

        n_steps = self.num_steps
        dwell_cycles = self._dwell_time_cycles # Read the cycle count

        if n_steps <= 0:
            logger.warning("Number of steps is zero or invalid. Returning empty array.")
            return np.array([], dtype=np.int64 if not average else np.float64)

        if average and dwell_cycles <= 0:
            logger.error("Cannot average data: dwell_time is zero cycles. "
                         "Returning raw accumulated data.")
            average = False # Force return of raw data

        logger.info(f"Reading {n_steps} data points from FPGA BRAM using block reads...")

        try:
            # The _reads method in the parent HardwareModule automatically adds the
            # module's base address (self.addr_base). Thus only provide relative offset here.
            lsb_data = self._reads(BRAM_LSB_BASE_ADDR, n_steps)
            msb_data = self._reads(BRAM_MSB_BASE_ADDR, n_steps)

            # --- Validation of received data ---
            if lsb_data is None or len(lsb_data) != n_steps:
                raise RuntimeError(f"Block read from LSB BRAM failed or returned incorrect length. "
                                   f"Expected {n_steps}, got {len(lsb_data) if lsb_data is not None else 'None'}.")
            if msb_data is None or len(msb_data) != n_steps:
                raise RuntimeError(f"Block read from MSB BRAM failed or returned incorrect length. "
                                   f"Expected {n_steps}, got {len(msb_data) if msb_data is not None else 'None'}.")

            # --- Combine LSB and MSB into a 64-bit integer array ---
            data_accum = (msb_data.astype(np.int64) << 32) | lsb_data.astype(np.int64)

        except Exception as e:
            logger.error(f"Error during block read from BRAM: {e}")
            raise RuntimeError(f"Failed to read data from BRAM. Original error: {e}")

        logger.info("Data readout complete.")

        if average:
            logger.info(f"Averaging data by dividing by dwell_time = {dwell_cycles} cycles.")
            # Convert to float for division
            avg_data = data_accum.astype(np.float64) / float(dwell_cycles)
            return avg_data
        else:
            return data_accum


    # --- Helper / Convenience ---
    def _check_overflow(self):
        """
        Estimates the maximum possible accumulated value and warns if
        it might exceed the 64-bit signed integer limit.
        """
        dwell_cycles = self._dwell_time_cycles
        if dwell_cycles <= 0:
            return # No accumulation if dwell time is zero

        # Max positive value from a 14-bit signed ADC
        max_adc_val = (2**(DATA_WIDTH - 1)) - 1
        # Max possible positive accumulated sum
        max_possible_accum = max_adc_val * dwell_cycles

        # Max positive value for a 64-bit signed integer
        max_int64 = (2**(ACCUM_WIDTH - 1)) - 1

        if max_possible_accum > max_int64:
            logger.warning("Potential accumulator overflow! "
                           f"Maximum possible sum ({max_possible_accum}) "
                           f"based on dwell_time ({dwell_cycles} cycles) "
                           f"exceeds the 64-bit limit ({max_int64}). "
                           "Consider reducing dwell_time.")
            return False # Indicate potential overflow
        return True # OK

    def run_sweep(self, timeout=None, average=True):
        """
        Convenience function to configure, run, wait, and retrieve data.

        Note: Assumes configuration attributes (num_steps, times, etc.)
              are already set correctly before calling.

        Args:
            timeout (float, optional): Maximum time to wait for completion.
                                       Defaults to num_steps * (dwell_time + settling_time) + 1s.
            average (bool): Passed to get_data() for averaging results.

        Returns:
            np.ndarray or None: The sweep data array, or None if timed out or error.
        """
        if timeout is None:
            timeout = self.num_steps * (self.dwell_time + self.settling_time) + 1.0

        self.start()
        if self.wait_done(timeout=timeout):
            try:
                return self.get_data(average=average)
            except RuntimeError as e:
                logger.error(f"Error getting data after sweep: {e}")
                return None
        else:
            logger.error("Sweep timed out or did not complete.")
            # Attempt to stop it cleanly
            self.stop()
            return None