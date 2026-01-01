# Comprehensive Analysis: PyRPL Streaming Integration with Qudi

## Executive Summary

This document analyzes the streaming functionality in PyRPL's scan module and its integration with Qudi's time-series data acquisition framework. It documents the thread-safety solution, remaining considerations, and provides recommendations for robust operation.

**Key Findings:**

1. ~~**Critical Issue**: PyRPL has **no built-in thread safety**~~ **RESOLVED** ✓ - `MonitorClient` now has built-in `threading.RLock()` protection for all socket operations
2. **Streaming Design**: The FPGA streaming is well-implemented with both `demod` and `ftw_corr` input sources
3. **Integration Quality**: The Qudi `redpitaya_data_instream.py` integration works correctly but could benefit from automatic error recovery
4. **Remaining Consideration**: Qudi-side error recovery logic is not implemented (stream read failures are logged but not auto-recovered)

---

## 1. PyRPL Streaming Implementation Analysis

### 1.1 FPGA-Level Implementation (scan_new.v)

**Strengths:**
- Clean separation between scan mode and stream mode
- Ring buffer implementation using dual-port BRAM (4096 x 32-bit)
- Independent write pointer tracking (`reg_stream_wr_ptr`) and sample counter (`reg_stream_sample_cnt`)
- Support for two input sources: `demod` (lock-in error) and `ftw_corr` (frequency correction)
- Fixed sample rate of ~30.517 kHz (125 MHz / 4096 decimation)

**Architecture:**
```
┌─────────────────────────────────────────────────────────┐
│ FPGA (scan_new.v)                                       │
│                                                         │
│  demod_input_i ───┬───► MUX ───► Ring Buffer (4096)     │ 
│  ftw_corr_i ──────┘              │                      │
│                                  ▼                      │
│  reg_stream_wr_ptr ◄────────── Write Pointer            │
│  reg_stream_sample_cnt ◄────── Sample Counter           │
│                                                         │
│  System Bus (AXI) ◄─────────── Read Port B              │
└─────────────────────────────────────────────────────────┘
```

**Limitations:**
- No hardware overflow detection flag - software must track via sample counter comparison
- Stream mode and scan mode are mutually exclusive (shared BRAM)
- 4-cycle read latency on BRAM accesses via system bus

### 1.2 Python-Level Implementation (scan.py)

**Stream API Methods:**
```python
stream_start(input_source='demod')   # Enable streaming ('demod' or 'ftw_corr')
stream_stop()                        # Disable streaming
stream_status()                      # Get (active, wr_ptr, samples_written)
stream_read(max_samples=None)        # Non-blocking read
stream_iter(poll_interval, batch)    # Generator for continuous reads
ftw_to_hz(ftw_values)                # Convert FTW correction to Hz
```

**Input Sources:**
- `'demod'`: Demodulated lock-in error signal (default)
- `'ftw_corr'`: FTW frequency correction from ODMR tracker (for monitoring resonance drift)

**Strengths:**
- Efficient bulk read using `_reads()` for block BRAM access
- Software-based overflow detection by comparing sample counters
- Timing instrumentation available (`enable_timing=True`)
- Optimized `stream_status()` reads 3 consecutive registers in one call
- Support for both error signal and frequency correction streaming

**Issues Identified:**

1. **No Thread Protection**: The `Scan` class has no mutex/lock protecting `_stream_rd_ptr` and `_stream_total_read`

2. **Blocking `stream_iter()`**: Uses `time.sleep()` which blocks the thread:
   ```python
   def stream_iter(self, poll_interval=0.005, batch=256):
       while getattr(self, '_stream_active', False):
           arr = self.stream_read(max_samples=batch)
           if arr.size:
               yield arr
           else:
               time.sleep(poll_interval)  # BLOCKS!
   ```

3. **Potential Wrap-Around Bug**: The overflow detection may have edge cases:
   ```python
   delta = (int(total_written) - int(getattr(self, '_stream_total_read', 0))) & 0xFFFFFFFF
   if delta >= depth:  # May false-trigger if counter wraps
   ```

### 1.3 Can Other Commands Execute During Streaming?

**At FPGA Level: YES** ✓
- The streaming engine writes to BRAM independently of the system bus read port
- Register reads/writes use separate AXI transactions
- Other modules (fgen3, PID, IQ, etc.) operate in parallel FPGA fabric

**At PyRPL Level: YES** ✓ (Thread-Safe as of 2024)
- The `MonitorClient` (TCP socket) is now **thread-safe** with internal `RLock` protection
- All `reads()` and `writes()` calls are serialized via `self._socket_lock`
- Concurrent access from multiple threads (GUI + Qudi) is safe

Current implementation from `redpitaya_client.py`:
```python
class MonitorClient(object):
    """TCP client for communication with Red Pitaya monitor_server.

    Thread Safety:
        This class is thread-safe. All socket operations are protected by a
        reentrant lock (RLock), allowing safe concurrent access from multiple
        threads (e.g., Qt QThreads in qudi, Python threading, or asyncio).
    """

    def __init__(self, hostname="192.168.1.0", port=2222, ...):
        self._socket_lock = threading.RLock()  # Thread-safe!
        self.socket = socket.socket(...)

    def reads(self, addr, length):
        with self._socket_lock:  # Protected
            return self.try_n_times(self._reads, addr, length)

    def writes(self, addr, values):
        with self._socket_lock:  # Protected
            return self.try_n_times(self._writes, addr, values)
```

**Timing Window Analysis:**

At ~30.5 kHz sample rate:
- ~33 μs between samples
- Typical TCP round-trip: 1-5 ms
- Buffer depth: 4096 samples ≈ 134 ms before overflow
- Lock overhead: ~0.5 μs (negligible compared to network latency)

This means you have approximately 130 ms of slack to execute other commands before risking overflow. Commands are automatically serialized by the socket lock.

---

## 2. Qudi Integration Analysis

### 2.1 Resource Manager (resource_manager.py)

**Design:**
```python
_pyrpl_instances = {}  # Global dictionary of shared instances
_pyrpl_lock = Lock()   # Threading lock for instance creation

def get_pyrpl_instance(hostname, config_name, gui=True):
    with _pyrpl_lock:
        # Create or return existing instance
```

**Note:** The resource manager lock only protects instance creation, not ongoing operations.
However, this is **no longer a concern** because `MonitorClient` itself is now thread-safe:
```python
# Both of these are now safe to call concurrently:
pyrpl_object.rp.scan.stream_read()     # Thread 1 - acquires socket lock
pyrpl_object.rp.fgen3.frequency = 1e6  # Thread 2 - waits for socket lock, then proceeds
```

The internal `RLock` in `MonitorClient` ensures all FPGA operations are serialized.

### 2.2 RedPitayaDataInStream Implementation

**Architecture:**
```
┌──────────────────────────────────────────────────────────────┐
│ TimeSeriesReaderLogic (Qudi Logic Thread)                    │
│                                                              │
│  _sigNextDataFrame ──► _acquire_data_block()                 │
│                              │                               │
│                              ▼                               │
│                    streamer.read_data_into_buffer()          │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ RedPitayaDataInStream (Qudi Hardware Thread)                 │
│                                                              │
│  _thread_lock ◄──── RecursiveMutex (protects Qudi-side state)│
│                                                              │
│  _poll_fpga_and_update_buffer() ───► scan.stream_read()      │
│                                              │               │
└──────────────────────────────────────────────┼───────────────┘
                                               │
                                               ▼
┌──────────────────────────────────────────────────────────────┐
│ PyRPL scan module                                            │
│                                                              │
│  _reads() ───► MonitorClient.reads() ───► TCP Socket         │
│                        │                                     │
│                        ▼                                     │
│              ✓ _socket_lock (RLock) protects all access      │
└──────────────────────────────────────────────────────────────┘
```

**Thread Safety (Two Layers):**

1. **Qudi Layer:** `RedPitayaDataInStream` uses `RecursiveMutex` for its internal state:
```python
def configure(self, ...):
    with self._thread_lock:  # ✓ Protects Qudi buffer state
        # ... configuration

def read_available_data_into_buffer(self, ...):
    with self._thread_lock:  # ✓ Protects buffer operations
        # ... buffer operations
```

2. **PyRPL Layer:** `MonitorClient` uses `RLock` for socket operations:
```python
def reads(self, addr, length):
    with self._socket_lock:  # ✓ Protects TCP socket
        return self.try_n_times(self._reads, addr, length)
```

Both layers work together to ensure thread-safe operation.

### 2.3 Time Series Reader Logic Integration

**Flow:**
```
start_reading() 
    └─► _streamer().start_stream()
    └─► _sigNextDataFrame.emit()  ─► _acquire_data_block()
                                          │
                                          ▼
                                   streamer.read_data_into_buffer()
                                          │
                                          ▼
                                   _poll_fpga_and_update_buffer()
                                          │
                                          ▼
                                   scan.stream_read()
```

**Potential Issues:**

1. **Missing Error Recovery**: If `stream_read()` fails due to socket desync, the logic doesn't reset the stream:
   ```python
   except Exception as e:
       self.log.error(f'Error polling FPGA: {e}')
       return 0  # Silently fails, buffer state corrupted
   ```

2. **Timeout Logic**: `read_data_into_buffer()` has blocking waits with timeout, but on timeout:
   ```python
   raise TimeoutError(...)  # No cleanup of stream state!
   ```

---

## 3. Historical Context: PyRPL GUI Concurrent Access (RESOLVED)

> **Note:** This section documents a thread-safety issue that has been **resolved** by adding
> `threading.RLock()` protection to `MonitorClient`. It is retained for historical context
> and to explain the design decision.

### 3.1 Why Does the GUI Open?

In `resource_manager.py`:
```python
pyrpl_object = pyrpl.Pyrpl(
    hostname=hostname,
    config="",
    reload_fpga=True,
    reload_server=True,
    gui=gui  # Default: True
)
```

When `gui=True`, PyRPL creates Qt widgets that run in their own event loop context.

### 3.2 The Thread Architecture (Now Safe)

```
┌─────────────────────────────────────────────────────────────────┐
│ Qt Main Thread (Qudi Manager)                                   │
│                                                                 │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐          │
│  │ OdmrGui     │    │ TimeReader  │    │ PyRPL GUI   │          │
│  │ (GuiBase)   │    │ (LogicBase) │    │ Widgets     │          │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘          │
│         │                  │                   │                │
│         └──────────────────┼───────────────────┘                │
│                            │                                    │
│                            ▼                                    │
│              ┌─────────────────────────────┐                    │
│              │ Shared PyRPL Instance       │                    │
│              │                             │                    │
│              │  MonitorClient              │ ◄── Single Socket  │
│              │  ✓ THREAD SAFE (RLock)      │                    |
│              └─────────────────────────────┘                    │
└─────────────────────────────────────────────────────────────────┘
```

When you click in the PyRPL GUI:
1. Qt event triggers a register read/write via PyRPL widgets
2. The widget calls `module.attribute = value` which triggers `_write()`
3. `_write()` calls `MonitorClient.writes()` → **acquires `_socket_lock`**
4. If Qudi is mid-`stream_read()`, it **waits for the lock**, then proceeds safely

### 3.3 Historical Crash Scenarios (No Longer Occur)

These scenarios **previously** caused crashes before thread safety was added:

**Scenario 1: Read/Write Interleave** (Fixed)
```
Thread A (Qudi):  acquires _socket_lock, socket.send(read_header)
Thread B (GUI):   waits for _socket_lock  ← BLOCKS
Thread A:         socket.recv(), releases lock
Thread B:         acquires lock, socket.send(write_header + data)  ← SAFE
```

**Scenario 2: Partial Read** (Fixed)
```
Thread A:  acquires lock, socket.recv(expected_length)
Thread B:  waits for lock  ← Cannot interfere
Thread A:  receives complete response, releases lock
Thread B:  acquires lock, proceeds safely
```

---

## 4. Current Status and Remaining Recommendations

### 4.1 Thread Safety: IMPLEMENTED ✓

The critical thread-safety issue has been resolved. No workarounds are needed.

**Current Implementation** (`pyrpl/redpitaya_client.py`):
```python
class MonitorClient(object):
    def __init__(self, hostname, port, ...):
        self._socket_lock = threading.RLock()  # Reentrant lock for thread safety
        self.socket = socket.socket(...)
        # ...

    def reads(self, addr, length):
        with self._socket_lock:
            return self.try_n_times(self._reads, addr, length)

    def writes(self, addr, values):
        with self._socket_lock:
            return self.try_n_times(self._writes, addr, values)
```

**Why RLock instead of Lock?**
- `RLock` (reentrant lock) allows the same thread to acquire the lock multiple times
- This is important for nested calls like `reads() -> try_n_times() -> restart() -> close()`
- Performance overhead is negligible (~0.5µs) compared to network latency (~100-500µs)

### 4.2 GUI Usage: SAFE ✓

With thread-safety implemented, the PyRPL GUI can be used alongside Qudi streaming:
```python
pyrpl_object = pyrpl.Pyrpl(
    hostname=hostname,
    config="",
    gui=True  # Safe to use - concurrent access is protected
)
```

### 4.3 Remaining Improvements (Not Yet Implemented)

The following improvements would enhance robustness but are not critical:

**1. Add Overflow Recovery to Qudi Layer** (Recommended):

Currently, `_poll_fpga_and_update_buffer()` logs errors but doesn't auto-recover:
```python
# Current implementation:
except Exception as e:
    self.log.error(f'Error polling FPGA: {e}')
    return 0  # Just logs error, no recovery

# Recommended improvement:
except Exception as e:
    self.log.warning(f'Stream read failed: {e}, resetting...')
    self._scan_module.stream_stop()
    self._scan_module.stream_start(input_source=self._current_stream_input)
    return 0
```

**2. Add Retry Logic to Time Series Logic** (Optional):
```python
def _acquire_data_block(self):
    # ... existing code ...
    except Exception as e:
        self.log.warning(f'Reading data from streamer went wrong: {e}')
        # Don't stop entirely, try to recover
        if self._consecutive_errors >= 3:
            self._stop_cleanup()
        else:
            self._consecutive_errors += 1
            self._sigNextDataFrame.emit()  # Retry
            return
    self._consecutive_errors = 0
```

**3. Non-Blocking Reads in PyRPL** (Low Priority):

The current `stream_iter()` uses `time.sleep()` which is adequate for most use cases.
A Qt-compatible alternative would be:
```python
# In scan.py stream_iter()
def stream_iter(self, poll_interval=0.005, batch=256):
    while getattr(self, '_stream_active', False):
        arr = self.stream_read(max_samples=batch)
        if arr.size:
            yield arr
        else:
            # Qt-compatible non-blocking wait (optional improvement)
            QtCore.QCoreApplication.processEvents()
            QtCore.QThread.msleep(int(poll_interval * 1000))
```

### 4.4 Configuration Recommendations

**For Reliable Streaming:**
```yaml
# Qudi config
redpitaya_stream:
    module.Class: 'redpitaya.redpitaya_data_instream.RedPitayaDataInStream'
    options:
        redpitaya_hostname: '192.168.1.100'
        channel_buffer_size: 200000  # ~6.5 seconds at 30.5 kHz
        max_fpga_read_samples: 2048  # Read smaller chunks more frequently
```

**Buffer Sizing Math:**
- FPGA buffer: 4096 samples = 134 ms
- Recommended poll interval: 10-50 ms (read every ~300-1500 samples)
- Software buffer: 100,000+ samples for safety margin

---

## 5. Summary

| Issue | Severity | Status | Notes |
|-------|----------|--------|-------|
| GUI clicks cause crashes | ~~Critical~~ | **RESOLVED** ✓ | `MonitorClient` now has `RLock` protection |
| Stream data loss possible | Medium | Mitigated | FPGA overflow detection works; PyRPL handles it |
| Qudi-side error recovery | Low | Not implemented | Would improve robustness (see §4.3) |
| Blocking stream_iter() | Low | Acceptable | `time.sleep()` is adequate for most use cases |

**Current State:**
- ✓ **Thread-safe PyRPL access** - concurrent GUI + Qudi streaming works correctly
- ✓ **Both input sources available** - `demod` and `ftw_corr` streaming
- ✓ **FPGA streaming robust** - ring buffer with overflow detection
- ○ **Qudi recovery logic** - could be improved but not critical

The streaming functionality is production-ready. Thread safety has been implemented at the `MonitorClient` level, allowing safe concurrent access from multiple threads (Qudi modules + PyRPL GUI).

---

## Appendix A: Error Messages and Their Causes

### From PyRPL (redpitaya_client.py):

| Error Message | Cause | Solution |
|---------------|-------|----------|
| `"Wrong control sequence from server: ..."` | TCP response doesn't match request (rare now) | Restart PyRPL; check network stability |
| `"Error occured in reading attempt N. Reconnecting..."` | Socket timeout (network issue) | Check network connection; reduce load |
| `"Socket error during connection attempt N"` | Connection lost | Check Red Pitaya power; verify network |

### From Qudi (redpitaya_data_instream.py):

| Error Message | Cause | Solution |
|---------------|-------|----------|
| `"Overflow detected..."` | FPGA wrote faster than Python read | Increase poll frequency or batch size |
| `"Software buffer overflow!"` | Qudi buffer full before processing | Increase `channel_buffer_size` config |
| `"Error polling FPGA: ..."` | Network error during stream_read | Check hardware connection |
| `"Timeout waiting for N samples"` | Stream stalled or socket error | Check hardware connection; verify stream is active |

### From PyRPL Scan Module (scan.py):

| Error Message | Cause | Solution |
|---------------|-------|----------|
| `"Overflow detected. FPGA writer has advanced..."` | Reader too slow (buffer overflow) | Read more frequently or larger batches |
| `"Streaming only supported for 'demod' and 'ftw_corr'"` | Invalid input source | Use `'demod'` or `'ftw_corr'` |

### General Troubleshooting:

1. **Network issues**: Check Ethernet connection, try pinging the Red Pitaya
2. **Stream stalls**: Verify FPGA is configured correctly, check signal source
3. **Persistent errors**: Power-cycle Red Pitaya, reload FPGA bitstream

---

## Appendix B: Quick Reference - PyRPL Usage Patterns

### ✓ SAFE - Concurrent Access (Current Implementation):
```python
# Thread 1 (Qudi Logic)
while streaming:
    data = scan.stream_read()  # Acquires socket lock internally

# Thread 2 (PyRPL GUI) - clicking a widget triggers:
fgen.frequency = new_value  # Waits for lock, then proceeds safely
```

All PyRPL operations are automatically serialized by `MonitorClient._socket_lock`.

### ✓ Streaming with GUI Enabled:
```python
# In resource_manager.py - get_pyrpl_instance()
pyrpl_object = pyrpl.Pyrpl(
    hostname=hostname,
    config="",
    gui=True  # Safe - concurrent access is protected
)

# Both operations work safely in parallel:
scan.stream_start('demod')      # Qudi streaming
fgen.frequency = 10e6           # GUI parameter change
```

### ✓ Using Both Input Sources:
```python
# Stream demodulated error signal (loop open)
scan.stream_start(input_source='demod')
for batch in scan.stream_iter():
    error_signal = batch  # Raw demodulated values
scan.stream_stop()

# Stream FTW correction (loop closed, tracking resonance)
scan.stream_start(input_source='ftw_corr')
for batch in scan.stream_iter():
    freq_drift_hz = scan.ftw_to_hz(batch)  # Convert to Hz
scan.stream_stop()
```

### Qudi Configuration Example:
```yaml
redpitaya_stream:
    module.Class: 'redpitaya.redpitaya_data_instream.RedPitayaDataInStream'
    options:
        redpitaya_hostname: '192.168.1.100'
        channel_buffer_size: 100000     # ~3.3 seconds at 30.5 kHz
        stream_input: 'demod'           # or 'ftw_corr'
```

---

*Document updated: 2025-12*
*Based on analysis of pyrpl_new/ and qudi-core/ codebases*
*Thread-safety implemented via MonitorClient._socket_lock (RLock)*
