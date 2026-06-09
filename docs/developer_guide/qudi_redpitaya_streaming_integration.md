# Qudi Red Pitaya Streaming Integration

This document describes how the PyRPL scan push stream is consumed by the qudi
Red Pitaya integration.

The relevant qudi files are in the `qudi-iqo-modules` repository:

- `qudi.hardware.redpitaya.redpitaya_data_instream.RedPitayaDataInStream`
- `qudi.hardware.redpitaya.resource_manager`
- `qudi.logic.time_series_reader_logic.TimeSeriesReaderLogic`
- `qudi.logic.motor_scan.hw_sync_scan` for the marker-streaming motor scan path

## Current Status

`RedPitayaDataInStream` has been migrated from the legacy PyRPL polling API to
the push-streaming API:

```python
scan.push_stream_start(...)
scan.push_stream_read()
scan.push_stream_stop()
```

It no longer polls the FPGA write pointer itself and no longer needs to drain
the 4096-sample FPGA BRAM ring within 134 ms. That deadline is handled by the
ARM-side `stream_server` in PyRPL.

## Data Path

```
scan_new.v
  FPGA stream ring in data3 BRAM
        |
        v
stream_server.c on Red Pitaya ARM
  local FPGA drain
  ARM DRAM backlog ring
  framed TCP push stream
        |
        v
pyrpl.stream_client.StreamClient
  PC receive thread
  NaN gap insertion
        |
        v
RedPitayaDataInStream
  _drain_rx() pulls from StreamClient
  applies calibration or FTW-to-Hz conversion
  appends to _pending FIFO
        |
        v
TimeSeriesReaderLogic or other qudi consumer
```

There are three buffers to keep distinct:

- FPGA BRAM ring: 4096 samples, short hard deadline, drained by the ARM.
- ARM DRAM ring: default 16 MB, absorbs PC/network stalls before TCP send.
- Qudi `_pending` FIFO: calibrated samples already received by PyRPL but not yet
  handed to the current qudi consumer.

## Resource Manager

`resource_manager.get_pyrpl_instance()` keeps one shared `pyrpl.Pyrpl` instance
per `(hostname, config_name)` key and reference-counts users.

Important behavior:

- Instance creation is protected by a module-level lock.
- Ongoing hardware access is protected inside PyRPL's `MonitorClient` by an
  `RLock`, not by the resource manager.
- If qudi runs without a GUI-capable `QApplication`, the resource manager forces
  PyRPL headless mode so PyRPL widgets are not created under a bare
  `QCoreApplication`.
- PyRPL logging is patched to avoid GUI logging recursion/signature failures.
- The shared PyRPL instance is created with `reload_fpga=True` and
  `reload_server=True`.

Because the register socket is locked inside `MonitorClient`, a PyRPL GUI
register read/write and a qudi register read/write can safely share one PyRPL
instance. They serialize complete request/response transactions instead of
interleaving bytes on the TCP socket.

The push stream itself uses a second TCP connection and a background receiver,
so normal qudi reads do not hold the register socket open while waiting for
stream data.

## RedPitayaDataInStream

The qudi hardware class implements `DataInStreamInterface` for one channel at a
fixed sample rate:

- `sample_rate`: 125 MHz / 4096, about 30.517 kSamples/s.
- `data_type`: `np.float64`.
- `streaming_modes`: continuous and finite.
- `sample_timing`: constant.
- Active channel: currently one channel, `ch1`.

Relevant config options:

```yaml
redpitaya_stream:
  module.Class: "redpitaya.redpitaya_data_instream.RedPitayaDataInStream"
  options:
    redpitaya_config_name: "rpy_shared_config"
    redpitaya_hostname: "192.168.1.100"
    channel_buffer_size: 100000
    stream_input: "demod"          # or "ftw_corr"
    calibration_factor: 1.0
    signal_scale: 1.0
    stream_ring_bytes: 0           # 0 = PyRPL stream_server default
    stream_coalesce_us: 0          # 0 = PyRPL stream_server default
```

`max_fpga_read_samples` remains accepted for old configs but is unused with
push streaming, because the ARM server drains continuously.

For `stream_input == "demod"`, the module can configure lock-in filter settings
through the PyRPL `lockin` module:

- `lock_in_fir_bypass_ch1`
- `lock_in_fir_bypass_ch2`
- `lock_in_filter_ch1`
- `lock_in_filter_ch2`

For `stream_input == "ftw_corr"`, `_drain_rx()` converts raw FTW values to Hz
using `scan.ftw_to_hz()` and then applies `signal_scale`.

## Start And Stop

On `start_stream()`:

1. Qudi clears `_pending`, `_total_samples_acquired`, and gap counters.
2. It calls `scan.push_stream_start(input_source=..., ring_bytes=..., coalesce_us=...)`.
3. PyRPL selects the FPGA stream input, resets and enables the stream engine,
   starts or reuses the ARM `stream_server`, and starts `StreamClient`.
4. Qudi stores the returned `StreamClient` in `_rx`.
5. The qudi module state is locked.

On `stop_stream()`:

1. Qudi calls `scan.push_stream_stop()`.
2. PyRPL stops the PC receiver and disables the FPGA stream engine.
3. The ARM `stream_server` is left running for faster restarts.
4. Qudi drains any tail samples from `_rx` into `_pending`.
5. The qudi module state is unlocked.

Tail draining is intentional: data already received before stop is not thrown
away and can still be read by the consumer.

## Reading

`RedPitayaDataInStream` does not read from the network directly. It calls
`_rx.read()`, where `_rx` is the PyRPL `StreamClient`.

`_drain_rx()`:

- Pulls all samples currently buffered by the receiver thread.
- Preserves NaN gap markers.
- Applies demod calibration or FTW-to-Hz conversion.
- Appends data to `_pending`.
- Updates `_total_samples_acquired`.
- Logs new gap growth using `stats()["n_gap"]`.
- In finite mode, stops the stream when `_channel_buffer_size` samples have
  been acquired.

`available_samples` reports:

```python
len(_pending) + _rx.available()
```

This means it includes data already in the qudi FIFO plus data received by
PyRPL but not yet drained into that FIFO.

`read_data_into_buffer()` blocks until the requested number of samples is
available or until a timeout expires. Sleeping in that loop does not risk FPGA
data loss because the PyRPL receiver thread and ARM server continue draining in
the background.

`read_available_data_into_buffer()` is non-blocking from the consumer's
perspective: it drains whatever is currently available and returns the number of
samples copied.

## TimeSeriesReaderLogic

`TimeSeriesReaderLogic.start_reading()` starts the configured streamer, then
emits `_sigNextDataFrame`. `_acquire_data_block()` is connected with
`Qt.QueuedConnection` and re-emits itself after each successful block.

Per iteration it:

1. Looks at `streamer.available_samples`.
2. Chooses at least `_samples_per_frame * oversampling_factor`, aligned to the
   oversampling factor and capped by the configured channel buffer size.
3. Calls `read_data_into_buffer()` for local streamers.
4. Processes the raw data into trace and averaged trace arrays.
5. Emits raw-data and trace update signals.

If a read raises an exception, the logic logs a warning and stops the reader via
`_stop_cleanup()`. There is not currently an automatic reconnect/restart loop at
this layer.

## Loss And Error Semantics

The expected loss signal is NaN in the sample stream plus growth in
`StreamClient.stats()["n_gap"]`.

Normal consumers should decide explicitly how to handle NaNs:

- Keep them to preserve the exact time axis.
- Use `np.nanmean`, `np.nanstd`, or similar for aggregate displays.
- Reject finite acquisitions if any NaN is present.
- Surface the gap count in experiment metadata.

Do not silently replace NaNs with zero. That turns data loss into a false
physical signal.

Other relevant status fields:

- `n_seq_skips`: should stay zero. Non-zero means the frame sequence was not
  continuous, which is a transport/protocol problem.
- `error`: set if the receiver thread fails.
- `running`: receiver thread state.

The current qudi module logs receiver errors when `_rx.read()` returns no data
and `_rx.error` is set, but it does not automatically restart the PyRPL stream.

## Finite Mode

Finite mode uses the same continuous push stream underneath. The qudi hardware
module stops itself once `_total_samples_acquired >= channel_buffer_size`.

Important details:

- Stop is performed through the same `_do_stop()` path as manual stop.
- Tail samples are drained into `_pending`.
- If the final receiver drain overshoots the requested size, samples are kept in
  `_pending` rather than silently dropped.
- If gaps occur in finite mode, they are logged as errors rather than warnings.

## Hardware-Synchronized Motor Scan Path

`qudi.logic.motor_scan.hw_sync_scan` uses the marker-streaming API rather than
the generic `DataInStreamInterface` path.

It calls:

```python
scan.mapped_stream_start(input_source=...)
scan.mapped_stream_read()
scan.read_x_markers()
scan.read_y_markers()
scan.mapped_stream_stop()
```

The demod data still uses the same PyRPL push stream. The extra marker reads use
the normal register path to drain marker rings from the scan module's LSB/MSB
BRAM banks. Marker values are absolute sample indices into the same demod
stream timeline, so qudi can re-bin a continuous motor scan without software
position-time interpolation.

This path is covered in more detail by
`docs/developer_guide/motor_position_sync_scan.md`.

## Test Coverage

The qudi repository contains a hardware-free logic test:

```text
scratch_test_instream_logic.py
```

It injects a fake PyRPL `StreamClient` and fake scan module into
`RedPitayaDataInStream`. It validates:

- Demod calibration.
- FTW-to-Hz conversion.
- NaN propagation.
- FIFO semantics.
- Blocking reads across multiple drains.
- `available_samples` accounting.
- Gap logging.
- Finite-mode auto-stop.
- Tail draining on stop.

The PyRPL repository contains lower-level push-stream tests in `streaming_dev/`;
those are documented in `scan_data_streaming.md`.

## Current Limitations

- The qudi hardware module supports one active channel.
- The sample rate is fixed by the FPGA decimation.
- Qudi does not currently implement automatic stream restart after receiver or
  timeout errors.
- `RedPitayaDataInStream` depends on PyRPL's scan module and the correct bitfile
  being loaded.
- The PyRPL stream server requires SSH deployment/startup access on first use.
- The dedicated stream TCP port must be reachable in addition to the register
  `monitor_server` port.
