//////////////////////////////////////////////////////////////////////////////////
// Module: iir_filter_8th_order
// Description: 8th-Order Butterworth IIR (Serial/Folded Architecture)
//              Optimized for Red Pitaya 125-14 (Zynq-7010)
//
// CORRECTED VERSION - Fixed issues:
//   1. Pipeline timing for multiply-accumulate
//   2. Double scaling bug removed
//   3. Proper blocking/non-blocking assignments
//   4. Reduced multiplier width for DSP48 efficiency
//
// Resource Usage: ~2-4 DSP slices, ~1000 LUTs
// Latency: ~28 clock cycles per sample
//////////////////////////////////////////////////////////////////////////////////

module iir_filter_8th_order #(
    parameter INPUT_WIDTH    = 40,
    parameter OUTPUT_WIDTH   = 32,
    parameter DATA_WIDTH     = 32,   // Internal data width
    parameter COEFF_WIDTH    = 28,   // Q4.24 coefficients
    parameter COEFF_FRAC     = 24,   // Fractional bits
    parameter ACC_WIDTH      = 64    // Accumulator width
)(
    input  wire                             clk,
    input  wire                             rst_n,
    input  wire                             ce,          // 30.5 kHz pulse
    input  wire signed [INPUT_WIDTH-1:0]    data_in,
    output reg                              valid_out,
    output reg  signed [OUTPUT_WIDTH-1:0]   data_out,
    output reg                              overflow
);

    // ========================================================================
    // INPUT TRUNCATION
    // Use top 32 bits of 40-bit CIC output (discard 8 LSBs of noise)
    // ========================================================================
    wire signed [DATA_WIDTH-1:0] truncated_input;
    assign truncated_input = data_in[INPUT_WIDTH-1 -: DATA_WIDTH];

    // ========================================================================
    // COEFFICIENT STORAGE (Q4.24 format)
    // ========================================================================
    // Coefficients stored as: [section][term]
    // term: 0=B0, 1=B1, 2=B2, 3=A1(-a1), 4=A2(-a2)
    
    reg signed [COEFF_WIDTH-1:0] coeff_rom [0:3][0:4];
    
    initial begin
        // Section 0: poles at 0.816
        coeff_rom[0][0] = 28'sd147577;      // B0
        coeff_rom[0][1] = 28'sd295155;      // B1  
        coeff_rom[0][2] = 28'sd147577;      // B2
        coeff_rom[0][3] = 28'sd27359891;    // A1 = -a1
        coeff_rom[0][4] = -28'sd11172985;   // A2 = -a2
        
        // Section 1: poles at 0.842
        coeff_rom[1][0] = 28'sd151428;
        coeff_rom[1][1] = 28'sd302856;
        coeff_rom[1][2] = 28'sd151428;
        coeff_rom[1][3] = 28'sd28073726;
        coeff_rom[1][4] = -28'sd11902221;
        
        // Section 2: poles at 0.892
        coeff_rom[2][0] = 28'sd159098;
        coeff_rom[2][1] = 28'sd318195;
        coeff_rom[2][2] = 28'sd159098;
        coeff_rom[2][3] = 28'sd29495685;
        coeff_rom[2][4] = -28'sd13354860;
        
        // Section 3: poles at 0.961
        coeff_rom[3][0] = 28'sd170373;
        coeff_rom[3][1] = 28'sd340746;
        coeff_rom[3][2] = 28'sd170373;
        coeff_rom[3][3] = 28'sd31586001;
        coeff_rom[3][4] = -28'sd15490276;
    end

    // ========================================================================
    // STATE MACHINE
    // ========================================================================
    localparam S_IDLE    = 3'd0;
    localparam S_SETUP   = 3'd1;  // Setup multiplier operands
    localparam S_MULT    = 3'd2;  // Wait for multiply, accumulate
    localparam S_UPDATE  = 3'd3;  // Update history, move to next section
    localparam S_OUTPUT  = 3'd4;  // Final output
    
    reg [2:0] state;
    reg [2:0] term_idx;     // 0..4 (B0, B1, B2, A1, A2)
    reg [1:0] section_idx;  // 0..3 (biquad section)

    // ========================================================================
    // HISTORY REGISTERS
    // ========================================================================
    reg signed [DATA_WIDTH-1:0] x_z1 [0:3];  // x[n-1] for each section
    reg signed [DATA_WIDTH-1:0] x_z2 [0:3];  // x[n-2] for each section
    reg signed [DATA_WIDTH-1:0] y_z1 [0:3];  // y[n-1] for each section
    reg signed [DATA_WIDTH-1:0] y_z2 [0:3];  // y[n-2] for each section
    
    // Current section input
    reg signed [DATA_WIDTH-1:0] section_input;

    // ========================================================================
    // MULTIPLY-ACCUMULATE UNIT
    // ========================================================================
    reg signed [DATA_WIDTH-1:0]   mult_data;    // Data operand
    reg signed [COEFF_WIDTH-1:0]  mult_coeff;   // Coefficient operand
    reg signed [ACC_WIDTH-1:0]    accumulator;  // Running sum
    
    // Combinational multiply (will be mapped to DSP48)
    wire signed [DATA_WIDTH+COEFF_WIDTH-1:0] product;
    assign product = mult_data * mult_coeff;
    
    // Scaled output (remove coefficient fractional bits)
    wire signed [ACC_WIDTH-1:0] scaled_acc;
    assign scaled_acc = accumulator >>> COEFF_FRAC;
    
    // Saturated output for section
    wire signed [DATA_WIDTH-1:0] saturated_output;
    assign saturated_output = 
        (scaled_acc > {{(ACC_WIDTH-DATA_WIDTH){1'b0}}, {(DATA_WIDTH-1){1'b1}}}) ? 
            {1'b0, {(DATA_WIDTH-1){1'b1}}} :  // Positive saturation
        (scaled_acc < {{(ACC_WIDTH-DATA_WIDTH){1'b1}}, {(DATA_WIDTH-1){1'b0}}}) ?
            {1'b1, {(DATA_WIDTH-1){1'b0}}} :  // Negative saturation
        scaled_acc[DATA_WIDTH-1:0];           // Normal

    // ========================================================================
    // MAIN STATE MACHINE
    // ========================================================================
    integer i;
    
    always @(posedge clk) begin
        if (!rst_n) begin
            state <= S_IDLE;
            valid_out <= 1'b0;
            data_out <= {OUTPUT_WIDTH{1'b0}};
            overflow <= 1'b0;
            term_idx <= 3'd0;
            section_idx <= 2'd0;
            section_input <= {DATA_WIDTH{1'b0}};
            mult_data <= {DATA_WIDTH{1'b0}};
            mult_coeff <= {COEFF_WIDTH{1'b0}};
            accumulator <= {ACC_WIDTH{1'b0}};
            
            for (i = 0; i < 4; i = i + 1) begin
                x_z1[i] <= {DATA_WIDTH{1'b0}};
                x_z2[i] <= {DATA_WIDTH{1'b0}};
                y_z1[i] <= {DATA_WIDTH{1'b0}};
                y_z2[i] <= {DATA_WIDTH{1'b0}};
            end
        end else begin
            // Default: clear valid
            valid_out <= 1'b0;
            overflow <= 1'b0;
            
            case (state)
                // ------------------------------------------------------------
                S_IDLE: begin
                    if (ce) begin
                        // New sample arrived
                        section_input <= truncated_input;
                        section_idx <= 2'd0;
                        term_idx <= 3'd0;
                        accumulator <= {ACC_WIDTH{1'b0}};
                        state <= S_SETUP;
                    end
                end
                
                // ------------------------------------------------------------
                S_SETUP: begin
                    // Setup multiplier operands for current term
                    case (term_idx)
                        3'd0: begin  // B0 * x[n]
                            mult_data  <= section_input;
                            mult_coeff <= coeff_rom[section_idx][0];
                        end
                        3'd1: begin  // B1 * x[n-1]
                            mult_data  <= x_z1[section_idx];
                            mult_coeff <= coeff_rom[section_idx][1];
                        end
                        3'd2: begin  // B2 * x[n-2]
                            mult_data  <= x_z2[section_idx];
                            mult_coeff <= coeff_rom[section_idx][2];
                        end
                        3'd3: begin  // A1 * y[n-1]
                            mult_data  <= y_z1[section_idx];
                            mult_coeff <= coeff_rom[section_idx][3];
                        end
                        3'd4: begin  // A2 * y[n-2]
                            mult_data  <= y_z2[section_idx];
                            mult_coeff <= coeff_rom[section_idx][4];
                        end
                        default: begin
                            mult_data  <= {DATA_WIDTH{1'b0}};
                            mult_coeff <= {COEFF_WIDTH{1'b0}};
                        end
                    endcase
                    state <= S_MULT;
                end
                
                // ------------------------------------------------------------
                S_MULT: begin
                    // Accumulate product (product is now valid from S_SETUP operands)
                    accumulator <= accumulator + product;
                    
                    if (term_idx == 3'd4) begin
                        // All 5 terms accumulated, go to update
                        state <= S_UPDATE;
                    end else begin
                        // More terms to process
                        term_idx <= term_idx + 1'b1;
                        state <= S_SETUP;
                    end
                end
                
                // ------------------------------------------------------------
                S_UPDATE: begin
                    // Update delay lines for this section
                    x_z2[section_idx] <= x_z1[section_idx];
                    x_z1[section_idx] <= section_input;
                    y_z2[section_idx] <= y_z1[section_idx];
                    y_z1[section_idx] <= saturated_output;
                    
                    // Output of this section is input to next
                    section_input <= saturated_output;
                    
                    // Check for saturation (overflow indicator)
                    if (scaled_acc > {{(ACC_WIDTH-DATA_WIDTH){1'b0}}, {(DATA_WIDTH-1){1'b1}}} ||
                        scaled_acc < {{(ACC_WIDTH-DATA_WIDTH){1'b1}}, {(DATA_WIDTH-1){1'b0}}}) begin
                        overflow <= 1'b1;
                    end
                    
                    if (section_idx == 2'd3) begin
                        // All 4 sections done
                        state <= S_OUTPUT;
                    end else begin
                        // Process next section
                        section_idx <= section_idx + 1'b1;
                        term_idx <= 3'd0;
                        accumulator <= {ACC_WIDTH{1'b0}};
                        state <= S_SETUP;
                    end
                end
                
                // ------------------------------------------------------------
                S_OUTPUT: begin
                    // Output final result (section_input holds last biquad output)
                    // NO additional scaling - biquads are already unity gain
                    data_out <= section_input;
                    valid_out <= 1'b1;
                    state <= S_IDLE;
                end
                
                default: state <= S_IDLE;
            endcase
        end
    end

endmodule