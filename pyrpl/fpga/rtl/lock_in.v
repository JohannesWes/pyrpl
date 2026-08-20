module lock_in #(
    parameter PHASEBITS = 32,
    parameter LUTBITS   = 17,
    parameter DATA_WIDTH = 14
) (
    input wire                          clk_i,
    input wire                          rstn_i,

    // Clock-enable / freeze gate for CIC and FIR chain
    input wire                          aclken_i,

    input wire signed [DATA_WIDTH-1:0]  adc_input_i,
    input wire signed [LUTBITS-1:0]     ref_signal_sin_i,
    input wire signed [LUTBITS-1:0]     ref_signal_cos_i,
    input wire signed [LUTBITS-1:0]     ref_signal_sin_shifted_i,
    input wire signed [LUTBITS-1:0]     ref_signal_cos_shifted_i,

    output reg signed [32-1:0]          filtered_output1_o,
    output reg                          filtered_output1_valid_o,
    output reg signed [32-1:0]          filtered_output2_o,
    output reg                          filtered_output2_valid_o,

    // System bus interface
    input      [32-1:0]                 sys_addr,
    input      [32-1:0]                 sys_wdata,
    input      [ 4-1:0]                 sys_sel,
    input                               sys_wen,
    input                               sys_ren,
    output reg [32-1:0]                 sys_rdata,
    output reg                          sys_err,
    output reg                          sys_ack
);

localparam EXTEND_BITS_TO_32 = 32 - (DATA_WIDTH + LUTBITS);
localparam [1:0] FILTER_2KHZ_MINIMUM_PHASE = 2'd0;
localparam [1:0] FILTER_2KHZ_LINEAR_PHASE  = 2'd1;

// Maximum positive value for signed LUTBITS-wide number: 2^(LUTBITS-1) - 1
// Used as fixed reference when demodulation is bypassed (DC ODMR mode),
// to maintain the same gain scaling as normal lock-in demodulation.
localparam signed [LUTBITS-1:0] BYPASS_CONSTANT = {1'b0, {(LUTBITS-1){1'b1}}};  // = 65535

// Control registers for system bus interface
reg [1:0]  ref_select1;        // Channel 1: 0=sin, 1=cos, 2=sin_shifted, 3=cos_shifted
reg [1:0]  ref_select2;        // Channel 2: 0=sin, 1=cos, 2=sin_shifted, 3=cos_shifted
reg        fir_bypass_ch1;     // Bypass FIR for channel 1 (use CIC output directly)
reg        fir_bypass_ch2;     // Bypass FIR for channel 2 (use CIC output directly)
reg [1:0]  filter_select_ch1;  // Channel 1: 0=2kHz minimum phase, 1=2kHz linear phase
reg [1:0]  filter_select_ch2;  // Channel 2: 0=2kHz minimum phase, 1=2kHz linear phase
reg        demod_bypass_ch1;   // Bypass demodulation for channel 1 (DC passthrough mode)
reg        demod_bypass_ch2;   // Bypass demodulation for channel 2 (DC passthrough mode)

// Multiplexers for reference signal selection
wire signed [LUTBITS-1:0] ref_signal_selected1;
wire signed [LUTBITS-1:0] ref_signal_selected2;

assign ref_signal_selected1 = (ref_select1 == 2'd0) ? ref_signal_sin_i :
                              (ref_select1 == 2'd1) ? ref_signal_cos_i :
                              (ref_select1 == 2'd2) ? ref_signal_sin_shifted_i :
                                                       ref_signal_cos_shifted_i;

assign ref_signal_selected2 = (ref_select2 == 2'd0) ? ref_signal_sin_i :
                              (ref_select2 == 2'd1) ? ref_signal_cos_i :
                              (ref_select2 == 2'd2) ? ref_signal_sin_shifted_i :
                                                       ref_signal_cos_shifted_i;

// register the ADC input and reference signals
reg signed [DATA_WIDTH-1:0] adc_input_reg1;
reg signed [DATA_WIDTH-1:0] adc_input_reg2;
reg signed [LUTBITS -1:0] ref_signal_reg1;
reg signed [LUTBITS -1:0] ref_signal_reg2;

always @(posedge clk_i) begin
    if (!rstn_i) begin
        adc_input_reg1  <= {DATA_WIDTH{1'b0}};
        ref_signal_reg1 <= {LUTBITS{1'b0}};
        adc_input_reg2  <= {DATA_WIDTH{1'b0}};
        ref_signal_reg2 <= {LUTBITS{1'b0}};
    end else begin
        adc_input_reg1  <= adc_input_i;
        adc_input_reg2  <= adc_input_i;
        ref_signal_reg1 <= demod_bypass_ch1 ? BYPASS_CONSTANT : ref_signal_selected1;
        ref_signal_reg2 <= demod_bypass_ch2 ? BYPASS_CONSTANT : ref_signal_selected2;
    end
end

// multiply the ADC input with the reference signal
reg signed [DATA_WIDTH+LUTBITS-1:0] product_adc_ref1;
reg signed [DATA_WIDTH+LUTBITS-1:0] product_adc_ref2;

always @(posedge clk_i) begin
    if (!rstn_i) begin
        product_adc_ref1 <= {DATA_WIDTH+LUTBITS{1'b0}};
        product_adc_ref2 <= {DATA_WIDTH+LUTBITS{1'b0}};
    end else begin
        product_adc_ref1 <= adc_input_reg1 * ref_signal_reg1;
        product_adc_ref2 <= adc_input_reg2 * ref_signal_reg2;
    end
end

// Extend the product to 32 bits for directing it to the CIC decimator
wire signed [31:0] product_adc_ref_32_bit1;
wire signed [31:0] product_adc_ref_32_bit2;
assign product_adc_ref_32_bit1 = {{EXTEND_BITS_TO_32{product_adc_ref1[DATA_WIDTH+LUTBITS-1]}}, product_adc_ref1};
assign product_adc_ref_32_bit2 = {{EXTEND_BITS_TO_32{product_adc_ref2[DATA_WIDTH+LUTBITS-1]}}, product_adc_ref2};

// CIC decimator outputs
wire signed [39:0]  decimator_output1;
wire signed [39:0]  decimator_output2;
wire                dec_m_axis_data_tvalid1;
wire                dec_m_axis_data_tvalid2;

// Truncate CIC 40-bit output to 32-bit for bypass path
// Drop 8 LSBs to match FIR's effective precision, preserve sign bit
wire signed [31:0]  decimator_output1_32bit;
wire signed [31:0]  decimator_output2_32bit;
assign decimator_output1_32bit = decimator_output1[39:8];
assign decimator_output2_32bit = decimator_output2[39:8];

// CIC decimator instance - Channel 1
cic_decimate_by_4096 cic_decimate_instance_ch1 (
  .aclk(clk_i),                                  // input wire aclk
  .aclken(aclken_i),                             // input wire aclken (freeze gate)
  .s_axis_data_tdata(product_adc_ref_32_bit1),   // input wire [31 : 0] s_axis_data_tdata.
  .s_axis_data_tvalid(1'b1),                     // input wire s_axis_data_tvalid
  .s_axis_data_tready(),                         // output wire s_axis_data_tready
  .m_axis_data_tdata(decimator_output1),         // output wire [39 : 0] m_axis_data_tdata
  .m_axis_data_tvalid(dec_m_axis_data_tvalid1)   // output wire m_axis_data_tvalid
);

// CIC decimator instance - Channel 2
cic_decimate_by_4096 cic_decimate_instance_ch2 (
  .aclk(clk_i),                                  // input wire aclk
  .aclken(aclken_i),                             // input wire aclken (freeze gate)
  .s_axis_data_tdata(product_adc_ref_32_bit2),   // input wire [31 : 0] s_axis_data_tdata.
  .s_axis_data_tvalid(1'b1),                     // input wire s_axis_data_tvalid
  .s_axis_data_tready(),                         // output wire s_axis_data_tready
  .m_axis_data_tdata(decimator_output2),         // output wire [39 : 0] m_axis_data_tdata
  .m_axis_data_tvalid(dec_m_axis_data_tvalid2)   // output wire m_axis_data_tvalid
);

// Both FIRs run continuously from the same CIC output.  Selection therefore
// does not reset or cold-start a filter, and aclken freezes both paths together.
wire signed [31:0]  fir_2kHz_minphase_output1;
wire signed [31:0]  fir_2kHz_minphase_output2;
wire                fir_2kHz_minphase_valid1;
wire                fir_2kHz_minphase_valid2;
wire signed [31:0]  fir_2kHz_linear_raw_output1;
wire signed [31:0]  fir_2kHz_linear_raw_output2;
wire                fir_2kHz_linear_raw_valid1;
wire                fir_2kHz_linear_raw_valid2;

// FIR lowpass 2000Hz instance - Channel 1
fir_lowpass_2000Hz fir_lowpass_2000Hz_inst_ch1 (
  .aclk(clk_i),                                  // input wire aclk
  .aclken(aclken_i),                             // input wire aclken (freeze gate)
  .s_axis_data_tvalid(dec_m_axis_data_tvalid1),  // input wire s_axis_data_tvalid
  .s_axis_data_tready(),                         // output wire s_axis_data_tready
  .s_axis_data_tdata(decimator_output1),         // input wire [39 : 0] s_axis_data_tdata
  .m_axis_data_tvalid(fir_2kHz_minphase_valid1), // output wire m_axis_data_tvalid
  .m_axis_data_tdata(fir_2kHz_minphase_output1)  // output wire [31 : 0] m_axis_data_tdata
);

// FIR lowpass 2000Hz instance - Channel 2
fir_lowpass_2000Hz fir_lowpass_2000Hz_inst_ch2 (
  .aclk(clk_i),                                  // input wire aclk
  .aclken(aclken_i),                             // input wire aclken (freeze gate)
  .s_axis_data_tvalid(dec_m_axis_data_tvalid2),  // input wire s_axis_data_tvalid
  .s_axis_data_tready(),                         // output wire s_axis_data_tready
  .s_axis_data_tdata(decimator_output2),         // input wire [39 : 0] s_axis_data_tdata
  .m_axis_data_tvalid(fir_2kHz_minphase_valid2), // output wire m_axis_data_tvalid
  .m_axis_data_tdata(fir_2kHz_minphase_output2)  // output wire [31 : 0] m_axis_data_tdata
);

// CIC-compensated 2 kHz linear-phase FIR - Channel 1
fir_linear_phase_2000Hz fir_linear_phase_2000Hz_inst_ch1 (
  .aclk(clk_i),
  .aclken(aclken_i),
  .s_axis_data_tvalid(dec_m_axis_data_tvalid1),
  .s_axis_data_tready(),
  .s_axis_data_tdata(decimator_output1),
  .m_axis_data_tvalid(fir_2kHz_linear_raw_valid1),
  .m_axis_data_tdata(fir_2kHz_linear_raw_output1)
);

// CIC-compensated 2 kHz linear-phase FIR - Channel 2
fir_linear_phase_2000Hz fir_linear_phase_2000Hz_inst_ch2 (
  .aclk(clk_i),
  .aclken(aclken_i),
  .s_axis_data_tvalid(dec_m_axis_data_tvalid2),
  .s_axis_data_tready(),
  .s_axis_data_tdata(decimator_output2),
  .m_axis_data_tvalid(fir_2kHz_linear_raw_valid2),
  .m_axis_data_tdata(fir_2kHz_linear_raw_output2)
);

// Restore the established minimum-phase raw gain after scaling the symmetric
// coefficient set to the DSP48E1-native 18-bit width.
wire signed [31:0] fir_2kHz_linear_output1;
wire signed [31:0] fir_2kHz_linear_output2;
wire               fir_2kHz_linear_valid1;
wire               fir_2kHz_linear_valid2;

fir_gain_compensation fir_gain_compensation_inst_ch1 (
  .clk_i(clk_i),
  .rstn_i(rstn_i),
  .aclken_i(aclken_i),
  .data_i(fir_2kHz_linear_raw_output1),
  .data_valid_i(fir_2kHz_linear_raw_valid1),
  .data_o(fir_2kHz_linear_output1),
  .data_valid_o(fir_2kHz_linear_valid1)
);

fir_gain_compensation fir_gain_compensation_inst_ch2 (
  .clk_i(clk_i),
  .rstn_i(rstn_i),
  .aclken_i(aclken_i),
  .data_i(fir_2kHz_linear_raw_output2),
  .data_valid_i(fir_2kHz_linear_raw_valid2),
  .data_o(fir_2kHz_linear_output2),
  .data_valid_o(fir_2kHz_linear_valid2)
);

// Value 0 is the reset/default minimum-phase path. Values 2 and 3 are reserved
// and also safely fall back to minimum phase.
wire signed [31:0] fir_selected_output1;
wire signed [31:0] fir_selected_output2;
wire               fir_selected_valid1;
wire               fir_selected_valid2;

assign fir_selected_output1 = (filter_select_ch1 == FILTER_2KHZ_LINEAR_PHASE) ?
                              fir_2kHz_linear_output1 : fir_2kHz_minphase_output1;
assign fir_selected_output2 = (filter_select_ch2 == FILTER_2KHZ_LINEAR_PHASE) ?
                              fir_2kHz_linear_output2 : fir_2kHz_minphase_output2;
assign fir_selected_valid1 = (filter_select_ch1 == FILTER_2KHZ_LINEAR_PHASE) ?
                             fir_2kHz_linear_valid1 : fir_2kHz_minphase_valid1;
assign fir_selected_valid2 = (filter_select_ch2 == FILTER_2KHZ_LINEAR_PHASE) ?
                             fir_2kHz_linear_valid2 : fir_2kHz_minphase_valid2;

// Select between the chosen FIR output and the truncated CIC output (bypass flag)
wire signed [31:0] selected_output1;
wire signed [31:0] selected_output2;
wire               selected_valid1;
wire               selected_valid2;

assign selected_output1 = fir_bypass_ch1 ? decimator_output1_32bit : fir_selected_output1;
assign selected_output2 = fir_bypass_ch2 ? decimator_output2_32bit : fir_selected_output2;
assign selected_valid1  = fir_bypass_ch1 ? dec_m_axis_data_tvalid1 : fir_selected_valid1;
assign selected_valid2  = fir_bypass_ch2 ? dec_m_axis_data_tvalid2 : fir_selected_valid2;

// Output register assignment - Channel 1
always @(posedge clk_i) begin
    if (!rstn_i) begin
        filtered_output1_o <= 32'b0;
        filtered_output1_valid_o <= 1'b0;
    end else begin
        filtered_output1_o <= selected_output1;
        filtered_output1_valid_o <= selected_valid1 & aclken_i;
    end
end

// Output register assignment - Channel 2
always @(posedge clk_i) begin
    if (!rstn_i) begin
        filtered_output2_o <= 32'b0;
        filtered_output2_valid_o <= 1'b0;
    end else begin
        filtered_output2_o <= selected_output2;
        filtered_output2_valid_o <= selected_valid2 & aclken_i;
    end
end

// System bus interface - write logic
// Register map:
//   0x000: ref_select1 (bits 1:0), ref_select2 (bits 3:2),
//          fir_bypass_ch1 (bit 4), fir_bypass_ch2 (bit 5),
//          filter_select_ch1 (bits 7:6), filter_select_ch2 (bits 9:8)
always @(posedge clk_i) begin
    if (!rstn_i) begin
        ref_select1 <= 2'd0;     // Default: sin
        ref_select2 <= 2'd1;     // Default: cos
        fir_bypass_ch1 <= 1'b0;  // Default: use FIR (bypass OFF)
        fir_bypass_ch2 <= 1'b0;  // Default: use FIR (bypass OFF)
        filter_select_ch1 <= FILTER_2KHZ_MINIMUM_PHASE;
        filter_select_ch2 <= FILTER_2KHZ_MINIMUM_PHASE;
        demod_bypass_ch1 <= 1'b0;  // Default: demodulation ON (lock-in mode)
        demod_bypass_ch2 <= 1'b0;  // Default: demodulation ON (lock-in mode)
    end else begin
        if (sys_wen) begin
            if (sys_addr[19:0] == 20'h00000) begin
                ref_select1 <= sys_wdata[1:0];
                ref_select2 <= sys_wdata[3:2];
                fir_bypass_ch1 <= sys_wdata[4];
                fir_bypass_ch2 <= sys_wdata[5];
                filter_select_ch1 <= sys_wdata[7:6];
                filter_select_ch2 <= sys_wdata[9:8];
                demod_bypass_ch1 <= sys_wdata[10];
                demod_bypass_ch2 <= sys_wdata[11];
            end
        end
    end
end

// System bus interface - read logic
wire sys_en;
assign sys_en = sys_wen | sys_ren;

always @(posedge clk_i) begin
    if (!rstn_i) begin
        sys_err <= 1'b0;
        sys_ack <= 1'b0;
    end else begin
        sys_err <= 1'b0;
        casez (sys_addr[19:0])
            20'h00000: begin
                sys_ack   <= sys_en;
                sys_rdata <= {20'b0, demod_bypass_ch2, demod_bypass_ch1, filter_select_ch2, filter_select_ch1, fir_bypass_ch2, fir_bypass_ch1, ref_select2, ref_select1};
            end
            default: begin
                sys_ack   <= sys_en;
                sys_rdata <= 32'h0;
            end
        endcase
    end
end

endmodule
