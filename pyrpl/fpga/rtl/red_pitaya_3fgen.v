//`timescale 1ns / 1ps // Assuming this is present elsewhere or not strictly needed for this block
/**
 * @brief Red Pitaya 3-Component FM Sine Generator.
 *
 * Generates a sum of three independently controlled sine waves, on two output
 * channels, with possible frequency modulation and phase offsets for all signals.
 * Uses memory-efficient quarter-sine LUTs.
 */
/*
###############################################################################
#    pyrpl - DSP servo controller for quantum optics with the RedPitaya
#    Copyright (C) 2014-2016  Leonhard Neuhaus  (neuhaus@spectro.jussieu.fr)
#    (... adaptations by user for 3fgen ...)
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
# (...)
###############################################################################
*/


module red_pitaya_3fgen #(
    parameter PHASEBITS   = 32, // Phase accumulator bits
    parameter LUTSZ       = 11, // log2(LUT entries per quarter wave) = log2(2048)
    parameter LUTBITS     = 14, // LUT output bits (matches DAC)
    parameter DACBITS     = 14, // DAC output bits
    parameter GAINBITS    = 14, // Amplitude control bits (unsigned, per component, similar to ASG set_amp)
    parameter FM_MOD_BITS = 17  // Bitwidth of the FM modulating signal input (signed)
) (
    // --- Clocks and Reset ---
    input                             clk_i,            // Processing clock (e.g., 125 MHz)
    input                             rstn_i,           // Reset, active low

    // --- Outputs ---
    output reg signed [DACBITS-1:0]   dac_a_o,
    output reg signed [DACBITS-1:0]   dac_b_o,

    output reg                        output_to_dsp_enable_o,

    // --- Inputs ---
    input signed [FM_MOD_BITS-1:0]    fm_mod_in,        // Input signal for Frequency Modulation

    // --- System Bus Interface ---
    input      [31:0]                 sys_addr,         // Bus address
    input      [31:0]                 sys_wdata,        // Bus write data
    input      [3:0]                  sys_sel,          // Bus write byte select
    input                             sys_wen,          // Bus write enable
    input                             sys_ren,          // Bus read enable
    output reg [31:0]                 sys_rdata,        // Bus read data
    output reg                        sys_err,          // Bus error indicator
    output reg                        sys_ack           // Bus acknowledge signal
);

    //--------------------------------------------------------------------------
    // Local Parameters
    //--------------------------------------------------------------------------
    localparam NUM_COMPONENTS = 3;
    localparam FM_SCALING_FACTOR = 34360;   // for scaling the FM deviation to the phase step size
    localparam MAX_FM_DEV_KHZ_BITS = 13;    // 13 bits for 0-8192 kHz deviation

    // Bit widths for internal calculations
    localparam PROD_BITS_COMPONENT_AMP = LUTBITS + GAINBITS; // signed(LUTBITS) * unsigned(GAINBITS) -> e.g., 14 + 14 = 28
    localparam SCALED_SUM_COMPONENT_BITS = 16;
    localparam SUM_COMPONENTS_BITS = SCALED_SUM_COMPONENT_BITS + 2; // e.g., 16 + $clog2(NUM_COMPONENTS)
    localparam PRE_SAT_BITS = SUM_COMPONENTS_BITS;

    //--------------------------------------------------------------------------
    // Registers for Control via System Bus
    //--------------------------------------------------------------------------
    reg gen_enable;
    reg output_zero;

    reg [PHASEBITS-1:0] comp_freq_step      [NUM_COMPONENTS-1:0];
    reg [PHASEBITS-1:0] comp_phase_offset_a [NUM_COMPONENTS-1:0];
    reg [GAINBITS-1:0]  comp_amplitude_a    [NUM_COMPONENTS-1:0];
    reg [PHASEBITS-1:0] comp_phase_offset_b [NUM_COMPONENTS-1:0];
    reg [GAINBITS-1:0]  comp_amplitude_b    [NUM_COMPONENTS-1:0];
    reg                 comp_enable         [NUM_COMPONENTS-1:0];

    reg                 fm_enable           [NUM_COMPONENTS-1:0];
    reg [31:0]          fm_deviation_kHz    [NUM_COMPONENTS-1:0];

    reg signed [DACBITS-1:0] overall_dc_offset_a;
    reg signed [DACBITS-1:0] overall_dc_offset_b;

    //--------------------------------------------------------------------------
    // Internal Signals and Registers
    //--------------------------------------------------------------------------
    reg  [PHASEBITS-1:0] phase_acc [NUM_COMPONENTS-1:0];

    // FM Calculation (pipelined)
    reg signed [(MAX_FM_DEV_KHZ_BITS + 16):0] fm_dev_scaled_reg [NUM_COMPONENTS-1:0];
    reg signed [(MAX_FM_DEV_KHZ_BITS + 16 + FM_MOD_BITS):0] fm_prod_reg [NUM_COMPONENTS-1:0];
    wire signed [PHASEBITS-1:0] fm_delta_step [NUM_COMPONENTS-1:0];
    wire [PHASEBITS-1:0] current_step [NUM_COMPONENTS-1:0];

    // Effective phase inputs to the quarter_wave_LUT module
    reg [PHASEBITS-1:0] phase_eff_a [NUM_COMPONENTS-1:0];
    reg [PHASEBITS-1:0] phase_eff_b [NUM_COMPONENTS-1:0];

    // Wires to connect to the outputs of red_pitaya_quarter_wave_LUT instances
    // These are signed outputs from the LUT module, LUTBITS wide.
    wire signed [LUTBITS-1:0] lut_sine_a [NUM_COMPONENTS-1:0];
    wire signed [LUTBITS-1:0] lut_sine_b [NUM_COMPONENTS-1:0];

    // Amplitude Scaling & Shift (pipelined)
    reg signed [PROD_BITS_COMPONENT_AMP-1:0] prod_a [NUM_COMPONENTS-1:0];
    reg signed [PROD_BITS_COMPONENT_AMP-1:0] prod_b [NUM_COMPONENTS-1:0];
    reg signed [SCALED_SUM_COMPONENT_BITS-1:0] scaled_shifted_a [NUM_COMPONENTS-1:0];
    reg signed [SCALED_SUM_COMPONENT_BITS-1:0] scaled_shifted_b [NUM_COMPONENTS-1:0];

    // Summation (pipelined)
    reg signed [SUM_COMPONENTS_BITS-1:0] sum_val_a;
    reg signed [SUM_COMPONENTS_BITS-1:0] sum_val_b;

    // DC Offset and Final Value for Saturation (pipelined)
    reg signed [PRE_SAT_BITS-1:0] dac_pre_sat_a;
    reg signed [PRE_SAT_BITS-1:0] dac_pre_sat_b;

    // Saturation Outputs
    wire signed [DACBITS-1:0] dac_a_o_signed;
    wire signed [DACBITS-1:0] dac_b_o_signed;

    always @(posedge clk_i) begin
        dac_a_o <= dac_a_o_signed;
        dac_b_o <= dac_b_o_signed;
    end

    //--------------------------------------------------------------------------
    // System Bus Logic
    //--------------------------------------------------------------------------
    integer k_idx;
    always @(posedge clk_i) begin
        if (!rstn_i) begin
            gen_enable <= 1'b0;
            output_zero <= 1'b0;
            output_to_dsp_enable_o <= 1'b0; 
            overall_dc_offset_a <= {DACBITS{1'b0}};
            overall_dc_offset_b <= {DACBITS{1'b0}};
            for (k_idx = 0; k_idx < NUM_COMPONENTS; k_idx = k_idx+1) begin
                comp_freq_step[k_idx]      <= {PHASEBITS{1'b0}};
                comp_phase_offset_a[k_idx] <= {PHASEBITS{1'b0}};
                comp_amplitude_a[k_idx]    <= {GAINBITS{1'b0}};
                comp_phase_offset_b[k_idx] <= {PHASEBITS{1'b0}};
                comp_amplitude_b[k_idx]    <= {GAINBITS{1'b0}};
                fm_enable[k_idx]           <= 1'b0;
                fm_deviation_kHz[k_idx]    <= 32'd0;
                comp_enable[k_idx]         <= 1'b1;
            end
            sys_ack <= 1'b0;
            sys_err <= 1'b0;
            sys_rdata <= 32'h0;
        end else begin
            sys_ack <= 1'b0;
            sys_err <= 1'b0;

            if (sys_wen) begin
                sys_ack <= 1'b1;
                case (sys_addr[15:0])
                    // Global Controls
                    16'h0000: {output_to_dsp_enable_o, output_zero, gen_enable} <= sys_wdata[2:0];
                    16'h0004: overall_dc_offset_a <= sys_wdata[DACBITS-1:0];
                    16'h0008: overall_dc_offset_b <= sys_wdata[DACBITS-1:0];

                    // Component 0
                    16'h0010: comp_freq_step[0]      <= sys_wdata[PHASEBITS-1:0];
                    16'h0014: comp_phase_offset_a[0] <= sys_wdata[PHASEBITS-1:0];
                    16'h0018: comp_amplitude_a[0]    <= sys_wdata[GAINBITS-1:0];
                    16'h001C: comp_phase_offset_b[0] <= sys_wdata[PHASEBITS-1:0];
                    16'h0020: comp_amplitude_b[0]    <= sys_wdata[GAINBITS-1:0];
                    16'h0024: fm_enable[0]           <= sys_wdata[0];
                    16'h0028: fm_deviation_kHz[0]    <= sys_wdata;
                    16'h002C: comp_enable[0]         <= sys_wdata[0];

                    // Component 1
                    16'h0040: comp_freq_step[1]      <= sys_wdata[PHASEBITS-1:0];
                    16'h0044: comp_phase_offset_a[1] <= sys_wdata[PHASEBITS-1:0];
                    16'h0048: comp_amplitude_a[1]    <= sys_wdata[GAINBITS-1:0];
                    16'h004C: comp_phase_offset_b[1] <= sys_wdata[PHASEBITS-1:0];
                    16'h0050: comp_amplitude_b[1]    <= sys_wdata[GAINBITS-1:0];
                    16'h0054: fm_enable[1]           <= sys_wdata[0];
                    16'h0058: fm_deviation_kHz[1]    <= sys_wdata;
                    16'h005C: comp_enable[1]         <= sys_wdata[0];

                    // Component 2
                    16'h0070: comp_freq_step[2]      <= sys_wdata[PHASEBITS-1:0];
                    16'h0074: comp_phase_offset_a[2] <= sys_wdata[PHASEBITS-1:0];
                    16'h0078: comp_amplitude_a[2]    <= sys_wdata[GAINBITS-1:0];
                    16'h007C: comp_phase_offset_b[2] <= sys_wdata[PHASEBITS-1:0];
                    16'h0080: comp_amplitude_b[2]    <= sys_wdata[GAINBITS-1:0];
                    16'h0084: fm_enable[2]           <= sys_wdata[0];
                    16'h0088: fm_deviation_kHz[2]    <= sys_wdata;
                    16'h008C: comp_enable[2]         <= sys_wdata[0];

                    default: sys_ack <= 1'b0;
                endcase
            end else if (sys_ren) begin
                sys_ack <= 1'b1;
                case (sys_addr[15:0])
                    // Global Controls
                    16'h0000: sys_rdata <= {29'b0, output_to_dsp_enable_o, output_zero, gen_enable};
                    16'h0004: sys_rdata <= {{32-DACBITS{overall_dc_offset_a[DACBITS-1]}}, overall_dc_offset_a};
                    16'h0008: sys_rdata <= {{32-DACBITS{overall_dc_offset_b[DACBITS-1]}}, overall_dc_offset_b};

                    // Component 0
                    16'h0010: sys_rdata <= comp_freq_step[0];
                    16'h0014: sys_rdata <= comp_phase_offset_a[0];
                    16'h0018: sys_rdata <= {{32-GAINBITS{1'b0}}, comp_amplitude_a[0]};
                    16'h001C: sys_rdata <= comp_phase_offset_b[0];
                    16'h0020: sys_rdata <= {{32-GAINBITS{1'b0}}, comp_amplitude_b[0]};
                    16'h0024: sys_rdata <= {31'b0, fm_enable[0]};
                    16'h0028: sys_rdata <= fm_deviation_kHz[0];
                    16'h002C: sys_rdata <= {31'b0, comp_enable[0]};

                    // Component 1
                    16'h0040: sys_rdata <= comp_freq_step[1];
                    16'h0044: sys_rdata <= comp_phase_offset_a[1];
                    16'h0048: sys_rdata <= {{32-GAINBITS{1'b0}}, comp_amplitude_a[1]};
                    16'h004C: sys_rdata <= comp_phase_offset_b[1];
                    16'h0050: sys_rdata <= {{32-GAINBITS{1'b0}}, comp_amplitude_b[1]};
                    16'h0054: sys_rdata <= {31'b0, fm_enable[1]};
                    16'h0058: sys_rdata <= fm_deviation_kHz[1];
                    16'h005C: sys_rdata <= {31'b0, comp_enable[1]};
                    
                    // Component 2
                    16'h0070: sys_rdata <= comp_freq_step[2];
                    16'h0074: sys_rdata <= comp_phase_offset_a[2];
                    16'h0078: sys_rdata <= {{32-GAINBITS{1'b0}}, comp_amplitude_a[2]};
                    16'h007C: sys_rdata <= comp_phase_offset_b[2];
                    16'h0080: sys_rdata <= {{32-GAINBITS{1'b0}}, comp_amplitude_b[2]};
                    16'h0084: sys_rdata <= {31'b0, fm_enable[2]};
                    16'h0088: sys_rdata <= fm_deviation_kHz[2];
                    16'h008C: sys_rdata <= {31'b0, comp_enable[2]};

                    // Read-only parameters
                    16'hFF00: sys_rdata <= PHASEBITS;
                    16'hFF04: sys_rdata <= LUTSZ;
                    16'hFF08: sys_rdata <= LUTBITS;
                    16'hFF0C: sys_rdata <= DACBITS;
                    16'hFF10: sys_rdata <= GAINBITS;
                    16'hFF14: sys_rdata <= FM_MOD_BITS;
                    16'hFF18: sys_rdata <= NUM_COMPONENTS;

                    default: begin
                        sys_rdata <= 32'hDEADBEEF;
                        sys_ack <= 1'b0;
                    end
                endcase
            end
        end
    end

    //--------------------------------------------------------------------------
    // Core Generator Logic
    //--------------------------------------------------------------------------
    genvar i;
    generate
        for (i = 0; i < NUM_COMPONENTS; i = i + 1) begin : component_gen_block
            // --- 1. Frequency Modulation Calculation (Pipelined) ---
            always @(posedge clk_i) begin
               if (!rstn_i) begin
                   fm_dev_scaled_reg[i] <= 0;
                   fm_prod_reg[i]       <= 0;
               end else begin
                  fm_dev_scaled_reg[i] <= $signed(fm_deviation_kHz[i][MAX_FM_DEV_KHZ_BITS-1:0]) * FM_SCALING_FACTOR;
                  fm_prod_reg[i]       <= $signed(fm_dev_scaled_reg[i]) * $signed(fm_mod_in);
               end
            end
            assign fm_delta_step[i] = $signed(fm_prod_reg[i]) >>> 16;
            assign current_step[i] = fm_enable[i] ? (comp_freq_step[i] + fm_delta_step[i])
                                                  : comp_freq_step[i];

            // --- 2. Phase Accumulation ---
            always @(posedge clk_i) begin
                if (!rstn_i) begin
                    phase_acc[i] <= {PHASEBITS{1'b0}};
                end else if (gen_enable) begin
                    phase_acc[i] <= phase_acc[i] + current_step[i];
                end else begin
                    phase_acc[i] <= {PHASEBITS{1'b0}}; // Reset phase if not enabled
                end
            end

            // --- 3. Effective Phase Calculation (for LUT input) ---
            always @(posedge clk_i) begin
                // These phase_eff signals are registered and become inputs to the LUT module
                phase_eff_a[i] <= phase_acc[i] + comp_phase_offset_a[i];
                phase_eff_b[i] <= phase_acc[i] + comp_phase_offset_b[i];
            end

            // --- 4. Sine Wave Generation using red_pitaya_quarter_wave_lut ---
            // The LUT module red_pitaya_quarter_wave_LUT now accepts two independent phase inputs.
            // phase_eff_a[i] is the absolute phase for lut_sine_a[i].
            // phase_eff_b[i] is the absolute phase for lut_sine_b[i].
            // This module has a 3-cycle latency.
            // lut_sine_a[i] will be sin(phase_eff_a[i])
            // lut_sine_b[i] will be sin(phase_eff_b[i])
            red_pitaya_quarter_wave_lut #(
                .LUTSZ(LUTSZ),
                .LUTBITS(LUTBITS),
                .PHASEBITS(PHASEBITS)
            ) qw_lut_inst (
                .clk_i(clk_i),
                .rstn_i(rstn_i),
                .phase_a_in(phase_eff_a[i]),   // Input phase for sin_a_o
                .phase_b_in(phase_eff_b[i]),   // Input phase for sin_b_o
                .sin_a_o(lut_sine_a[i]),       // Output: sin(phase_eff_a[i])
                .sin_b_o(lut_sine_b[i])        // Output: sin(phase_eff_b[i])
            );

            // --- 5. Amplitude Scaling & Shift (Pipelined) ---
            // This stage now takes inputs from lut_sine_a/b which are outputs of qw_lut_inst
            // lut_sine_a/b are LUTBITS wide, signed.
            always @(posedge clk_i) begin
                if (!rstn_i) begin
                    prod_a[i] <= {PROD_BITS_COMPONENT_AMP{1'b0}};
                    prod_b[i] <= {PROD_BITS_COMPONENT_AMP{1'b0}};
                    scaled_shifted_a[i] <= {SCALED_SUM_COMPONENT_BITS{1'b0}};
                    scaled_shifted_b[i] <= {SCALED_SUM_COMPONENT_BITS{1'b0}};
                end else begin
                    // comp_amplitude_a/b are unsigned GAINBITS wide
                    prod_a[i] <= $signed(lut_sine_a[i]) * $signed(comp_amplitude_a[i]);
                    prod_b[i] <= $signed(lut_sine_b[i]) * $signed(comp_amplitude_b[i]);

                    // If component is enabled, scale and shift. Otherwise, output zero for this component.
                    if (comp_enable[i]) begin
                        scaled_shifted_a[i] <= $signed(prod_a[i]) >>> 13; // Scale factor, 2^13 = 8192
                        scaled_shifted_b[i] <= $signed(prod_b[i]) >>> 13; // Ensure this scaling is appropriate
                    end else begin
                        scaled_shifted_a[i] <= {SCALED_SUM_COMPONENT_BITS{1'b0}};
                        scaled_shifted_b[i] <= {SCALED_SUM_COMPONENT_BITS{1'b0}};
                    end
                end
            end
        end // component_gen_block
    endgenerate

    // --- 6. Summation of Components (Pipelined) ---
    always @(posedge clk_i) begin
        if (!rstn_i) begin
            sum_val_a <= {SUM_COMPONENTS_BITS{1'b0}};
            sum_val_b <= {SUM_COMPONENTS_BITS{1'b0}};
        end else if (!gen_enable || output_zero) begin // Ensure output is zero if gen disabled or output_zero flag is set
            sum_val_a <= {SUM_COMPONENTS_BITS{1'b0}};
            sum_val_b <= {SUM_COMPONENTS_BITS{1'b0}};
        end else begin
            // Unrolled sum for NUM_COMPONENTS = 3. If NUM_COMPONENTS changes, this needs adjustment or a loop.
            sum_val_a <= scaled_shifted_a[0] + scaled_shifted_a[1] + scaled_shifted_a[2];
            sum_val_b <= scaled_shifted_b[0] + scaled_shifted_b[1] + scaled_shifted_b[2];
        end
    end

    // --- 7. DC Offset Addition (Pipelined) ---
    always @(posedge clk_i) begin
        if (!rstn_i) begin
            dac_pre_sat_a <= {PRE_SAT_BITS{1'b0}};
            dac_pre_sat_b <= {PRE_SAT_BITS{1'b0}};
        end else begin
            // Sign-extend overall_dc_offset before adding
            dac_pre_sat_a <= sum_val_a + $signed({{PRE_SAT_BITS-DACBITS{overall_dc_offset_a[DACBITS-1]}}, overall_dc_offset_a});
            dac_pre_sat_b <= sum_val_b + $signed({{PRE_SAT_BITS-DACBITS{overall_dc_offset_b[DACBITS-1]}}, overall_dc_offset_b});
        end
    end

    // --- 8. Saturation ---
    red_pitaya_saturate #(
        .BITS_IN (PRE_SAT_BITS),
        .SHIFT(0), // Assuming no additional shift before saturation
        .BITS_OUT(DACBITS)
    ) saturate_a (
       .input_i (dac_pre_sat_a),
       .output_o(dac_a_o_signed),
       .overflow() // overflow signal not used in this design
    );

    red_pitaya_saturate #(
        .BITS_IN (PRE_SAT_BITS),
        .SHIFT(0), // Assuming no additional shift before saturation
        .BITS_OUT(DACBITS)
    ) saturate_b (
       .input_i (dac_pre_sat_b),
       .output_o(dac_b_o_signed),
       .overflow() // overflow signal not used in this design
    );

endmodule

// Dummy module for red_pitaya_saturate if not defined elsewhere for compilation
// You should have the actual red_pitaya_saturate module available.
/*
module red_pitaya_saturate #(
    parameter BITS_IN  = 18,
    parameter SHIFT    = 0,
    parameter BITS_OUT = 14
) (
    input signed [BITS_IN-1:0] input_i,
    output signed [BITS_OUT-1:0] output_o,
    output overflow
);
    // Simplified saturation logic for placeholder
    localparam MAX_OUT = (1 << (BITS_OUT-1)) - 1;
    localparam MIN_OUT = -(1 << (BITS_OUT-1));
    reg signed [BITS_IN-1:0] shifted_input; // Not strictly needed if SHIFT is 0 or handled carefully

    // Apply shift (if any)
    // Note: Verilog >>> performs arithmetic shift on signed, >> performs logical shift
    // If SHIFT can be negative for left shift, care must be taken.
    // For SHIFT >= 0:
    assign shifted_input = (SHIFT >= 0) ? (input_i >>> SHIFT) : (input_i <<< (-SHIFT));


    assign output_o = (shifted_input > MAX_OUT) ? MAX_OUT :
                      (shifted_input < MIN_OUT) ? MIN_OUT :
                      shifted_input[BITS_OUT-1:0];
    assign overflow = (shifted_input > MAX_OUT) || (shifted_input < MIN_OUT);
endmodule
*/