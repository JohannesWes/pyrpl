# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in the pyrpl repository.

## Project Overview

PyRPL (Python RedPitaya Lockbox) is a DSP controller for quantum optics experiments that turns Red Pitaya hardware into a powerful real-time signal processing device. The project bridges Python scripting with FPGA hardware via a layered architecture using register-mapped I/O over TCP.

## Architecture Overview

### Core Design Pattern: Descriptor-Based Register Mapping

PyRPL's fundamental abstraction uses Python descriptors to transparently map hardware registers to Python attributes:

```python
class MyModule(HardwareModule):
    frequency = FrequencyRegister(0x04)  # Maps to FPGA address offset 0x04
    gain = FloatRegister(0x08, bits=14)  # 14-bit fixed-point at offset 0x08

# Usage looks like normal Python, but underneath it's FPGA I/O:
module.frequency = 10e6  # → TCP write to FPGA register
value = module.gain      # → TCP read from FPGA register
```

**This pattern is pervasive throughout the codebase.** When you see attribute assignments on hardware modules, they're actually register writes.

### System Layers (High to Low)

```
┌─────────────────────────────────────┐
│  User Scripts / GUI Widgets         │ ← PyQt5 interfaces, user applications
├─────────────────────────────────────┤
│  Software Modules                   │ ← NetworkAnalyzer, Lockbox (orchestrators)
├─────────────────────────────────────┤
│  Hardware Modules (Python)          │ ← Scope, PID, IQ, Scan (HardwareModule subclasses)
├─────────────────────────────────────┤
│  Attribute/Register System          │ ← FloatRegister, SelectRegister (descriptors)
├─────────────────────────────────────┤
│  Configuration (MemoryTree)         │ ← YAML persistence with debounced writes
├─────────────────────────────────────┤
│  TCP Client (MonitorClient)         │ ← Binary protocol over port 2222
├─────────────────────────────────────┤
│  Red Pitaya monitor_server          │ ← Embedded binary on ARM CPU
├─────────────────────────────────────┤
│  AXI Bus (SoC Interconnect)         │ ← Memory-mapped I/O to FPGA
├─────────────────────────────────────┤
│  FPGA (red_pitaya.bin @ 125 MHz)    │ ← Verilog modules in pyrpl/fpga/rtl/
└─────────────────────────────────────┘
```

### Key Directories

**`pyrpl/hardware_modules/`** - Python wrappers for FPGA modules
- Each file (scope.py, pid.py, iq.py, etc.) defines a HardwareModule subclass
- Register definitions map to FPGA addresses via descriptors
- `dsp.py` handles signal routing between modules

**`pyrpl/software_modules/`** - High-level instruments
- NetworkAnalyzer, SpectrumAnalyzer, Lockbox
- Orchestrate hardware modules but don't directly access FPGA
- Use module ownership system to temporarily control hardware

**`pyrpl/fpga/`** - FPGA build system and Verilog source
- `rtl/` contains Verilog modules (scan_new.v, red_pitaya_pid_block.v, etc.)
- `Makefile` and TCL scripts drive Vivado compilation
- Each hardware module occupies 64KB address space: `0x40300000 + module_num * 0x10000`

**`pyrpl/widgets/`** - GUI components
- Module widgets are auto-generated from `_gui_attributes` lists
- Attribute widgets (spinboxes, plots, etc.) update via Qt signals
- `pyrpl_widget.py` is the main tabbed interface

**`pyrpl/`** - Core infrastructure
- `attributes.py` (1500 lines): All register/property descriptor classes
- `modules.py` (800 lines): Module base class with metaclass magic
- `memory.py` (650 lines): YAML configuration tree with debounced persistence
- `redpitaya.py` (500 lines): Device connection, SSH, FPGA loading
- `redpitaya_client.py` (270 lines): Binary TCP protocol implementation

## Critical Architecture Concepts

### 1. Module Ownership System

Prevents conflicts when multiple software modules need the same hardware:

```python
# Software module temporarily "owns" hardware module
with pyrpl.network_analyzer.pop('my_task') as na:
    # Inside context: na is owned, config changes NOT auto-saved
    na.points = 1000
    na.run()
# After context: na reverts to saved config, becomes available
```

### 2. Attribute Categories

**Registers** (hardware-backed):
- `IntRegister`, `FloatRegister`, `BoolRegister`: Basic types
- `FrequencyRegister`, `PhaseRegister`: Domain-specific with units
- `SelectRegister`: Enumerated options (mux controls)
- `FilterRegister`: IIR filter coefficients
- Changes write to FPGA via TCP

**Properties** (software-only):
- `IntProperty`, `FloatProperty`, etc.: In-memory state
- `ProxyProperty`: Transparent alias to another module's attribute
- `CurveSelectProperty`: Database curve selection
- Changes only affect Python state

### 3. Signal Routing

The FPGA is a modular signal processor. Modules connect via register writes:

```python
# Route IQ0 output → PID0 input
r.pid0.input = r.iq0  # or r.pid0.input = 'iq0'

# Route PID0 output → DAC output 1
r.pid0.output_direct = 'out1'

# Available inputs discovered dynamically from running modules
available = r.scope.input._get_options(r.scope)
```

### 4. Configuration Persistence

All module settings automatically save to YAML via MemoryTree:

```python
# Attribute changes auto-save (debounced, 3-second timer)
scope.decimation = 64  # Writes to config file after delay

# Save/restore named states
scope.save_state('high_bandwidth')
scope.load_state('high_bandwidth')
scope.states  # List saved states
```

### 5. Setup Attributes

Modules define which attributes are persisted via `_setup_attributes`:

```python
class Scope(HardwareModule):
    _setup_attributes = ["input1", "input2", "decimation", "trigger_source"]

    # Metaclass auto-generates:
    # def setup(self, input1=None, input2=None, decimation=None, ...):
    #     ...
```

## FPGA-Python Mapping Example

**Verilog** (scan_new.v):
```verilog
// Register at offset 0x04: number of scan steps (12 bits)
reg [11:0] num_steps;

// Register at offset 0x08: dwell time in clock cycles (32 bits)
reg [31:0] dwell_cycles;
```

**Python** (scan.py):
```python
class Scan(HardwareModule):
    addr_base = 0x40500000  # Base address for Scan module

    # Maps to addr_base + 0x04
    num_steps = IntRegister(0x04, bits=12, doc="Number of scan steps")

    # Maps to addr_base + 0x08, converts seconds→cycles
    dwell_time = CyclesRegister(0x08, doc="Time per step in seconds")
```

**User Code**:
```python
p = Pyrpl('config')
p.rp.scan.num_steps = 100      # Write 100 to 0x40500004
p.rp.scan.dwell_time = 0.001   # Convert 1ms→125000 cycles, write to 0x40500008
```

## Recent Development Work

### Scan Module Streaming Optimization

The Scan module recently received significant performance improvements for continuous data streaming.

**Push streaming (robust, recommended):** In addition to the legacy poll-based
`stream_read`, the Scan module now supports ARM-side drain + TCP push streaming,
which moves the real-time deadline off the PC/network and onto the board and
NaN-fills any lost samples instead of dropping them silently. API:
`scan.push_stream_start('demod'|'ftw_corr')`, `push_stream_read()` (float64,
NaN = loss), `push_stream_iter()`, `push_stream_stats()`, `push_stream_stop()`.
Implemented by `pyrpl/monitor_server/stream_server.c` (compiled natively on the
board), `pyrpl/stream_client.py`, `pyrpl/stream_deploy.py`, and lazy
`RedPitaya.ensure_stream_server()`. See
`docs/developer_guide/scan_data_streaming.md` and
`docs/developer_guide/qudi_redpitaya_streaming_integration.md`.

### ODMR Frequency Lock Integration

A new hardware module for ODMR (Optically-Detected Magnetic Resonance) frequency tracking has been implemented:

**Signal Flow:**
```
Lock-in (demod) → odmr_freq_lock (Region 8) → fgen3 (FTW correction) → Scan (streaming)
```

**Key Components:**
- **`odmr_freq_lock_1f.v`** (FPGA): Frequency-locked loop at Region 8 (0x40800000)
  - Demodulates 1f-I error signal from lock-in at ~30.5 kHz
  - Computes FTW correction using Q8.24 fixed-point arithmetic
  - Default bandwidth: 300 Hz (conservative start: 150 Hz)
  - Saturation limit: ±1 MHz correction range

- **`odmr_freq_lock.py`** (Python): HardwareModule interface
  - User-friendly properties: `mu_hz_per_lsb`, `max_correction_hz`, `correction_hz`
  - Status monitoring: `locked`, `saturated`, `error_lsb`
  - Convenience methods: `set_bandwidth()`, `get_status()`, `clear()`

- **`fgen3` Integration**: FTW correction automatically applied to all 3 frequency components
  - Transparent to Python frequency attributes
  - Affects both base frequency and FM modulation
  - Zero Python overhead (FPGA-level integration)

- **`scan` Streaming**: New `ftw_corr` input mode for monitoring frequency drift
  - Stream FTW corrections at ~30.5 kHz via `scan.stream_start(input_source='ftw_corr')`
  - Convert FTW to Hz using `scan.ftw_to_hz()`

**Documentation:**
- Implementation guide: `docs/developer_guide/odmr_freq_lock_implementation.md`
- Module docstrings: `odmr_freq_lock.py`, `fgen3.py`
- FPGA headers: `odmr_freq_lock_1f.v`, `red_pitaya_3fgen.v`

### Hardware-Synchronized Motor Position Scanning (Scan MODE 3)

A third scan-module mode for 2D motorized-stage scans that allocates the demod
stream to spatial positions using **hardware** triggers instead of software
timestamps. Both KDC101 controllers emit encoder-referenced "At Position Steps"
TTL pulses (x = one per fast-axis bin, y = one per line); level-shifted into
`exp_p_in[5]`/`exp_p_in[6]` (DIO5_P/DIO6_P; DIO7_P stays the MW trigger out),
the FPGA edge-detects them and records the current
demod sample index into marker rings (x in `ram_lsb`, y in `ram_msb`) sharing the
demod ring's free-running sample counter. The PC slices the continuous push
stream at the marker indices for exact data↔position allocation (no line shifts).

**Key components:**
- `scan_new.v` MODE 3: `STREAM_CONTROL` bit2 enables marker capture; new regs
  `0x30`–`0x3C` (x/y marker wr_ptr + count); inputs `x_pos_trig_i`/`y_pos_trig_i`.
- `scan.py`: `mapped_stream_start/read/stop`, `read_x_markers`, `read_y_markers`,
  `slice_by_markers`.
- qudi: `ScanMode.KDC_HW_SYNC`, `thorlabs_kdc101_kinesis.setup_position_trigger`
  (pylablib `setup_kcube_trigio`/`trigpos`), `motor_scan/hw_sync_scan.py`
  (`reconstruct_hw_sync_scan` + `HwSyncScanMixin`).
- Tests: `pyrpl/test/test_scan_markers.py`,
  `qudi-iqo-modules/tests/test_hw_sync_reconstruct.py`.

**Documentation:** `docs/developer_guide/motor_position_sync_scan.md`

## Important Patterns

### Adding New Hardware Module

1. Create Verilog module in `pyrpl/fpga/rtl/your_module.v`
2. Instantiate in `pyrpl/fpga/red_pitaya_dsp.v` with address assignment
3. Create Python class in `pyrpl/hardware_modules/your_module.py`:
   ```python
   class YourModule(HardwareModule):
       addr_base = 0x40XXX000  # Assigned address
       _setup_attributes = ['param1', 'param2']

       param1 = IntRegister(0x00, bits=16)
       param2 = FloatRegister(0x04, bits=14)

       def _setup(self):
           # Custom initialization logic
           pass
   ```
4. Register in `pyrpl/redpitaya.py` in `_make_module_list()`
5. Compile FPGA with `cd pyrpl/fpga && copy_and_make.bat`

### Modifying Attributes

When editing attribute definitions:

1. Changes to register addresses require FPGA recompilation
2. Changes to property types/validation only need Python restart
3. GUI updates automatically via Qt signals
4. Config file format preserved (YAML is backward compatible)

### Debugging FPGA Issues

1. Use `DummyClient` for testing without hardware:
   ```python
   p = Pyrpl('test', client=DummyClient())
   ```
2. Check register reads/writes with logging:
   ```python
   import logging
   logging.getLogger('pyrpl.redpitaya').setLevel(logging.DEBUG)
   ```
3. Check Vivado reports, e.g. `pyrpl/fpga/out/post_route_timing_summary.rpt`
4. Use SignalTap/ILA for live FPGA debugging (requires Vivado license)

## Common Gotchas

1. **Register bit width**: Must match Verilog definition exactly. Overflow causes silent wrapping.

2. **Module ownership**: Attempting to use a module owned by another context raises exception. Use `.pop(owner)`.

3. **Setup attributes**: Only attributes in `_setup_attributes` are saved/restored. Others are ephemeral.

4. **FPGA clock domain**: All timing is in 125 MHz clock cycles (8 ns). Use `CyclesRegister` for automatic conversion.

5. **Debounced saves**: Config writes are batched (3 sec default). Force immediate save with `module._config._save_now()`.

6. **Qt thread safety**: GUI updates must use signals. Never directly modify widgets from module code.

7. **RPyC serialization**: Complex numpy dtypes don't serialize. Use `.tolist()` then reconstruct locally.

8. **Signed vs unsigned**: Verilog 2's complement requires careful handling in Python. Use `signed=True` parameter.

## Testing Guidelines

- Always test with actual hardware when modifying hardware modules
- Use `DummyClient` for unit testing pure Python logic
- Run full test suite before committing FPGA changes (3 minutes)
- Check for timing violations in Vivado reports after FPGA compilation
- Verify no config file corruption after module changes

