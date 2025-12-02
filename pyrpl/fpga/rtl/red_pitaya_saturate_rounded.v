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

// Saturation limits
localparam signed [BITS_OUT-1:0] MAX_VAL = {1'b0, {(BITS_OUT-1){1'b1}}};
localparam signed [BITS_OUT-1:0] MIN_VAL = {1'b1, {(BITS_OUT-1){1'b0}}};

// Round and shift
wire signed [BITS_IN-1:0] rounded;
wire signed [BITS_IN-1:0] shifted;
assign rounded = input_i + (1 << (SHIFT-1));
assign shifted = rounded >>> SHIFT;

// Check for overflow
wire pos_overflow = (shifted > {{(BITS_IN-BITS_OUT){1'b0}}, MAX_VAL});
wire neg_overflow = (shifted < {{(BITS_IN-BITS_OUT){1'b1}}, MIN_VAL});

// Output with saturation
assign output_o = pos_overflow ? MAX_VAL :
                  neg_overflow ? MIN_VAL :
                  shifted[BITS_OUT-1:0];

assign overflow = pos_overflow | neg_overflow;

endmodule