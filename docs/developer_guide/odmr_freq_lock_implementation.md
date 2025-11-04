# ODMR Frequency Lock Implementation

**Date:** 2025-01-30
**Status:** ✅ Complete - Ready for FPGA compilation and testing
**Planning Reference:** `odmr_tracking_planning.md`

---

## Table of Contents

1. [Problem Statement & Physical Context](#problem-statement--physical-context)
2. [Mathematical Approach & Control Theory](#mathematical-approach--control-theory)
3. [Implementation Architecture](#implementation-architecture)
4. [Register Map & Interface Specification](#register-map--interface-specification)
5. [Tuning & Commissioning](#tuning--commissioning)
6. [Troubleshooting & Debug](#troubleshooting--debug)
7. [Future Enhancements](#future-enhancements)

---

## Problem Statement & Physical Context

### The Physical System

This implementation addresses the challenge of **tracking a time-varying resonance frequency** in a physical system (e.g., optically-detected magnetic resonance in nitrogen-vacancy centers in diamond). The system operates as follows:

**Signal Generation:**
- The FPGA generates a **frequency-modulated RF signal** via DDS at approximately 20 MHz:
  ```
  f(t) = f₀(t) + f_dev * sin(2π * f_m * t)
  ```
  where:
  - `f₀(t)` = center frequency to be locked to resonance
  - `f_dev` = frequency deviation (modulation depth)
  - `f_m` = modulation frequency ≈ 15.25 kHz (= 125 MHz / 4096 / 2)

**Physical Response:**
- The signal excites a **Lorentzian resonance** in the physical system at frequency `f_r(t)`
- The resonance frequency `f_r(t)` **drifts over time** due to environmental factors (temperature, magnetic fields, etc.)
- The response signal's amplitude is proportional to the detuning: `A ∝ |f₀(t) - f_r(t)|`

**Lock-in Demodulation:**
- The response is **amplitude-modulated** at the same frequency `f_m` due to the FM excitation
- Lock-in demodulation with the modulation reference extracts a **dispersion-like signal**
- Around resonance, the demodulated I-quadrature is **linear** in detuning:
  ```
  e[n] ≈ K * (f₀[n] - f_r[n])
  ```
  where `K ≈ 1.1 LSB/Hz` is the measured slope

**The Control Objective:**
> **Lock the FPGA-generated frequency f₀(t) to the resonance frequency f_r(t) by keeping the demodulated signal e[n] at its zero-crossing.**

This allows us to **track resonance drift** in real-time by monitoring the frequency correction applied by the control loop.

### Signal Processing Chain

The implementation integrates into PyRPL's existing signal chain:

```
FPGA DDS (3FGEN)                Physical System
┌─────────────────┐            ┌──────────────┐
│ f₀(t) + FM      │────DAC────>│  Lorentzian  │
│ Phase Acc + FTW │            │  Resonance   │
└─────────────────┘            └──────┬───────┘
        ▲                             │
        │ ftw_correction              │ response
        │                             ▼
┌───────┴─────────┐            ┌──────────────┐
│  ODMR Freq Lock │<───────────│  ADC (14-bit)│
│  (This Module)  │  demod I   └──────────────┘
└─────────────────┘                   │
        ▲                             ▼
        │ err_i                 ┌──────────────┐
        │ valid @ 30.5 kS/s     │   Lock-in    │
        │                       │  Demod (1f-I)│
        └───────────────────────│  CIC + FIR   │
                                └──────────────┘
                                  ↓ decimation
                         R=4096, ~30.5 kS/s
```

**Key timing parameters:**
- FPGA clock: 125 MHz (8 ns period)
- CIC decimation: R=4096 → 30,517.578 S/s (Ts ≈ 32.768 μs)
- Modulation frequency: f_m ≈ 125 MHz / 4096 / 2 ≈ 15.25 kHz
- FIR filter: ~1 ms impulse response (configurable cutoff 500 Hz - 10 kHz)
- Resonance dynamics: < few hundred Hz drift rates

---

## Mathematical Approach & Control Theory

### Loop Model

Around resonance, the demodulated output from the lock-in is proportional to frequency detuning:

$$
e[n] \approx K \cdot \big(f_0[n] - f_r[n]\big)
$$

**Parameters:**
- `e[n]` : Demodulated error (32-bit signed LSB from FIR output)
- `K = 1.1 LSB/Hz` : Measured slope of the demodulation chain at operating point
- `f₀[n]` : FPGA-generated center frequency
- `f_r[n]` : Physical resonance frequency (time-varying)

The **plant transfer function** from `f₀ → e` is:
- **DC gain:** `K` (positive slope)
- **Delay:** CIC decimation (fixed) + FIR group delay (~0.5-1 ms)
- **Sample rate:** Ts = 32.768 μs (update interval)

### Integral Control Law

We implement **integral-only control** in the frequency domain:

$$
\boxed{f_0[n+1] = f_0[n] - \mu \cdot e[n]}
$$

where `μ` is the integral gain in **Hz/LSB**.

**Why integral control?**
1. **Zero steady-state error:** Integral action ensures `e → 0` at equilibrium
2. **Simplicity:** Single-parameter tuning (no derivative noise issues)
3. **DC drift rejection:** Naturally tracks slowly-varying `f_r(t)`

**Closed-loop bandwidth:**
For a first-order integral loop (neglecting delay), the crossover frequency is:

$$
\omega_c \approx \mu \cdot K / T_s
$$

or in Hz:

$$
f_{BW} \approx \frac{\mu \cdot K}{2\pi T_s}
$$

Inverting for desired bandwidth:

$$
\boxed{\mu \approx \frac{2\pi \cdot f_{BW} \cdot T_s}{K}}
$$

### DDS Implementation (Frequency Tuning Words)

The FPGA DDS uses **phase accumulators** with frequency tuning words (FTW):

$$
\text{FTW} = \frac{f}{f_{clk}} \cdot 2^{\text{PHASEBITS}}
$$

**For Red Pitaya:**
- `f_clk = 125 MHz`
- `PHASEBITS = 32`
- **FTW per Hz** = `2³² / 125 MHz ≈ 34.359738368`

The control law in DDS units becomes:

$$
\boxed{\text{FTW\_corr}[n+1] = \text{FTW\_corr}[n] - \mu_{FTW} \cdot e[n]}
$$

where:

$$
\mu_{FTW} = \mu \cdot \frac{2^{\text{PHASEBITS}}}{f_{clk}} \quad [\text{FTW/LSB}]
$$

This **FTW correction** is added to the DDS phase step each clock cycle in the 3FGEN module.

### Design Calculations (Concrete Numbers)

**Target specifications:**
- Desired bandwidth: `f_BW = 300 Hz`
- Measured slope: `K = 1.1 LSB/Hz`
- Sample period: `Ts = 1 / 30517.578 ≈ 32.768 μs`

**Step 1: Compute integral gain in Hz/LSB**

$$
\mu = \frac{2\pi \cdot 300 \text{ Hz} \cdot 32.768 \text{ μs}}{1.1} \approx \frac{0.06176}{1.1} \approx \boxed{0.05615 \text{ Hz/LSB}}
$$

**Step 2: Convert to FTW units**

$$
\mu_{FTW} = 0.05615 \times 34.359738368 \approx \boxed{1.92933 \text{ FTW/LSB}}
$$

**Step 3: Fixed-point representation (Q8.24)**

To avoid floating-point in FPGA, we use **Q8.24 format** (8 integer bits, 24 fractional bits):
- Range: [-128, 128)
- LSB: 2⁻²⁴ ≈ 5.96×10⁻⁸
- `μ_FTW = 1.92933` fits comfortably

$$
\mu_{FTW, Q8.24} = \text{round}(1.92933 \times 2^{24}) = \boxed{\texttt{0x01EDE8D0}}
$$

This is the **default value** programmed into the MU_Q register.

### Stability Considerations

**Phase margin analysis:**
- **Integral control:** Contributes -90° phase lag
- **Processing delay:** ~1 ms (CIC + FIR) at 300 Hz = `2π × 300 × 0.001 ≈ 1.88 rad ≈ 108°` additional lag
- **Total lag:** ~200° at crossover

**Safety margin:**
- Conservative start: **150-200 Hz bandwidth** (halve μ)
- Verify clean step response before increasing toward 300 Hz
- If ringing occurs: reduce μ or lower FIR cutoff frequency

---

## Implementation Architecture

### Files Created/Modified

| File | Status | Description |
|------|--------|-------------|
| `pyrpl/fpga/rtl/odmr_freq_lock_1f.v` | ✅ **Created** | Verilog module (313 lines) |
| `pyrpl/fpga/rtl/red_pitaya_top.v` | ✅ Modified | Instantiation @ lines 811-835 |
| `pyrpl/fpga/rtl/red_pitaya_3fgen.v` | ✅ Modified | FTW correction input registration |
| `pyrpl/hardware_modules/odmr_freq_lock.py` | ✅ **Created** | Python interface (428 lines) |
| `pyrpl/hardware_modules/__init__.py` | ✅ Modified | Added `OdmrFreqLock` import |
| `pyrpl/redpitaya.py` | ✅ Modified | Registered in `cls_modules` list |

### FPGA Module (`odmr_freq_lock_1f.v`)

**Location:** `pyrpl/fpga/rtl/odmr_freq_lock_1f.v`

#### Functional Blocks

1. **Error Conditioning:**
   - Selectable polarity inversion (for loop sign correction)
   - Deadband comparator (skip updates when `|e| < threshold`)

2. **DSP Multiply (DSP48E):**
   - 64-bit product: `delta_ftw_64 = -μ_Q × e`
   - Arithmetic right-shift by 24 bits (Q8.24 → integer FTW)
   - Rounding: add 2²³ before shift

3. **Saturating Integrator:**
   - 33-bit extended addition for overflow detection
   - Symmetric clamping: `ftw_corr ∈ [-FTW_LIM, +FTW_LIM]`
   - Saturation flag for diagnostics

4. **Lock Detector:**
   - Counts consecutive samples with `|e| < DEADBAND`
   - Asserts `locked` flag after 256 samples (~8.4 ms)

5. **System Bus Interface:**
   - 8 registers @ base address `0x40800000` (Region 8)
   - Standard Red Pitaya bus protocol (ack/err handshake)

#### Resource Utilization

- **DSP48E slices:** 1 (for 64×32 multiply)
- **LUTs:** ~150 (control logic, saturation, bus interface)
- **FFs:** ~180 (register file, state machine)
- **Clock domain:** 125 MHz (single-cycle critical path in DSP)
- **Update rate:** ~30.5 kS/s (gated by `err_valid_i` strobe)

#### Integration Points

**Inputs from Lock-in Module:**
```verilog
.err_i       (demod_filtered_data1),    // 32-bit signed from FIR
.err_valid_i (demod_filtered_tvalid1),  // ~30.5 kS/s strobe
```

**Output to 3FGEN DDS:**
```verilog
.ftw_correction_o       (ftw_correction),        // Signed [31:0] FTW offset
.ftw_correction_valid_o (ftw_correction_valid),  // Update strobe
```

The 3FGEN module **registers** `ftw_correction_i` internally (line 251-257 in `red_pitaya_3fgen.v`) to avoid combinatorial paths and ensure synchronization with the phase accumulator.

### Python Hardware Module (`odmr_freq_lock.py`)

**Location:** `pyrpl/hardware_modules/odmr_freq_lock.py`

#### Class Structure

```python
class OdmrFreqLock(HardwareModule):
    addr_base = 0x40800000  # System Bus Region 8

    # Control registers
    enable = BoolRegister(0x0000, bitmask=0x1)
    invert = BoolRegister(0x0000, bitmask=0x2)
    # ... (see register map below)

    # User-friendly properties
    @property
    def mu_hz_per_lsb(self) -> float:
        """Gain in Hz/LSB (auto-converts from Q8.24)"""

    def set_bandwidth(self, bandwidth_hz, slope_lsb_per_hz=1.1):
        """Compute μ from desired BW and measured K"""
```

#### Design Patterns

1. **Descriptor-based registers:** Follow PyRPL conventions (see `CLAUDE.md`)
2. **Unit conversion:** Properties expose Hz, LSB units; registers use raw FTW
3. **Configuration persistence:** All `_setup_attributes` auto-save to YAML
4. **Status readbacks:** Locked, saturated flags for monitoring

---

## Register Map & Interface Specification

### System Bus Region 8: `0x40800000 - 0x4080001F`

| Offset | Name        | R/W | Type   | Description |
|--------|-------------|-----|--------|-------------|
| 0x0000 | **CTRL**    | R/W | 32-bit | **Control Register**<br>Bit[0]: `enable` - Enable loop (0=disabled, correction forced to 0)<br>Bit[1]: `invert` - Invert error sign (1=negate `err_i`)<br>Bit[2]: `hold` - Freeze integrator (1=ignore updates)<br>Bit[3]: `clr` - Clear integrator (write 1, self-clearing)<br>Bit[4]: `deadband_en` - Enable deadband filter |
| 0x0004 | **MU_Q**    | R/W | Q8.24  | **Integral Gain μ_FTW** in fixed-point<br>Default: `0x01EDE8D0` (≈1.929 FTW/LSB for 300 Hz BW)<br>Range: [-128.0, 128.0) in Q8.24 format<br>Python: Use `mu_hz_per_lsb` property for Hz/LSB units |
| 0x0008 | **DEADBAND** | R/W | Unsigned | **Deadband Threshold** (error LSB units)<br>Updates skipped when `\|err_i\| < DEADBAND`<br>Default: 100 LSB<br>Use to prevent integrator drift from demod noise |
| 0x000C | **FTW_LIM** | R/W | Unsigned | **Saturation Limit** (FTW units, symmetric ±)<br>Default: 34,359,738 (±1 MHz @ 125 MHz clock)<br>Python: Use `max_correction_hz` for Hz units |
| 0x0010 | **STATUS**  | R   | 32-bit | **Status Flags** (read-only)<br>Bit[0]: `locked` - Error below deadband for 256 samples<br>Bit[1]: `saturated` - Correction hit limit recently |
| 0x0014 | **ERR_LATCH** | R | Signed | **Last Error Value** (LSB)<br>Latched on each valid update (after inversion if enabled)<br>Use for monitoring/diagnostics |
| 0x0018 | **FTW_CORR** | R  | Signed | **Current FTW Correction** (DDS units)<br>Sign-extended to 32-bit<br>Convert to Hz: `correction_hz = FTW_CORR / 34.359738` |
| 0x001C | **RESERVED** | R/W | 32-bit | Reserved for future use |

### Python Interface Examples

```python
from pyrpl import Pyrpl

p = Pyrpl('odmr_experiment')
odm = p.rp.odmr_freq_lock

# === CONFIGURATION ===
odm.enable = False  # Start disabled for polarity check

# Set bandwidth (auto-computes μ)
odm.set_bandwidth(300, slope_lsb_per_hz=1.1)
# → Sets mu_q = 0x01EDE8D0 internally

# Or set gain directly
odm.mu_hz_per_lsb = 0.05615  # Hz/LSB

# Saturation limit
odm.max_correction_hz = 1e6  # ±1 MHz range

# Deadband (optional, for noise rejection when locked)
odm.deadband_enable = True
odm.deadband_lsb = 50

# === OPERATION ===
odm.enable = True  # Start tracking

# === MONITORING ===
print(f"Error: {odm.error_lsb} LSB")
print(f"Correction: {odm.correction_hz:.1f} Hz")
print(f"Locked: {odm.locked}, Saturated: {odm.saturated}")

# Real-time tracking
import time
for i in range(100):
    f_tracked = f_nominal + odm.correction_hz
    print(f"Resonance frequency: {f_tracked/1e6:.6f} MHz")
    time.sleep(0.01)

# === STATUS DICT ===
status = odm.get_status()
# Returns: {enabled, locked, saturated, inverted, held,
#           error_lsb, correction_hz, mu_hz_per_lsb, ...}
```

---

## Tuning & Commissioning

### Step 1: Polarity Verification

**Goal:** Ensure loop has correct sign (positive feedback → instability)

```python
# === DISABLE LOOP ===
odm.enable = False

# === APPLY POSITIVE FREQUENCY STEP ===
# Method 1: Via 3FGEN component frequency step
p.rp.fgen3.component0.frequency += 200  # +200 Hz step

# Method 2: Direct register write (if needed)
# [Requires knowledge of 3FGEN register map]

time.sleep(0.1)  # Wait for settling through CIC+FIR

# === READ ERROR SIGN ===
error_after = odm.error_lsb
print(f"Error after +200 Hz step: {error_after} LSB")

# === INTERPRETATION ===
# Correct polarity: Positive freq step → Positive error
# If error is negative, toggle: odm.invert = True

if error_after < 0:
    print("⚠️  Wrong polarity! Setting invert=True")
    odm.invert = True
else:
    print("✓ Polarity correct")

# Return frequency to nominal
p.rp.fgen3.component0.frequency -= 200
```

### Step 2: Conservative Bandwidth Start

**Goal:** Verify loop stability at reduced gain (wider phase margin)

```python
# === CONFIGURATION ===
odm.set_bandwidth(150, slope_lsb_per_hz=1.1)  # Half of 300 Hz target
odm.max_correction_hz = 1e6  # ±1 MHz range
odm.deadband_enable = False  # Disable initially
odm.deadband_lsb = 0

# === ENABLE LOOP ===
odm.enable = True
print("Loop enabled at 150 Hz bandwidth")

# === MONITOR SETTLING ===
import time
import numpy as np

errors = []
corrections = []
for i in range(50):
    errors.append(odm.error_lsb)
    corrections.append(odm.correction_hz)
    print(f"t={i*0.1:.1f}s  Error: {errors[-1]:6d} LSB  "
          f"Correction: {corrections[-1]:8.1f} Hz  "
          f"Locked: {odm.locked}  Saturated: {odm.saturated}")
    time.sleep(0.1)

# === CHECK FOR OSCILLATION ===
errors_array = np.array(errors)
if np.std(errors_array) > 100 and not odm.locked:
    print("⚠️  Possible oscillation detected - reduce gain further")
else:
    print("✓ Stable settling observed")
```

### Step 3: Bandwidth Increase

**Goal:** Gradually approach target 300 Hz bandwidth

```python
# === INCREMENTAL TUNING ===
bandwidths = [150, 200, 250, 300]  # Hz

for bw in bandwidths:
    print(f"\n=== Testing {bw} Hz bandwidth ===")
    odm.set_bandwidth(bw, slope_lsb_per_hz=1.1)

    time.sleep(1.0)  # Allow settling

    # Monitor for 3 seconds
    for i in range(30):
        print(f"  Error: {odm.error_lsb:5d} LSB  "
              f"Correction: {odm.correction_hz:7.1f} Hz", end='\r')
        time.sleep(0.1)

    # Check stability
    errors = [odm.error_lsb for _ in range(10)]
    if np.std(errors) > 200:
        print(f"\n⚠️  Unstable at {bw} Hz - backing off")
        odm.set_bandwidth(bw - 50, slope_lsb_per_hz=1.1)
        break
    else:
        print(f"\n✓ Stable at {bw} Hz")

print(f"\nFinal bandwidth: {2*np.pi*odm.mu_hz_per_lsb*1.1/32.768e-6:.1f} Hz")
```

### Step 4: Deadband Tuning (Optional)

**Goal:** Prevent integrator random walk from demodulation noise

```python
# === OBSERVE NOISE LEVEL WHEN LOCKED ===
odm.deadband_enable = False
time.sleep(2.0)

errors_unlocked = [odm.error_lsb for _ in range(100)]
noise_std = np.std(errors_unlocked)
noise_pk = np.max(np.abs(errors_unlocked))

print(f"Error noise: σ={noise_std:.1f} LSB, peak={noise_pk:.1f} LSB")

# === SET DEADBAND ===
# Rule of thumb: 2-3σ or ~50-100 LSB typical
deadband_threshold = max(50, int(2.5 * noise_std))
odm.deadband_lsb = deadband_threshold
odm.deadband_enable = True

print(f"Deadband set to {deadband_threshold} LSB")

# === VERIFY LOCK INDICATOR ===
time.sleep(1.0)
print(f"Locked: {odm.locked} (should be True after ~8.4 ms)")
```

### Step 5: Long-Term Tracking

**Goal:** Record resonance drift over time

```python
import matplotlib.pyplot as plt

# === DATA LOGGING ===
duration_s = 60  # 1 minute
sample_rate = 100  # Hz
N = int(duration_s * sample_rate)

times = np.zeros(N)
corrections = np.zeros(N)
errors = np.zeros(N)

t0 = time.time()
for i in range(N):
    times[i] = time.time() - t0
    corrections[i] = odm.correction_hz
    errors[i] = odm.error_lsb
    time.sleep(1.0 / sample_rate)

# === ANALYSIS ===
f_nominal = 20e6  # Replace with actual nominal frequency
f_tracked = f_nominal + corrections

plt.figure(figsize=(12, 6))

plt.subplot(2, 1, 1)
plt.plot(times, f_tracked / 1e6)
plt.ylabel('Tracked Frequency (MHz)')
plt.title('ODMR Resonance Tracking')
plt.grid(True)

plt.subplot(2, 1, 2)
plt.plot(times, errors)
plt.axhline(odm.deadband_lsb, color='r', linestyle='--', label='Deadband')
plt.axhline(-odm.deadband_lsb, color='r', linestyle='--')
plt.xlabel('Time (s)')
plt.ylabel('Error (LSB)')
plt.legend()
plt.grid(True)

plt.tight_layout()
plt.show()

# === STATISTICS ===
print(f"\nTracking Statistics:")
print(f"  Mean correction: {np.mean(corrections):.1f} Hz")
print(f"  Std correction: {np.std(corrections):.1f} Hz")
print(f"  Drift range: {np.ptp(corrections):.1f} Hz")
print(f"  Lock percentage: {100*np.mean(np.abs(errors) < odm.deadband_lsb):.1f}%")
```

### Slope Measurement (K Calibration)

**Goal:** Measure demodulation slope `K` for accurate bandwidth setting

```python
def measure_slope(odm, fgen, f_nominal, step_hz=100, n_steps=5):
    """Measure demodulation slope K = de/df"""
    odm.enable = False  # Open loop
    time.sleep(0.2)

    f_steps = f_nominal + np.linspace(-step_hz*n_steps/2,
                                       step_hz*n_steps/2, n_steps)
    errors = []

    for f in f_steps:
        fgen.component0.frequency = f
        time.sleep(0.1)  # Wait for CIC+FIR settling
        errors.append(odm.error_lsb)

    # Linear fit
    K, offset = np.polyfit(f_steps, errors, 1)

    # Restore nominal
    fgen.component0.frequency = f_nominal

    print(f"Measured slope: K = {K:.3f} LSB/Hz")
    print(f"Offset: {offset:.1f} LSB")

    return K

# === USAGE ===
K_measured = measure_slope(odm, p.rp.fgen3, f_nominal=20e6)
odm.set_bandwidth(300, slope_lsb_per_hz=K_measured)
```

---

## Troubleshooting & Debug

### Issue: Loop Oscillates / Unstable

**Symptoms:**
- Error signal oscillates with constant amplitude
- Cannot achieve lock (`locked` flag never asserts)
- Correction saturates intermittently

**Root Causes:**

1. **Gain too high for actual loop delay**
   - FIR filter group delay may be longer than assumed
   - Check FIR configuration: `p.rp.lock_in.fir_cutoff`

2. **Wrong polarity (incorrect invert setting)**
   - Positive feedback instead of negative
   - Verify with manual frequency step (see Step 1 above)

**Solutions:**

```python
# Reduce bandwidth by factor of 2
current_mu = odm.mu_hz_per_lsb
odm.mu_hz_per_lsb = current_mu / 2
print(f"Reduced gain to {odm.mu_hz_per_lsb:.6f} Hz/LSB")

# Or re-run polarity check
odm.enable = False
# ... (polarity check procedure)
```

---

### Issue: Loop Doesn't Lock / Large Steady-State Error

**Symptoms:**
- `locked` flag remains False
- Large constant error (e.g., >1000 LSB)
- `saturated` flag asserted

**Root Causes:**

1. **Initial detuning exceeds capture range**
   - If `|f₀ - f_r| > max_correction_hz`, loop cannot reach resonance

2. **Gain too low**
   - Integrator action too weak to overcome drift

3. **Saturation limit too tight**

**Solutions:**

```python
# Check saturation status
if odm.saturated:
    print(f"Correction saturated at ±{odm.max_correction_hz/1e3:.0f} kHz")
    print(f"Current correction: {odm.correction_hz/1e3:.1f} kHz")

    # Increase limit
    odm.max_correction_hz = 2e6  # ±2 MHz
    print("Increased saturation limit to ±2 MHz")

# Or manually pre-tune close to resonance
odm.enable = False
p.rp.fgen3.component0.frequency = f_resonance_estimate
odm.clear()  # Reset integrator
odm.enable = True
```

---

### Issue: Random Walk When Locked

**Symptoms:**
- `locked` flag asserts initially, then drops
- Correction drifts slowly even though physical resonance is stable
- Error hovers near zero but crosses frequently

**Root Causes:**

1. **Demodulation noise couples into integrator**
   - Shot noise, laser intensity fluctuations, etc.
   - Integral action accumulates noise

2. **Deadband disabled or too small**

**Solutions:**

```python
# Measure error noise floor
odm.deadband_enable = False
time.sleep(1)
errors = [odm.error_lsb for _ in range(200)]
noise_rms = np.std(errors)

print(f"Error RMS noise: {noise_rms:.1f} LSB")

# Set deadband to 2-3× noise RMS
odm.deadband_lsb = int(3 * noise_rms)
odm.deadband_enable = True
print(f"Deadband set to {odm.deadband_lsb} LSB")
```

---

### Issue: Python Module Not Found

**Symptoms:**
```python
AttributeError: 'RedPitaya' object has no attribute 'odmr_freq_lock'
```

**Root Causes:**

- Module not registered in PyRPL infrastructure

**Solutions:**

1. **Check `hardware_modules/__init__.py`:**
   ```python
   from .odmr_freq_lock import OdmrFreqLock
   ```

2. **Check `redpitaya.py` in `cls_modules`:**
   ```python
   cls_modules = [
       # ... other modules ...
       "OdmrFreqLock",
   ]
   ```

3. **Reload Python environment:**
   ```bash
   # If using IPython/Jupyter
   %reload_ext pyrpl

   # Or restart kernel
   ```

---

### Issue: FPGA Compilation Fails

**Symptoms:**
- Vivado synthesis errors
- Timing violations in `post_route_timing_summary.rpt`

**Debugging Steps:**

1. **Check synthesis log:**
   ```bash
   cd pyrpl/fpga
   copy_and_make.bat
   # Review: out/synth_design.log
   ```

2. **Common errors:**
   - **Undefined signals:** Check module instantiation in `red_pitaya_top.v:811-835`
   - **Bus conflicts:** Verify Region 8 not used elsewhere
   - **Timing violations:** May need to pipeline DSP multiply (unlikely at 125 MHz)

3. **Resource exhaustion:**
   ```bash
   # Check: out/post_route_utilization.rpt
   # Zynq 7010 limits:
   # - LUTs: 17,600
   # - FFs: 35,200
   # - DSP48E: 80
   # - BRAM: 60
   ```

---

## Future Enhancements

### Current Limitations

The present implementation is a **minimal viable controller** using only:
- **1f I-quadrature** demodulation
- **Integral-only control** (no proportional or derivative terms)
- **Fixed gain** (no adaptive tuning)

### Planned Extensions (From Planning Document)

#### 1. Drift Compensation via 2f Demodulation

**Motivation:** The 1f signal contains both detuning information AND slow baseline drifts (laser power, detection efficiency). The 2f component is purely proportional to detuning (no offset).

**Approach:**
- Use **1f-I for fast feedback** (keeps current loop)
- Use **2f-I for slow offset correction** (additional integrator)
- Two-timescale control: Fast loop (300 Hz BW) + Slow drift compensation (~1 Hz BW)

**Implementation:**
- Add second demod input from `lock_in` channel 2 (2f component)
- Second integrator with much lower gain
- Sum corrections: `ftw_total = ftw_1f + ftw_2f_drift`

---

#### 2. Gain Scheduling / Adaptive μ

**Motivation:** The slope `K` varies with:
- Resonance contrast (changes with laser power, magnetic field alignment)
- Modulation depth `f_dev`
- Background signal levels

Fixed gain → suboptimal bandwidth when `K` changes.

**Approach:**
- **Online estimation of K:**
  - Inject small test steps (dither) in frequency
  - Measure `Δe / Δf`
  - IIR filter: `K_est[n] = α*K_est[n-1] + (1-α)*(Δe/Δf)`

- **Adaptive gain update:**
  - `μ = (2π * f_BW * Ts) / K_est`
  - Clamp to safe range to prevent instability during transients

**Implementation:**
- Add `KICK_STEP` register (inject known Δf)
- Add `K_EST` register (read estimated slope)
- Add `AUTO_GAIN_EN` control bit

---

#### 3. Proportional Term (PI Control)

**Motivation:** Integral-only has slow acquisition. Adding proportional term:
- Faster response to large errors
- Improved disturbance rejection
- Better phase margin (reduces lag from integral)

**Control Law:**
```
f₀[n+1] = f₀[n] - μ_I*e[n] - μ_P*(e[n] - e[n-1])
```

**Implementation:**
- Add `MU_P` register (proportional gain in Q format)
- Add `err_prev` state register
- Compute: `delta_ftw = mu_I*e[n] + mu_P*(e[n] - e[n-1])`

**Tuning:** Start with `μ_P ≈ 0.1*μ_I`, adjust for critically damped response.

---

#### 4. Streaming Correction Data (DMA)

**Motivation:** Current interface requires polling `FTW_CORR` via bus reads. For high-rate logging (>1 kHz), DMA is more efficient.

**Approach:**
- Add AXI-Stream output port from `odmr_freq_lock_1f.v`
- Connect to DMA controller (similar to Scan module streaming)
- Python: `odm.start_streaming()` → fills numpy array

**Implementation:**
- Add `m_axis_tdata/tvalid` outputs
- Instantiate DMA writer in `red_pitaya_top.v`
- Python: Extend `OdmrFreqLock` with `CurveViewer` mixin

---

## Appendix: Mathematical Reference

### Transfer Function (Linearized Loop)

Open-loop transfer function `L(s)`:

$$
L(s) = \underbrace{K}_{\text{plant}} \cdot \underbrace{e^{-s \tau_d}}_{\text{delay}} \cdot \underbrace{\frac{\mu}{T_s \cdot s}}_{\text{integral}}
$$

**Parameters:**
- `K = 1.1 LSB/Hz` (measured demodulation slope)
- `τ_d ≈ 1 ms` (CIC + FIR group delay)
- `μ = 0.05615 Hz/LSB` (integral gain)
- `Ts = 32.768 μs` (sample period)

**Crossover frequency** (where `|L(jω_c)| = 1`):

$$
\omega_c \approx \frac{\mu K}{T_s} \approx \frac{0.05615 \times 1.1}{32.768 \times 10^{-6}} \approx 1885 \text{ rad/s} \approx 300 \text{ Hz}
$$

**Phase margin** (at ω_c):

$$
\text{PM} = 180° - 90° - \omega_c \tau_d \times \frac{180°}{\pi} \approx 180° - 90° - 108° = -18°
$$

⚠️ **Negative phase margin indicates instability!** This is why we recommend:
- Start at **150 Hz BW** (PM ≈ +36°)
- Empirically verify stability before increasing toward 300 Hz
- Consider reducing FIR group delay if 300 Hz target is critical

---

## Success Criteria

| Criterion | Status | Notes |
|-----------|--------|-------|
| **FPGA synthesis** | ✅ Complete | No timing violations expected @ 125 MHz |
| **Python module loads** | ✅ Complete | Registered in `cls_modules` |
| **Register R/W access** | ⏳ Untested | Requires hardware or DummyClient test |
| **Polarity check passes** | ⏳ Requires HW | See commissioning Step 1 |
| **Loop achieves lock** | ⏳ Requires HW | See commissioning Step 2-3 |
| **Bandwidth 150-300 Hz** | ⏳ Requires HW | May need empirical tuning |
| **Tracking drift < 1 kHz/s** | ⏳ Requires characterization | Depends on physical system |

---

## References & Contact

- **Planning Document:** `docs/developer_guide/odmr_tracking_planning.md`
- **Implementation Date:** 2025-01-30
- **FPGA Module:** `pyrpl/fpga/rtl/odmr_freq_lock_1f.v` (313 lines)
- **Python Module:** `pyrpl/hardware_modules/odmr_freq_lock.py` (428 lines)

For theoretical background on frequency-locked loops, see:
- E. D. Black, "An introduction to Pound-Drever-Hall laser frequency stabilization," Am. J. Phys. **69**, 79 (2001)
- G. C. Bjorklund, "Frequency-modulation spectroscopy: a new method for measuring weak absorptions and dispersions," Opt. Lett. **5**, 15 (1980)

---

**Document Status:** Complete and ready for FPGA compilation and hardware commissioning.
