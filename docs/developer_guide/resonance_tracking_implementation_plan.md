# Resonance Tracking Implementation Plan for Lock-in ODMR

**Date:** October 13, 2025  
**Branch:** scan_module_dev_johannes_filtering_tests  
**Status:** Planning Phase

## Overview

This document describes a plan to implement closed-loop resonance tracking for lock-in ODMR measurements on the Red Pitaya FPGA, based on the method described in:

**Reference:** Clevenson et al. (2018) - "Robust high-dynamic-range vector magnetometry with nitrogen-vacancy centers in diamond"

## Background

### Current System
- **Hardware:** Red Pitaya FPGA board
- **Measurement:** Lock-in ODMR with external LO microwave source (2.7-3 GHz range)
- **Current Method:** Full frequency range scans with post-processing to detect resonance features
  - CW-ODMR: Lorentzian dip
  - Lock-in ODMR: Dispersion-like feature

### Goal
Implement real-time resonance tracking to continuously follow resonance shifts without repeated full-range scans, similar to Clevenson et al. approach.

## Existing Code Analysis

### Key Modules

1. **lock_in.v**
   - Implements phase-sensitive detection
   - CIC decimation by 4096
   - FIR lowpass filtering (500 Hz)
   - Outputs: filtered lock-in signal (32-bit) and valid flag

2. **scan_new.v**
   - Coordinates stepped frequency scans
   - Features: settling time, dwell time, trigger generation
   - BRAM storage for LSB, MSB, and count data
   - Streaming interface available

3. **red_pitaya_3fgen.v**
   - Three-component FM sine generator
   - Can provide FM modulation input
   - Suitable for frequency control

4. **red_pitaya_top.v**
   - Top-level integration
   - System bus decoder/multiplexer

## Implementation Strategy

### Phase 1: Add Tracking Controller Module

Create `odmr_tracker.v` implementing:
- **Input:** Dispersive lock-in signal
- **Output:** Frequency correction to track zero-crossing
- **Controller:** PI (Proportional-Integral) feedback
- **Features:**
  - Lock detection and monitoring
  - Lock loss detection with automatic recovery
  - Configurable gains and thresholds

#### Module Interface

```verilog
module odmr_tracker #(
    parameter DATA_WIDTH = 32,
    parameter FREQ_STEP_WIDTH = 32,
    parameter INTEGRATOR_WIDTH = 40
) (
    // Clock and Reset
    input wire                          clk_i,
    input wire                          rstn_i,
    
    // Lock-in amplifier input (dispersion signal)
    input wire signed [DATA_WIDTH-1:0]  lockin_signal_i,
    input wire                          lockin_valid_i,
    
    // Control interface
    input wire                          tracking_enable_i,
    input wire signed [FREQ_STEP_WIDTH-1:0] center_freq_i,
    input wire [15:0]                   lock_threshold_i,
    input wire [31:0]                   integrator_gain_i,
    input wire [31:0]                   proportional_gain_i,
    
    // Output to frequency synthesizer
    output reg signed [FREQ_STEP_WIDTH-1:0] freq_correction_o,
    output reg                          locked_o,
    output reg                          lock_lost_o,
    
    // Status outputs
    output reg signed [DATA_WIDTH-1:0]  error_signal_o,
    output reg signed [INTEGRATOR_WIDTH-1:0] integrator_state_o,
    
    // System bus interface
    input wire [31:0]                   sys_addr,
    input wire [31:0]                   sys_wdata,
    input wire                          sys_wen,
    input wire                          sys_ren,
    output reg [31:0]                   sys_rdata,
    output reg                          sys_err,
    output reg                          sys_ack
);
```

#### Register Map (Base Address: TBD)

| Address | Access | Name | Description |
|---------|--------|------|-------------|
| 0x00000 | W/R | CONTROL | bit[0]: tracking_enable |
| 0x00004 | R | STATUS | bit[0]: locked, bit[1]: lock_lost |
| 0x00008 | W/R | CENTER_FREQ | Center frequency setpoint |
| 0x0000C | W/R | LOCK_THRESHOLD | Lock detection threshold |
| 0x00010 | W/R | P_GAIN | Proportional gain (Q16.16) |
| 0x00014 | W/R | I_GAIN | Integral gain (Q16.16) |
| 0x00018 | R | FREQ_CORRECTION | Current frequency correction |
| 0x0001C | R | ERROR_SIGNAL | Current error signal |
| 0x00020 | R | INTEGRATOR_STATE | Integrator state (lower 32 bits) |

#### PI Controller Algorithm

```verilog
// Error signal is the lock-in output (should be zero at resonance)
error_signal = lockin_signal_i;

// Proportional term
proportional_term = (error_signal * p_gain) >> 16;

// Integral term with anti-windup
if (!locked || abs(error_signal) < lock_threshold) {
    integrator += (error_signal * i_gain) >> 16;
}

// Frequency correction
freq_correction = center_freq + proportional_term + (integrator >> N);
```

#### Lock Detection Logic

- Count consecutive cycles where `|error_signal| < lock_threshold`
- Declare "locked" after 1000 consecutive cycles
- Set "lock_lost" flag if error exceeds threshold while locked
- Reset lock counter when error exceeds threshold

### Phase 2: Modify scan_new.v for Dual-Mode Operation

Add tracking mode alongside existing scan mode:

```verilog
// New parameters
parameter TRACKING_MODE_SUPPORT = 1

// New registers
reg reg_tracking_mode;           // 0=scan, 1=tracking
reg reg_initial_scan_done;       // Initial scan completed flag

// New address map entries
localparam ADDR_TRACKING_MODE   = 20'h00030;
localparam ADDR_RESONANCE_FREQ  = 20'h00034;

// Modified state machine
// After scan completion:
//   1. If tracking_mode enabled, analyze data
//   2. Find resonance frequency (zero-crossing)
//   3. Pass to tracking module
//   4. Enter tracking state
```

### Phase 3: Integration in red_pitaya_top.v

```verilog
// Instantiate tracker module
wire signed [31:0] tracker_freq_correction;
wire tracker_locked;
wire tracker_lock_lost;
wire signed [31:0] lockin_output;
wire lockin_valid;

// Connect lock-in to tracker
odmr_tracker i_tracker (
    .clk_i(adc_clk),
    .rstn_i(adc_rstn),
    .lockin_signal_i(lockin_output),
    .lockin_valid_i(lockin_valid),
    .freq_correction_o(tracker_freq_correction),
    .locked_o(tracker_locked),
    .lock_lost_o(tracker_lock_lost),
    // ... system bus connections ...
);

// Frequency source selection
// Mode select: scan vs tracking
wire [31:0] frequency_control;
assign frequency_control = tracking_mode ? 
                          tracker_freq_correction : 
                          scan_frequency_output;
```

### Phase 4: Software Control Layer (Python)

```python
class ODMRTracker:
    """
    High-level interface for resonance tracking system
    """
    
    def __init__(self, pyrpl_instance):
        self.pyrpl = pyrpl_instance
        self.tracker_base_addr = 0x40600000  # TBD
        
    def initial_scan_and_lock(self, freq_start, freq_stop, num_steps=100):
        """
        Perform initial scan, find resonance, and enable tracking
        """
        # 1. Perform coarse scan
        scan_result = self.pyrpl.scan_module.perform_scan(
            freq_start=freq_start,
            freq_stop=freq_stop,
            num_steps=num_steps
        )
        
        # 2. Analyze dispersion curve
        resonance_freq = self._find_zero_crossing(scan_result)
        
        if resonance_freq is None:
            raise ValueError("No resonance found in scan range")
        
        # 3. Configure tracker
        self.set_center_frequency(resonance_freq)
        self.set_gains(p_gain=0x00010000, i_gain=0x00001000)
        self.set_lock_threshold(100)
        
        # 4. Enable tracking
        self.enable(True)
        
        # 5. Wait for lock
        timeout = 10  # seconds
        start_time = time.time()
        while not self.is_locked():
            if time.time() - start_time > timeout:
                raise TimeoutError("Failed to achieve lock")
            time.sleep(0.01)
        
        return resonance_freq
    
    def _find_zero_crossing(self, scan_data):
        """
        Fit dispersion curve and find zero-crossing
        """
        # Fit to dispersion function:
        # f(x) = A * (x - x0) / ((x - x0)^2 + Gamma^2)
        from scipy.optimize import curve_fit
        
        def dispersion(x, A, x0, Gamma):
            return A * (x - x0) / ((x - x0)**2 + Gamma**2)
        
        try:
            popt, _ = curve_fit(
                dispersion, 
                scan_data['frequency'], 
                scan_data['signal']
            )
            return popt[1]  # x0 is the resonance frequency
        except:
            return None
    
    def enable(self, enable=True):
        """Enable/disable tracking"""
        self.pyrpl.rw(self.tracker_base_addr + 0x00, 
                     int(enable), 
                     write=True)
    
    def is_locked(self):
        """Check if system is locked"""
        status = self.pyrpl.rw(self.tracker_base_addr + 0x04)
        return bool(status & 0x1)
    
    def lock_lost(self):
        """Check if lock was lost"""
        status = self.pyrpl.rw(self.tracker_base_addr + 0x04)
        return bool(status & 0x2)
    
    def get_tracked_frequency(self):
        """Get current tracked frequency"""
        return self.pyrpl.rw(self.tracker_base_addr + 0x18)
    
    def set_center_frequency(self, freq):
        """Set center frequency"""
        self.pyrpl.rw(self.tracker_base_addr + 0x08, 
                     int(freq), 
                     write=True)
    
    def set_gains(self, p_gain, i_gain):
        """Set PI controller gains (Q16.16 format)"""
        self.pyrpl.rw(self.tracker_base_addr + 0x10, 
                     int(p_gain), 
                     write=True)
        self.pyrpl.rw(self.tracker_base_addr + 0x14, 
                     int(i_gain), 
                     write=True)
    
    def set_lock_threshold(self, threshold):
        """Set lock detection threshold"""
        self.pyrpl.rw(self.tracker_base_addr + 0x0C, 
                     int(threshold), 
                     write=True)
    
    def continuous_tracking(self, duration, callback=None):
        """
        Continuously track resonance for specified duration
        
        Args:
            duration: Time in seconds
            callback: Optional function called with (time, frequency, locked)
        """
        start_time = time.time()
        data = {'time': [], 'frequency': [], 'locked': []}
        
        while time.time() - start_time < duration:
            # Check for lock loss
            if self.lock_lost():
                print("Lock lost! Re-acquiring...")
                self.initial_scan_and_lock(2.7e9, 3.0e9)
            
            # Read current state
            t = time.time() - start_time
            freq = self.get_tracked_frequency()
            locked = self.is_locked()
            
            data['time'].append(t)
            data['frequency'].append(freq)
            data['locked'].append(locked)
            
            if callback:
                callback(t, freq, locked)
            
            time.sleep(0.01)  # 100 Hz update rate
        
        return data
```

### Example Usage

```python
# Initialize pyrpl
from pyrpl import Pyrpl
p = Pyrpl(config='odmr_tracking_test')

# Create tracker interface
tracker = ODMRTracker(p)

# Perform initial scan and lock
resonance_freq = tracker.initial_scan_and_lock(
    freq_start=2.7e9,
    freq_stop=3.0e9,
    num_steps=200
)
print(f"Locked to resonance at {resonance_freq/1e9:.6f} GHz")

# Continuous tracking with live plotting
import matplotlib.pyplot as plt

fig, ax = plt.subplots()
line, = ax.plot([], [])
ax.set_xlabel('Time (s)')
ax.set_ylabel('Resonance Frequency (GHz)')

def update_plot(t, freq, locked):
    line.set_xdata(np.append(line.get_xdata(), t))
    line.set_ydata(np.append(line.get_ydata(), freq/1e9))
    ax.relim()
    ax.autoscale_view()
    plt.pause(0.01)

tracker.continuous_tracking(duration=60, callback=update_plot)
```

## Implementation Considerations

### 1. Bandwidth and Stability

- **Lock-in filter:** CIC decimation by 4096 → ~30 kHz output rate
- **FIR filter:** 500 Hz lowpass → final bandwidth ~500 Hz
- **Tracking loop:** Should be 10-100x slower than filter bandwidth
  - Suggested: 5-50 Hz bandwidth
- **Gain tuning:** Empirical tuning required for stability

### 2. Initial Acquisition

- Always start with coarse scan to avoid false locks
- Verify dispersion feature quality before enabling tracking
- Consider multi-stage acquisition:
  1. Coarse scan (wide range, few points)
  2. Fine scan (narrow range around detected resonance)
  3. Enable tracking

### 3. Lock Loss Handling

- Automatic detection via threshold monitoring
- Automatic re-scan on lock loss
- Hysteresis in lock detection to avoid chattering
- Log all lock loss events with timestamps

### 4. Digital Precision

- **Integrator:** Use 40 bits minimum to prevent overflow
- **Gains:** Q16.16 fixed-point format (16 integer, 16 fractional bits)
- **Frequency:** Match external LO control resolution

### 5. Testing Strategy

1. **Simulation:** Test PI controller in HDL simulator
2. **Static lock:** Lock to fixed resonance, verify stability
3. **Slow drift:** Apply known drift, verify tracking
4. **Step response:** Step frequency, measure lock re-acquisition time
5. **Noise immunity:** Test with varying signal-to-noise ratios

### 6. Performance Metrics

- **Lock acquisition time:** Target < 1 second
- **Tracking bandwidth:** 5-50 Hz
- **Frequency stability:** Limited by lock-in SNR
- **Dynamic range:** Match scan module range

## File Structure

### New Files to Create

```
pyrpl/fpga/rtl/
    odmr_tracker.v              # Main tracking controller
    
pyrpl/hardware_modules/
    odmr_tracker.py             # Python interface class
    
docs/developer_guide/
    resonance_tracking_implementation_plan.md  # This file
    
docs/example-notebooks/
    odmr_tracking_example.ipynb # Tutorial notebook
```

### Files to Modify

```
pyrpl/fpga/rtl/
    red_pitaya_top.v            # Add tracker instantiation
    scan_new.v                  # Add tracking mode support
    
pyrpl/
    modules.py                  # Register new hardware module
```

## Development Phases

### Phase 1: Core Verilog Module (Week 1-2)
- [ ] Create `odmr_tracker.v`
- [ ] Implement PI controller logic
- [ ] Implement lock detection
- [ ] Add system bus interface
- [ ] Write testbench

### Phase 2: Integration (Week 2-3)
- [ ] Modify `red_pitaya_top.v`
- [ ] Update `scan_new.v` for dual-mode
- [ ] Update memory map documentation
- [ ] Synthesize and test FPGA bitstream

### Phase 3: Software Interface (Week 3-4)
- [ ] Create Python `odmr_tracker.py` class
- [ ] Implement control functions
- [ ] Add to module registry
- [ ] Test with hardware

### Phase 4: Testing and Optimization (Week 4-6)
- [ ] Static lock tests
- [ ] Dynamic tracking tests
- [ ] Gain optimization
- [ ] Performance characterization
- [ ] Documentation and examples

## References

1. Clevenson et al. (2018) - "Robust high-dynamic-range vector magnetometry with nitrogen-vacancy centers in diamond"
2. Red Pitaya System Bus Documentation
3. PyRPL Architecture Documentation

## Notes

- This plan assumes lock-in ODMR produces a dispersive signal with zero-crossing at resonance
- PI controller gains will require empirical tuning based on actual system response
- Consider implementing adaptive gain control for varying signal strengths
- May need to implement "search mode" if lock is lost and coarse position unknown

## Next Steps

1. Review this plan with team
2. Allocate FPGA memory addresses for tracker module
3. Set up development/testing environment
4. Begin Phase 1 implementation
5. Schedule regular progress reviews

---

**Document Version:** 1.0  
**Last Updated:** October 13, 2025  
**Author:** AI Assistant (GitHub Copilot)  
**Review Status:** Draft - Awaiting Review
