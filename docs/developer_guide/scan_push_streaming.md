# Scan-Module Push Streaming

Robust, high-throughput streaming of the scan module's demodulated lock-in data
(and ODMR FTW corrections) from the Red Pitaya to the PC.

This replaces the fragile *pull/poll* model (PC polls the FPGA write pointer and
pulls batches across the network against a 134 ms ring buffer) with an *ARM-side
drain + TCP push* model. The deadline-critical work happens locally on the
board; the PC just reads a socket. Lost samples are reported explicitly and
NaN-filled — never silently dropped.

---

## Why

The demod/FTW stream runs at `125 MHz / 4096 ≈ 30.5 kHz`, i.e. ~122 KB/s of
32-bit samples. The FPGA ring buffer (`data3` BRAM) holds only 4096 samples =
**134 ms**. In the old model the PC had to poll several times per second or lose
data, with the real-time deadline sitting *on the network* — fragile to GC
pauses, Qt event-loop stalls, and network jitter.

Bandwidth was never the problem (122 KB/s vs. a ~20 MB/s ceiling for
`/dev/mem` + Ethernet). The problem was *where the deadline lived*. Push
streaming moves it onto the ARM, where ring access is microseconds and
deterministic.

**Decoupled drain / send (DRAM ring).** The BRAM drain and the TCP send are
decoupled by a large userspace ring buffer in ordinary ARM DRAM (default
16 MB). Each iteration the server drains the FPGA BRAM into the DRAM ring (a
microsecond memcpy) and *separately* attempts a **non-blocking** send.
If the PC's receive path stalls (GC pause, OS freeze, network hiccup),
`send()` just returns `EAGAIN` while the
drain keeps running — the DRAM ring absorbs the backlog. Stall headroom is
therefore `ring_bytes / wire_rate`: ~15 s @100 kS/s up to ~2 min @30 kS/s with
the 16 MB default, tunable via `ring_bytes`. Only if the *DRAM ring itself*
fills is anything lost, and that is reported as an explicit NaN gap.

This replaced an earlier design that did drain→*blocking*-send in one step: a
stalled PC blocked `send()`, which **halted the drain**, so the FPGA lapped the
reader after 134 ms (headroom was just the ~160 KB `SO_SNDBUF` ≈ 1.3 s). The
ring removes that coupling.

**Frame coalescing.** Samples are batched into one frame until `coalesce_us`
(default 5 ms) elapses or a frame fills, cutting the 16-byte header from
>50% of the wire (the old per-iteration framing sent ~3 samples/frame) to a few
percent, and the ARM frame rate from ~10 k/s to ~200/s (CPU ~1–3 %).

## Architecture

```
 FPGA scan ring (data3 BRAM, 4096x32 @ 30.5 kHz)
        │  reg_stream_wr_ptr / reg_stream_sample_cnt
        ▼
 stream_server.c  (ARM, read-only /dev/mem mmap)
   - tight local drain loop: read wr_ptr, copy new samples
   - frames them, pushes over a dedicated TCP port
   - detects overrun locally (no network in the loop) -> gap count
        │  TCP (framed, little-endian)
        ▼
 StreamClient  (PC, pyrpl/stream_client.py)
   - background thread, blocking recv()
   - NaN-fills declared gaps, checks frame-sequence continuity
   - thread-safe buffer -> read() returns float64 (NaN = lost)
        │
        ▼
 Scan.push_stream_* (pyrpl/hardware_modules/scan.py)  <- user / qudi API
```

The streaming server is a **separate process on its own port** (default
`monitor_server port + 1000` = `3222`). The proven register `monitor_server`
path and the FPGA bitstream are never touched. The server maps `/dev/mem`
`PROT_READ` only, so it can never write to the FPGA — enabling/disabling the
stream stays on the normal register path.

## Components

| File | Role |
|---|---|
| `pyrpl/monitor_server/stream_server.c` | ARM-side drain+push server. Compiled natively on the board. |
| `pyrpl/stream_client.py` | `StreamClient`: PC-side receiver, NaN-fill, seq check. Qt-free (stdlib + numpy). |
| `pyrpl/stream_deploy.py` | Deploy/compile/start/stop the server over SSH (paramiko). Qt-free. |
| `pyrpl/redpitaya.py` | `stream_port` param; `ensure_stream_server()` / `stop_stream_server()` (lazy; never in startup). |
| `pyrpl/hardware_modules/scan.py` | `push_stream_start/read/iter/stats/stop` (canonical; legacy poll API now deprecated, kept as fallback). |

## Wire protocol

Little-endian throughout (ARM and x86 are both LE). See `stream_server.c` for the
authoritative definition.

**Request** (PC → server, once after connect): 11 × uint32
`magic('RPSS'), mmap_base, mmap_size, wrptr_addr, samples_addr, data_addr, depth, poll_us, sndbuf, ring_bytes, coalesce_us`.
- `ring_bytes` = ARM DRAM ring size (0 = 16 MB default) — sets stall headroom.
- `coalesce_us` = max ARM batching latency (0 = 5 ms default; 1 ≈ no coalescing).

**Frame** (server → PC, repeated): 4 × uint32 header + payload
`magic('RPSF'), seq, gap, n` then `n × int32`.
- `seq` increments by 1 every frame (incl. keepalives) → strict continuity check.
- `gap` = samples lost before this batch (NaN-fill count).
- `n` = int32 samples following. `gap==0, n==0` is an idle keepalive.

## Deployment

The server is **compiled natively on the board** (`gcc -O2`) by
`stream_deploy.deploy_and_start()`, which guarantees the correct ABI for any
Red Pitaya OS (the dev board runs OS v1.04 / Ubuntu 16.04 armhf; gcc is present).
This avoids the cross-toolchain in `monitor_server/Makefile`. Deployment is
**lazy** — triggered the first time `push_stream_start()` runs, not at startup —
and idempotent (rebuilds only if the binary is missing; starts only if not
already running). The compiled binary lives at
`<serverdirname>/stream_server` (e.g. `/opt/pyrpl/stream_server`).

## Public API (`Scan`)

```python
rp = pyrpl.redpitaya
scan = rp.scan

# Start: selects input, resets+enables the FPGA stream engine, deploys+starts
# the ARM server if needed, and starts the PC receiver thread.
scan.push_stream_start(input_source='demod')      # or 'ftw_corr'

# Tune stall headroom / batching latency (optional):
#   ring_bytes  : ARM DRAM ring size (0 = 16 MB default). e.g. 64<<20 for ~60 s.
#   coalesce_us : max ARM batching latency in us (0 = 5 ms default).
scan.push_stream_start(input_source='demod', ring_bytes=64 << 20, coalesce_us=5000)

# Read: returns all samples since the last call (float64). NaN marks any span
# the FPGA overran the drainer (loss is explicit, never silent).
data = scan.push_stream_read()

# For FTW-correction streams, convert to Hz (NaN-safe):
freq_hz = scan.ftw_to_hz(data)

# Iterate batches until stopped:
for batch in scan.push_stream_iter(poll_interval=0.05):
    process(batch)

# Counters for health monitoring:
stats = scan.push_stream_stats()
#   {n_samples, n_gap, n_frames, n_keepalive, n_seq_skips, error, running}

# Stop: stops the receiver and disables the FPGA stream engine. The ARM server
# is left running for fast restarts (stop it with stop_server=True if desired).
scan.push_stream_stop()
```

Invariants a consumer can rely on:
- Output is in strict acquisition order; total timeline = `n_samples + n_gap`.
- `n_seq_skips == 0` always (TCP + seq numbers); non-zero indicates a transport
  bug, not normal loss.
- `n_gap > 0` only on a genuine FPGA-ring overrun (should not happen under
  normal load given the >1 s buffer headroom); those samples appear as NaN.

## qudi integration notes

> Status: **done.** The qudi hardware module
> `qudi.hardware.redpitaya.redpitaya_data_instream.RedPitayaDataInStream`
> (in the `qudi-iqo-modules` repo) has been migrated from the legacy poll API to
> push streaming. The notes below document how that mapping was done (and how to
> migrate any other consumer). The legacy `scan.stream_*` poll API remains for
> back-compat.

The module previously used the legacy `scan.stream_start()` / `stream_read()`
poll API. The push-streaming mapping:

- **Start/stop:** call `scan.push_stream_start(input_source=...)` in the
  acquisition start (e.g. `start_buffered_acquisition`) and
  `scan.push_stream_stop()` on stop. `push_stream_start` returns the
  `StreamClient` if you want direct access.
- **Reads:** replace polled `stream_read()` with `scan.push_stream_read()`.
  Return type changed from `int32` to **`float64` with NaN for lost samples** —
  update any downstream dtype assumptions. NaNs propagate cleanly through
  `np.nanmean` etc.; decide per measurement whether to drop or keep them.
- **Threading:** `StreamClient` already runs its own daemon receive thread and
  `read()`/`stats()` are mutex-protected, so they are safe to call from a qudi
  `LogicBase` QThread. Do not share one `StreamClient` across modules.
- **No PC-side polling deadline:** the previous "must read every <134 ms" logic
  (and overflow-reset handling) can be removed; pace `push_stream_read()` to the
  measurement cadence. Use `stats()['n_gap']` / NaN counts to surface loss to the
  user instead.
- **Connection:** uses a second TCP port (`rp.stream_server_port()`, default
  3222). Ensure it is reachable (same host as the register port). The server is
  deployed/started automatically on first use over the existing SSH connection.
- **FTW vs demod:** `'demod'` streams the lock-in error signal; `'ftw_corr'`
  streams the ODMR tracker's frequency correction (use `ftw_to_hz`). Both are
  32-bit at ~30.5 kHz.

## Testing

Hardware and unit tests live in `streaming_dev/` (scratch; not packaged):
- `test_gap_unit.py` — deterministic NaN-fill / seq-continuity unit tests
  (loopback fake server, no hardware). 14/14 pass.
- `test_stream.py` — end-to-end lossless streaming on hardware.
- `test_overrun.py` — SIGSTOP-induced FPGA overrun → NaN recovery on hardware.
- `test_integration.py` — full `stream_deploy` + `StreamClient` path, records
  ~90 s to disk, mirrors `push_stream_*`.

These import the real shipping `stream_client.py` / `stream_deploy.py` (the
`pyrpl` package itself needs Qt, so the Qt-free submodules are loaded by path).

## Limitations / future work

- **Stall headroom** is now set by the ARM DRAM ring (`ring_bytes`, default
  16 MB ≈ 15 s–2 min depending on rate), which absorbs PC-side stalls in
  userspace DRAM without touching the OS or FPGA. Validated: an 8 s PC-recv
  freeze caused **0 loss** with the default ring; a deliberately tiny 512 KB
  ring overflowed and reported the loss as an exact NaN gap (`nan == gap`,
  0 seq-skips); a SIGSTOP'd ARM server (BRAM overrun) likewise reported an exact
  NaN gap and recovered.
- **Tier 2 (optional, needs one FPGA rebuild):** a sticky overrun flag in
  `STREAM_STATUS[1]` would add FPGA-level loss detection. Largely moot for
  headroom now that the DRAM ring dwarfs the BRAM; mainly of interest for
  detecting a drain that falls >134 ms behind under severe CPU starvation.
- **Tier 3 (not pursued):** AXI-DMA to DDR — the "textbook" RP solution for
  MHz-rate raw ADC streaming, but overkill for this 122 KB/s demod/FTW stream.
  The DRAM ring already buffers in DRAM (ARM CPU copy) without the AXI-DMA build
  or a reserved-memory carveout; the FPGA-DMA version only earns its keep at
  much higher (MS/s) rates where the ARM CPU can no longer keep up.
- The legacy poll API (`stream_start`/`stream_read`/…) remains for backward
  compatibility and can be retired once consumers migrate.
