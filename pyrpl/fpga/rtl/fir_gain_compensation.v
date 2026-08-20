// Pipelined gain correction for the 18-bit 2 kHz linear-phase FIR.
//
// The coefficient scale 802861 is corrected to the existing minimum-phase
// raw scale 937718 using only shifts and adds:
//   1 + 2^-3 + 2^-5 + 2^-7 + 2^-8 = 1.16796875.
module fir_gain_compensation (
    input  wire               clk_i,
    input  wire               rstn_i,
    input  wire               aclken_i,
    input  wire signed [31:0] data_i,
    input  wire               data_valid_i,
    output wire signed [31:0] data_o,
    output reg                data_valid_o
);

wire signed [32:0] data_extended = {data_i[31], data_i};
reg signed [32:0] gain_a;
reg signed [32:0] gain_b;
reg signed [32:0] gain_c;
reg signed [32:0] gain_ab;
reg signed [32:0] gain_c_delayed;
reg signed [32:0] gain_result;
reg valid_stage1;
reg valid_stage2;

always @(posedge clk_i) begin
    if (!rstn_i) begin
        gain_a <= 33'sd0;
        gain_b <= 33'sd0;
        gain_c <= 33'sd0;
        gain_ab <= 33'sd0;
        gain_c_delayed <= 33'sd0;
        gain_result <= 33'sd0;
        valid_stage1 <= 1'b0;
        valid_stage2 <= 1'b0;
        data_valid_o <= 1'b0;
    end else if (aclken_i) begin
        gain_a <= data_extended + (data_extended >>> 3);
        gain_b <= (data_extended >>> 5) + (data_extended >>> 7);
        gain_c <= data_extended >>> 8;
        valid_stage1 <= data_valid_i;

        gain_ab <= gain_a + gain_b;
        gain_c_delayed <= gain_c;
        valid_stage2 <= valid_stage1;

        gain_result <= gain_ab + gain_c_delayed;
        data_valid_o <= valid_stage2;
    end
end

// Saturate the corrected 33-bit result to the common FIR output width.
wire overflow = gain_result[32] ^ gain_result[31];
assign data_o = overflow ?
    (gain_result[32] ? 32'sh80000000 : 32'sh7fffffff) :
    gain_result[31:0];

endmodule
