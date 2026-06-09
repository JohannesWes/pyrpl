# Scan Data Streaming

This document describes the current scan-module data-streaming architecture in
PyRPL.

Status: the canonical continuous streaming path is the ARM-side push stream
(`Scan.push_stream_*`). The older PC polling path (`Scan.stream_*`) is deprecated
and kept only as a fallback.

## Architecture

The stream still starts in the FPGA. The important change is where the short
real-time deadline is handled.

```
scan_new.v
  FPGA data3 BRAM ring, 4096 x 32-bit samples
  STREAM_WR_PTR and STREAM_SAMPLES counters
        |
        | local /dev/mem reads on the Red Pitaya ARM
        v
stream_server.c
  read-only mmap of the scan module
  drains the FPGA BRAM ring locally
  buffers framed bytes in an ARM userspace DRAM ring
  non-blocking TCP send on a dedicated stream port
        |
        v
StreamClient
  PC-side background receiver thread
  validates frame sequence numbers
  converts declared gaps into NaN samples
        |
        v
Scan.push_stream_read()
  user, qudi, or other Python consumer
```

The old polling model put the deadline on the PC: Python read the FPGA write
pointer and then pulled sample batches through the regular `monitor_server`
request/response socket. The FPGA ring holds only 4096 samples, so at
125 MHz / 4096 (~30.5 kSamples/s) the PC had about 134 ms to poll again before
the writer could lap the reader.

The push model moves that deadline to the Red Pitaya ARM. The ARM process reads
the FPGA ring locally, where access is fast and deterministic, then pushes a
framed TCP stream to the PC. If the PC pauses, samples are temporarily stored in
ordinary ARM memory before they are sent onward.

So the short answer to the architecture question is:

- Samples are first written into the FPGA `data3` BRAM ring.
- `stream_server` then drains that ring into a userspace DRAM ring on the ARM.
- The ARM DRAM ring is temporary backlog storage for PC/network stalls.
- The PC no longer polls the FPGA for the canonical streaming path.

The ARM streaming server is a separate process from the register
`monitor_server` and listens on a separate TCP port. By default this is the
register port plus 1000, so port `2222` for registers and port `3222` for the
push stream.

## FPGA Stream Engine

The scan FPGA module (`pyrpl/fpga/rtl/scan_new.v`) has two main operating modes:

- Scan mode: triggered step acquisition with accumulation into BRAM.
- Stream mode: continuous 32-bit samples into the `data3` BRAM ring.

Plain streaming uses:

| Register | Offset | Meaning |
| --- | ---: | --- |
| `STREAM_CONTROL` | `0x20` | bit 0 enable, bit 1 reset, bit 2 marker enable |
| `STREAM_STATUS` | `0x24` | bit 0 active |
| `STREAM_WR_PTR` | `0x28` | current FPGA write pointer |
| `STREAM_SAMPLES` | `0x2c` | total samples written since reset |
| `data3` BRAM | `0x30000` | 4096-sample stream ring |

Supported stream inputs are:

- `demod`: lock-in demodulated signal.
- `ftw_corr`: ODMR frequency tracker correction word, convertible with
  `Scan.ftw_to_hz()`.

Stream mode and scan mode are mutually exclusive because they share BRAM banks.

## ARM Push Server

`pyrpl/monitor_server/stream_server.c` is the ARM-side drain and push server.
It is deployed by `pyrpl/stream_deploy.py` and started lazily by
`RedPitaya.ensure_stream_server()`.

Key properties:

- It opens `/dev/mem` read-only and maps the scan module `PROT_READ`.
- It cannot enable, disable, reset, or otherwise write the FPGA.
- Stream control remains on the normal register path via `monitor_server`.
- It serves one stream client at a time and survives reconnects.
- It compiles natively on the board with `gcc -O2`.
- Deployment is source-aware: a changed `stream_server.c` triggers rebuild and
  stops stale server instances.

The critical improvement over the first push prototype is the ARM DRAM ring.
The drain loop and TCP send path are decoupled:

- The server drains new FPGA samples into a userspace ring in ARM DRAM.
- TCP sends are non-blocking.
- If the PC receive path stalls, `send()` can return `EAGAIN` while the FPGA
  drain continues.
- Loss occurs only if the ARM DRAM ring itself fills, or if the ARM process is
  starved badly enough that the FPGA BRAM ring is overrun.

The default ARM ring is 16 MB. Headroom is approximately:

```
ring_bytes / wire_rate
```

At the nominal ~30.5 kSamples/s demod stream the payload is about 122 kB/s, and
the framed wire rate is still small. With the default ring this gives many
seconds to minutes of PC-stall tolerance depending on framing and actual rate.
The exact value should be treated as an operational estimate, not as a hard API
guarantee.

The server also coalesces samples into frames. The default coalescing latency is
5 ms, which reduces header overhead and ARM wakeups while adding only a small
bounded latency.

## Wire Protocol

All words are little-endian 32-bit values.

Initial client request, sent once after connect:

```
magic, mmap_base, mmap_size, wrptr_addr, samples_addr, data_addr,
depth, poll_us, sndbuf, ring_bytes, coalesce_us
```

Server frames:

```
magic, seq, gap, n, payload[n]
```

- `seq` increments for every frame, including keepalives.
- `gap` is the number of lost samples immediately before this frame.
- `n` is the number of int32 samples in the payload.
- `gap == 0` and `n == 0` is a keepalive.

The PC receiver inserts exactly `gap` NaN samples. This keeps the time axis
truthful: a returned array length always represents real samples plus explicit
lost-sample placeholders.

## Python API

Use the `push_stream_*` methods for new code:

```python
scan = pyrpl.rp.scan

rx = scan.push_stream_start(input_source="demod")

data = scan.push_stream_read()
stats = scan.push_stream_stats()

scan.push_stream_stop()
```

Useful tuning parameters:

```python
scan.push_stream_start(
    input_source="demod",
    ring_bytes=64 << 20,   # ARM DRAM backlog ring; 0 uses server default
    coalesce_us=5000,      # max batching latency; 0 uses server default
)
```

Return types and invariants:

- `push_stream_read()` returns `np.float64`.
- Finite values are int32 stream samples cast to float64.
- NaN values are declared gaps and should not be silently converted to zero.
- `push_stream_stats()` reports `n_samples`, `n_gap`, `n_frames`,
  `n_keepalive`, `n_seq_skips`, `error`, and `running`.
- `n_seq_skips` should remain zero over TCP. Non-zero indicates a protocol or
  implementation bug, not normal data loss.

For FTW correction streams:

```python
scan.push_stream_start(input_source="ftw_corr")
raw = scan.push_stream_read()
freq_hz = scan.ftw_to_hz(raw)
```

`ftw_to_hz()` is NaN-safe because the conversion is arithmetic on float arrays.

## Legacy Polling API

The old methods remain available:

- `stream_start()`
- `stream_stop()`
- `stream_status()`
- `stream_read()`
- `stream_iter()`

They emit deprecation warnings. They are useful as a zero-dependency fallback
because they do not require SSH deployment or the second TCP port, but they put
the 134 ms FPGA-ring deadline back on the PC. The legacy reader returns `int32`
and handles overrun by warning and resetting the stream, not by preserving the
lost span with NaNs.

## Robustness Changes Beyond The Architecture Shift

The recent streaming work was not only the ARM push design. The current state
also includes several hardening changes:

- ARM DRAM ring between FPGA drain and TCP send.
- Non-blocking send path, so PC stalls do not stop the FPGA drain.
- Frame coalescing to reduce header overhead and ARM CPU load.
- Source-aware deployment and rebuild of `stream_server`.
- `monitor_server.c` socket hardening: partial-transfer-safe `send_all` and
  `recv_all`, ignored `SIGPIPE`, TCP_NODELAY, and an accept loop that survives
  client disconnects instead of exiting the whole server.
- `redpitaya_client.py` hardening: `sendall()` and explicit exceptions on closed
  sockets, so mid-transaction disconnects recover through the existing reconnect
  logic instead of spinning forever.
- `MonitorClient` request/response calls are serialized with an `RLock`, so
  GUI, qudi, and other Python threads cannot interleave bytes on the register
  socket.
- `scan_new.v` bus acknowledge logic was rewritten to a single registered
  decode pattern. This fixed intermittent AXI external aborts observed when the
  ARM stream server was reading `data3` BRAM while the PC made register reads.
- Legacy poll-stream methods were deprecated and the scan module docstring now
  points new users to push streaming.

## Position-Marker Streaming

The current working tree also contains marker streaming for hardware-synchronized
motor scans. It builds on the same push-streaming path:

- Demod samples still use the `data3` BRAM ring and the ARM push server.
- `STREAM_CONTROL` bit 2 enables marker capture.
- External position pulses are synchronized in the FPGA.
- X markers are written into the LSB BRAM bank.
- Y markers are written into the MSB BRAM bank.
- Marker values are absolute demod sample indices in the same stream timeline.

Python API:

```python
scan.mapped_stream_start(input_source="demod")
demod = scan.mapped_stream_read()
x_markers = scan.read_x_markers()
y_markers = scan.read_y_markers()
scan.mapped_stream_stop()
```

This mode is documented separately in `docs/developer_guide/motor_position_sync_scan.md`
because it concerns motor-trigger wiring and scan re-binning rather than the
core continuous data stream.

## Tests And Diagnostics

Streaming development tests are in `streaming_dev/`:

- `test_gap_unit.py`: frame parsing, sequence checks, and NaN gap insertion.
- `test_stream.py`: hardware end-to-end push streaming.
- `test_overrun.py`: induced ARM halt and overrun recovery.
- `test_ring_headroom.py`: DRAM-ring stall tolerance and gap behavior.
- `test_integration.py`: deploy/start plus real `StreamClient` path.
- `real_pyrpl_pushtest.py`: real `Scan.push_stream_*` API path.

The tests intentionally exercise the same shipped modules used by PyRPL:
`pyrpl/stream_client.py`, `pyrpl/stream_deploy.py`, `pyrpl/monitor_server/stream_server.c`,
and `pyrpl/hardware_modules/scan.py`.
