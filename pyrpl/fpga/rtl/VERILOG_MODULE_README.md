# Verilog Hardware Module Template Guide

This guide explains how to create custom FPGA modules in Verilog that interface with PyRPL's Python framework.

## Overview

PyRPL hardware modules consist of **two synchronized parts**:

1. **Python side** (`pyrpl/hardware_modules/your_module.py`): Register definitions, Python API
2. **Verilog side** (`pyrpl/fpga/rtl/your_module.v`): FPGA implementation, actual hardware

**Critical**: The register address maps in Python and Verilog **must match exactly!**

## Template Files

- **Verilog Template**: `example_hardware_module.v` (this directory)
- **Python Template**: `pyrpl/hardware_modules/example_module.py`
- **Python Guide**: `pyrpl/hardware_modules/EXAMPLE_MODULE_README.md`

### Register Map Correspondence

The template files demonstrate a complete matching register map:

| Address | Python (example_module.py) | Verilog (example_hardware_module.v) | Type |
|---------|---------------------------|-------------------------------------|------|
| 0x0000 | `enable` | `ADDR_ENABLE` | BoolRegister |
| 0x0004 | `gain` | `ADDR_GAIN` | FloatRegister (14-bit signed) |
| 0x0008 | `threshold` | `ADDR_THRESHOLD` | IntRegister (16-bit) |
| 0x000C | `mode` | `ADDR_MODE` | SelectRegister (4 options) |
| 0x0010 | `frequency` | `ADDR_FREQUENCY` | FrequencyRegister (32-bit) |
| 0x0020 | `counter` | `ADDR_COUNTER` | IntRegister (32-bit, read-only) |
| 0x0024 | `overflow` | `ADDR_OVERFLOW` | BoolRegister (read-only) |
| 0x0030 | `reset_counter` | `ADDR_RESET` | BoolRegister (action/pulse) |
| 0x0040 | `_config` | `ADDR_CONFIG` | IntRegister (32-bit) |

**This exact correspondence is required** for your custom modules!

## System Bus Interface

### Signal Description

All PyRPL modules connect to the system bus with this standard interface:

```verilog
module your_module (
    // Clock and reset
    input  wire        clk_i,          // 125 MHz system clock
    input  wire        rstn_i,         // Active-low reset

    // System bus (AXI-lite style)
    input  wire [31:0] sys_addr,       // Address bus
    input  wire [31:0] sys_wdata,      // Write data (32-bit)
    input  wire [ 3:0] sys_sel,        // Byte select (usually not used)
    input  wire        sys_wen,        // Write enable (active high)
    input  wire        sys_ren,        // Read enable (active high)
    output reg  [31:0] sys_rdata,      // Read data (32-bit)
    output reg         sys_err,        // Error flag (usually 0)
    output reg         sys_ack         // Acknowledge (must respond!)
);
```

### Address Space

Each module gets **1MB** (0x100000) of address space:
- Module 8: 0x40800000 - 0x408FFFFF
- Module 9: 0x40900000 - 0x409FFFFF
- etc.

**In your Verilog code**, only use the lower 20 bits of `sys_addr`:
```verilog
if (sys_addr[19:0] == 20'h0004)  // Use [19:0] for register offsets (1MB address space)
```

The Python `addr_base` (e.g., 0x40800000) is handled by the top-level module.

### Standard Bus Protocol

#### Write Transaction

```verilog
always @(posedge clk_i) begin
    if (rstn_i == 1'b0) begin
        my_register <= 32'd0;
    end
    else if (sys_wen) begin
        case (sys_addr[19:0])
            20'h0000: my_register <= sys_wdata;
            // ... more registers
        endcase
    end
end
```

#### Read Transaction

```verilog
wire sys_en;
assign sys_en = sys_wen | sys_ren;

always @(posedge clk_i) begin
    if (rstn_i == 1'b0) begin
        sys_err   <= 1'b0;
        sys_ack   <= 1'b0;
        sys_rdata <= 32'h0;
    end
    else begin
        sys_err <= 1'b0;
        sys_ack <= sys_en;  // Acknowledge ALL transactions!

        case (sys_addr[19:0])
            20'h0000: sys_rdata <= my_register;
            20'h0004: sys_rdata <= status_register;
            default:  sys_rdata <= 32'h0;  // Unmapped addresses return 0
        endcase
    end
end
```

**Critical**: Always set `sys_ack <= sys_en` to acknowledge both reads and writes!

## Python-to-Verilog Register Mapping

### BoolRegister (1 bit)

**Python:**
```python
enable = BoolRegister(0x00, doc="Enable module")
```

**Verilog:**
```verilog
localparam ADDR_ENABLE = 20'h00000;
reg enable_reg;  // 1-bit register

// Write
if (sys_addr[19:0] == ADDR_ENABLE && sys_wen)
    enable_reg <= sys_wdata[0];

// Read
ADDR_ENABLE: sys_rdata <= {31'b0, enable_reg};  // Zero-extend to 32 bits
```

### IntRegister (unsigned integer)

**Python:**
```python
threshold = IntRegister(0x04, bits=16, min=0, max=65535)
```

**Verilog:**
```verilog
localparam ADDR_THRESHOLD = 20'h00004;
reg [15:0] threshold_reg;  // 16-bit unsigned

// Write
if (sys_addr[19:0] == ADDR_THRESHOLD && sys_wen)
    threshold_reg <= sys_wdata[15:0];

// Read
ADDR_THRESHOLD: sys_rdata <= {16'b0, threshold_reg};  // Zero-extend upper bits
```

### FloatRegister (fixed-point representation)

**Python:**
```python
gain = FloatRegister(0x08, bits=14, norm=2**13)
# Python: gain = 0.5 → FPGA receives: int(0.5 * 8192) = 4096
```

**Verilog:**
```verilog
localparam ADDR_GAIN = 20'h00008;
reg signed [13:0] gain_reg;  // 14-bit SIGNED (2's complement)

// Write
if (sys_addr[19:0] == ADDR_GAIN && sys_wen)
    gain_reg <= sys_wdata[13:0];

// Read (sign-extend to 32 bits)
ADDR_GAIN: sys_rdata <= {{18{gain_reg[13]}}, gain_reg};

// Usage in arithmetic (already in 2's complement)
wire signed [27:0] scaled = input_signal * gain_reg;  // 14-bit * 14-bit
```

**Key**: Python handles float ↔ integer conversion using `norm`. FPGA always works with integers.

### SelectRegister (enumerated options)

**Python:**
```python
mode = SelectRegister(0x0C,
                      options=['off', 'continuous', 'triggered', 'gated'])
# Python: mode = 'continuous' → FPGA receives: 1
```

**Verilog:**
```verilog
localparam ADDR_MODE = 20'h0000C;
reg [1:0] mode_reg;  // 2 bits = 4 options (0-3)

// Define mode values as localparam for readability
localparam MODE_OFF        = 2'd0;
localparam MODE_CONTINUOUS = 2'd1;
localparam MODE_TRIGGERED  = 2'd2;
localparam MODE_GATED      = 2'd3;

// Write
if (sys_addr[19:0] == ADDR_MODE && sys_wen)
    mode_reg <= sys_wdata[1:0];

// Read
ADDR_MODE: sys_rdata <= {30'b0, mode_reg};

// Usage
if (mode_reg == MODE_CONTINUOUS) begin
    // Do something in continuous mode
end
```

### FrequencyRegister (phase accumulator value)

**Python:**
```python
frequency = FrequencyRegister(0x10, bits=32)
# Python: frequency = 10e6 (10 MHz) → FPGA receives phase increment value
```

**Verilog:**
```verilog
localparam ADDR_FREQUENCY = 20'h00010;
reg [31:0] freq_reg;  // 32-bit phase increment

// Write
if (sys_addr[19:0] == ADDR_FREQUENCY && sys_wen)
    freq_reg <= sys_wdata;

// Read
ADDR_FREQUENCY: sys_rdata <= freq_reg;

// Usage: Direct Digital Synthesis (DDS)
reg [31:0] phase_accumulator;
always @(posedge clk_i) begin
    if (rstn_i == 1'b0)
        phase_accumulator <= 32'd0;
    else
        phase_accumulator <= phase_accumulator + freq_reg;
end
```

### LongRegister (64-bit value)

**Python:**
```python
timestamp = LongRegister(0x20, bits=64, doc="64-bit timestamp")
```

**Verilog:**
```verilog
localparam ADDR_TIMESTAMP_LO = 20'h00020;  // Lower 32 bits
localparam ADDR_TIMESTAMP_HI = 20'h00024;  // Upper 32 bits (offset +4)

reg [63:0] timestamp_reg;

// Write (usually read-only for timestamps, but shown for completeness)
case (sys_addr[19:0])
    ADDR_TIMESTAMP_LO: timestamp_reg[31:0]  <= sys_wdata;
    ADDR_TIMESTAMP_HI: timestamp_reg[63:32] <= sys_wdata;
endcase

// Read
ADDR_TIMESTAMP_LO: sys_rdata <= timestamp_reg[31:0];
ADDR_TIMESTAMP_HI: sys_rdata <= timestamp_reg[63:32];

// Usage: Free-running counter
always @(posedge clk_i) begin
    if (rstn_i == 1'b0)
        timestamp_reg <= 64'd0;
    else
        timestamp_reg <= timestamp_reg + 1'b1;
end
```

**Important**: Python reads both addresses and combines them into a single 64-bit value.

### Action Registers (write-only, pulse behavior)

**Python:**
```python
reset_counter = BoolRegister(0x30, doc="Write 1 to reset counter")
```

**Verilog:**
```verilog
localparam ADDR_RESET = 20'h00030;
reg reset_pulse;

// Write: Capture write, auto-clear after 1 cycle
always @(posedge clk_i) begin
    if (rstn_i == 1'b0)
        reset_pulse <= 1'b0;
    else begin
        reset_pulse <= 1'b0;  // Default: clear
        if (sys_addr[19:0] == ADDR_RESET && sys_wen)
            reset_pulse <= sys_wdata[0];  // Set for 1 cycle
    end
end

// Read (return 0)
ADDR_RESET: sys_rdata <= 32'h0;

// Usage: Use pulse to trigger action
always @(posedge clk_i) begin
    if (reset_pulse)
        counter <= 32'd0;
    else
        counter <= counter + 1;
end
```

## Common Patterns

### Reset Handling

```verilog
always @(posedge clk_i) begin
    if (rstn_i == 1'b0) begin
        // Initialize ALL registers to known states
        register1 <= 32'd0;
        register2 <= 1'b0;
        counter   <= 32'd0;
    end
    else begin
        // Normal operation
    end
end
```

### Overflow Detection

```verilog
always @(posedge clk_i) begin
    if (rstn_i == 1'b0) begin
        counter <= 32'd0;
        overflow <= 1'b0;
    end
    else begin
        if (counter == 32'hFFFFFFFF)
            overflow <= 1'b1;  // Set flag, stop counting
        else
            counter <= counter + 1'b1;
    end
end
```

### Signed Arithmetic

```verilog
// Declare signals as signed
wire signed [13:0] input_a;
wire signed [13:0] input_b;
wire signed [27:0] product;

// Arithmetic automatically handles 2's complement
assign product = input_a * input_b;

// Saturation example
wire signed [13:0] sum;
wire signed [14:0] sum_full;  // Extra bit to detect overflow
assign sum_full = input_a + input_b;

// Saturate to ±2^13-1
assign sum = (sum_full > 14'sd8191)  ? 14'sd8191 :
             (sum_full < -14'sd8192) ? -14'sd8192 :
             sum_full[13:0];
```

### Clipping/Saturation

```verilog
// Clip unsigned value to maximum
assign clipped = (value > 16'd1000) ? 16'd1000 : value;

// Saturating signed arithmetic
wire signed [27:0] product;  // Result of 14-bit * 14-bit
wire signed [13:0] saturated;

assign saturated = (product > 28'sd8191)  ? 14'sd8191 :
                   (product < -28'sd8192) ? -14'sd8192 :
                   product[13:0];
```

## Common Pitfalls

### 1. Address Mismatch

**Problem**: Register reads return wrong values

**Python:**
```python
my_register = IntRegister(0x04)
```

**Verilog (WRONG):**
```verilog
localparam ADDR_MY_REG = 20'h00008;  // WRONG ADDRESS!
```

**Solution**: Always double-check addresses match exactly!

### 2. Bit Width Mismatch

**Problem**: Data corruption or unexpected behavior

**Python:**
```python
value = IntRegister(0x00, bits=16)
```

**Verilog (WRONG):**
```verilog
reg [31:0] value_reg;  // WRONG - should be [15:0]
```

**Solution**: Match bit widths exactly between Python and Verilog.

### 3. Missing sys_ack

**Problem**: System hangs when accessing module

**Verilog (WRONG):**
```verilog
always @(posedge clk_i) begin
    // ... forgot to set sys_ack!
end
```

**Solution**: Always set `sys_ack <= sys_en` in read logic!

### 4. Signed vs Unsigned

**Problem**: Incorrect sign extension or arithmetic

**Python:**
```python
gain = FloatRegister(0x00, bits=14, norm=2**13)  # Signed!
```

**Verilog (WRONG):**
```verilog
reg [13:0] gain_reg;  // WRONG - should be "signed"
// Read returns: {18'b0, gain_reg}  // WRONG - should sign-extend!
```

**Solution**: Use `signed` keyword and proper sign extension:
```verilog
reg signed [13:0] gain_reg;
// Read returns: {{18{gain_reg[13]}}, gain_reg}  // Correct!
```

### 5. Forgetting to Register Outputs

**Problem**: Timing violations, combinational loops

**Verilog (WRONG):**
```verilog
assign sys_rdata = (sys_addr[19:0] == 20'h00) ? reg1 : reg2;  // Combinational!
```

**Solution**: Always register outputs:
```verilog
always @(posedge clk_i) begin
    sys_rdata <= (sys_addr[19:0] == 20'h00) ? reg1 : reg2;  // Registered
end
```

## Timing Considerations

### Clock Frequency

- System clock: **125 MHz** (8 ns period)
- All logic must meet this timing
- Critical paths must be < 8 ns

### Meeting Timing

```verilog
// BAD: Long combinational path
wire [31:0] result;
assign result = ((a * b) + c) * d;  // Multiple operations, likely timing violation!

// GOOD: Pipeline stages
reg [31:0] stage1, stage2;
always @(posedge clk_i) begin
    stage1 <= a * b;           // Cycle 1: multiply
    stage2 <= stage1 + c;      // Cycle 2: add
    result <= stage2 * d;      // Cycle 3: multiply
end
```



## Integration Checklist

Before compiling your module, verify:

- [ ] All register addresses match Python module exactly
- [ ] Bit widths match (bits parameter in Python = reg width in Verilog)
- [ ] Signed registers use `signed` keyword and sign-extend on read
- [ ] `sys_ack` is set for all transactions
- [ ] `sys_err` is connected (usually tied to 0)
- [ ] All registers have reset values
- [ ] All outputs are registered (not combinational)
- [ ] Module is instantiated in `red_pitaya_top.v`
- [ ] Verilog file is added to `red_pitaya_vivado.tcl`
- [ ] Testbench created and simulation passes



## Advanced Topics

### Multi-Cycle Paths

For operations that take multiple cycles:

```verilog
reg [2:0] state;
localparam IDLE = 3'd0;
localparam CALC1 = 3'd1;
localparam CALC2 = 3'd2;
localparam DONE = 3'd3;

always @(posedge clk_i) begin
    case (state)
        IDLE: if (start) state <= CALC1;
        CALC1: begin
            partial_result <= input_a * input_b;
            state <= CALC2;
        end
        CALC2: begin
            final_result <= partial_result + input_c;
            state <= DONE;
        end
        DONE: state <= IDLE;
    endcase
end
```

### Clock Domain Crossing

If you need signals from different clock domains, use proper synchronizers:

```verilog
// DON'T: Direct connection across clock domains
// assign output_signal = input_from_other_clock;  // WRONG!

// DO: Use 2-FF synchronizer
reg sync1, sync2;
always @(posedge clk_i) begin
    sync1 <= input_from_other_clock;
    sync2 <= sync1;  // Use sync2 in your logic
end
```

### BRAM Usage

For large data buffers:

```verilog
// Infer BRAM: Single-port RAM
reg [31:0] data_ram [0:1023];  // 1024 x 32-bit

always @(posedge clk_i) begin
    if (write_enable)
        data_ram[write_addr] <= write_data;
    read_data <= data_ram[read_addr];
end
```

## Further Reading

- **Red Pitaya HDL Documentation**: https://redpitaya.readthedocs.io/en/latest/developerGuide/hardware.html
- **Existing PyRPL Modules**: Study `red_pitaya_scope.v`, `red_pitaya_hk.v` for examples
- **Python Side**: `pyrpl/hardware_modules/EXAMPLE_MODULE_README.md`
- **Vivado Timing Reports**: `pyrpl/fpga/out/post_route_timing_summary.rpt`

## Questions?

The `example_hardware_module.v` template demonstrates all key patterns. Use it as a starting point and adapt to your needs!
