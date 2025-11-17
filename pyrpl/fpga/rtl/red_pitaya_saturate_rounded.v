/**
 * 
 * Saturating arithmetic with rounding for improved SNR
 * Adds 0.5 LSB before truncation (reduces quantization noise by ~6dB)
 * 
 *
 */

module red_pitaya_saturate_rounded
#( 
    parameter BITS_IN  = 24,
    parameter BITS_OUT = 14,
    parameter SHIFT    = 10
)
(
    input  signed [BITS_IN-1:0]  input_i,
    output signed [BITS_OUT-1:0] output_o,
    output                       overflow
);

//-----------------------------------------------------------------------------
// Rounding: Add 0.5 LSB (2^(SHIFT-1)) before shifting
//-----------------------------------------------------------------------------
wire signed [BITS_IN-1:0] rounded;
assign rounded = input_i + (1 << (SHIFT-1));

//-----------------------------------------------------------------------------
// Saturation with overflow detection
//-----------------------------------------------------------------------------
assign {output_o, overflow} = 
    ({rounded[BITS_IN-1], |rounded[BITS_IN-2:SHIFT+BITS_OUT-1]} == 2'b01) ? 
        {{1'b0, {BITS_OUT-1{1'b1}}}, 1'b1} :
    ({rounded[BITS_IN-1], &rounded[BITS_IN-2:SHIFT+BITS_OUT-1]} == 2'b10) ? 
        {{1'b1, {BITS_OUT-1{1'b0}}}, 1'b1} :
    {rounded[SHIFT+BITS_OUT-1:SHIFT], 1'b0};

endmodule