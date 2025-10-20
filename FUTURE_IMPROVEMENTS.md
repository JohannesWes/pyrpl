# Future Improvements and TODOs

This document tracks potential optimizations and improvements for future development of PyRPL.

## 1. Performance Optimizations

### 1.1. Scan Module: Contiguous BRAM Mapping for Single-Call Data Readout

**Status:** Proposed
**Priority:** Medium
**Estimated Effort:** 4-6 hours (FPGA + Python changes)
**Branch Context:** scan_module_dev_johannes_filtering_tests

#### Current Situation

The Scan module uses three separate Block RAMs (BRAMs) to store scan data:
- **LSB BRAM** (0x10000): Lower 32 bits of 64-bit accumulator
- **MSB BRAM** (0x20000): Upper 32 bits of 64-bit accumulator
- **Data3 BRAM** (0x30000): Valid sample counts per step (scan mode) or streaming data (stream mode)

These are mapped to **non-contiguous 64KB address regions**, requiring **three separate TCP `_reads()` calls** to fetch complete scan data (see `scan.py:516-521`).

#### Proposed Optimization

Remap the BRAMs to **contiguous 4KB regions**:

```python
BRAM_LSB_BASE_ADDR   = 0x10000  # 0x10000-0x10FFF (4096 words = 16KB)
BRAM_MSB_BASE_ADDR   = 0x11000  # 0x11000-0x11FFF (4096 words = 16KB)
BRAM_DATA3_BASE_ADDR = 0x12000  # 0x12000-0x12FFF (4096 words = 16KB)
```

This would allow reading all three BRAMs in a **single `_reads()` call**:

```python
# Single bulk read instead of three separate reads
all_data = self._reads(BRAM_LSB_BASE_ADDR, n_steps * 3)

# Split the result
lsb_data   = all_data[0:n_steps]
msb_data   = all_data[n_steps:n_steps*2]
data3_data = all_data[n_steps*2:n_steps*3]

# Combine LSB and MSB as before
combined = (msb_data.astype(np.uint64) << 32) | lsb_data.astype(np.uint64)
data_accum = combined.view(np.int64)
```

#### Benefits

1. **Reduced network overhead:** Eliminates 2 out of 3 TCP round-trips (~66% reduction)
2. **More atomic operation:** All three BRAMs captured in same transaction
3. **Simpler error handling:** Single read success/failure path
4. **Increased maximum steps:** Could support up to 21,845 steps instead of 4,096
   - Calculation: 65,535 max words (monitor_server.c limit) / 3 BRAMs = 21,845 steps

#### Implementation Checklist

**Files to modify:**

1. **FPGA Verilog** (`pyrpl/fpga/rtl/scan_new.v`)
   - [ ] Update address decode logic around line 241-243:
     ```verilog
     // Current (non-contiguous):
     wire bram_access_lsb   = (reg_addr[19:16] == 4'h1);  // 0x10000-0x1FFFF
     wire bram_access_msb   = (reg_addr[19:16] == 4'h2);  // 0x20000-0x2FFFF
     wire bram_access_data3 = (reg_addr[19:16] == 4'h3);  // 0x30000-0x3FFFF

     // Proposed (contiguous 4KB regions):
     wire bram_access_lsb   = (reg_addr[19:12] == 8'h10);  // 0x10000-0x10FFF
     wire bram_access_msb   = (reg_addr[19:12] == 8'h11);  // 0x11000-0x11FFF
     wire bram_access_data3 = (reg_addr[19:12] == 8'h12);  // 0x12000-0x12FFF
     ```
   - [ ] Verify no address conflicts with other module regions
   - [ ] Update address documentation in Verilog header comments (lines 11-16)

2. **Python Constants** (`pyrpl/hardware_modules/scan.py`)
   - [ ] Update BRAM address constants (lines 80-82):
     ```python
     BRAM_LSB_BASE_ADDR   = 0x10000
     BRAM_MSB_BASE_ADDR   = 0x11000  # Changed from 0x20000
     BRAM_DATA3_BASE_ADDR = 0x12000  # Changed from 0x30000
     ```

3. **Python Data Readout** (`pyrpl/hardware_modules/scan.py`)
   - [ ] Modify `get_data()` method (lines 528-530) to use single bulk read
   - [ ] Add bounds checking for n_steps * 3 <= 65535
   - [ ] Update error handling for unified read operation
   - [ ] Consider adding a config flag to support both old/new FPGA versions

4. **Testing**
   - [ ] Verify FPGA compiles without timing violations
   - [ ] Test with various step counts (1, 100, 4096, 21845)
   - [ ] Verify data integrity (LSB/MSB/Data3 match expected values)
   - [ ] Benchmark read performance (before/after comparison)
   - [ ] Test streaming mode still works correctly

5. **Documentation**
   - [ ] Update scan_new.v header documentation
   - [ ] Update scan.py module docstring
   - [ ] Add note about minimum PyRPL version requiring new FPGA bitfile

#### Technical Constraints

- **Maximum `_reads()` length:** 65,535 words (262,140 bytes)
  - Defined in `monitor_server.c:88` as `MAX_LENGTH`
  - Enforced in `redpitaya_client.py:108-110`
- **Current BRAM depth:** 4,096 words (2^12) per BRAM
- **Proposed max steps:** 21,845 (limited by 65,535 / 3)
- **Address space available:** 0x10000-0x1FFFF region (64KB) is allocated to scan module

#### Backward Compatibility

This change would require:
- **FPGA recompilation** with updated address mapping
- **Config file migration** (no Python-side config changes needed)
- **Version detection:** Consider adding FPGA version register to auto-detect capability

#### Related Code References

- **monitor_server.c:246-266** - `read_values()` implementation that reads contiguous memory
- **redpitaya_client.py:107-123** - `_reads()` TCP protocol implementation
- **scan.py:255-270** - Example of reading consecutive registers (`stream_status()`)
- **scan.py:516-521** - Current three-read implementation in `get_data()`

#### Performance Estimate

For a typical 4096-step scan over network (assuming 1ms RTT per read):
- **Current:** 3 reads × 1ms = 3ms overhead
- **Optimized:** 1 read × 1ms = 1ms overhead
- **Improvement:** 2ms saved per scan readout (~66% reduction in read latency)

For high-speed continuous streaming where `get_data()` is called repeatedly, this could provide significant throughput improvements.

---

## 2. Reliability and Robustness Improvements

### 2.1. Scan Module: Implement Hardware Overflow Detection in Streaming Mode

**Status:** Proposed
**Priority:** Medium
**Estimated Effort:** 1-2 hours (FPGA only)
**Branch Context:** scan_module_dev_johannes_filtering_tests

#### Current Situation

The streaming overflow flag (`reg_stream_overflow`) is defined in the FPGA but not properly implemented (see `scan_new.v:475`):

```verilog
if (&reg_stream_wr_ptr) begin
    reg_stream_overflow <= reg_stream_overflow; // TODO: Check this
end
```

The flag is just assigned to itself, doing nothing. The Python side acknowledges this at `scan.py:269`: "hardware overflow flag not currently working/updated correctly in FPGA".

Currently, overflow detection relies entirely on software-side counter comparison (`scan.py:305`), which:
- Requires periodic polling from Python
- Can miss fast overflows between polls
- Adds computational overhead to every `stream_read()` call

#### Proposed Implementation

Implement proper hardware overflow detection by comparing read and write pointers:

```verilog
// In streaming engine logic (around line 469-477)
if (reg_stream_enable) begin
    reg_stream_active <= 1'b1;
    if (reg_input_select == INPUT_SELECT_DEMOD) begin
        if (demod_input_valid_i) begin
            // Check if write pointer is about to lap the read pointer
            // Read pointer would need to be exposed from Python side
            wire [BRAM_ADDR_BITS-1:0] estimated_rd_ptr = reg_stream_wr_ptr - SAFE_BUFFER_MARGIN;

            // Set overflow if buffer is too full
            if (reg_stream_wr_ptr + 1 == estimated_rd_ptr) begin
                reg_stream_overflow <= 1'b1;
            end

            reg_stream_wr_ptr <= reg_stream_wr_ptr + 1'b1;
            reg_stream_sample_cnt <= reg_stream_sample_cnt + 1'b1;
        end
    end
end
```

**Alternative simpler approach:** Sticky overflow flag on wrap-around:
```verilog
if (reg_stream_wr_ptr == {BRAM_ADDR_BITS{1'b1}}) begin
    reg_stream_overflow <= 1'b1;  // Latch on full wrap
end
```

#### Benefits

1. **Hardware-based detection:** Immediate overflow flagging without polling delay
2. **Reduced software overhead:** Python only needs to check flag once per read
3. **Better debugging:** Clear hardware indication of overflow conditions

#### Implementation Checklist

**Files to modify:**

1. **FPGA Verilog** (`pyrpl/fpga/rtl/scan_new.v`)
   - [ ] Implement overflow detection logic (lines 469-477)
   - [ ] Add register for read pointer tracking (optional advanced version)
   - [ ] Test overflow flag clearing on `reg_stream_reset_cmd`

2. **Python** (`pyrpl/hardware_modules/scan.py`)
   - [ ] Update comment at line 269 to reflect hardware overflow is now working
   - [ ] Consider simplifying software overflow check (line 305) to rely more on hardware flag
   - [ ] Add register for writing read pointer to FPGA (advanced version only)

3. **Testing**
   - [ ] Verify overflow flag sets correctly when BRAM fills
   - [ ] Test overflow flag clears on stream reset
   - [ ] Benchmark performance with reduced software checks

---

## 3. Code Quality and Maintainability

(No items currently proposed)

---

## 4. Safety and Error Handling

### 4.1. Scan Module: Enforce Mutual Exclusion Between Scan and Streaming Modes

**Status:** Proposed
**Priority:** High
**Estimated Effort:** 2-3 hours (FPGA + Python)
**Branch Context:** scan_module_dev_johannes_filtering_tests

#### Current Situation

The scan FSM prevents starting a scan while streaming is active (`scan_new.v:344`):
```verilog
S_IDLE: if (reg_start_cmd && reg_num_steps > 0 && !reg_stream_enable) next_state = S_START_STEP;
```

However, there's no reverse check—you can start streaming while a scan is running. Both modes share the data3 BRAM (`ram_data3`):
- **Scan mode:** Writes sample counts at `step_counter` address (`scan_new.v:532`)
- **Stream mode:** Writes demod samples at `reg_stream_wr_ptr` address (`scan_new.v:536`)

If both write simultaneously, data corruption occurs due to the write arbiter priority (`scan_new.v:523-538`).

#### Proposed Implementation

**FPGA Side** (`scan_new.v`):

1. **Prevent streaming start during scan** (around line 292-296):
```verilog
ADDR_STREAM_CONTROL: begin
    // Only allow enabling stream if scan is idle
    if (current_state == S_IDLE) begin
        reg_stream_enable <= sys_wdata[0];
    end else begin
        // Optionally set an error flag or ignore the write
        reg_stream_enable <= 1'b0;
    end
    if (sys_wdata[1]) reg_stream_reset_cmd <= 1'b1;
end
```

2. **Add status bit for "scan active"** (around line 653):
```verilog
ADDR_STATUS: sys_rdata <= {29'b0, (current_state != S_IDLE), reg_done_flag, reg_busy_flag};
```

**Python Side** (`scan.py`):

3. **Check busy flag before streaming** (around line 230):
```python
def stream_start(self, input_source="demod"):
    """Enable continuous streaming of demodulated samples."""
    if self.busy:
        raise RuntimeError("Cannot start streaming: scan sweep is currently running. "
                          "Wait for scan to complete or call stop() first.")

    if input_source != "demod":
        logger.warning("Streaming currently only supported for 'demod'. Forcing input_select to 'demod'.")
    self.input_select = 'demod'
    # ... rest of implementation
```

4. **Check streaming before scan start** (around line 411):
```python
def start(self):
    """Starts the sweep sequence."""
    if self.busy:
        logger.warning("Scan module is already busy. Ignoring start command.")
        return

    # Check if streaming is active
    active, _, _, _ = self.stream_status()
    if active:
        raise RuntimeError("Cannot start scan: streaming mode is currently active. "
                          "Call stream_stop() first.")

    # ... rest of implementation
```

#### Benefits

1. **Data integrity:** Prevents BRAM corruption from simultaneous writes
2. **Clear error messages:** Users understand why operation failed
3. **Defensive programming:** Catches misuse at both hardware and software levels

#### Implementation Checklist

1. **FPGA** (`pyrpl/fpga/rtl/scan_new.v`)
   - [ ] Add scan state check to streaming enable logic (line 292)
   - [ ] Add scan-active status bit to status register (line 653)
   - [ ] Test mutual exclusion in simulation

2. **Python** (`pyrpl/hardware_modules/scan.py`)
   - [ ] Add busy check to `stream_start()` (line 230)
   - [ ] Add streaming check to `start()` (line 405)
   - [ ] Update docstrings to document mutual exclusion

3. **Testing**
   - [ ] Verify `stream_start()` raises exception during active scan
   - [ ] Verify `start()` raises exception during active streaming
   - [ ] Test clean transitions (stop → start other mode)

4. **Documentation**
   - [ ] Update scan.py docstring (line 2-31)
   - [ ] Update CLAUDE.md with mutual exclusion note

---

### 4.2. Scan Module: Add Safety Check to stream_reset During Active Scan

**Status:** Proposed
**Priority:** Medium
**Estimated Effort:** 1 hour (Python only)
**Branch Context:** scan_module_dev_johannes_filtering_tests

#### Current Situation

The `stream_start()` method resets the streaming engine without checking if a scan sweep is running (`scan.py:241`):

```python
def stream_start(self, input_source="demod"):
    # ...
    self._stream_ctrl_write(enable=True, reset=True)  # No safety check
    # ...
```

If a sweep is in progress, resetting streaming state could interfere with the scan FSM, although the BRAM arbiter gives scan priority. Still, this is a race condition risk.

#### Proposed Implementation

Add safety check before reset:

```python
def stream_start(self, input_source="demod"):
    """Enable continuous streaming of demodulated samples into the BRAM ring buffer.

    Raises:
        RuntimeError: If a scan sweep is currently running.

    Notes:
        - Uses the count BRAM region (32-bit words) as a circular buffer.
        - Blocks scanning functionality while active (shared memory).
    """
    # Check if scan is active before starting stream
    if self.busy:
        raise RuntimeError("Cannot start streaming: scan sweep is currently running. "
                          "Stop the scan first with stop() or reset().")

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
```

#### Benefits

1. **Safety:** Prevents potential interference between scan and streaming operations
2. **Clear errors:** User understands why operation was blocked
3. **Consistency:** Matches proposed mutual exclusion improvements

#### Implementation Checklist

1. **Python** (`pyrpl/hardware_modules/scan.py`)
   - [ ] Add busy check to `stream_start()` (line 230-246)
   - [ ] Update docstring with Raises section
   - [ ] Consider adding similar check to `_stream_ctrl_write()` (optional)

2. **Testing**
   - [ ] Verify exception raised when calling `stream_start()` during scan
   - [ ] Test normal streaming start when scan is idle

---

### 4.3. Scan Module: Add Input Validation for Configuration Parameters

**Status:** Proposed
**Priority:** Medium
**Estimated Effort:** 2-3 hours (Python only)
**Branch Context:** scan_module_dev_johannes_filtering_tests

#### Current Situation

Several validation checks are missing in the Python scan module:

1. **`stream_start(input_source)` parameter is ignored** (`scan.py:237-239`):
   ```python
   if input_source != "demod":
       logger.warning("Streaming currently only supported for 'demod'. Forcing input_select to 'demod'.")
   ```
   The parameter exists but has no effect—always forces 'demod'. This is misleading API design.

2. **No validation that `num_steps` fits within BRAM depth**:
   - BRAM depth is 2^12 = 4096 steps
   - Python allows setting `num_steps` beyond this limit
   - FPGA will silently wrap addresses, causing data corruption

3. **No check for zero `dwell_time` in `start()` method**:
   - Checked in `get_data()` (line 509) but not `start()` (line 405)
   - Scan will start but produce useless zero-sample data
   - Better to fail fast at `start()`

#### Proposed Implementation

**1. Remove misleading `input_source` parameter:**

```python
def stream_start(self):  # Remove input_source parameter
    """Enable continuous streaming of demodulated samples into the BRAM ring buffer.

    Notes:
        - Only supports 'demod' input mode (32-bit lock-in output).
        - Automatically sets input_select to 'demod'.
        - Uses the count BRAM region (32-bit words) as a circular buffer.
        - Blocks scanning functionality while active (shared memory).
    """
    # Always use demod mode for streaming
    self.input_select = 'demod'
    # ... rest of implementation
```

**2. Add `num_steps` validation in register setter:**

```python
# Around line 161, update num_steps register definition
num_steps = IntRegister(ADDR_NUM_STEPS, bits=MAX_STEPS_BITS, min=1, max=2**MAX_STEPS_BITS,
                        doc="Number of steps in the sweep (1 to {}).\n"
                            "Limited by BRAM depth. Exceeding {} will cause address wrapping."
                            .format(2**MAX_STEPS_BITS, 2**MAX_STEPS_BITS))
```

The `IntRegister` descriptor already clamps values, but update docs for clarity.

**3. Add validation in `start()` method:**

```python
def start(self):
    """Starts the sweep sequence.

    Raises:
        ValueError: If configuration is invalid (zero dwell_time, etc.)
        RuntimeError: If scan is already busy or streaming is active.

    Checks for potential accumulator overflow before starting.
    """
    if self.busy:
        logger.warning("Scan module is already busy. Ignoring start command.")
        return

    # Check streaming not active
    active, _, _, _ = self.stream_status()
    if active:
        raise RuntimeError("Cannot start scan: streaming mode is active. Call stream_stop() first.")

    # Validate configuration
    if self.num_steps <= 0:
        raise ValueError(f"Invalid num_steps: {self.num_steps}. Must be >= 1.")

    if self.num_steps > 2**MAX_STEPS_BITS:
        raise ValueError(f"num_steps {self.num_steps} exceeds BRAM depth {2**MAX_STEPS_BITS}.")

    if self.dwell_time <= 0:
        raise ValueError(f"Invalid dwell_time: {self.dwell_time}s. Must be > 0.")

    # If we're in DONE state, reset first to ensure clean start
    if self.done:
        self.reset()

    self._check_overflow()
    logger.info("Starting scan sweep with input source: %s...", self.input_select)
    self._write_control_bit(CONTROL_START_BIT, True)
```

#### Benefits

1. **Fail-fast validation:** Catch configuration errors before starting FPGA operation
2. **Clear API:** Remove misleading parameters
3. **Better error messages:** Guide users to correct configuration
4. **Data integrity:** Prevent silent address wrapping corruption

#### Implementation Checklist

1. **Python** (`pyrpl/hardware_modules/scan.py`)
   - [ ] Remove `input_source` parameter from `stream_start()` (line 230)
   - [ ] Update `stream_start()` docstring
   - [ ] Add validation checks to `start()` method (line 405-421)
   - [ ] Update `start()` docstring with Raises section
   - [ ] Update `num_steps` register docstring (line 161)

2. **Testing**
   - [ ] Test `start()` with zero `dwell_time` → ValueError
   - [ ] Test `start()` with `num_steps` > 4096 → ValueError
   - [ ] Test `start()` with negative values → ValueError
   - [ ] Test valid configuration proceeds normally
   - [ ] Verify backward compatibility (existing scripts don't break)

3. **Documentation**
   - [ ] Update module docstring (line 2-31) with validation notes
   - [ ] Add examples to docstrings showing proper usage

---

## 5. Documentation Improvements

### 5.1. Scan Module: Improve Streaming Mode Documentation

**Status:** Proposed
**Priority:** Low
**Estimated Effort:** 30 minutes (documentation only)
**Branch Context:** scan_module_dev_johannes_filtering_tests

#### Current Situation

The `input_select` register docstring (`scan.py:192-195`) doesn't explain that streaming is only supported for 'demod' mode:

```python
input_select = SelectRegister(ADDR_INPUT_SELECT, options=_input_options,
                              default="adc",
                              doc="Selects the input signal source: "
                                  "'adc' for 14-bit ADC input at 125 MHz, "
                                  "'iq0' for 24-bit IQ demodulator output at 125 MHz, "
                                  "'demod' for 32-bit lock-in demodulated output (valid every 4096 cycles).")
```

Users might assume they can stream ADC or IQ data, leading to confusion when `stream_start()` forces 'demod' mode.

#### Proposed Implementation

Update docstrings to clearly document streaming limitations:

**1. `input_select` register docstring:**
```python
input_select = SelectRegister(ADDR_INPUT_SELECT, options=_input_options,
                              default="adc",
                              doc="Selects the input signal source:\n"
                                  "  'adc'  : 14-bit ADC input at 125 MHz (scan mode only)\n"
                                  "  'iq0'  : 24-bit IQ demodulator output at 125 MHz (scan mode only)\n"
                                  "  'demod': 32-bit lock-in demodulated output at ~30.5 kHz (supports scan + streaming)\n"
                                  "\n"
                                  "Note: Streaming mode (stream_start) requires 'demod' input.")
```

**2. Module-level docstring:**
```python
"""
Scan Module for Pyrpl.

This module controls the FPGA scan block, enabling automated sweeps.
For each step:
1. Outputs a trigger pulse on a *fixed* digital output pin (exp_p_io[7]).
2. Waits for a settling time.
3. Acquires and accumulates data from either ADC input, IQ demodulator,
   or demodulated lock-in output for a dwell time.
4. Stores the 64-bit accumulated sum in BRAM.

The accumulated data can then be read back and averaged in Python.

**Operating Modes:**

1. **Scan Mode** (run_sweep/start/get_data):
   - Performs stepped sweep with configurable num_steps
   - Supports all three input modes: 'adc', 'iq0', 'demod'
   - Stores accumulated sums in BRAM for later readout

2. **Streaming Mode** (stream_start/stream_read/stream_stop):
   - Continuous data acquisition into ring buffer
   - Only supports 'demod' input mode (32-bit @ ~30.5 kHz)
   - Real-time readout via stream_read() or stream_iter()
   - Mutually exclusive with scan mode (shared BRAM)

**Input Modes:**
*   **adc:** Direct 14-bit ADC input at 125 MHz (scan mode only)
*   **iq0:** 24-bit IQ demodulator output at 125 MHz (scan mode only)
*   **demod:** 32-bit demodulated lock-in output, valid every 4096 cycles ≈30.5 kHz (scan + streaming)

    When using 'demod' mode, the dwell_time still represents the total acquisition
    time in clock cycles, but data is only accumulated when the valid signal is high.
    The actual number of samples accumulated per step is stored in BRAM and used for averaging.

**Important Notes:**
*   **Trigger Output Pin:** The trigger output pulse is currently HARDWIRED
    to the most significant bit pin of the expansion connector ('exp_p_io[7]')
    in the FPGA design (`red_pitaya_hk.v`). Changing the
    `trigger_pin_select` attribute will write to the corresponding FPGA register,
    but it will NOT change the physical output pin without modifications
    to the FPGA design.
*   **Mutual Exclusion:** Scan and streaming modes cannot run simultaneously.
    They share the same BRAM resources. Starting one mode while the other is
    active will raise an exception.
"""
```

#### Benefits

1. **User clarity:** Explicitly document streaming limitations
2. **Reduced confusion:** Users understand mode restrictions upfront
3. **Better onboarding:** New users see full capabilities at a glance

#### Implementation Checklist

1. **Python** (`pyrpl/hardware_modules/scan.py`)
   - [ ] Update `input_select` docstring (line 190-195)
   - [ ] Update module docstring (line 2-31)
   - [ ] Add docstring to `stream_start()` method clarifying demod requirement (line 230)

2. **Documentation**
   - [ ] Update CLAUDE.md if needed
   - [ ] Consider adding usage examples in module docstring

---

## 6. Advanced Optimizations

### 6.1. Scan Module: Address Potential Ring Buffer Race Condition

**Status:** Proposed
**Priority:** Low
**Estimated Effort:** 3-4 hours (analysis + potential fix)
**Branch Context:** scan_module_dev_johannes_filtering_tests

#### Current Situation

The ring buffer read logic (`scan.py:315-320`) calculates available samples based on FPGA write pointer:

```python
# Compute number available between software read pointer and FPGA write pointer
rd = getattr(self, '_stream_rd_ptr', 0) % depth
if wrp >= rd:
    avail = wrp - rd
else:
    avail = (depth - rd) + wrp
```

This assumes the FPGA write pointer (`wrp`) doesn't advance between:
1. The `stream_status()` call that reads `wrp` (line 298)
2. The actual `_reads()` BRAM read operations (lines 334, 343)

If the write pointer advances significantly during this window, two issues can occur:
1. **Stale data read:** Reading from addresses the FPGA has already overwritten
2. **Missed samples:** The calculation doesn't account for new samples written during the read

The overflow check (line 305) partially mitigates this by detecting large advances, but there's still a race window.

#### Proposed Solutions

**Option 1: Atomic status + data read (requires FPGA changes)**

Add a "freeze write pointer" register that latches the current write pointer value for a consistent read:

```verilog
// In scan_new.v, add new register
reg [BRAM_ADDR_BITS-1:0] reg_stream_wr_ptr_latched;

// On read of STREAM_WR_PTR, latch the current value
ADDR_STREAM_WR_PTR: begin
    reg_stream_wr_ptr_latched <= reg_stream_wr_ptr;
    sys_rdata <= { {(32-BRAM_ADDR_BITS){1'b0}}, reg_stream_wr_ptr_latched };
end
```

Then subsequent BRAM reads use the latched pointer for consistency.

**Option 2: Double-read validation (software only)**

Read the write pointer before and after BRAM read, retry if it changed significantly:

```python
def stream_read(self, max_samples=None, enable_timing=False, check_overflow_every=1):
    # ... existing code ...

    # Read status BEFORE BRAM access
    active1, overflow1, wrp1, total1 = self.stream_status()

    # Calculate available samples
    rd = getattr(self, '_stream_rd_ptr', 0) % depth
    if wrp1 >= rd:
        avail = wrp1 - rd
    else:
        avail = (depth - rd) + wrp1

    if avail == 0:
        return np.array([], dtype=np.int32)
    if max_samples is not None:
        avail = min(avail, int(max_samples))

    # Read BRAM data
    # ... existing segment reads ...

    # Read status AFTER BRAM access
    active2, overflow2, wrp2, total2 = self.stream_status()

    # Validate write pointer didn't advance too much
    delta = (wrp2 - wrp1) % depth
    if delta > avail:
        logger.warning(f"Write pointer advanced by {delta} during read (expected <= {avail}). Retrying.")
        return self.stream_read(max_samples, enable_timing, check_overflow_every)

    # ... rest of implementation ...
```

**Option 3: Conservative underread (software only, simplest)**

Always read fewer samples than available to leave a safety margin:

```python
# Add safety margin to prevent reading samples being written
SAFETY_MARGIN = 16  # samples

avail = calculate_available(wrp, rd, depth)
if avail > SAFETY_MARGIN:
    avail = avail - SAFETY_MARGIN  # Leave margin for FPGA writes
else:
    avail = 0  # Not enough samples to read safely
```

#### Benefits

1. **Data integrity:** Prevents reading samples during FPGA write
2. **Reliability:** Reduces risk of corrupted data in high-throughput scenarios
3. **Robustness:** Handles edge cases in fast streaming applications

#### Implementation Checklist

1. **Choose solution approach**
   - Option 1: Most robust, requires FPGA changes
   - Option 2: Good reliability, Python only
   - Option 3: Simplest, may reduce effective throughput

2. **Implementation**
   - [ ] Implement chosen solution
   - [ ] Add logging for race detection (debug level)
   - [ ] Document race condition mitigation in docstring

3. **Testing**
   - [ ] Test high-rate streaming (maximum demod rate ~30 kHz)
   - [ ] Verify data integrity with known test patterns
   - [ ] Benchmark throughput with new safety mechanisms
   - [ ] Test with slow read rates (worst case for races)

4. **Documentation**
   - [ ] Document race condition and mitigation in module docstring
   - [ ] Add technical note about read consistency guarantees

---

### 6.2. Scan Module: Clarify BRAM Write Priority and Add Safeguards

**Status:** Proposed
**Priority:** Low
**Estimated Effort:** 1-2 hours (FPGA + documentation)
**Branch Context:** scan_module_dev_johannes_filtering_tests

#### Current Situation

The data3 BRAM write arbiter gives priority to scan FSM over streaming (`scan_new.v:523-538`):

```verilog
always @(*) begin
    // Default no write
    data3_we_mux    = 1'b0;
    data3_waddr_mux = {BRAM_ADDR_BITS{1'b0}};
    data3_wdata_mux = {BUS_DATA_WIDTH{1'b0}};

    // Priority: scan write over stream write
    if (bram_wr_en) begin
        data3_we_mux    = 1'b1;
        data3_waddr_mux = bram_wr_addr;
        data3_wdata_mux = bram_wr_data_data3;
    end else if (stream_wr_en) begin
        data3_we_mux    = 1'b1;
        data3_waddr_mux = reg_stream_wr_ptr;
        data3_wdata_mux = demod_input_i[31:0];
    end
end
```

While the FSM prevents both from activating simultaneously (`S_IDLE` checks `!reg_stream_enable`, line 344), there's no assertion or error flag if both `bram_wr_en` and `stream_wr_en` assert together due to a bug or misconfiguration.

#### Proposed Implementation

**1. Add conflict detection (synthesis-time assertion):**

```verilog
// Add after write mux logic (around line 537)
`ifdef SIMULATION
    always @(posedge clk) begin
        if (bram_wr_en && stream_wr_en) begin
            $error("BRAM write conflict: both scan and stream trying to write simultaneously!");
        end
    end
`endif

// Add runtime error flag (optional, for debugging)
reg reg_bram_conflict_error;
always @(posedge clk) begin
    if (!rstn) begin
        reg_bram_conflict_error <= 1'b0;
    end else begin
        if (bram_wr_en && stream_wr_en) begin
            reg_bram_conflict_error <= 1'b1;  // Sticky error flag
        end
        // Clear on explicit reset
        if (reg_reset_cmd) begin
            reg_bram_conflict_error <= 1'b0;
        end
    end
end
```

**2. Expose conflict flag in status register:**

```verilog
// Around line 653, add conflict bit to status
ADDR_STATUS: sys_rdata <= {28'b0, reg_bram_conflict_error,
                          (current_state != S_IDLE),
                          reg_done_flag, reg_busy_flag};
```

**3. Document priority in Python:**

```python
# In scan.py module docstring (around line 2-31), add section:

"""
...

**BRAM Write Priority:**
The data3 BRAM is shared between scan and streaming modes:
- Scan mode: Writes sample counts at completion of each step
- Streaming mode: Writes demodulated samples continuously

Priority is given to scan writes. If both modes attempt to write simultaneously
(which should never happen due to mutual exclusion), the scan write succeeds
and the stream write is dropped. A conflict error flag is set in the FPGA status
register (bit 3) for debugging purposes.

Under normal operation, mutual exclusion ensures this never occurs.
"""
```

#### Benefits

1. **Early detection:** Catch arbiter conflicts during development/testing
2. **Debugging aid:** Error flag helps diagnose unexpected behavior
3. **Documentation:** Clear understanding of priority rules

#### Implementation Checklist

1. **FPGA** (`pyrpl/fpga/rtl/scan_new.v`)
   - [ ] Add simulation-time assertion (line 537)
   - [ ] Add conflict error flag register (new)
   - [ ] Expose conflict flag in status register (line 653)
   - [ ] Test assertion triggers in simulation

2. **Python** (`pyrpl/hardware_modules/scan.py`)
   - [ ] Document BRAM priority in module docstring (line 2-31)
   - [ ] Optionally add conflict check in `get_data()` or `stream_read()`

3. **Testing**
   - [ ] Create testbench that deliberately triggers conflict
   - [ ] Verify assertion fires in simulation
   - [ ] Verify conflict flag sets correctly

4. **Documentation**
   - [ ] Update scan_new.v header with priority documentation
   - [ ] Add note to CLAUDE.md about BRAM sharing

---

