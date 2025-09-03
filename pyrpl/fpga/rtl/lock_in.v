module lock_in #(
    parameter PHASEBITS = 32,
    parameter LUTBITS   = 17,
    parameter DATA_WIDTH = 14
) (
    input wire                          clk_i,
    input wire                          rstn_i,

    input wire signed [DATA_WIDTH-1:0]  adc_input_i,
    input wire signed [LUTBITS-1:0]     ref_signal_i,

    output reg signed [32-1:0]          filtered_output_o,
    output reg                          filtered_output_valid_o

    // Currently no system-bus interface implemented, as everything is hardcoded
);

localparam EXTEND_BITS_TO_32 = 32 - (DATA_WIDTH + LUTBITS);

// register the ADC input and reference signal
reg signed [DATA_WIDTH-1:0] adc_input_reg;
reg signed [LUTBITS -1:0] ref_signal_reg;

always @(posedge clk_i) begin
    if (!rstn_i) begin
        adc_input_reg  <= {DATA_WIDTH{1'b0}};
        ref_signal_reg <= {LUTBITS{1'b0}};
    end else begin
        adc_input_reg  <= adc_input_i;
        ref_signal_reg <= ref_signal_i;
    end
end

// multiply the ADC input with the reference signal
reg signed [DATA_WIDTH+LUTBITS-1:0] product_adc_ref;

always @(posedge clk_i) begin
    if (!rstn_i) begin
        product_adc_ref <= {DATA_WIDTH+LUTBITS{1'b0}};
    end else begin
        product_adc_ref <= adc_input_reg * ref_signal_reg;
    end
end

// Extend the product to 32 bits for directing it to the CIC decimator
wire signed [31:0] product_adc_ref_32_bit;
assign product_adc_ref_32_bit = {{EXTEND_BITS_TO_32{product_adc_ref[DATA_WIDTH+LUTBITS-1]}}, product_adc_ref};

wire signed [39:0]  decimator_output;
wire                dec_m_axis_data_tvalid;

cic_decimate_by_4096 cic_decimate_instance   (
  .aclk(clk_i),                                 // input wire aclk
  .s_axis_data_tdata(product_adc_ref_32_bit),   // input wire [31 : 0] s_axis_data_tdata.
  .s_axis_data_tvalid(1'b1),                    // input wire s_axis_data_tvalid
  .s_axis_data_tready(),                        // output wire s_axis_data_tready
  .m_axis_data_tdata(decimator_output),         // output wire [39 : 0] m_axis_data_tdata
  .m_axis_data_tvalid(dec_m_axis_data_tvalid)   // output wire m_axis_data_tvalid
);

wire signed [31:0]  fir_output;
wire                fir_m_axis_data_tvalid;

fir_lowpass_500Hz fir_lowpass_inst (
  .aclk(clk_i),                                 // input wire aclk
  .s_axis_data_tvalid(dec_m_axis_data_tvalid),  // input wire s_axis_data_tvalid
  .s_axis_data_tready(),                        // output wire s_axis_data_tready
  .s_axis_data_tdata(decimator_output),         // input wire [39 : 0] s_axis_data_tdata
  .m_axis_data_tvalid(fir_m_axis_data_tvalid),  // output wire m_axis_data_tvalid
  .m_axis_data_tdata(fir_output)                // output wire [31 : 0] m_axis_data_tdata
);

always @(posedge clk_i) begin
    if (!rstn_i) begin
        filtered_output_o <= 32'b0;
        filtered_output_valid_o <= 1'b0;
    end else begin
        filtered_output_o <= fir_output;
        filtered_output_valid_o <= fir_m_axis_data_tvalid;
    end
end
    
endmodule