# Concurrent Access During Streaming in PyRPL

**Author:** Claude
**Date:** 2025-11-12
**Context:** Analysis of concurrent register access during long streaming operations
**Branch:** scan_module_dev_johannes_filtering_tests

---

## Executive Summary

**Key Question:** During a 30-second streaming operation, can the user safely execute other commands like reading/writing small registers?

**Answer:** Yes, but **only if the streaming implementation uses asyncio properly**. PyRPL uses an asyncio-based architecture (not threading) that allows concurrent operations through cooperative multitasking. The scope module demonstrates the correct pattern.

**Critical Findings:**
1. PyRPL has **one TCP connection** per client - operations are serialized
2. The monitor_server is **single-threaded** - processes one request at a time
3. No thread-safety locks exist in `redpitaya_client.py`
4. **Asyncio's `await` yields control**, enabling interleaved operations
5. **Blocking calls** (`time.sleep()`) prevent ALL other operations

---

## Table of Contents

1. [PyRPL's Concurrency Architecture](#pyrpls-concurrency-architecture)
2. [How Scope Handles Streaming](#how-scope-handles-streaming)
3. [Limitations and Constraints](#limitations-and-constraints)
4. [What Works and What Doesn't](#what-works-and-what-doesnt)
5. [Recommendations for Scan Module](#recommendations-for-scan-module)
6. [Implementation Examples](#implementation-examples)
7. [Testing Concurrent Access](#testing-concurrent-access)

---

## PyRPL's Concurrency Architecture

### Asyncio Event Loop

PyRPL uses Python's `asyncio` framework integrated with Qt's event loop via `qasync`:

**From `async_utils.py:59-63`:**
```python
LOOP = qasync.QEventLoop(already_running=False)

async def sleep_async(delay, result=None):
    """Yields control back to event loop during delay"""
    # ... implementation
```

**Key Concept:** `await sleep_async(time_s)` **yields control** to the event loop, allowing:
- GUI updates
- Other coroutines to execute
- User interactions to be processed
- **Other register reads/writes to occur**

### Single TCP Connection Architecture

**From `redpitaya_client.py:57-81`:**
```python
class MonitorClient(object):
    def __init__(self, hostname="192.168.1.0", port=2222):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((self._hostname, self._port))
        # One persistent connection for entire session
```

**Implications:**
- One socket per MonitorClient instance
- Socket operations are **blocking** (not async)
- No `threading.Lock()` or mutex protection
- Concurrent reads/writes would corrupt the protocol

### Monitor Server Architecture

**From `monitor_server.c:201-237`:**
```c
// Main service loop - single-threaded
while (0==0) {
    // Wait for next command (blocking)
    n = recv(newsockfd, buffer, 8, MSG_WAITALL);

    // Process command
    if (buffer[0] == 'r') {
        read_values(address, rw_buffer, data_length);
        n = send(newsockfd, (void*)data_buffer, data_length * sizeof(unsigned long) + 8, 0);
    }
    else if (buffer[0] == 'w') {
        n = recv(newsockfd, (void*)rw_buffer, data_length * sizeof(unsigned long), MSG_WAITALL);
        write_values(address, rw_buffer, data_length);
    }
}
```

**Key Points:**
- Processes **one request at a time**
- No threading or multiplexing
- Sequential request/response pattern
- Cannot handle simultaneous connections

---

## How Scope Handles Streaming

The scope module provides two modes that demonstrate proper concurrent access patterns:

### Mode 1: Single/Triggered Acquisition

**From `scope.py:607-643`:**
```python
def _start_trace_acquisition(self):
    """Start acquisition of a curve"""
    self._acquisition_started = True
    self._reset_writestate_machine = True

    # Configure trigger delay
    if self.trigger_source == 'immediately':
        self._trigger_delay_register = self.data_length
    else:
        delay = int(np.round(self.trigger_delay / self.sampling_time)) + self.data_length // 2
        self._trigger_delay_register = delay

    # Arm trigger
    self._trigger_armed = True
    self._trigger_source_register = self.trigger_source

async def _trace_async(self, min_delay_ms):
    """Asynchronous trace acquisition"""
    self._start_trace_acquisition()
    await self._data_ready_async(min_delay_ms)  # Yields control here!
    return self._get_trace()
```

**Concurrent Access Pattern:**
1. Configure scope (quick register writes)
2. Arm trigger (single register write)
3. `await` until data ready (yields control!)
4. During wait, other operations can execute
5. Read full buffer when ready

### Mode 2: Rolling/Continuous Mode

**From `scope.py:591-599`:**
```python
async def _do_average_continuous_async(self):
    if not self._is_rolling_mode_active():
        await super(Scope, self)._do_average_continuous_async()
    else: # Rolling mode for continuous acquisition
        self._start_acquisition_rolling_mode()
        while(self.running_state=="running_continuous"):
            await sleep_async(self.MIN_DELAY_CONTINUOUS_ROLLING_MS*0.001)  # 20 ms
            self.data_x, self.data_avg = self._get_rolling_curve()
            self._emit_signal_by_name('display_curve', [self.data_x, self.data_avg])
```

**Timing Analysis:**
- Reads 16384 samples: ~10 ms (blocking)
- Sleeps 20 ms: **yields control via `await`**
- Total cycle: 30 ms
- **During the 20 ms sleep, other coroutines can run!**

**From `scope.py:676-701` - Reading with wrap detection:**
```python
def _get_rolling_curve(self):
    datas = np.zeros((2, len(self.times)))
    wp0 = self._write_pointer_current  # Snapshot before read

    for ch in [1, 2]:
        datas[ch-1] = self._get_ch_no_roll(ch)  # Read BRAM (blocking ~5 ms/ch)

    wp1 = self._write_pointer_current  # Snapshot after read

    # Discard samples written during read
    to_discard = (wp1 - wp0) % self.data_length
    # ... roll and adjust data
```

**Key Insight:** The scope reads the ENTIRE buffer periodically, not individual samples. This minimizes the number of blocking operations.

---

## Limitations and Constraints

### TCP/IP Protocol Limitations

**Protocol Flow (8-byte header + data):**
```
Python → monitor_server:
    [command][reserved][length][address]

monitor_server → Python:
    [header_echo][data_payload]
```

**Problem:** No request ID or multiplexing. A second request sent before the first completes would:
1. Corrupt the header parsing
2. Misalign data reception
3. Cause protocol desynchronization

### Socket Blocking Behavior

**From `redpitaya_client.py:107-123`:**
```python
def _reads(self, addr, length):
    header = b'r' + bytes(bytearray([...]))
    self.socket.send(header)  # Blocks until sent

    # Blocks until ALL data received
    data = self.socket.recv(length * 4 + 8)
    while (len(data) < length * 4 + 8):
        data += self.socket.recv(length * 4 - len(data) + 8)

    return np.frombuffer(data[8:], dtype=np.uint32)
```

**Blocking duration:**
- 8-byte header send: ~50-200 µs
- Monitor server processing: ~50-150 µs
- Data receive (1000 samples): ~32 µs (4 KB / 125 MB/s)
- **Total: ~132-382 µs per read**

**During this time, the socket is BUSY and cannot service other requests.**

### Memory Access Overhead

**From `monitor_server.c:248-268`:**
```c
unsigned long* read_values(unsigned long a_addr, ...) {
    int fd = open("/dev/mem", O_RDWR | O_SYNC);  // ~10-20 µs
    map_base = mmap(...);                         // ~20-50 µs

    for (i = 0; i < a_len; i++) {
        a_values_buffer[i] = ((unsigned long*) virt_addr)[i];  // ~0.1 µs/word
    }

    munmap(map_base, MAP_SIZE);                   // ~10-20 µs
    close(fd);                                    // ~5-10 µs

    return a_values_buffer;
}
```

**Total per read:** 45-100 µs + (n_samples × 0.1 µs)

---

## What Works and What Doesn't

### ✅ SAFE: Asyncio-Based Interleaved Operations

```python
async def stream_with_concurrent_access():
    """This pattern allows concurrent operations"""

    # Start streaming
    scan.stream_control = 0x3

    while scan.running:
        # Read stream data (blocks for ~1 ms)
        data = scan._reads(0x30000, 1000)
        process_data(data)

        # CRITICAL: Yield control!
        await sleep_async(0.050)  # 50 ms
        # ↑ During this sleep, other operations can execute!

# In another coroutine or GUI callback:
async def adjust_pid_during_stream():
    await sleep_async(5.0)  # Wait 5 seconds
    pid.p = 1.5  # This can execute during the sleep_async() above!
    print(f"Current setpoint: {pid.setpoint}")  # Read also works!
```

**Why it works:**
- `await sleep_async()` yields control to event loop
- Event loop can schedule other coroutines
- Each coroutine gets exclusive socket access when running
- No corruption because operations are serialized by event loop

### ❌ UNSAFE: Blocking Loop Without Yield

```python
def stream_blocking_bad():
    """This blocks ALL other operations!"""

    scan.stream_control = 0x3

    for i in range(300):  # 30 seconds
        data = scan._reads(0x30000, 1000)  # Blocks ~1 ms
        process_data(data)
        time.sleep(0.100)  # BLOCKS EVENT LOOP!
        # ↑ During this sleep, NOTHING else can run!

# Meanwhile, this will NOT execute until loop finishes:
pid.p = 1.5  # Queued, but blocked by time.sleep()
```

**Why it fails:**
- `time.sleep()` blocks the entire thread
- Event loop cannot run
- All other operations (GUI, coroutines, register access) frozen

### ⚠️ QUESTIONABLE: Threading Without Locks

```python
import threading

def stream_threaded_risky():
    """Threading without proper locking - RACE CONDITIONS!"""

    def stream_worker():
        while not stop_event.is_set():
            data = scan._reads(0x30000, 1000)  # Uses self._client.reads()
            buffer.append(data)
            time.sleep(0.1)

    thread = threading.Thread(target=stream_worker)
    thread.start()

    # Meanwhile in main thread:
    pid.p = 1.5  # Also calls self._client.writes() - RACE CONDITION!
```

**Why it's risky:**
- Both threads share the same `MonitorClient.socket`
- No locking around socket operations
- Concurrent sends could interleave bytes
- Protocol corruption likely

**Fix requires:**
```python
class MonitorClient:
    def __init__(self, ...):
        self.socket = ...
        self._socket_lock = threading.Lock()  # ADD THIS

    def _reads(self, addr, length):
        with self._socket_lock:  # PROTECT SOCKET
            self.socket.send(header)
            data = self.socket.recv(...)
        return data
```

---

## Recommendations for Scan Module

### Recommendation 1: Implement Asyncio Pattern (Like Scope)

**Advantages:**
- ✅ Safe concurrent access
- ✅ Integrates with existing PyRPL architecture
- ✅ GUI remains responsive
- ✅ No threading complexity
- ✅ Works with existing infrastructure

**Implementation pattern:**

```python
# In scan.py (hardware_modules/scan.py)
from ..async_utils import sleep_async, ensure_future, wait
from ..acquisition_module import AcquisitionModule

class Scan(HardwareModule, AcquisitionModule):
    addr_base = 0x40500000  # Example address
    name = 'scan'

    # Streaming configuration
    MIN_DELAY_STREAM_MS = 50  # Poll interval

    # Registers
    stream_control = IntRegister(0x20, doc="Stream control")
    stream_wr_ptr = IntRegister(0x28, doc="FPGA write pointer")
    stream_samples = IntRegister(0x2C, doc="Total samples written")

    async def _stream_continuous_async(self):
        """Continuous streaming with concurrent access support"""
        self.stream_control = 0x3  # Enable + reset
        last_ptr = 0

        while self.running_state == "running_continuous":
            wr_ptr = self.stream_wr_ptr

            # Calculate available samples
            if wr_ptr >= last_ptr:
                available = wr_ptr - last_ptr
            else:  # Wrapped
                available = (4096 - last_ptr) + wr_ptr

            if available > 100:  # Read in chunks of 100+
                samples_to_read = min(available, 1000)
                data = self._read_stream_chunk(last_ptr, samples_to_read)
                self._process_stream_data(data)
                last_ptr = (last_ptr + samples_to_read) % 4096

            # CRITICAL: Yield control to event loop!
            await sleep_async(self.MIN_DELAY_STREAM_MS * 0.001)
            # During this sleep, other operations can execute:
            # - GUI updates
            # - Other coroutines
            # - Register reads/writes (e.g., pid.p = 1.5)

    def _read_stream_chunk(self, start_ptr, length):
        """Read chunk from circular BRAM buffer"""
        base_addr = 0x30000  # Data3 BRAM base

        if start_ptr + length <= 4096:
            # No wrap
            return self._reads(base_addr + start_ptr * 4, length)
        else:
            # Wrap around
            first_part = 4096 - start_ptr
            second_part = length - first_part
            data1 = self._reads(base_addr + start_ptr * 4, first_part)
            data2 = self._reads(base_addr, second_part)
            return np.concatenate([data1, data2])

    def _process_stream_data(self, data):
        """Process and emit data"""
        # Convert to physical units, accumulate, etc.
        self.data_buffer.extend(data)
        self._emit_signal_by_name('update_stream', data)

    def stream_continuous(self):
        """Public blocking wrapper"""
        return wait(self._stream_continuous_async())
```

### Recommendation 2: Optimize Polling Interval

**Current constraint:** 4096-sample BRAM, 30.5 kHz rate = 134 ms buffer time

**Optimal polling strategy:**
```python
# Bad: Poll too frequently (wastes time)
await sleep_async(0.001)  # 1 ms - 99% of time no new data

# Bad: Poll too slowly (risk overflow)
await sleep_async(0.130)  # 130 ms - only 4 ms margin!

# Good: Poll at ~1/3 buffer time
await sleep_async(0.040)  # 40 ms - safe margin, efficient
```

**With DDR streaming (68-second buffer):**
```python
# Can poll much less frequently
await sleep_async(1.0)  # 1 second - still 67s margin!
```

### Recommendation 3: Add Overflow Detection

```python
async def _stream_continuous_async(self):
    last_sample_count = 0
    last_ptr = 0

    while self.running_state == "running_continuous":
        current_sample_count = self.stream_samples
        wr_ptr = self.stream_wr_ptr

        # Detect overflow
        expected_ptr = (last_ptr + (current_sample_count - last_sample_count)) % 4096
        if abs(wr_ptr - expected_ptr) > 100:  # Significant discrepancy
            self._logger.error("Stream overflow detected! Lost data.")
            # Handle overflow: reset, notify user, etc.

        # ... read and process data

        last_sample_count = current_sample_count
        await sleep_async(0.040)
```

### Recommendation 4: Provide Synchronous Wrapper

For simple use cases where concurrent access isn't needed:

```python
def stream_blocking(self, duration_s, callback=None):
    """Blocking stream acquisition (simpler but no concurrent access)"""
    async def _stream():
        start_time = time.time()
        while time.time() - start_time < duration_s:
            data = await self._read_stream_chunk_async()
            if callback:
                callback(data)
            await sleep_async(0.040)
        return self.data_buffer

    return wait(_stream())
```

---

## Implementation Examples

### Example 1: Stream While Adjusting PID

```python
import asyncio
from pyrpl.async_utils import ensure_future, wait, sleep_async

async def scan_experiment():
    """Stream data while adjusting PID parameters"""

    # Configure scan for streaming
    scan.input_select = 'demod'  # 30 kHz lock-in data
    scan.stream_control = 0x3  # Enable + reset

    # Start streaming in background
    stream_task = ensure_future(scan._stream_continuous_async())

    # Initial PID configuration
    pid.p = 1.0
    pid.i = 100

    # Wait for 5 seconds of data
    await sleep_async(5.0)

    # Adjust PID during streaming (this works!)
    pid.p = 2.0
    print(f"Adjusted PID p to {pid.p}")

    # Wait another 5 seconds
    await sleep_async(5.0)

    # Stop streaming
    scan.running_state = "stop"

    # Get results
    data = await stream_task
    return data

# Run the experiment
data = wait(scan_experiment())
```

### Example 2: Adaptive Streaming Rate

```python
async def adaptive_stream():
    """Adjust polling rate based on data rate"""

    scan.stream_control = 0x3
    last_count = 0
    poll_interval = 0.050  # Start with 50 ms

    while scan.running:
        start_time = time.time()

        # Read data
        current_count = scan.stream_samples
        new_samples = current_count - last_count
        data = scan._read_stream_chunk(...)

        # Measure actual rate
        elapsed = time.time() - start_time
        sample_rate = new_samples / elapsed if elapsed > 0 else 0

        # Adapt polling interval
        if sample_rate > 25000:  # High rate
            poll_interval = 0.030  # Poll faster
        elif sample_rate < 10000:  # Low rate
            poll_interval = 0.100  # Poll slower

        await sleep_async(poll_interval)
        last_count = current_count
```

### Example 3: Multi-Module Coordination

```python
async def coordinated_acquisition():
    """Coordinate multiple modules during scan streaming"""

    # Start scan streaming
    scan.stream_control = 0x3

    # Configure PID for tracking
    pid.input = 'in1'
    pid.output_direct = 'out1'
    pid.setpoint = 0.0

    data_log = []

    for step in range(10):
        # Change setpoint every 3 seconds
        new_setpoint = step * 0.1
        pid.setpoint = new_setpoint
        print(f"Step {step}: Setpoint = {new_setpoint}")

        # Collect data for 3 seconds
        step_start = scan.stream_samples
        await sleep_async(3.0)
        step_end = scan.stream_samples

        # Read data from this step
        samples_this_step = step_end - step_start
        data = scan._read_stream_chunk(...)

        data_log.append({
            'setpoint': new_setpoint,
            'data': data,
            'pid_output': pid.output  # Can read this too!
        })

    scan.running_state = "stop"
    return data_log
```

---

## Testing Concurrent Access

### Test 1: Verify No Protocol Corruption

```python
async def test_concurrent_access():
    """Test that concurrent reads don't corrupt protocol"""

    errors = []

    async def stream_reader():
        """Simulate streaming reads"""
        for i in range(100):
            try:
                data = scan._reads(0x30000, 100)
                await sleep_async(0.010)
            except Exception as e:
                errors.append(f"Stream error: {e}")

    async def register_reader():
        """Simulate concurrent register reads"""
        for i in range(50):
            try:
                value = pid._read(0x00)  # Read some register
                await sleep_async(0.020)
            except Exception as e:
                errors.append(f"Register error: {e}")

    # Run both concurrently
    await asyncio.gather(stream_reader(), register_reader())

    if errors:
        print("ERRORS DETECTED:")
        for err in errors:
            print(f"  {err}")
    else:
        print("SUCCESS: No protocol corruption")

# Run test
wait(test_concurrent_access())
```

### Test 2: Measure Timing Overhead

```python
import time
import numpy as np

def test_timing():
    """Measure timing of various operations"""

    # Test 1: Single read timing
    times = []
    for i in range(100):
        t0 = time.time()
        data = scan._read(0x28)  # Read write pointer
        t1 = time.time()
        times.append((t1 - t0) * 1e6)  # microseconds

    print(f"Single register read: {np.mean(times):.1f} ± {np.std(times):.1f} µs")

    # Test 2: Bulk read timing
    times = []
    for i in range(100):
        t0 = time.time()
        data = scan._reads(0x30000, 1000)  # Read 1000 samples
        t1 = time.time()
        times.append((t1 - t0) * 1e3)  # milliseconds

    print(f"Bulk read (1000 samples): {np.mean(times):.3f} ± {np.std(times):.3f} ms")

    # Test 3: Theoretical vs actual throughput
    theoretical_bw = 125e6  # 125 MB/s Gigabit Ethernet
    actual_bw = (1000 * 4) / (np.mean(times) * 1e-3)  # bytes/second
    efficiency = actual_bw / theoretical_bw * 100

    print(f"Throughput efficiency: {efficiency:.1f}%")

test_timing()
```

### Test 3: Overflow Detection

```python
async def test_overflow_detection():
    """Test that overflow is properly detected"""

    # Configure for very fast data rate
    scan.input_select = 'demod'  # 30 kHz
    scan.stream_control = 0x3

    overflow_detected = False

    # Deliberately poll too slowly
    for i in range(5):
        await sleep_async(0.200)  # 200 ms - should overflow!

        wr_ptr = scan.stream_wr_ptr
        samples = scan.stream_samples

        expected_samples = i * 0.200 * 30517  # ~6100 samples
        actual_samples = samples

        if actual_samples - expected_samples > 4096:
            overflow_detected = True
            print(f"Overflow detected at iteration {i}")
            break

    if overflow_detected:
        print("SUCCESS: Overflow detection works")
    else:
        print("WARNING: No overflow detected (polling fast enough?)")

wait(test_overflow_detection())
```

---

## Conclusion

**Key Takeaways:**

1. **Asyncio enables safe concurrent access** through cooperative multitasking
2. **Always use `await sleep_async()`** instead of `time.sleep()` in streaming loops
3. **The scope module is the reference implementation** for concurrent acquisition
4. **No threading locks exist** - rely on asyncio's event loop serialization
5. **Blocking operations freeze everything** - must be minimized

**For scan module implementation:**
- ✅ Use `async def _stream_continuous_async()`
- ✅ Use `await sleep_async()` between reads
- ✅ Inherit from `AcquisitionModule`
- ✅ Poll at reasonable intervals (40-50 ms for BRAM, 1 s for DDR)
- ✅ Add overflow detection
- ❌ Don't use `time.sleep()`
- ❌ Don't use threading without proper locks
- ❌ Don't poll too frequently (wastes time) or too slowly (overflow risk)

**Long-term improvement:**
Implement DDR streaming (from previous analysis) to enable:
- Much longer buffers (68 seconds vs 134 ms)
- Less frequent polling (1 second vs 40 ms)
- More time for concurrent operations
- Higher overall system efficiency

---

## References

**PyRPL Code Files:**
- `pyrpl/async_utils.py` - Asyncio integration and event loop
- `pyrpl/acquisition_module.py` - Base class for acquisition modules
- `pyrpl/hardware_modules/scope.py` - Reference implementation
- `pyrpl/redpitaya_client.py` - TCP client (no thread safety)
- `pyrpl/monitor_server/monitor_server.c` - Single-threaded server

**Python Documentation:**
- https://docs.python.org/3/library/asyncio.html
- https://docs.python.org/3/library/asyncio-task.html#coroutines

**Related Documents:**
- `STREAMING_ANALYSIS_AND_IMPROVEMENT_PLAN.md` - DDR streaming architecture
- `docs/developer_guide/communication_architecture.md` - Protocol details

---

## Next Steps

If you'd like to proceed with implementation:

1. **Create `pyrpl/hardware_modules/scan.py`** with asyncio-based streaming
2. **Add streaming methods** to existing scan module (if separate)
3. **Implement tests** for concurrent access verification
4. **Add GUI integration** for real-time display during streaming
5. **Long-term:** Integrate DDR streaming for 500x buffer improvement

Please let me know if you'd like me to implement any of these components!
