# -*- coding: utf-8 -*-
"""
Scan Module for Pyrpl.

This module controls the FPGA scan block, providing two main operating modes:

================================================================================
1. SCAN MODE - Automated Sweeps with Triggered Acquisition
================================================================================

Performs automated sweeps across multiple steps. For each step:
1. Outputs a trigger pulse on a *fixed* digital output pin (exp_p_io[7]).
2. Waits for a settling time.
3. Acquires and accumulates data from either ADC input, IQ demodulator,
   or demodulated lock-in output for a dwell time.
4. Stores the 64-bit accumulated sum in BRAM (LSB/MSB banks).
5. Stores the sample count in the data3 BRAM bank.

The accumulated data can then be read back and averaged in Python.

**Important Note:**
*   **Trigger Output Pin:** The trigger output pulse is currently HARDWIRED
    to the most significant bit pin of the expansion connector ('exp_p_io[7]')
    in the FPGA design (`red_pitaya_hk.v`). Changing the
    `trigger_pin_select` attribute will write to the corresponding FPGA register,
    but it will NOT change the physical output pin without modifications
    to the FPGA design (`red_pitaya_hk.v`).

================================================================================
2. STREAM MODE - Continuous High-Speed Data Acquisition
================================================================================

Provides continuous streaming of demodulated lock-in data at ~30.5 kHz without
triggering or step sequencing. This mode is designed for real-time monitoring
and high-throughput data collection.

**Stream Architecture:**
*   Uses the data3 BRAM (4096 x 32-bit words) as a circular ring buffer
*   FPGA writes incoming demodulated samples to incrementing addresses
*   Python reads data in batches using efficient block reads
*   Independent read/write pointers prevent data loss (with overflow detection)

**Stream vs Scan Mode:**
*   Mutually exclusive: streaming blocks scan functionality (shared BRAM)
*   Stream mode only supports 'demod' input (32-bit @ ~30.5 kHz)
*   No triggering, settling, or accumulation - raw samples streamed directly
*   Optimized for minimal latency and maximum throughput

**Performance Characteristics:**
*   Sample rate: ~30.5 kHz (125 MHz / 4096 decimation)
*   Buffer depth: 4096 samples (~134 ms at full rate)
*   Typical read latency: 5-20 ms depending on batch size and network
*   Overflow protection: Automatic detection and recovery with warning

**Typical Streaming Workflow:**
```python
# 1. Start streaming
scan.stream_start()

# 2. Continuously read data
for batch in scan.stream_iter(poll_interval=0.01, batch=256):
    process_data(batch)  # batch is np.ndarray of int32 samples

# 3. Or manual reads
while acquiring:
    data = scan.stream_read(max_samples=512)
    if data.size > 0:
        process_data(data)
    time.sleep(0.005)

# 4. Stop streaming
scan.stream_stop()
```

**Stream API Methods:**
*   `stream_start()` - Enable streaming (resets FPGA pointers)
*   `stream_stop()` - Disable streaming
*   `stream_status()` - Get (active, overflow, wr_ptr, samples_written)
*   `stream_read()` - Read available samples (non-blocking)
*   `stream_iter()` - Generator yielding batches until stopped

**Overflow Handling:**
*   Software tracks read position to detect if FPGA writer has wrapped
*   If overflow detected: automatic reset, data loss warning logged
*   Mitigation: increase read frequency or batch size to keep up with rate

================================================================================
Input Modes (Both Scan and Stream)
================================================================================
*   **adc:** Direct 14-bit ADC input at 125 MHz (scan only)
*   **iq0:** 24-bit IQ demodulator output at 125 MHz (scan only)
*   **demod:** 32-bit demodulated lock-in output, valid every 4096 cycles (≈30.5 kHz)

    When using 'demod' mode in scan, the dwell_time still represents the total
    acquisition time in clock cycles, but data is only accumulated when the valid
    signal is high. The actual number of samples accumulated per step is stored
    in the data3 BRAM bank and used for averaging.

================================================================================
BRAM Banks (Memory-Mapped Storage)
================================================================================
*   **LSB Bank (0x10000-0x1FFFF):** Lower 32 bits of 64-bit accumulator (scan mode)
*   **MSB Bank (0x20000-0x2FFFF):** Upper 32 bits of 64-bit accumulator (scan mode)
*   **Data3 Bank (0x30000-0x3FFFF):** Dual-purpose 4096 x 32-bit storage
    - Scan mode: Stores sample counts for each step (for accurate averaging)
    - Stream mode: Ring buffer for continuous demodulated data streaming
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
DATA_WIDTH_ADC = 14
DATA_WIDTH_IQ = 24
DATA_WIDTH_DEMOD = 32
ACCUM_WIDTH = 64
BUS_DATA_WIDTH = 32
FPGA_CLK_PERIOD_S = 8e-9  # 1/125MHz
DEMOD_DECIMATION = 4096  # Decimation factor for demodulated data

# Address Map (relative to module base)
ADDR_CONTROL = 0x00
ADDR_STATUS = 0x00 # Same as control, different bits
ADDR_NUM_STEPS = 0x04
ADDR_DWELL_TIME = 0x08
ADDR_SETTLING_TIME = 0x0C
ADDR_TRIGGER_LENGTH = 0x10
ADDR_TRIGGER_PIN_SEL = 0x14
ADDR_CURRENT_STEP = 0x18
ADDR_INPUT_SELECT = 0x1C  # Input source selection
# Streaming control/status (demod ring buffer in data3 BRAM)
ADDR_STREAM_CONTROL = 0x20  # bit0 enable, bit1 reset
ADDR_STREAM_STATUS  = 0x24  # bit0 active, bit1 overflow
ADDR_STREAM_WR_PTR  = 0x28  # write pointer (index in BRAM)
ADDR_STREAM_SAMPLES = 0x2C  # total samples written
BRAM_LSB_BASE_ADDR = 0x10000  # As per Verilog: Module Base + 0x10000
BRAM_MSB_BASE_ADDR = 0x20000  # As per Verilog: Module Base + 0x20000
BRAM_DATA3_BASE_ADDR = 0x30000 # As per Verilog: Module Base + 0x30000 (sample counts or stream data)

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
    #_widget_class = ScanWidget # Create a widget later if needed

    _setup_attributes = ["num_steps", "dwell_time", "settling_time",
                         "trigger_length", "trigger_pin_select", "input_select"]

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

    # Input Selection - now with 3 options
    _input_options = {"adc": 0, "iq0": 1, "demod": 2}
    input_select = SelectRegister(ADDR_INPUT_SELECT, options=_input_options,
                                  default="adc",
                                  doc="Selects the input signal source: "
                                      "'adc' for 14-bit ADC input at 125 MHz, "
                                      "'iq0' for 24-bit IQ demodulator output at 125 MHz, "
                                      "'demod' for 32-bit lock-in demodulated output (valid every 4096 cycles).")

    # ---------------- Streaming (32-bit @ ~31 kHz) ----------------
    def _stream_ctrl_write(self, enable=None, reset=False):
        """Drive the streaming control register (``ADDR_STREAM_CONTROL``).

        Register layout (write side):
        - bit 0 (``ENABLE``): 1 = enable streaming; 0 = disable.
        - bit 1 (``RESET``): write-one-to-pulse reset of the stream engine
        (clears pointers/counters in FPGA). Hardware clears/de-latches it.

        Read-modify-write behaviour:
        - If ``enable`` is ``None``, the current enable state (bit 0) is preserved.
        This lets callers issue a reset pulse without unintentionally toggling
        the stream.
        - If ``enable`` is ``True``/``False``, bit 0 is explicitly set/cleared.
        - If ``reset`` is ``True``, bit 1 is OR'ed in to request a reset pulse.

        Args:
        enable: If ``True`` enable streaming; if ``False`` disable streaming;
        if ``None`` (default) keep the current enable state (bit 0).
        reset: If ``True``, pulse the RESET bit (bit 1). Defaults to ``False``.
        """
        val = 0
        if enable is None:
            # Read-modify-write to preserve current enable state
            cur = self._read(ADDR_STREAM_CONTROL)
            val |= (cur & 0x1)
        else:
            val |= 0x1 if enable else 0x0
        if reset:
            val |= 0x2
        self._write(ADDR_STREAM_CONTROL, val)


    def stream_start(self, input_source="demod"):
        """Enable continuous streaming of demodulated samples into the BRAM ring buffer.

        Notes:
            - Uses the data3 BRAM region (32-bit words) as a circular buffer.
            - Blocks scanning functionality while active (shared memory).
        """
        if input_source != "demod":
            logger.warning("Streaming currently only supported for 'demod'. Forcing input_select to 'demod'.")
        self.input_select = 'demod'
        # Reset FPGA streaming engine and enable
        self._stream_ctrl_write(enable=True, reset=True)
        # Initialize software reader state aligned to current writer
        _, _, wrp, total_samples = self.stream_status()
        self._stream_rd_ptr = int(wrp)
        self._stream_total_read = int(total_samples)
        self._stream_active = True


    def stream_stop(self):
        """Disable streaming and clear software-side counters."""
        self._stream_ctrl_write(enable=False)
        self._stream_active = False


    def stream_status(self):
        """Return tuple (active, overflow, wr_ptr, samples_written).
        
        Optimized to read all 3 registers (STATUS, WR_PTR, SAMPLES) in a single
        bulk read operation to reduce network overhead. This only works when the three
        register adresses are consecutive (as they are here).
        """
        # Read 3 consecutive 32-bit registers starting at ADDR_STREAM_STATUS
        # Addresses: 0x24 (STATUS), 0x28 (WR_PTR), 0x2C (SAMPLES)
        values = self._reads(ADDR_STREAM_STATUS, 3)
        status = int(values[0])
        wr_ptr = int(values[1])
        cnt = int(values[2])
        active = bool(status & 0x1)
        overflow = bool((status >> 1) & 0x1) # hardware overflow flag not currently working/updated correctly in FPGA
        return active, overflow, wr_ptr, cnt
    

    def stream_read(self, max_samples=None, enable_timing=False, check_overflow_every=1):
        """Read available demodulated samples from the ring buffer.

        Args:
            max_samples (int|None): Optional limit on number of <samples to read.
            enable_timing (bool): If True, log detailed timing information.
            check_overflow_every (int): Check for overflow every N calls (default 1 = every call).
        Returns:
            np.ndarray int32 of shape (n,) with the read samples. If accessed via RPyC,
            this will be a netref and the user must convert it using data.tolist() to get a local array.
        """
        if enable_timing:
            _t_start = time.time()
            _timings = {}
        
        if not getattr(self, '_stream_active', False):
            return np.array([], dtype=np.int32)

        # Track call count for periodic overflow checking
        call_count = getattr(self, '_stream_read_calls', 0) + 1
        self._stream_read_calls = call_count
        check_overflow = (call_count % check_overflow_every) == 0

        if enable_timing:
            _t0 = time.time()
        active, overflow, wrp, total_written = self.stream_status()
        if enable_timing:
            _timings['stream_status'] = time.time() - _t0
            
        # Software overflow detection: if writer advanced by >= depth since last read
        depth = 2**MAX_STEPS_BITS
        if check_overflow:
            delta = (int(total_written) - int(getattr(self, '_stream_total_read', 0))) & 0xFFFFFFFF
            if delta >= depth or overflow:
                logger.warning("FPGA streaming overflow flagged. Consider reading faster. Resetting stream.")
                # Clear overflow via reset pulse and restart from current write pointer
                self._stream_ctrl_write(enable=True, reset=True)
                _, _, wrp2, total2 = self.stream_status()
                self._stream_rd_ptr = int(wrp2)
                self._stream_total_read = int(total2)
                return np.array([], dtype=np.int32)

        # Compute number available between software read pointer and FPGA write pointer
        rd = getattr(self, '_stream_rd_ptr', 0) % depth
        if wrp >= rd:
            avail = wrp - rd
        else:
            avail = (depth - rd) + wrp

        if avail == 0:
            return np.array([], dtype=np.int32)
        if max_samples is not None:
            avail = min(avail, int(max_samples))

        # We may need to read in two segments (until end, then wrap)
        first_len = min(avail, depth - rd)
        segs = []
        if first_len > 0:
            if enable_timing:
                _t1 = time.time()
            base = BRAM_DATA3_BASE_ADDR + rd * 4
            segs.append(self._reads(base, first_len))
            if enable_timing:
                _timings['first_reads'] = time.time() - _t1
                _timings['first_len'] = first_len
        rem = avail - first_len
        if rem > 0:
            if enable_timing:
                _t2 = time.time()
            base = BRAM_DATA3_BASE_ADDR  # wrapped
            segs.append(self._reads(base, rem))
            if enable_timing:
                _timings['second_reads'] = time.time() - _t2
                _timings['second_len'] = rem
        
        # Concatenate and cast
        if enable_timing:
            _t3 = time.time()
        data = np.concatenate([np.asarray(s, dtype=np.uint32) for s in segs]) if len(segs) > 1 else np.asarray(segs[0], dtype=np.uint32)
        data = data.view(np.int32)
        if enable_timing:
            _timings['concatenate_cast'] = time.time() - _t3

        # Advance software read pointer
        self._stream_rd_ptr = (rd + avail) % depth
        self._stream_total_read = getattr(self, '_stream_total_read', 0) + avail
        
        if enable_timing:
            _timings['total'] = time.time() - _t_start
            logger.debug(f"stream_read timing (samples={avail}): " + 
                        ", ".join([f"{k}={v*1000:.2f}ms" for k, v in _timings.items()]))
        
        return data
    

    def stream_iter(self, poll_interval=0.005, batch=256):
        """Yield batches of samples as they arrive. Stops when stream_stop() is called.

        Args:
            poll_interval (float): sleep between polls [s]
            batch (int): preferred batch size
        Yields:
            np.ndarray int32
        """
        while getattr(self, '_stream_active', False):
            arr = self.stream_read(max_samples=batch)
            if arr.size:
                yield arr
            else:
                time.sleep(poll_interval)


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
        logger.info("Starting scan sweep with input source: %s...", self.input_select)
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
                            by the per-step valid sample count (from data3 BRAM).
                            This handles all input modes:
                            - For 'adc'/'iq0': count equals dwell_time in cycles.
                            - For 'demod': count equals actual valid samples acquired.
                            Steps with zero samples are set to 0 (with a warning).
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
        dwell_cycles = self._dwell_time_cycles  # Read the cycle count
        input_mode = self.input_select

        if n_steps <= 0:
            logger.warning("Number of steps is zero or invalid. Returning empty array.")
            return np.array([], dtype=np.int64 if not average else np.float64)

        if average and dwell_cycles <= 0:
            logger.error("Cannot average data: dwell_time is zero cycles. "
                         "Returning raw accumulated data.")
            average = False  # Force return of raw data

        logger.info(f"Reading {n_steps} data points from FPGA BRAM using block reads...")

        try:
            # The _reads method in the parent HardwareModule automatically adds the
            # module's base address (self.addr_base). Thus only provide relative offset here.
            lsb_data = self._reads(BRAM_LSB_BASE_ADDR, n_steps)
            msb_data = self._reads(BRAM_MSB_BASE_ADDR, n_steps)
            count_data = self._reads(BRAM_DATA3_BASE_ADDR, n_steps)

            # --- Validation of received data ---
            if lsb_data is None or len(lsb_data) != n_steps:
                raise RuntimeError(f"Block read from LSB BRAM failed or returned incorrect length. "
                                   f"Expected {n_steps}, got {len(lsb_data) if lsb_data is not None else 'None'}.")
            if msb_data is None or len(msb_data) != n_steps:
                raise RuntimeError(f"Block read from MSB BRAM failed or returned incorrect length. "
                                   f"Expected {n_steps}, got {len(msb_data) if msb_data is not None else 'None'}.")
            if count_data is None or len(count_data) != n_steps:
                raise RuntimeError(f"Block read from count data failed or returned incorrect length. "
                                   f"Expected {n_steps}, got {len(count_data) if count_data is not None else 'None'}.")

            # --- Combine LSB and MSB into a 64-bit integer array ---
            combined = (msb_data.astype(np.uint64) << 32) | lsb_data.astype(np.uint64)
            data_accum = combined.view(np.int64)
            count_data = count_data.astype(np.int64)
            # print("count_data:", count_data)
            # print("lsbdata:", lsb_data)
            # print("msb_data:", msb_data)

        except Exception as e:
            logger.error(f"Error during block read from BRAM: {e}")
            raise RuntimeError(f"Failed to read data from BRAM. Original error: {e}")

        logger.info("Data readout complete.")

        if average:
            logger.info("Averaging data using per-step sample counts from data3 BRAM.")
            data = data_accum.astype(np.float64)
            count = count_data.astype(np.float64)
            avg_data = np.zeros_like(data)
            mask = count > 0
            if np.any(~mask):
                logger.warning("Some steps had zero valid samples. Setting average to 0.")
            avg_data[mask] = data[mask] / count[mask]
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
            return None

        if self.input_select == 'demod':
            max_val = (2 ** (DATA_WIDTH_DEMOD - 1)) - 1
            # Conservative: assume up to dwell_cycles / DECIMATION +1 samples (extra for partial)
            effective_samples = (dwell_cycles // DEMOD_DECIMATION) + 1
        elif self.input_select == 'iq0':
            max_val = (2 ** (DATA_WIDTH_IQ - 1)) - 1
            effective_samples = dwell_cycles  # Add this line
        else:
            max_val = (2 ** (DATA_WIDTH_ADC - 1)) - 1
            effective_samples = dwell_cycles

        # Max possible positive accumulated sum
        max_possible_accum = max_val * effective_samples

        # Max positive value for a 64-bit signed integer
        max_int64 = (2 ** (ACCUM_WIDTH - 1)) - 1

        if max_possible_accum > max_int64:
            logger.warning("Potential accumulator overflow! "
                           f"Maximum possible sum ({max_possible_accum}) "
                           f"based on dwell_time ({dwell_cycles} cycles) "
                           f"exceeds the 64-bit limit ({max_int64}). "
                           "Consider reducing dwell_time.")
            return False  # Indicate potential overflow
        return True  # OK

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