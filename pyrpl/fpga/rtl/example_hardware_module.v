/**
 * @brief Example Hardware Module Template
 *
 * This module demonstrates the Verilog side of a custom hardware module
 * for PyRPL. It corresponds to the Python template in:
 * pyrpl/hardware_modules/example_module.py
 *
 * This template shows:
 * - System bus interface (read/write)
 * - Register mapping to Python attributes
 * - Reset handling
 * - Different register types (bool, int, float representation, select)
 * - Action registers (write-only, pulse behavior)
 * - Status registers (read-only)
 * - Counter and overflow logic
 * - 64-bit register handling (optional)
 *
 * IMPORTANT: Register addresses MUST match the Python module exactly!
 */

module example_hardware_module (
    // Clock and reset
    input  wire        clk_i,           // 125 MHz system clock
    input  wire        rstn_i,          // Active-low reset

    // Optional: Signal inputs/outputs (uncomment if needed)
    // input  wire [13:0] signal_in,    // Example 14-bit signed input
    // output wire [13:0] signal_out,   // Example 14-bit signed output

    // System bus interface (AXI-lite style)
    input  wire [31:0] sys_addr,        // Address bus
    input  wire [31:0] sys_wdata,       // Write data bus
    input  wire [ 3:0] sys_sel,         // Write byte select (usually ignored)
    input  wire        sys_wen,         // Write enable
    input  wire        sys_ren,         // Read enable
    output reg  [31:0] sys_rdata,       // Read data bus
    output reg         sys_err,         // Error flag
    output reg         sys_ack          // Acknowledge signal
);

//=============================================================================
// Register Address Map (must match Python example_module.py)
//=============================================================================

// Control Registers (Read/Write)
localparam ADDR_ENABLE     = 20'h00000;  // BoolRegister: Enable/disable module
localparam ADDR_GAIN       = 20'h00004;  // FloatRegister: Signal gain (14-bit signed)
localparam ADDR_THRESHOLD  = 20'h00008;  // IntRegister: Threshold value (16-bit)
localparam ADDR_MODE       = 20'h0000C;  // SelectRegister: Operating mode (2 bits for 4 modes)
localparam ADDR_FREQUENCY  = 20'h00010;  // FrequencyRegister: Operating frequency (32-bit)

// Status Registers (Read-Only)
localparam ADDR_COUNTER    = 20'h00020;  // IntRegister: Event counter (32-bit, read-only)
localparam ADDR_OVERFLOW   = 20'h00024;  // BoolRegister: Overflow flag (read-only)

// Action Registers (Write-Only)
localparam ADDR_RESET      = 20'h00030;  // BoolRegister: Write 1 to reset counter

// Configuration Register
localparam ADDR_CONFIG     = 20'h00040;  // IntRegister: Configuration (internal use)

// Optional: 64-bit register example (uncomment if using LongRegister in Python)
// localparam ADDR_TIMESTAMP_LO = 20'h00050;  // Lower 32 bits of 64-bit timestamp
// localparam ADDR_TIMESTAMP_HI = 20'h00054;  // Upper 32 bits of 64-bit timestamp

//=============================================================================
// Internal Registers
//=============================================================================

// Control registers (writable from system bus)
reg         enable_reg;           // Module enable flag
reg  [13:0] gain_reg;             // 14-bit signed gain (represents ±1.0 range)
reg  [15:0] threshold_reg;        // 16-bit unsigned threshold
reg  [ 1:0] mode_reg;             // 2 bits = 4 modes (off=0, continuous=1, triggered=2, gated=3)
reg  [31:0] frequency_reg;        // 32-bit frequency control value

// Status registers (read-only, updated by module logic)
reg  [31:0] counter_reg;          // Event counter
reg         overflow_reg;         // Overflow flag

// Configuration register
reg  [31:0] config_reg;           // General configuration register

// Optional: 64-bit timestamp example
// reg  [63:0] timestamp_reg;

// Internal signals
wire        reset_counter_pulse;  // Pulse generated when reset_counter is written

//=============================================================================
// System Bus Write Logic
//=============================================================================

// Capture write pulses for action registers
reg reset_counter_write;

always @(posedge clk_i) begin
    if (rstn_i == 1'b0) begin
        // Reset all writable registers to default values
        enable_reg        <= 1'b0;
        gain_reg          <= 14'd0;
        threshold_reg     <= 16'd0;
        mode_reg          <= 2'd0;        // 0 = off
        frequency_reg     <= 32'd0;
        config_reg        <= 32'd0;
        reset_counter_write <= 1'b0;
    end
    else begin
        // Default: action registers auto-clear after one cycle
        reset_counter_write <= 1'b0;

        // Handle write requests
        if (sys_wen) begin
            case (sys_addr[19:0])
                ADDR_ENABLE:    enable_reg    <= sys_wdata[0];
                ADDR_GAIN:      gain_reg      <= sys_wdata[13:0];  // 14-bit signed
                ADDR_THRESHOLD: threshold_reg <= sys_wdata[15:0];
                ADDR_MODE:      mode_reg      <= sys_wdata[1:0];
                ADDR_FREQUENCY: frequency_reg <= sys_wdata[31:0];
                ADDR_CONFIG:    config_reg    <= sys_wdata[31:0];

                // Action register: pulse for one cycle when written with 1
                ADDR_RESET:     reset_counter_write <= sys_wdata[0];

                // 64-bit register example (lower 32 bits)
                // ADDR_TIMESTAMP_LO: timestamp_reg[31:0]  <= sys_wdata[31:0];
                // ADDR_TIMESTAMP_HI: timestamp_reg[63:32] <= sys_wdata[31:0];
            endcase
        end
    end
end

// Generate pulse for reset action
assign reset_counter_pulse = reset_counter_write;

//=============================================================================
// System Bus Read Logic
//=============================================================================

// Combine read and write enables
wire sys_en;
assign sys_en = sys_wen | sys_ren;

always @(posedge clk_i) begin
    if (rstn_i == 1'b0) begin
        sys_err   <= 1'b0;
        sys_ack   <= 1'b0;
        sys_rdata <= 32'h0;
    end
    else begin
        // Default: no error, acknowledge all transactions
        sys_err <= 1'b0;
        sys_ack <= sys_en;

        // Read data multiplexer
        case (sys_addr[19:0])
            // Control registers (read back written values)
            ADDR_ENABLE:    sys_rdata <= {31'b0, enable_reg};
            ADDR_GAIN:      sys_rdata <= {{18{gain_reg[13]}}, gain_reg};  // Sign-extend 14-bit
            ADDR_THRESHOLD: sys_rdata <= {16'b0, threshold_reg};
            ADDR_MODE:      sys_rdata <= {30'b0, mode_reg};
            ADDR_FREQUENCY: sys_rdata <= frequency_reg;
            ADDR_CONFIG:    sys_rdata <= config_reg;

            // Status registers (read module state)
            ADDR_COUNTER:   sys_rdata <= counter_reg;
            ADDR_OVERFLOW:  sys_rdata <= {31'b0, overflow_reg};

            // Action register (reading typically returns 0)
            ADDR_RESET:     sys_rdata <= 32'h0;

            // 64-bit register example
            // ADDR_TIMESTAMP_LO: sys_rdata <= timestamp_reg[31:0];
            // ADDR_TIMESTAMP_HI: sys_rdata <= timestamp_reg[63:32];

            // Default: return 0 for unmapped addresses
            default:        sys_rdata <= 32'h0;
        endcase
    end
end

//=============================================================================
// Module Logic - Example Counter Implementation
//=============================================================================

// This is where your actual module functionality goes.
// This example implements a simple counter that increments based on mode.

always @(posedge clk_i) begin
    if (rstn_i == 1'b0) begin
        counter_reg   <= 32'd0;
        overflow_reg  <= 1'b0;
    end
    else begin
        // Handle counter reset
        if (reset_counter_pulse) begin
            counter_reg  <= 32'd0;
            overflow_reg <= 1'b0;
        end
        // Module is enabled and not in 'off' mode
        else if (enable_reg && (mode_reg != 2'd0)) begin
            // Check for overflow before incrementing
            if (counter_reg == 32'hFFFFFFFF) begin
                overflow_reg <= 1'b1;  // Set overflow flag, stop counting
            end
            else begin
                // Increment counter based on mode
                case (mode_reg)
                    2'd1: counter_reg <= counter_reg + 1'b1;  // continuous mode
                    2'd2: counter_reg <= counter_reg + 1'b1;  // triggered mode (simplified)
                    2'd3: counter_reg <= counter_reg + 1'b1;  // gated mode (simplified)
                    default: ;  // off mode, don't increment
                endcase
            end
        end
    end
end

//=============================================================================
// Optional: Additional Module Logic
//=============================================================================

// Example: Using gain and threshold for signal processing
// Uncomment and modify if you need actual signal processing

/*
wire [13:0] scaled_signal;
wire        threshold_exceeded;

// Apply gain to input signal (simplified multiplication)
assign scaled_signal = signal_in * gain_reg[13:10];  // Use upper bits as simple gain

// Compare against threshold
assign threshold_exceeded = (scaled_signal > threshold_reg[13:0]);

// Output processed signal
assign signal_out = enable_reg ? scaled_signal : 14'd0;
*/

endmodule
