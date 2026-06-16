# Hardware-Synchronized Motor Position Scanning (KDC marker streaming)


## Problem

A sensor on a 2D motorized stage (Thorlabs MTS50 + KDC101) acquires a continuous
demodulated data stream (~30.5 kHz) while the stage moves **continuously** along
each fast-axis line. We must allocate each stream sample to the spatial position
at which it was acquired, with no line shifts or latency-induced misallocation.

The previous, USB-only implementation
(`qudi.logic.motor_scan.continuous_line_scan`) sampled the encoder position over
USB at ~20 Hz with software timestamps, then reconstructed bin boundaries in
software (`interp1d(position->time)` + `searchsorted`). The data stream and the
position samples lived in two unsynchronized clocks stitched together in software,
which produced line shifts and synchronization errors whenever there was
latency.

## Key hardware fact

The KDC101 `TRIG1`/`TRIG2` SMA ports support an **"At Position Steps"** output
mode (manual §6.3.5 in the manual, see C:\Users\aj92uwef\PycharmProjects\pyrpl_new\docs\manuals\kdc101.pdf). 
As the stage moves continuously, the controller emits a 5 V TTL pulse each time the 
**encoder** position crosses
`start + n * interval`, for `count` pulses, with a configurable pulse width and
polarity. The edges are encoder-referenced (not time- or velocity-referenced),
so each pulse edge *is* an exact fast-axis bin boundary, independent of velocity
ripple, acceleration, or USB latency.

Each axis has its own KDC101. The current qudi implementation uses position-step
pulses on the fast axis and an **"In Motion"** trigger on the slow axis. The slow
axis stops at every row; on the actual hardware, position-step triggers at that
settling point could be skipped or delayed under concurrent USB polling. The
`out_in_motion` trigger instead gives one robust rising edge at the start of each
slow-axis row transition, which the FPGA records as the hardware line-boundary
marker.

## Architecture

The new mode sits between the existing scan-mode (FPGA emits triggers *out*) and
stream-mode (free-running demod stream) modes: the demod stream free-runs, but an
external hardware trigger flowing *in* (KDC -> FPGA) delimits the data into
segments/bins, captured in the FPGA's 125 MHz clock domain alongside the data.

Both KDC `TRIG` outputs go through 5 V -> 3.3 V level shifters into free
Red Pitaya expansion inputs. The FPGA only captures rising edges; the x/y
semantics come from how qudi configures the KDC outputs:

| Signal          | Pin                    | Notes                                   |
|-----------------|------------------------|-----------------------------------------|
| external trig   | `exp_p_in[0]` (DIO0_P) | pre-existing (scope/asg/dsp)            |
| MW trigger out  | `exp_p_io[7]` (DIO7_P) | pre-existing (`scan_trigger_o` to MW)   |
| x KDC trigger   | `exp_p_in[5]` (DIO5_P) | fast-axis position-step bin boundaries  |
| y KDC trigger   | `exp_p_in[6]` (DIO6_P) | slow-axis in-motion line boundaries     |

> DIO5_P shares a (default-off) housekeeping mux source with `dac_pwm_o[3]`; keep
> `exp_p_src_sel[5:6]=0` (the reset default) so both pins stay inputs. DIO6_P has
> no module-source overlap.

### Three BRAM banks, one job each

In stream mode the scan FSM is idle, so the `ram_lsb` and `ram_msb` banks (used
only for the 64-bit accumulator in scan mode) are free. We repurpose them as
marker rings:

| Bank        | Offset    | Role in marker mode                                  |
|-------------|-----------|------------------------------------------------------|
| `ram_data3` | `0x30000` | demod sample ring — **existing push stream, unchanged** |
| `ram_lsb`   | `0x10000` | **x-marker ring**: demod-sample index at each x pulse |
| `ram_msb`   | `0x20000` | **y-marker ring**: demod-sample index at each y pulse |

The single source of truth is the free-running demod sample counter
`reg_stream_sample_cnt`, which increments once per demod sample written to the
data ring. On each rising edge of the x (y) trigger, the FPGA writes the
*current* counter value into the x (y) marker ring. Therefore a marker value `m`
is exactly the index into the demod array where that hardware event falls:
`demod[m]` is the first sample after the pulse. For the fast axis that event is a
position-step bin boundary; for the slow axis it is the start of the row move.

Because we use **push** streaming, the PC-side client reconstructs the absolute
sample count and NaN-fills any FPGA-ring overruns, so the demod array index stays
aligned with the FPGA counter even if the PC stalls. That alignment is the whole
reliability guarantee; the deprecated poll path does not provide it.

The counter is **not** reset between lines: one continuous stream and one
monotonic sample axis spans the entire 2D scan (a 32-bit counter lasts ~39 h at
30.5 kHz). The marker rings (4096 deep) are drained periodically by the PC with a
tracked read pointer (the same pattern as `stream_read`), so a long raster does
not overflow them.

### Reconstruction (PC)

```text
continuous demod trace:
  demod[0] demod[1] ... demod[m] ... demod[n] ...

hardware marker arrays:
  ym = [line boundary sample indices]
  xm = [bin boundary sample indices]

reconstruction:
  1. Use y markers to choose the sample range for each raster line.
  2. Within that line range, use consecutive x markers as bin boundaries.
  3. Bin k = demod[xm[k] : xm[k + 1]].
```

The x markers are fast-axis position-step boundaries. The y markers are
slow-axis line-boundary edges from the in-motion trigger. Together they locate
each bin in the same absolute sample-index space as the demod trace.

For reverse (snake) lines the per-line bin order is reversed in software, but the
boundaries themselves are exact, so the line-shift failures of the old approach
cannot occur.

## Register/BRAM map additions (`scan_new.v`)

| Offset | Name                  | Access | Meaning                                   |
|--------|-----------------------|--------|-------------------------------------------|
| `0x20` | `STREAM_CONTROL`      | W      | bit0 enable, bit1 reset, **bit2 marker enable** |
| `0x30` | `MARKER_X_WR_PTR`     | R      | x-marker ring write pointer (mod depth)   |
| `0x34` | `MARKER_X_COUNT`      | R      | total x markers since reset               |
| `0x38` | `MARKER_Y_WR_PTR`     | R      | y-marker ring write pointer (mod depth)   |
| `0x3C` | `MARKER_Y_COUNT`      | R      | total y markers since reset               |

x markers are read from `ram_lsb` (`0x10000`), y markers from `ram_msb`
(`0x20000`), via the existing BRAM read pipeline.

## PC API (`pyrpl/hardware_modules/scan.py`)

- `mapped_stream_start(input_source='demod', ...)` — select input, enable
  stream + marker mode + reset, start the push receiver.
- `read_x_markers()` / `read_y_markers()` — wrap-aware, pointer-tracked reads of
  new markers since the last call (absolute demod-sample indices).
- `mapped_stream_read()` — alias of `push_stream_read()` (the demod array).
- `slice_by_markers(demod, markers)` — split a demod array into per-bin slices.
- `mapped_stream_stop()`.

## qudi integration

- `thorlabs_kdc101_kinesis`: `setup_position_trigger()` configures the fast-axis
  position-step output by wrapping pylablib
  `setup_kcube_trigio(... out_pulse_fw/bk ...)` and
  `setup_kcube_trigpos(start_fw, step_fw, num_fw, width, ...)` (physical units).
  `setup_motion_trigger()` configures the slow-axis `out_in_motion` output used
  as a robust line-boundary marker. `disable_position_trigger()` turns the TRIG
  output off during teardown.
- A new `KDC_HW_SYNC` scan mode that reuses the existing line/snake orchestration
  but replaces software binning with marker-defined slicing.
