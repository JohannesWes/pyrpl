# Example Hardware Module - Usage Guide

This document explains how to use the `example_module.py` as a template for creating custom FPGA hardware modules using the newly expanded address space (modules 8-15).

## Scope of This Template

**This template is for creating standalone HardwareModule instances** that occupy system bus address regions 8-15. These are modules that:
- Connect directly to the system bus (not through the DSP signal routing matrix)
- Have their own dedicated address space (1MB per module)
- Do NOT have built-in `input`/`output_direct` signal routing
- Examples: Scope, Scan, HK, AMS, Fgen3

**This template is NOT for creating DSP modules** (like PID, IQ, IIR) which have signal routing capabilities. DSP modules use a different architecture and address space (0x40300000 + dsp_num * 0x10000).

## Address Space Allocation

After extending the system bus from 8 to 16 modules, these address regions are now available:

| Module | Base Address | Status | Notes |
|--------|--------------|--------|-------|
| 8 | 0x40800000 | **Example Module** | Used by this template |
| 9 | 0x40900000 | Available | Ready for custom modules |
| 10 | 0x40A00000 | Available | Ready for custom modules |
| 11 | 0x40B00000 | Available | Ready for custom modules |
| 12 | 0x40C00000 | Available | Ready for custom modules |
| 13 | 0x40D00000 | Available | Ready for custom modules |
| 14 | 0x40E00000 | Available | Ready for custom modules |
| 15 | 0x40F00000 | Available | Ready for custom modules |


## Verilog Template

A matching Verilog template is provided at:
- **Template**: `pyrpl/fpga/rtl/example_hardware_module.v`
- **Verilog Guide**: `pyrpl/fpga/rtl/VERILOG_MODULE_README.md`

The Verilog template shows the FPGA implementation side with **matching register addresses**. See the Verilog guide for detailed information on:
- System bus interface implementation
- Register type mapping (Python types → Verilog)
- Timing considerations and best practices
- Simulation and testing

## Creating a Working Custom Module

To create a fully functional custom module, follow these steps:

### Step 1: Copy and Modify Python Template

```bash
# Copy the example module
cp pyrpl/hardware_modules/example_module.py pyrpl/hardware_modules/my_module.py
```

Edit `my_module.py`:
```python
class MyModule(HardwareModule):
    """Your module description"""

    # Choose an available address slot (8-15)
    addr_base = 0x40900000  # Module 9

    # Define your registers (must match Verilog addresses)
    enable = BoolRegister(0x00, doc="Enable module")
    data = IntRegister(0x04, bits=32, doc="Data register")
    # ... add your registers
```

### Step 2: Create Verilog Implementation

**Start with the template**: Copy `pyrpl/fpga/rtl/example_hardware_module.v` to `pyrpl/fpga/rtl/my_module.v`

```bash
# Copy the Verilog template
cp pyrpl/fpga/rtl/example_hardware_module.v pyrpl/fpga/rtl/my_module.v
```

Then modify it for your needs:
- Update module name
- Change register addresses to match your Python module
- Implement your custom logic
- Remove unused example code

**Critical**: Ensure register addresses in Verilog **exactly match** Python `IntRegister(addr, ...)` offsets!

See `pyrpl/fpga/rtl/VERILOG_MODULE_README.md` for detailed guidance on:
- Register type mapping (Bool, Int, Float, Select, etc.)
- System bus protocol
- Timing requirements
- Common patterns and pitfalls

### Step 3: Instantiate in Top Module

Edit `pyrpl/fpga/rtl/red_pitaya_top.v` around line 770:

```verilog
// Remove unused assignment for your chosen module (e.g., module 9)
// DELETE THESE LINES:
// assign sys_rdata[ 9*32+:32] = 32'h0;
// assign sys_err  [ 9       ] =  1'b0;
// assign sys_ack  [ 9       ] =  1'b1;

// Add your module instantiation:
my_module i_my_module (
    .clk_i      (adc_clk),
    .rstn_i     (adc_rstn),
    .sys_addr   (sys_addr),
    .sys_wdata  (sys_wdata),
    .sys_sel    (sys_sel),
    .sys_wen    (sys_wen[9]),      // Module 9
    .sys_ren    (sys_ren[9]),
    .sys_rdata  (sys_rdata[9*32+31 : 9*32]),
    .sys_err    (sys_err[9]),
    .sys_ack    (sys_ack[9])
);
```

### Step 4: Add to Build System

Edit `pyrpl/fpga/red_pitaya_vivado.tcl` around line 79-80 (after the scan_new.v line):

```tcl
# Add your Verilog source file
add_files rtl/my_module.v
```

### Step 5: Register in Python

**5a.** Add import to `pyrpl/hardware_modules/__init__.py` (at the end of imports, around line 38):

```python
from .my_module import MyModule
```

**5b.** Register in module list in `pyrpl/redpitaya.py` (line 67-68):

```python
class RedPitaya(object):
    cls_modules = [rp.HK, rp.AMS, rp.Scope, rp.Scan, rp.Sampler, rp.Asg0, rp.Asg1, rp.Fgen3] + \
                  [rp.Pwm] * 2 + [rp.Iq] * 3  + [rp.Trig] + [rp.IIR] + [rp.MyModule]
    #                                                                   ^^^^^^^^^^^^^^ Add this
```

**Note:** Use `[rp.MyModule]` for a single instance, or `[rp.MyModule] * 2` for multiple instances (like PWM).

## Common Pitfalls

### 1. **Address Mismatch**
**Problem**: Register reads return wrong values
```python
# Python:
my_register = IntRegister(0x04)  # Offset 0x04

# Verilog:
ADDR_MY_REG = 16'h0008;  // WRONG - should be 16'h0004!
```

### 2. **Bit Width Mismatch**
**Problem**: Data corruption or overflow
```python
# Python:
value = IntRegister(0x00, bits=16)  # Expects 16-bit value

# Verilog:
reg [31:0] value_reg;  // WRONG - should be [15:0]
```

### 3. **Signed vs Unsigned**
```python
# Python:
gain = FloatRegister(0x04, bits=14, norm=2**13)  # Expects signed

# Verilog:
reg [13:0] gain_reg;  // OK - but treat as 2's complement!
```

### 4. **Forgetting sys_ack**
```verilog
// FPGA must acknowledge ALL bus transactions
always @(posedge clk_i) begin
    sys_ack <= sys_wen | sys_ren;  // Critical!
end
```

### 5. **Module Number Mismatch**
```python
# Python:
addr_base = 0x40900000  # Module 9

# red_pitaya_top.v:
.sys_wen    (sys_wen[8]),  // WRONG - should be sys_wen[9]!
```

## Register Type Reference

### IntRegister
- Direct integer values
- Specify `bits` for width
- Use `min`/`max` for validation

```python
counter = IntRegister(0x00, bits=32, doc="32-bit counter")
threshold = IntRegister(0x04, bits=16, min=0, max=1000)
```

### FloatRegister
- Fixed-point representation
- `norm` converts between float and FPGA integer
- Typical: `bits=14, norm=2**13` for ±1.0 range

```python
gain = FloatRegister(0x00, bits=14, norm=2**13)
# Python: gain = 0.5
# FPGA: receives int(0.5 * 8192) = 4096
```

### BoolRegister
- Single bit (stored in bit 0 of 32-bit word)

```python
enable = BoolRegister(0x00, doc="Enable flag")
```

### LongRegister
- 64-bit integer values (uses two consecutive 32-bit registers)
- Common for timestamps, large counters, or accumulated values

```python
timestamp = LongRegister(0x00, bits=64, doc="64-bit timestamp counter")
# In Verilog: occupies addresses 0x00 (lower 32 bits) and 0x04 (upper 32 bits)
```

### SelectRegister
- Enumerated options
- Maps strings to integers

```python
mode = SelectRegister(0x00,
                      options=['off', 'continuous', 'triggered'],
                      doc="Operating mode")
# mode = 'continuous'  ->  writes 1 to FPGA
```

### FrequencyRegister
- Converts Hz to FPGA clock cycles
- Accounts for frequency_correction
- Uses 32-bit phase accumulator

```python
frequency = FrequencyRegister(0x00, bits=32)
# Python: frequency = 10e6  # 10 MHz
# FPGA: receives phase increment value
```

## Further Reading

- **CLAUDE.md**: Architecture overview
- **pyrpl/modules.py**: HardwareModule base class
- **pyrpl/attributes.py**: All register descriptor types
- **pyrpl/fpga/rtl/**: Existing Verilog modules as examples
- **Red Pitaya Docs**: https://redpitaya.readthedocs.io/
