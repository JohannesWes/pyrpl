`timescale 1ns/1ps

// Fixed-point scaling check for the two selectable lock-in FIR paths.
module tb_fir_dc_gain;
  reg clk = 1'b0;
  reg rstn = 1'b0;
  reg valid = 1'b0;
  wire ready_min, ready_lin;
  wire valid_min, valid_lin_raw, valid_lin;
  wire signed [31:0] data_min, data_lin_raw, data_lin;
  localparam signed [39:0] CONSTANT_INPUT = 40'sd1073741824; // 2^30

  always #4 clk = ~clk;

  fir_lowpass_2000Hz minphase (
    .aclk(clk), .aclken(1'b1),
    .s_axis_data_tvalid(valid), .s_axis_data_tready(ready_min),
    .s_axis_data_tdata(CONSTANT_INPUT),
    .m_axis_data_tvalid(valid_min), .m_axis_data_tdata(data_min));

  fir_linear_phase_2000Hz linear_raw (
    .aclk(clk), .aclken(1'b1),
    .s_axis_data_tvalid(valid), .s_axis_data_tready(ready_lin),
    .s_axis_data_tdata(CONSTANT_INPUT),
    .m_axis_data_tvalid(valid_lin_raw), .m_axis_data_tdata(data_lin_raw));

  fir_gain_compensation linear_gain (
    .clk_i(clk), .rstn_i(rstn), .aclken_i(1'b1),
    .data_i(data_lin_raw), .data_valid_i(valid_lin_raw),
    .data_o(data_lin), .data_valid_o(valid_lin));

  integer accepted = 0;
  integer out_min = 0;
  integer out_lin = 0;
  always @(posedge clk) begin
    if (valid && ready_min && ready_lin)
      accepted <= accepted + 1;
    if (valid_min)
      out_min <= out_min + 1;
    if (valid_lin)
      out_lin <= out_lin + 1;
    if (out_min >= 170 && out_lin >= 170) begin
      $display("RESULT min=%0d linear=%0d input=%0d", data_min, data_lin,
               CONSTANT_INPUT);
      $finish;
    end
  end

  initial begin
    repeat (10) @(posedge clk);
    rstn <= 1'b1;
    valid <= 1'b1;
    wait (accepted >= 220);
    valid <= 1'b0;
    #200000;
    $fatal(1, "Timed out waiting for FIR outputs");
  end
endmodule
