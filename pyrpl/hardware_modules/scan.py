# -*- coding: utf-8 -*-
"""
Scan Module for Pyrpl.

This module controls the FPGA scan block, providing three operating
configurations:

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

**>>> Use the push streaming API for new code. <<<**
The canonical streaming path is now `push_stream_start/read/iter/stats/stop`
(ARM-side drain + TCP push): it moves the real-time deadline onto the board and
NaN-fills lost samples instead of dropping them silently. The poll-based
`stream_*` API documented below is **deprecated** (emits a DeprecationWarning)
and retained only as a zero-dependency fallback (no SSH deploy / no second TCP
port). See `docs/developer_guide/scan_data_streaming.md`.

**Stream Architecture:**
*   The FPGA writes 32-bit samples to the data3 BRAM (4096 words) as a circular
    ring buffer; this ring is shared by both streaming paths.
*   Canonical (push): the ARM `stream_server` drains the BRAM ring locally and
    pushes a framed TCP stream to the PC; the PC never reads the BRAM directly.
*   Deprecated (poll): the PC reads the BRAM ring in batches via block reads,
    tracking independent read/write pointers with software overflow detection.

**Stream vs Scan Mode:**
*   Mutually exclusive: streaming blocks scan functionality (shared BRAM)
*   Supported inputs: 'demod' (lock-in error) and 'ftw_corr' (ODMR correction),
    both 32-bit @ ~30.5 kHz
*   No triggering, settling, or accumulation - raw samples streamed directly
*   Optimized for minimal latency and maximum throughput

**Performance Characteristics:**
*   Sample rate: ~30.5 kHz (125 MHz / 4096 decimation)
*   FPGA ring depth: 4096 samples (~134 ms); push adds an ARM DRAM ring
    (default 16 MB ≈ 15 s–2 min headroom) so PC-side stalls do not lose data
*   Typical read latency: ~5 ms (push `coalesce_us` default) plus network
*   Loss handling: push NaN-fills genuine overruns explicitly; the deprecated
    poll path auto-resets and logs a warning

**Typical Streaming Workflow (canonical: push API):**
```python
# 1. Stream demodulated error (loop open)
scan.push_stream_start(input_source='demod')
for batch in scan.push_stream_iter(poll_interval=0.05):
    process_demod_data(batch)  # batch is np.ndarray of float64; NaN = lost sample
scan.push_stream_stop()

# 2. Stream FTW correction (loop closed, tracking resonance)
odmr.enable = True  # Enable ODMR frequency lock
scan.push_stream_start(input_source='ftw_corr')
for batch in scan.push_stream_iter(poll_interval=0.05):
    freq_drift_hz = scan.ftw_to_hz(batch)  # Convert FTW to Hz (NaN-safe)
    monitor_frequency_drift(freq_drift_hz)
scan.push_stream_stop()

# 3. Or manual reads (paced to your own cadence; no 134 ms deadline)
while acquiring:
    data = scan.push_stream_read()  # all samples since last call (float64)
    if data.size:
        freq_hz = scan.ftw_to_hz(data)
        process_data(freq_hz)
    time.sleep(0.05)
```

**Stream API Methods (canonical: ARM-side push):**
*   `push_stream_start()` - Select input, enable engine, start ARM server + PC receiver
*   `push_stream_stop()`  - Stop receiver and disable the FPGA stream engine
*   `push_stream_read()`  - All samples since last call (float64; NaN = lost)
*   `push_stream_iter()`  - Generator yielding batches until stopped
*   `push_stream_stats()` - Health counters (n_samples, n_gap, n_seq_skips, ...)
    See `docs/developer_guide/scan_data_streaming.md` for the full design.

**Stream API Methods (deprecated: poll-based fallback):**
Retained as a zero-dependency fallback (pure register reads, no SSH deploy / no
second TCP port). Each emits a DeprecationWarning. Prefer the push API above.
*   `stream_start()` - Enable streaming (resets FPGA pointers)
*   `stream_stop()` - Disable streaming
*   `stream_status()` - Get (active, wr_ptr, samples_written)
*   `stream_read()` - Read available samples (non-blocking; int32)
*   `stream_iter()` - Generator yielding batches until stopped

**Loss Handling:**
*   Push API: the ARM drainer detects FPGA-ring overruns locally and reports lost
    spans explicitly; the PC NaN-fills them so the time axis stays truthful.
    A large ARM DRAM ring (default 16 MB) absorbs PC-side stalls (~15 s–2 min).
*   Poll API (deprecated): software-only overflow detection by comparing sample
    counters; on overrun it resets and logs a data-loss warning. Mitigate by
    reading more frequently — i.e. the fragility the push API removes.

================================================================================
3. POSITION-MARKER STREAM MODE - Hardware-Synchronized Motor Scans
================================================================================

This is stream mode with FPGA marker capture enabled. The demod/FTW sample ring
continues to run in data3 BRAM and is normally drained by the ARM push server,
while two external KDC101 "At Position Steps" outputs are edge-detected by the
FPGA:

*   x position pulse: exp_p_in[5] / DIO5_P, fast-axis bin boundaries.
*   y position pulse: exp_p_in[6] / DIO6_P, slow-axis line boundaries.

On each position pulse the FPGA records the current stream sample counter into a
marker ring: x markers in the LSB bank, y markers in the MSB bank. Since the
sample ring and both marker rings share the same free-running sample counter, a
marker value m means sample[m] is the first stream sample after that spatial
boundary. The PC can then slice one continuous trace into exact spatial bins and
lines without USB position timestamps or interpolation.

Use the mapped-stream API for this mode:
```python
scan.mapped_stream_start(input_source='demod')  # enables stream + marker capture
while scanning:
    demod = scan.mapped_stream_read()
    x_markers = scan.read_x_markers()
    y_markers = scan.read_y_markers()
scan.mapped_stream_stop()
```

The marker rings are 4096 entries deep and must be drained periodically during
long rasters. See `docs/developer_guide/motor_position_sync_scan.md` for the
FPGA/PC reconstruction contract and the qudi-side motor integration.

================================================================================
Input Modes
================================================================================
*   **adc:** Direct 14-bit ADC input at 125 MHz (scan only)
*   **iq0:** 24-bit IQ demodulator output at 125 MHz (scan only)
*   **demod:** 32-bit demodulated lock-in output, valid every 4096 cycles (≈30.5 kHz)
*   **ftw_corr:** 32-bit FTW correction from the ODMR tracker, valid every 4096
    cycles (stream and marker-stream modes only)

    When using 'demod' mode in scan, the dwell_time still represents the total
    acquisition time in clock cycles, but data is only accumulated when the valid
    signal is high. The actual number of samples accumulated per step is stored
    in the data3 BRAM bank and used for averaging.

================================================================================
BRAM Banks (Memory-Mapped Storage)
================================================================================
*   **LSB Bank (0x10000-0x1FFFF):** Lower 32 bits of 64-bit accumulator (scan
    mode); x-marker ring in position-marker stream mode
*   **MSB Bank (0x20000-0x2FFFF):** Upper 32 bits of 64-bit accumulator (scan
    mode); y-marker ring in position-marker stream mode
*   **Data3 Bank (0x30000-0x3FFFF):** Dual-purpose 4096 x 32-bit storage
    - Scan mode: Stores sample counts for each step (for accurate averaging)
    - Stream/marker-stream mode: Ring buffer for continuous demod/FTW samples
"""
import time
import warnings
import numpy as np
import logging

from ..modules import HardwareModule
from ..widgets.module_widgets.scan_widget import ScanWidget
from ..attributes import (IntRegister, FloatProperty, BoolRegister,
                          SelectRegister, BaseRegister)
from ..stream_client import StreamClient

# from ..dsp import InputSelectRegister # Cannot use this due to FPGA limitation
# from ..pyrpl_utils import time

logger = logging.getLogger(__name__)

# Tracks which legacy stream_* methods have already emitted their deprecation
# warning this process, so the warning fires once per method instead of on every
# call (stream_read in particular runs in a tight poll loop).
_LEGACY_STREAM_WARNED = set()


def _warn_legacy_stream(method):
    """Emit a one-time DeprecationWarning steering callers to push_stream_*.

    The poll-based stream_* API is superseded by the ARM-side push streaming
    (push_stream_start/read/iter/stats/stop), which moves the real-time deadline
    onto the board and NaN-fills lost samples instead of dropping them silently.
    The poll API is retained as a zero-dependency fallback (no SSH deploy / no
    second TCP port). See docs/developer_guide/scan_data_streaming.md.
    """
    if method in _LEGACY_STREAM_WARNED:
        return
    _LEGACY_STREAM_WARNED.add(method)
    warnings.warn(
        "Scan.{0}() is deprecated: use the push streaming API "
        "(push_stream_start/read/iter/stats/stop) instead. The poll-based "
        "stream_* API is kept only as a zero-dependency fallback. "
        "See docs/developer_guide/scan_data_streaming.md.".format(method),
        DeprecationWarning,
        stacklevel=3,
    )
    logger.warning(
        "Scan.%s() is deprecated; prefer push_stream_* (see "
        "docs/developer_guide/scan_data_streaming.md).", method)


# Define constants based on scan_new.v
MAX_STEPS_BITS = 12
DATA_WIDTH_ADC = 14
DATA_WIDTH_IQ = 24
DATA_WIDTH_DEMOD = 32
ACCUM_WIDTH = 64
BUS_DATA_WIDTH = 32
FPGA_CLK_PERIOD_S = 8e-9  # 1/125MHz
DEMOD_DECIMATION = 4096  # Decimation factor for demodulated data
FPGA_CLK_HZ = 125e6  # FPGA clock frequency
FTW_PER_HZ = (2**32) / FPGA_CLK_HZ  # FTW units per Hz ≈ 34.359738

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
# Position-marker streaming (MODE 3): x markers in LSB bank, y markers in MSB bank
ADDR_MARKER_X_WR_PTR = 0x30  # x-marker ring write pointer (index in LSB BRAM)
ADDR_MARKER_X_COUNT  = 0x34  # total x markers written since reset
ADDR_MARKER_Y_WR_PTR = 0x38  # y-marker ring write pointer (index in MSB BRAM)
ADDR_MARKER_Y_COUNT  = 0x3C  # total y markers written since reset
# Hop-boundary marker streaming (multi-resonance monitoring, Phase B-stream):
# tick -> LSB bank, current_step -> MSB bank, at the shared hop ring index.
ADDR_HOP_WR_PTR      = 0x40  # hop-marker ring write pointer (index in LSB/MSB BRAM)
ADDR_HOP_COUNT       = 0x44  # total hop markers written since reset
ADDR_STREAM_FORMAT   = 0x48  # words per self-describing stream sample (new FPGA: 4)
BRAM_LSB_BASE_ADDR = 0x10000  # As per Verilog: Module Base + 0x10000
BRAM_MSB_BASE_ADDR = 0x20000  # As per Verilog: Module Base + 0x20000
BRAM_DATA3_BASE_ADDR = 0x30000 # As per Verilog: Module Base + 0x30000 (sample counts or stream data)

# Control Register Bits
CONTROL_START_BIT = 0
CONTROL_STOP_BIT = 1
CONTROL_RESET_BIT = 2
CONTROL_CONTINUOUS_BIT = 3  # latched with START: continuous/loop hopping (no S_DONE)

# Status Register Bits
STATUS_BUSY_BIT = 0
STATUS_DONE_BIT = 1
STATUS_CONTINUOUS_BIT = 2  # read-back of reg_continuous (continuous/loop mode active)


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

    Provides triggered step scans, continuous push/poll streaming, and
    position-marker streaming for hardware-synchronized motor scans.
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

    continuous = BoolRegister(ADDR_STATUS, bit=STATUS_CONTINUOUS_BIT,
                              doc="Read-only: True while the scan FSM is in "
                                  "continuous/loop hopping mode (started via "
                                  "start(continuous=True)). In this mode busy stays "
                                  "True and done never asserts until stop()/reset().")

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

    # Input Selection - now with 4 options
    _input_options = {"adc": 0, "iq0": 1, "demod": 2, "ftw_corr": 3}
    input_select = SelectRegister(ADDR_INPUT_SELECT, options=_input_options,
                                  default="adc",
                                  doc="Selects the input signal source: "
                                      "'adc' for 14-bit ADC input at 125 MHz (scan only), "
                                      "'iq0' for 24-bit IQ demodulator output at 125 MHz (scan only), "
                                      "'demod' for 32-bit lock-in demodulated output (valid every 4096 cycles), "
                                      "'ftw_corr' for 32-bit FTW correction from ODMR tracker (stream only).")

    # ---------------- Streaming (32-bit @ ~31 kHz) ----------------
    @property
    def stream_words_per_sample(self):
        """Self-describing stream record width reported by the FPGA.

        New bitstreams report four words: ``[fir_error, correction, cic_error,
        state]``.  Pre-CIC-stream bitstreams leave 0x48 unmapped and read as zero;
        those are treated as the legacy three-word format for safe diagnostics.
        """
        value = int(self._read(ADDR_STREAM_FORMAT))
        if value in (3, 4):
            return value
        logger.warning("FPGA does not report a recognised stream format at 0x48 "
                       "(read %d); assuming legacy 3-word records.", value)
        return 3

    def _stream_ctrl_write(self, enable=None, reset=False, marker=None, hop=None,
                           dual=None, marked=None):
        """Drive the streaming control register (``ADDR_STREAM_CONTROL``).

        Register layout (write side):
        - bit 0 (``ENABLE``): 1 = enable streaming; 0 = disable.
        - bit 1 (``RESET``): write-one-to-pulse reset of the stream engine
        (clears stream pointers/counters *and* the position-/hop-marker rings in
        the FPGA). Hardware clears/de-latches it.
        - bit 2 (``MARKER``): 1 = enable x/y position-marker capture (MODE 3); 0 =
        disable. Markers only matter while streaming is enabled.
        - bit 3 (``HOP``): 1 = enable hop-boundary marker capture (multi-resonance
        monitoring); 0 = disable. Mutually exclusive with bit 2 (both reuse the
        LSB/MSB marker banks).
        - bit 4 (``DUAL``): 1 = enable dual-quantity self-describing streaming
        (writes [err, corr, cic, step] records into the data3 ring per demod strobe,
        overriding INPUT_SELECT for the stream engine); 0 = legacy single-word.
        In dual mode the hop-marker ring is not needed (the step label is inline).

        Read-modify-write behaviour:
        - If ``enable`` is ``None``, the current enable state (bit 0) is preserved.
        This lets callers issue a reset pulse without unintentionally toggling
        the stream.
        - If ``enable`` is ``True``/``False``, bit 0 is explicitly set/cleared.
        - If ``marker``/``hop`` is ``None``, the current state of that bit is
        preserved; otherwise it is explicitly set/cleared.
        - If ``reset`` is ``True``, bit 1 is OR'ed in to request a reset pulse.

        Args:
        enable: If ``True`` enable streaming; if ``False`` disable streaming;
        if ``None`` (default) keep the current enable state (bit 0).
        reset: If ``True``, pulse the RESET bit (bit 1). Defaults to ``False``.
        marker: If ``True`` enable / ``False`` disable x/y marker capture (bit 2);
        if ``None`` (default) keep the current state.
        hop: If ``True`` enable / ``False`` disable hop-marker capture (bit 3);
        if ``None`` (default) keep the current state.
        """
        val = 0
        cur = None
        if (enable is None or marker is None or hop is None or dual is None
                or marked is None):
            # Read-modify-write to preserve unspecified level bits
            cur = self._read(ADDR_STREAM_CONTROL)
        if enable is None:
            val |= (cur & 0x1)
        else:
            val |= 0x1 if enable else 0x0
        if marker is None:
            val |= (cur & 0x4)
        else:
            val |= 0x4 if marker else 0x0
        if hop is None:
            val |= (cur & 0x8)
        else:
            val |= 0x8 if hop else 0x0
        if dual is None:
            val |= (cur & 0x10)
        else:
            val |= 0x10 if dual else 0x0
        # bit 5 (MARKED): continuous uniform-rate dead-time-aware stream (writes
        # [err, corr, cic, state] records on a free-running /4096 tick; state = step when
        # live else DEAD sentinel). Mutually exclusive with DUAL (FPGA gives MARKED
        # priority); the step label is inline so no hop-marker ring is used.
        if marked is None:
            val |= (cur & 0x20)
        else:
            val |= 0x20 if marked else 0x0
        if reset:
            val |= 0x2
        self._write(ADDR_STREAM_CONTROL, val)

    def _teardown_existing_push_stream(self, context):
        """Stop a live push/mapped stream client before starting a new one.

        The board exposes a SINGLE stream (one ring, one free-running sample
        counter selected by ``input_select``). Starting a new push/mapped stream
        pulses a counter reset and replaces ``self._push_rx``. If a previous
        client is still running -- e.g. a different qudi module already streaming
        this board -- silently overwriting it orphans its receive thread and
        resets the counter out from under it (the server then reports a wrapped,
        near-2**32 "gap"). Stop it cleanly and warn so the collision is visible
        rather than corrupting both consumers.
        """
        rx = getattr(self, '_push_rx', None)
        if rx is not None and getattr(rx, 'running', False):
            logger.warning(
                "%s: a push stream is already running on this board (input=%s); "
                "stopping it before starting the new one. The board streams ONE "
                "quantity at a time -- concurrent stream owners are not supported.",
                context, getattr(self, '_push_input', '?'))
            try:
                rx.stop()
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.exception("%s: error stopping the existing stream client", context)
        self._push_rx = None

    def stream_start(self, input_source="demod"):
        """Enable continuous streaming of demodulated samples or FTW correction into the BRAM ring buffer.

        Args:
            input_source (str): Input to stream - 'demod' for lock-in error signal or
                               'ftw_corr' for ODMR frequency correction. Default: 'demod'

        Notes:
            - Uses the data3 BRAM region (32-bit words) as a circular buffer.
            - Blocks scanning functionality while active (shared memory).
            - For tracking resonance drift, use 'ftw_corr' when ODMR lock is enabled.

        .. deprecated::
            Use :meth:`push_stream_start` (ARM-side push streaming). The poll API
            is retained only as a zero-dependency fallback.
        """
        _warn_legacy_stream("stream_start")
        if input_source not in ["demod", "ftw_corr"]:
            logger.warning("Streaming only supported for 'demod' and 'ftw_corr'. Forcing input_select to 'demod'.")
            input_source = "demod"
        self.input_select = input_source
        # Reset FPGA streaming engine and enable (markers off for plain streaming)
        self._stream_ctrl_write(enable=True, reset=True, marker=False)
        # Initialize software reader state aligned to current writer
        _, wrp, total_samples = self.stream_status()
        self._stream_rd_ptr = int(wrp)
        self._stream_total_read = int(total_samples)
        self._stream_active = True


    def stream_stop(self):
        """Disable streaming and clear software-side counters.

        .. deprecated:: Use :meth:`push_stream_stop`.
        """
        _warn_legacy_stream("stream_stop")
        self._stream_ctrl_write(enable=False)
        self._stream_active = False


    def stream_status(self):
        """Return tuple (active, wr_ptr, samples_written).

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
        return active, wr_ptr, cnt
    

    def stream_read(self, max_samples=None, enable_timing=False, check_overflow_every=1):
        """Read available demodulated samples from the ring buffer.

        Args:
            max_samples (int|None): Optional limit on number of samples to read.
            enable_timing (bool): If True, log detailed timing information.
            check_overflow_every (int): Check for overflow every N calls (default 1 = every call).
        Returns:
            np.ndarray int32 of shape (n,) with the read samples. If accessed via RPyC,
            this will be a netref and the user must convert it using data.tolist() to get a local array.

        .. deprecated:: Use :meth:`push_stream_read` (returns float64, NaN = loss).
        """
        _warn_legacy_stream("stream_read")
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
        _, wrp, total_written = self.stream_status()
        if enable_timing:
            _timings['stream_status'] = time.time() - _t0

        # Software overflow detection: if writer advanced by >= depth since last read
        depth = 2**MAX_STEPS_BITS
        if check_overflow:
            delta = (int(total_written) - int(getattr(self, '_stream_total_read', 0))) & 0xFFFFFFFF
            if delta >= depth:
                logger.warning("Overflow detected. FPGA writer has advanced by >= %d samples. "
                              "Consider reading faster. Resetting stream.", depth)
                # Clear overflow via reset pulse and restart from current write pointer
                self._stream_ctrl_write(enable=True, reset=True)
                _, wrp2, total2 = self.stream_status()
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
        # TODO: Check if reading in two segments is potentially inefficient due to read overheads 
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

        .. deprecated:: Use :meth:`push_stream_iter`.
        """
        _warn_legacy_stream("stream_iter")
        # TODO: Check if this will block other operations. If so, potentially use async as in scope module.
        while getattr(self, '_stream_active', False):
            arr = self.stream_read(max_samples=batch)
            if arr.size:
                yield arr
            else:
                time.sleep(poll_interval)

    # ----------- Push streaming (ARM-side drain + TCP push) -------------
    # Robust alternative to the poll-based stream_read above: the deadline-
    # critical ring drain runs locally on the Red Pitaya ARM and pushes a
    # continuous framed stream to the PC, which reads it without polling. Lost
    # samples (FPGA overrun) are reported explicitly and NaN-filled, never
    # silently dropped. See pyrpl/stream_client.py and
    # pyrpl/monitor_server/stream_server.c.
    def push_stream_start(self, input_source="demod", poll_us=200,
                          ring_bytes=0, coalesce_us=0, force_recompile=False):
        """Start robust push-based streaming of demod or FTW-correction data.

        Selects the input, resets+enables the FPGA stream engine over the normal
        register path, ensures the ARM streaming server is running, and starts a
        background receiver. Returns the :class:`StreamClient`.

        Args:
            input_source (str): 'demod' or 'ftw_corr'.
            poll_us (int): ARM idle poll interval when caught up.
            ring_bytes (int): ARM-side DRAM ring size, setting the PC-stall
                headroom (``ring_bytes / wire_rate`` seconds). 0 = 16 MB default
                (~15 s); pass e.g. ``64<<20`` for ~60 s.
            coalesce_us (int): max ARM batching latency in microseconds
                (0 = 5 ms default). Larger trims frame-header overhead.
            force_recompile (bool): rebuild the ARM server binary.
        """
        if input_source not in ("demod", "ftw_corr"):
            logger.warning("Push streaming supports 'demod' and 'ftw_corr' only; "
                           "using 'demod'.")
            input_source = "demod"
        # 0. ensure no other consumer is mid-stream on this single-stream board
        self._teardown_existing_push_stream("push_stream_start")
        # 1. select input and reset+enable the FPGA stream engine (register path);
        #    markers off for plain push streaming
        self.input_select = input_source
        self._stream_ctrl_write(enable=True, reset=True, marker=False)
        # 2. ensure the ARM push-streaming server is up; get its port
        port = self.parent.ensure_stream_server(force_recompile=force_recompile)
        host = self.parent.parameters['hostname']
        # 3. start the background receiver
        self._push_rx = StreamClient(host, port, addr_base=self.addr_base,
                                     poll_us=poll_us, ring_bytes=ring_bytes,
                                     coalesce_us=coalesce_us)
        self._push_rx.start()
        self._push_input = input_source
        logger.info("Push streaming started (%s) from %s:%d", input_source, host, port)
        return self._push_rx

    def push_stream_read(self):
        """Return all samples received since the last call.

        Returns:
            np.ndarray float64, in order, with NaN where the FPGA overran the ARM
            drainer (so the time axis stays truthful). Empty if not streaming.
        """
        rx = getattr(self, '_push_rx', None)
        if rx is None:
            return np.empty(0, dtype=np.float64)
        return rx.read()

    def push_stream_iter(self, poll_interval=0.05, min_samples=1):
        """Yield batches of received samples until streaming stops."""
        rx = getattr(self, '_push_rx', None)
        while rx is not None and rx.running:
            data = rx.read()
            if data.size >= min_samples:
                yield data
            else:
                time.sleep(poll_interval)
        if rx is not None:
            tail = rx.read()
            if tail.size:
                yield tail

    def push_stream_stats(self):
        """Return streaming counters (samples, gaps, frames, seq skips, error)."""
        rx = getattr(self, '_push_rx', None)
        return rx.stats() if rx is not None else {}

    def add_stream_tap(self, max_seconds=10.0):
        """Return a secondary read view of the live push stream, or None.

        Lets a second consumer (e.g. a live time-trace display) read the SAME
        samples the primary consumer (e.g. a marker-mode scan) is reading, without
        stealing them. The tap is bounded (drop-oldest) for display safety.
        """
        rx = getattr(self, '_push_rx', None)
        return rx.add_tap(max_seconds=max_seconds) if rx is not None else None

    def remove_stream_tap(self, tap):
        """Unregister a tap previously returned by :meth:`add_stream_tap`."""
        rx = getattr(self, '_push_rx', None)
        if rx is not None and tap is not None:
            rx.remove_tap(tap)

    def push_stream_stop(self, stop_server=False):
        """Stop the receiver and disable the FPGA stream engine.

        The ARM server is left running by default for fast restarts; pass
        ``stop_server=True`` (or call ``rp.stop_stream_server()``) to stop it.
        """
        rx = getattr(self, '_push_rx', None)
        if rx is not None:
            rx.stop()
        self._stream_ctrl_write(enable=False)
        if stop_server:
            self.parent.stop_stream_server()
        logger.info("Push streaming stopped.")

    # --------- Position-marker streaming (MODE 3: hardware-synced motor scan) ---------
    # Demod data keeps flowing through the unchanged push-streaming path (data3
    # ring). In addition, two external KDC101 position-step triggers (one per axis,
    # level-shifted into exp_p_in[5]/[6] = DIO5_P/DIO6_P) are edge-detected in the FPGA; on each
    # pulse the current demod sample index is recorded into a marker ring (x in the
    # LSB bank, y in the MSB bank). Because data and both marker rings share the
    # same free-running sample counter, a marker value m means demod[m] is the first
    # sample after that spatial boundary, so the continuous demod trace can be sliced
    # into exact spatial bins (x) and lines (y) with no software timing guesswork.
    # See docs/developer_guide/motor_position_sync_scan.md.
    def mapped_stream_start(self, input_source="demod", poll_us=200,
                            ring_bytes=0, coalesce_us=0, force_recompile=False):
        """Start position-marker streaming for hardware-synchronized motor scans.

        Identical to :meth:`push_stream_start` (continuous demod push stream) but
        additionally enables FPGA marker capture, so :meth:`read_x_markers` /
        :meth:`read_y_markers` return the demod-sample indices at each axis's
        encoder position pulses.

        Args:
            input_source (str): 'demod' or 'ftw_corr'.
            poll_us, ring_bytes, coalesce_us, force_recompile: see
                :meth:`push_stream_start`.

        Returns:
            StreamClient: the background demod receiver.
        """
        if input_source not in ("demod", "ftw_corr"):
            logger.warning("Mapped streaming supports 'demod' and 'ftw_corr' only; "
                           "using 'demod'.")
            input_source = "demod"
        # 0. ensure no other consumer is mid-stream on this single-stream board
        self._teardown_existing_push_stream("mapped_stream_start")
        # 1. select input; enable stream + marker capture and reset both (markers
        #    and the demod ring share one sample counter, reset together).
        self.input_select = input_source
        self._stream_ctrl_write(enable=True, reset=True, marker=True)
        # 2. reset PC-side marker read state (FPGA counters are 0 after the reset)
        self._marker_x_rd_ptr = 0
        self._marker_x_total_read = 0
        self._marker_y_rd_ptr = 0
        self._marker_y_total_read = 0
        self._mapped_active = True
        # 3. start the demod push receiver (same infra as push_stream_start)
        port = self.parent.ensure_stream_server(force_recompile=force_recompile)
        host = self.parent.parameters['hostname']
        self._push_rx = StreamClient(host, port, addr_base=self.addr_base,
                                     poll_us=poll_us, ring_bytes=ring_bytes,
                                     coalesce_us=coalesce_us)
        self._push_rx.start()
        self._push_input = input_source
        logger.info("Mapped (marker) streaming started (%s) from %s:%d",
                    input_source, host, port)
        return self._push_rx

    def mapped_stream_read(self):
        """All demod samples received since the last call (see push_stream_read).

        NOTE: marker indices are absolute (from sample 0 at stream start). To use
        them, accumulate every chunk returned here into one contiguous array whose
        index 0 is the first sample of the session, then slice with the markers
        (e.g. via :meth:`slice_by_markers`).
        """
        return self.push_stream_read()

    def _read_marker_ring(self, ring_base, wr_ptr, total_written,
                          rd_ptr_attr, total_read_attr):
        """Wrap-aware, pointer-tracked read of new markers from one marker ring.

        Mirrors the pointer/overflow bookkeeping of :meth:`stream_read`. Returns the
        absolute demod-sample indices recorded since the last call (int64,
        non-negative). On ring overrun (markers produced faster than drained) the
        lost markers are reported and the reader resyncs to the current writer.
        """
        depth = 2 ** MAX_STEPS_BITS
        total_read = int(getattr(self, total_read_attr, 0))
        # Overflow: writer advanced by >= depth markers since our last read
        delta = (int(total_written) - total_read) & 0xFFFFFFFF
        if delta >= depth:
            logger.warning("Marker ring overflow (>= %d markers since last read); "
                           "drained too slowly. Resyncing; some markers lost.", depth)
            setattr(self, rd_ptr_attr, int(wr_ptr))
            setattr(self, total_read_attr, int(total_written))
            return np.array([], dtype=np.int64)

        rd = int(getattr(self, rd_ptr_attr, 0)) % depth
        avail = (wr_ptr - rd) if wr_ptr >= rd else ((depth - rd) + wr_ptr)
        if avail == 0:
            return np.array([], dtype=np.int64)

        first_len = min(avail, depth - rd)
        segs = []
        if first_len > 0:
            segs.append(self._reads(ring_base + rd * 4, first_len))
        rem = avail - first_len
        if rem > 0:
            segs.append(self._reads(ring_base, rem))  # wrapped segment

        if len(segs) > 1:
            data = np.concatenate([np.asarray(s, dtype=np.uint32) for s in segs])
        else:
            data = np.asarray(segs[0], dtype=np.uint32)

        setattr(self, rd_ptr_attr, (rd + avail) % depth)
        setattr(self, total_read_attr, total_read + avail)
        return data.astype(np.int64)

    def read_x_markers(self):
        """Return new x-axis (fast-axis) marker sample-indices since the last call.

        Returns:
            np.ndarray int64 of absolute demod-sample indices, one per x position
            pulse. Empty if not mapped-streaming or no new markers.
        """
        if not getattr(self, '_mapped_active', False):
            return np.array([], dtype=np.int64)
        # bulk read wr_ptr (0x30) and count (0x34)
        vals = self._reads(ADDR_MARKER_X_WR_PTR, 2)
        return self._read_marker_ring(BRAM_LSB_BASE_ADDR, int(vals[0]), int(vals[1]),
                                      '_marker_x_rd_ptr', '_marker_x_total_read')

    def read_y_markers(self):
        """Return new y-axis (slow-axis) marker sample-indices since the last call.

        Returns:
            np.ndarray int64 of absolute demod-sample indices, one per y position
            pulse (i.e. one per scan line). Empty if not mapped-streaming or no new
            markers.
        """
        if not getattr(self, '_mapped_active', False):
            return np.array([], dtype=np.int64)
        # bulk read wr_ptr (0x38) and count (0x3C)
        vals = self._reads(ADDR_MARKER_Y_WR_PTR, 2)
        return self._read_marker_ring(BRAM_MSB_BASE_ADDR, int(vals[0]), int(vals[1]),
                                      '_marker_y_rd_ptr', '_marker_y_total_read')

    def mapped_stream_stop(self, stop_server=False):
        """Stop the demod receiver and disable streaming + marker capture."""
        rx = getattr(self, '_push_rx', None)
        if rx is not None:
            rx.stop()
        self._stream_ctrl_write(enable=False, marker=False)
        self._mapped_active = False
        if stop_server:
            self.parent.stop_stream_server()
        logger.info("Mapped (marker) streaming stopped.")

    # ---- x/y markers ON a self-describing (dual/marked) stream (2D multi-res scan) --
    # The multi-resonance tracker runs a continuous self-describing stream (dual or
    # marked): [err, corr, cic, step] records in the data3 ring, so the resonance label
    # travels inline and the ram_lsb/ram_msb marker banks are FREE. A 2D motor scan
    # can therefore capture KDC x/y position markers in those free banks WHILE the
    # tracker keeps hopping -- the composition the "dedicated 4th bank" note
    # anticipated, needing no 4th bank precisely because dual/marked keep the label
    # inline (the earlier caveat only applied to single-word hop markers, which use
    # ram_msb for the step label). This is enabled via ``hop_stream_start(...,
    # xy_markers=True)``, which RESETS the stream so demod word 0 aligns with marker 0
    # (the continuous-hop FSM + per-slot lock integrators are separate from the stream
    # engine, so the reset does not perturb the lock).
    #
    # NOTE on marker units: in dual/marked mode STREAM_SAMPLES -- and hence the marker
    # values -- count WORDS (the width reported by ``stream_words_per_sample``), not
    # demod samples. Use :meth:`markers_to_sample_index` to get the time-sample
    # index for slicing a reconstructed per-resonance trace. In single-word mapped
    # streaming (``mapped_stream_start`` with 'demod'/'ftw_corr') the markers are
    # already sample indices -- do NOT divide there.
    @staticmethod
    def markers_to_sample_index(markers, words_per_sample=4):
        """Convert marker WORD indices to self-describing sample indices."""
        width = int(words_per_sample)
        if width not in (3, 4):
            raise ValueError("words_per_sample must be 3 or 4")
        return np.asarray(markers, dtype=np.int64) // width

    @staticmethod
    def markers_to_triplet_index(markers):
        """Convert x/y marker WORD indices to triplet (time-sample) indices.

        In the self-describing dual/marked streams three words ([err, corr, step])
        are written per demod period, so the free-running sample counter -- and hence
        the x/y marker values captured by ``hop_stream_start(..., xy_markers=True)`` --
        advance by 3 per time sample. Floor-dividing by 3 maps a marker to the triplet
        (quantization <= 1 triplet ~ 32.8 us, negligible against a spatial bin), which
        is the correct index into a per-resonance trace from
        :meth:`reconstruct_marked_series` / :meth:`reconstruct_dual_hop_series`.

        Do NOT use this for single-word mapped streaming (demod/ftw_corr), where the
        markers are already sample indices.
        """
        return np.asarray(markers, dtype=np.int64) // 3

    # --------- Hop-boundary marker streaming (multi-resonance monitoring) ---------
    # The live resonance's correction/error keeps flowing through the unchanged push
    # stream (data3 ring). In addition, every time the FPGA scan FSM advances
    # current_step (an LO hop) the pair (tick = stream sample index, current_step) is
    # recorded into a hop-marker ring (tick in the LSB bank, step in the MSB bank).
    # Because the data ring and the markers share the one free-running sample counter,
    # the markers partition the continuous stream into segments, each labelled by the
    # resonance live in it -- the deterministic (tick, resonance) -> value contract of
    # Section 11. The scan FSM drives the hops while the stream runs concurrently.
    # See docs/developer_guide/multi_resonance_tracking.md (Section 11, Phase B-stream).
    def hop_stream_start(self, input_source="ftw_corr", poll_us=200,
                         ring_bytes=0, coalesce_us=0, force_recompile=False,
                         xy_markers=False):
        """Start hop-marker streaming for multi-resonance monitoring.

        Like :meth:`push_stream_start` (continuous push stream of the live
        resonance's correction/error) but additionally enables FPGA hop-marker
        capture, so :meth:`read_hop_markers` returns the (tick, current_step) pairs
        at each LO hop. Run the scan FSM concurrently (configure ``num_steps`` etc.
        and call :meth:`start`) so ``current_step`` actually advances and drives the
        hops; the FSM's accumulator BRAM writes are suppressed while streaming.

        Args:
            input_source (str): 'ftw_corr' (per-resonance correction, the usual
                monitoring quantity), 'demod' (per-resonance error), or 'dual'
                (self-describing [err, corr, cic, step] record stream for simultaneous
                4-trace reconstruction; no hop-marker ring is used, the step label
                is inline -- decode with :meth:`reconstruct_dual_hop_series`).
            poll_us, ring_bytes, coalesce_us, force_recompile: see
                :meth:`push_stream_start`.
            xy_markers (bool): for 'dual'/'marked' only -- also capture KDC x/y
                position markers in the (free) LSB/MSB banks for a 2D multi-resonance
                motor scan. The reset aligns demod word 0 with the marker origin; read
                the markers with :meth:`read_x_markers` / :meth:`read_y_markers` (WORD
                indices; use :meth:`markers_to_sample_index`).

        Returns:
            StreamClient: the background push receiver.
        """
        dual = (input_source == "dual")
        marked = (input_source == "marked")
        if not (dual or marked) and input_source not in ("demod", "ftw_corr"):
            logger.warning("Hop streaming supports 'demod', 'ftw_corr', 'dual' and "
                           "'marked' only; using 'ftw_corr'.")
            input_source = "ftw_corr"
        # 0. ensure no other consumer is mid-stream on this single-stream board
        self._teardown_existing_push_stream("hop_stream_start")
        # 1. select input; enable stream. Single-word modes use the hop-marker ring
        #    (tick/step in the LSB/MSB banks). Dual (bit4) and marked (bit5) are
        #    self-describing (step is inline in each triplet) and need no marker ring.
        #    Marked additionally emits dead-time samples on a free-running tick so the
        #    PC timeline is uniform/exact (decode with reconstruct_marked_series).
        #    Disable x/y position markers (mutually exclusive, same banks).
        if not (dual or marked):
            self.input_select = input_source
        # xy_markers: capture KDC x/y position markers in the (free) LSB/MSB banks
        # alongside a self-describing dual/marked stream (2D multi-resonance motor
        # scan). Only valid for dual/marked (single-word modes need those banks for
        # the hop-marker ring). The reset (bit1) here aligns demod word 0 with the
        # marker origin, so accumulated words and marker indices share one axis.
        want_xy = bool(xy_markers) and (dual or marked)
        if xy_markers and not (dual or marked):
            logger.warning("hop_stream_start: xy_markers only supported with 'dual'/"
                            "'marked' (self-describing) sources; ignoring.")
        self._stream_ctrl_write(enable=True, reset=True, marker=want_xy,
                                hop=(not (dual or marked)), dual=dual, marked=marked)
        # 2. reset PC-side hop-marker read state (FPGA counters are 0 after the reset)
        self._hop_rd_ptr = 0
        self._hop_total_read = 0
        self._hop_active = (not (dual or marked))
        self._dual_active = dual
        self._marked_active = marked
        # x/y position-marker read state (only active when want_xy). read_x_markers /
        # read_y_markers gate on _mapped_active; the markers are WORD indices here;
        # convert with markers_to_sample_index using the FPGA-reported width.
        self._marker_x_rd_ptr = 0
        self._marker_x_total_read = 0
        self._marker_y_rd_ptr = 0
        self._marker_y_total_read = 0
        self._mapped_active = want_xy
        # 3. start the push receiver (same infra as push_stream_start)
        port = self.parent.ensure_stream_server(force_recompile=force_recompile)
        host = self.parent.parameters['hostname']
        self._push_rx = StreamClient(host, port, addr_base=self.addr_base,
                                     poll_us=poll_us, ring_bytes=ring_bytes,
                                     coalesce_us=coalesce_us)
        self._push_rx.start()
        self._push_input = input_source
        logger.info("Hop-marker streaming started (%s) from %s:%d",
                    input_source, host, port)
        return self._push_rx

    def hop_stream_read(self):
        """All stream samples received since the last call (see push_stream_read).

        Accumulate every chunk into one contiguous array (index 0 = first sample of
        the session) so the absolute hop-marker ticks line up, then reconstruct with
        :meth:`reconstruct_hop_series`.
        """
        return self.push_stream_read()

    def read_hop_markers(self):
        """Return new hop markers (tick, step) pairs recorded since the last call.

        Wrap-aware, pointer-tracked (mirrors :meth:`read_x_markers`). Both rings are
        read in lockstep at the same indices.

        Returns:
            (ticks, steps): two int64 np.ndarrays of equal length. ``ticks[k]`` is
            the absolute stream sample index at which ``current_step`` became
            ``steps[k]`` (i.e. the first sample of that segment). Empty if not
            hop-streaming or no new markers.
        """
        empty = np.array([], dtype=np.int64)
        if not getattr(self, '_hop_active', False):
            return empty, empty
        vals = self._reads(ADDR_HOP_WR_PTR, 2)  # wr_ptr (0x40), count (0x44)
        wr_ptr, total_written = int(vals[0]), int(vals[1])
        depth = 2 ** MAX_STEPS_BITS
        total_read = int(getattr(self, '_hop_total_read', 0))
        delta = (total_written - total_read) & 0xFFFFFFFF
        if delta >= depth:
            logger.warning("Hop-marker ring overflow (>= %d markers since last read); "
                           "drained too slowly. Resyncing; some markers lost.", depth)
            self._hop_rd_ptr = wr_ptr
            self._hop_total_read = total_written
            return empty, empty
        rd = int(getattr(self, '_hop_rd_ptr', 0)) % depth
        avail = (wr_ptr - rd) if wr_ptr >= rd else ((depth - rd) + wr_ptr)
        if avail == 0:
            return empty, empty

        def _read_ring(base):
            first_len = min(avail, depth - rd)
            segs = [self._reads(base + rd * 4, first_len)]
            rem = avail - first_len
            if rem > 0:
                segs.append(self._reads(base, rem))  # wrapped segment
            if len(segs) > 1:
                arr = np.concatenate([np.asarray(s, dtype=np.uint32) for s in segs])
            else:
                arr = np.asarray(segs[0], dtype=np.uint32)
            return arr.astype(np.int64)

        ticks = _read_ring(BRAM_LSB_BASE_ADDR)
        steps = _read_ring(BRAM_MSB_BASE_ADDR)
        self._hop_rd_ptr = (rd + avail) % depth
        self._hop_total_read = total_read + avail
        return ticks, steps

    def hop_stream_stop(self, stop_server=False):
        """Stop the push receiver and disable streaming + hop/xy-marker capture."""
        rx = getattr(self, '_push_rx', None)
        if rx is not None:
            rx.stop()
        self._stream_ctrl_write(enable=False, hop=False, dual=False, marked=False,
                                marker=False)
        self._hop_active = False
        self._dual_active = False
        self._marked_active = False
        self._mapped_active = False
        if stop_server:
            self.parent.stop_stream_server()
        logger.info("Hop-marker streaming stopped.")

    def continuous_hop_start(self, nslots, input_source="ftw_corr",
                             dwell_time=None, settling_time=None,
                             trigger_length=None, poll_us=200, ring_bytes=0,
                             coalesce_us=0, force_recompile=False):
        """Start indefinite hardware-driven multi-resonance hopping + monitoring.

        One call sets up the complete continuous tracker on the FPGA: it starts the
        hop-marker push stream and then puts the scan FSM into continuous/loop mode
        so it cycles ``current_step`` 0..nslots-1 **forever** (one LO-hop trigger per
        step, wrapping at the last resonance) until :meth:`continuous_hop_stop`. No
        software re-arming — the hop source is entirely on the FPGA. The per-channel
        freeze/settle, cal-slot selection and per-slot integrators all follow
        ``current_step`` as in the finite case.

        The push stream + hop markers run concurrently and share the demod sample
        counter, so the PC reconstructs deterministic per-resonance traces with
        :meth:`read_hop_markers` + :meth:`reconstruct_hop_series`. For *indefinite*
        runs, drain markers (``read_hop_markers``) and stream
        (``hop_stream_read``) frequently and process them incrementally rather than
        accumulating one ever-growing array (the 32-bit sample tick wraps after
        ~39 h; use wrap-safe deltas).

        Args:
            nslots (int): number of resonances N -> written to ``num_steps`` (the
                loop length). Must match the LO JUMP_LIST length.
            input_source (str): 'ftw_corr' (per-resonance correction, default),
                'demod' (per-resonance error), or 'dual' (self-describing
                [err, corr, step] triplet stream -> simultaneous 4-trace
                reconstruction via :meth:`reconstruct_dual_hop_series`).
            dwell_time, settling_time, trigger_length (float|None): scan timing in
                seconds; if given, applied before starting (controls the per-step
                live time / LO-settle / trigger pulse). If None, the current values
                are kept. Use the bench-verified config timing (trigger 50 us,
                settling 100 us, Windfreak t~1 ms) for clean JUMP_LIST wrapping.
            poll_us, ring_bytes, coalesce_us, force_recompile: see
                :meth:`push_stream_start`.

        Returns:
            StreamClient: the background push receiver.
        """
        if self.busy:
            logger.warning("Scan module is already busy; stop it before starting "
                           "continuous hopping. Ignoring.")
            return None
        if nslots is not None:
            self.num_steps = int(nslots)
        if dwell_time is not None:
            self.dwell_time = dwell_time
        if settling_time is not None:
            self.settling_time = settling_time
        if trigger_length is not None:
            self.trigger_length = trigger_length
        # Start the stream + hop markers first (resets the data/marker rings and
        # brings up the ARM push server), then start the indefinite hop loop so
        # current_step begins advancing and driving the hops.
        rx = self.hop_stream_start(input_source=input_source, poll_us=poll_us,
                                   ring_bytes=ring_bytes, coalesce_us=coalesce_us,
                                   force_recompile=force_recompile)
        self.start(continuous=True)
        logger.info("Continuous hop tracking started: N=%d, source=%s, dwell=%.1f us.",
                    self.num_steps, input_source, self.dwell_time * 1e6)
        return rx

    def continuous_hop_stop(self, stop_server=False):
        """Stop continuous hopping and the hop-marker stream (inverse of
        :meth:`continuous_hop_start`)."""
        self.stop()  # leave continuous/loop mode (clears reg_continuous on the FPGA)
        self.hop_stream_stop(stop_server=stop_server)
        logger.info("Continuous hop tracking stopped.")

    @staticmethod
    def _ffill(a):
        """Forward-fill NaNs in a 1-D float array (zero-order hold).

        Positions before the first non-NaN value stay NaN.
        """
        a = np.asarray(a, dtype=np.float64)
        valid = ~np.isnan(a)
        if not valid.any():
            return a.copy()
        idx = np.where(valid, np.arange(a.size), 0)
        np.maximum.accumulate(idx, out=idx)
        out = a[idx]
        first_valid = int(np.argmax(valid))
        out[:first_valid] = np.nan
        return out

    @classmethod
    def reconstruct_hop_series(cls, values, ticks, steps, nslots=None, to_hz=False):
        """Reconstruct per-resonance traces on the common tick axis (Section 11).

        Implements the deterministic fresh-only + zero-order-hold reconstruction:
        the hop markers partition the stream into segments labelled by resonance;
        each resonance's trace carries its *fresh* samples while it is live and the
        *held* (ZOH) value while it is parked. NaN is reserved for transport loss
        only (a dropped sample inside a live dwell stays NaN); samples before a
        resonance's first dwell are NaN (no data yet).

        Args:
            values (np.ndarray): the full contiguous stream (float64, NaN = transport
                loss), index 0 = first sample of the session. Concatenate every
                :meth:`hop_stream_read` chunk.
            ticks (array-like): hop-marker ticks from :meth:`read_hop_markers`.
            steps (array-like): hop-marker step values (same length as ticks).
            nslots (int): number of resonances. Default: ``max(steps) + 1``.
            to_hz (bool): if True, convert FTW values to Hz (use for 'ftw_corr').

        Returns:
            np.ndarray of shape (nslots, len(values)): per-resonance trace on the
            common tick axis. Row r is resonance r's correction/error over time.
        """
        values = np.asarray(values, dtype=np.float64)
        n = values.size
        ticks = np.asarray(ticks, dtype=np.int64)
        steps = np.asarray(steps, dtype=np.int64)
        if nslots is None:
            nslots = int(steps.max()) + 1 if steps.size else 1
        out = np.full((nslots, n), np.nan, dtype=np.float64)
        if ticks.size == 0:
            return out / FTW_PER_HZ if to_hz else out

        # Segment k spans [ticks[k], ticks[k+1]) (last runs to n), labelled steps[k].
        bounds = np.concatenate([ticks, [n]])
        live = [np.zeros(n, dtype=bool) for _ in range(nslots)]
        for k in range(ticks.size):
            a = max(0, int(bounds[k]))
            b = min(n, int(bounds[k + 1]))
            s = int(steps[k])
            if 0 <= s < nslots and b > a:
                out[s, a:b] = values[a:b]   # fresh samples (transport-loss NaN kept)
                live[s][a:b] = True

        for r in range(nslots):
            filled = cls._ffill(out[r])              # ZOH across parked regions
            loss = live[r] & np.isnan(values)        # transport loss inside live dwell
            filled[loss] = np.nan                    # ...stays NaN, not held
            out[r] = filled

        return out / FTW_PER_HZ if to_hz else out

    @classmethod
    def reconstruct_dual_hop_series(cls, words, nslots=None, to_hz_corr=True,
                                    words_per_sample=3):
        """Reconstruct 4 per-resonance traces from a dual-quantity (self-describing)
        stream (FPGA STREAM_CONTROL[4]).

        Current dual mode writes four words per demod strobe into the data3 ring:
        ``[err, corr, cic, step]``. Legacy three-word records remain decodable.
        uses the inline ``step`` column to label each sample's resonance, and builds
        per-resonance error and correction traces with the same fresh-while-live +
        zero-order-hold semantics as :meth:`reconstruct_hop_series` (NaN reserved for
        transport loss inside a live dwell). Because the label travels with the value
        in one ring, value and label can never desync (no separate marker channel).

        Args:
            words (np.ndarray): contiguous int32 words as float64 (NaN = transport
                loss), index 0 = first word of the session. Concatenate every
                :meth:`hop_stream_read` chunk. A trailing incomplete record is dropped.
            nslots (int): number of resonances N. Default: max finite step + 1.
            to_hz_corr (bool): convert the correction column from FTW to Hz.

        Returns:
            dict containing ``err``, ``corr``, and ``cic`` arrays of shape (N, T).
        """
        words = np.asarray(words, dtype=np.float64).ravel()
        width = int(words_per_sample)
        if width not in (3, 4):
            raise ValueError("words_per_sample must be 3 or 4")
        usable = (words.size // width) * width
        if usable == 0:
            z = np.full((int(nslots) if nslots else 1, 0), np.nan, dtype=np.float64)
            return {'err': z, 'corr': z.copy(), 'cic': z.copy()}
        record = words[:usable].reshape(-1, width)
        err_col, corr_col = record[:, 0], record[:, 1]
        if width == 4:
            cic_col, step_raw = record[:, 2], record[:, 3]
        else:
            cic_col, step_raw = np.full(record.shape[0], np.nan), record[:, 2]
        T = record.shape[0]

        # The resonance label is piecewise-constant (one value per dwell), so a lost
        # step word is recovered by zero-order hold from the preceding sample.
        step_filled = cls._ffill(step_raw)
        finite = np.isfinite(step_filled)
        step_int = np.where(finite, np.rint(step_filled), -1).astype(np.int64)
        if nslots is None:
            nslots = (int(step_int[step_int >= 0].max()) + 1
                      if np.any(step_int >= 0) else 1)
        nslots = int(nslots)

        # Alignment guard: legal step labels are [0, nslots). Out-of-range finite
        # labels indicate a word-level misalignment (should never happen).
        bad = finite & ((step_int < 0) | (step_int >= nslots))
        if np.any(bad):
            logger.warning("Dual-stream reconstruct: %d/%d samples have out-of-range "
                           "step labels (expected 0..%d); possible word "
                           "misalignment.", int(bad.sum()), T, nslots - 1)

        out_err = np.full((nslots, T), np.nan, dtype=np.float64)
        out_corr = np.full((nslots, T), np.nan, dtype=np.float64)
        out_cic = np.full((nslots, T), np.nan, dtype=np.float64)
        for r in range(nslots):
            live = (step_int == r)
            e = np.full(T, np.nan, dtype=np.float64); e[live] = err_col[live]
            c = np.full(T, np.nan, dtype=np.float64); c[live] = corr_col[live]
            i = np.full(T, np.nan, dtype=np.float64); i[live] = cic_col[live]
            ef = cls._ffill(e)                       # ZOH across parked regions
            cf = cls._ffill(c)
            inf = cls._ffill(i)
            ef[live & np.isnan(err_col)] = np.nan    # transport loss stays NaN
            cf[live & np.isnan(corr_col)] = np.nan
            inf[live & np.isnan(cic_col)] = np.nan
            out_err[r] = ef
            out_corr[r] = cf
            out_cic[r] = inf
        if to_hz_corr:
            out_corr = out_corr / FTW_PER_HZ
        return {'err': out_err, 'corr': out_corr, 'cic': out_cic}

    @classmethod
    def reconstruct_marked_series(cls, words, nslots=None, to_hz_corr=True,
                                  words_per_sample=3):
        """Reconstruct per-resonance traces from the MARKED-continuous stream
        (FPGA STREAM_CONTROL[5]).

        Current marked mode writes ``[err, corr, cic, state]`` on a FREE-RUNNING /4096
        tick -- one sample EVERY demod period regardless of the per-hop freeze. So
        unlike the dual stream (whose triplets are gated by the demod strobe, making
        the settle/hop dead-time zero-width in the word stream and corrupting the PC
        timeline), here every demod-period slot is present and the time axis is EXACT
        and uniform: ``t[k] = k / (FPGA_CLK_HZ/4096)``. ``state`` is the active
        resonance index (0..N-1) on LIVE samples, or a DEAD sentinel during the
        per-hop settle/freeze (FPGA writes 32'hFFFFFFFF; the stream client may surface
        it as -1 or 4294967295 depending on signedness -- both are handled here as
        "not a valid step" -> dead).

        Args:
            words (np.ndarray): contiguous int32-as-float64 stream (NaN = transport
                loss), index 0 = first word of the session. Concatenate every
                :meth:`hop_stream_read` chunk. A trailing incomplete record is dropped.
            nslots (int): number of resonances N. Default: max valid step + 1.
            to_hz_corr (bool): convert the correction column from FTW to Hz.

        Returns:
            dict with (T = number of complete records = uniform samples at
            FPGA_CLK_HZ/4096):
              - ``'err'``  (N, T) float64, raw LSB: the live error of resonance r at
                samples where it is live, NaN elsewhere (dead OR another resonance OR
                transport loss). Fresh-only (NO zero-order hold) so the dead-time is
                explicit.
              - ``'corr'`` (N, T) float64, Hz (or FTW): live correction, same masking.
              - ``'cic'``  (N, T) float64, raw pre-FIR CIC LSB, same masking.
              - ``'dead'`` (T,) bool: True where the sample is dead-time (settle/hop).
              - ``'step'`` (T,) int64: resonance index per sample, or -1 for dead/loss.
              - ``'sample_rate'`` float: FPGA_CLK_HZ/4096 (exact uniform rate).
        """
        words = np.asarray(words, dtype=np.float64).ravel()
        width = int(words_per_sample)
        if width not in (3, 4):
            raise ValueError("words_per_sample must be 3 or 4")
        usable = (words.size // width) * width
        fs = FPGA_CLK_HZ / 4096.0
        if usable == 0:
            z = np.full((int(nslots) if nslots else 1, 0), np.nan, dtype=np.float64)
            return {'err': z, 'corr': z.copy(), 'cic': z.copy(),
                    'dead': np.zeros(0, dtype=bool), 'step': np.zeros(0, dtype=np.int64),
                    'sample_rate': fs}
        record = words[:usable].reshape(-1, width)
        err_col, corr_col = record[:, 0], record[:, 1]
        if width == 4:
            cic_col, state_raw = record[:, 2], record[:, 3]
        else:
            cic_col, state_raw = np.full(record.shape[0], np.nan), record[:, 2]
        T = record.shape[0]

        # A state word is a valid step iff it is a finite, non-negative integer below
        # nslots. The DEAD sentinel 0xFFFFFFFF surfaces as -1.0 (signed) or
        # 4294967295.0 (unsigned); either way it fails the 0<=s<nslots test -> dead.
        finite = np.isfinite(state_raw)
        step_round = np.where(finite, np.rint(state_raw), -1.0)
        if nslots is None:
            valid_steps = step_round[finite & (step_round >= 0) & (step_round < 4096)]
            nslots = int(valid_steps.max()) + 1 if valid_steps.size else 1
        nslots = int(nslots)
        live = finite & (step_round >= 0) & (step_round < nslots)
        step_int = np.where(live, step_round, -1).astype(np.int64)
        dead = finite & ~live          # finite state but not a valid step = dead-time
        # (transport-loss samples: state is NaN -> neither live nor dead; step = -1)

        out_err = np.full((nslots, T), np.nan, dtype=np.float64)
        out_corr = np.full((nslots, T), np.nan, dtype=np.float64)
        out_cic = np.full((nslots, T), np.nan, dtype=np.float64)
        for r in range(nslots):
            live_r = (step_int == r)
            err_sel = live_r & np.isfinite(err_col)
            corr_sel = live_r & np.isfinite(corr_col)
            out_err[r, err_sel] = err_col[err_sel]
            out_corr[r, corr_sel] = corr_col[corr_sel]
            cic_sel = live_r & np.isfinite(cic_col)
            out_cic[r, cic_sel] = cic_col[cic_sel]
        if to_hz_corr:
            out_corr = out_corr / FTW_PER_HZ
        return {'err': out_err, 'corr': out_corr, 'cic': out_cic, 'dead': dead,
                'step': step_int, 'sample_rate': fs}

    @staticmethod
    def slice_by_markers(demod, markers):
        """Slice a contiguous demod array into per-bin segments at marker indices.

        Args:
            demod (np.ndarray): the full demod trace whose index 0 is the first
                sample of the streaming session (concatenate every
                :meth:`mapped_stream_read` chunk).
            markers (array-like): absolute sample indices (e.g. from
                :meth:`read_x_markers`). N markers define N-1 bins between
                consecutive markers; samples before the first / after the last
                marker are not part of any bin.

        Returns:
            list[np.ndarray]: the demod samples in each bin, in marker order.
        """
        m = np.asarray(markers, dtype=np.int64)
        n = demod.shape[0] if hasattr(demod, 'shape') else len(demod)
        bins = []
        for i in range(len(m) - 1):
            a = max(0, int(m[i]))
            b = min(n, int(m[i + 1]))
            bins.append(demod[a:b])
        return bins

    def ftw_to_hz(self, ftw_values):
        """Convert raw FTW (Frequency Tuning Word) values to Hz.

        Args:
            ftw_values: Scalar, list, or np.ndarray of signed 32-bit FTW values

        Returns:
            Frequency values in Hz (same shape as input)

        Notes:
            - FTW correction represents frequency offset applied by ODMR tracker
            - Conversion: freq_hz = ftw_value / FTW_PER_HZ
            - FTW_PER_HZ = 2^32 / 125 MHz ≈ 34.359738 FTW/Hz
            - Typical range: ±1 MHz correction ≈ ±34,359,738 FTW units

        Example:
            >>> ftw_data = scan.stream_read()
            >>> freq_drift_hz = scan.ftw_to_hz(ftw_data)
            >>> print(f"Resonance drift: {freq_drift_hz.mean():.1f} Hz")
        """
        return np.asarray(ftw_values, dtype=np.float64) / FTW_PER_HZ


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


    def start(self, continuous=False):
        """
        Starts the sweep sequence.

        Args:
            continuous (bool): If False (default), run a single finite sweep of
                ``num_steps`` steps, then stop (``done`` asserts). If True, start
                continuous/loop hopping: the FPGA FSM wraps ``step_counter``->0 at
                the last step and keeps emitting LO-hop triggers + advancing
                ``current_step`` (0..num_steps-1) **indefinitely** until
                :meth:`stop`/:meth:`reset`. ``busy`` stays True and ``done`` never
                asserts. Set ``num_steps`` = number of resonances N; run a push/hop
                stream concurrently (see :meth:`continuous_hop_start`) to monitor the
                per-resonance corrections. This is the hardware-driven hop source for
                indefinite multi-resonance tracking (no software re-arming).

        Checks for potential accumulator overflow before starting (skipped in
        continuous mode, where streaming suppresses accumulation).
        """
        if self.busy:
            logger.warning("Scan module is already busy. Ignoring start command.")
            return

        # If we're in DONE state, reset first to ensure clean start
        if self.done:
            self.reset()

        if continuous:
            # In continuous mode the data path is the concurrent push stream; the
            # scan accumulator is suppressed, so the 64-bit overflow check is moot.
            logger.info("Starting CONTINUOUS scan-loop hopping (num_steps=%d, "
                        "input source: %s)...", self.num_steps, self.input_select)
            self._write(ADDR_CONTROL,
                        (1 << CONTROL_START_BIT) | (1 << CONTROL_CONTINUOUS_BIT))
        else:
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

    # TODO: improve naming, to differentiate method names between stream and scan modes
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
