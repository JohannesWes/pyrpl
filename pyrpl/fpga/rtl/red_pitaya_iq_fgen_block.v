//`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Company: LKB
// Engineer: Leonhard Neuhaus
// 
// Create Date: 27.11.2014 14:15:43
// Design Name: 
// Module Name: red_pitaya_iq_fgen_block
// Project Name: 
// Target Devices: 
// Tool Versions: 
// Description: 
//  
// This module outputs 4 signed signals: 
// sin(f*t), cos(f*t),sin(f*t+phi), cos(f*t+phi)
// The maximally positive signal is 2**(LUTBITS-1)-1
// The maximally negative signal is -(2**(LUTBITS-1)-1)
// 
// Revision:
// Revision 0.01 - File Created
// Additional Comments:
// 
//////////////////////////////////////////////////////////////////////////////////
/*
###############################################################################
#    pyrpl - DSP servo controller for quantum optics with the RedPitaya
#    Copyright (C) 2014-2016  Leonhard Neuhaus  (neuhaus@spectro.jussieu.fr)
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
############################################################################### 
*/


module red_pitaya_iq_fgen_block #(
    parameter     LUTSZ     = 11,  
    parameter     LUTBITS   = 17,  
    parameter     PHASEBITS = 32 
)
(
    input clk_i,
    input rstn_i,
    input on,
    input sin_at_2f,
    input cos_at_2f,
    input sin_shifted_at_2f,
    input cos_shifted_at_2f,

    input  [PHASEBITS-1:0] start_phase,
    input  [PHASEBITS-1:0] shift_phase,
    output [LUTBITS-1:0] sin_o      ,
    output [LUTBITS-1:0] cos_o      ,
    output [LUTBITS-1:0] sin_shifted_o      ,
    output [LUTBITS-1:0] cos_shifted_o      ,
    output [PHASEBITS-1:0] phase_o // Output current phase - either of sin or sin_shifted
);

reg [LUTBITS-1:0] sin;
reg [LUTBITS-1:0] cos;
reg [LUTBITS-1:0] sin_shifted;
reg [LUTBITS-1:0] cos_shifted;
reg [PHASEBITS-1:0] phase_o_reg;

assign sin_o = sin;
assign sin_shifted_o = sin_shifted;
assign cos_o = cos;
assign cos_shifted_o = cos_shifted;

localparam QSHIFT = {{PHASEBITS-1{1'b0}},1'b1}  << (PHASEBITS-2);

//lut data block
reg [LUTBITS-1-1:0] lutrom [0:(1<<LUTSZ)-1];
reg [PHASEBITS-1:0] phase;

wire [PHASEBITS-1:0] wwphase1;
wire [PHASEBITS-1:0] wwphase2;
wire [PHASEBITS-1:0] wwphase3;
wire [PHASEBITS-1:0] wwphase4;
wire [LUTBITS-1:0] wphase1;
wire [LUTBITS-1:0] wphase2;
wire [LUTBITS-1:0] wphase3;
wire [LUTBITS-1:0] wphase4;
wire invertphase1;
wire invertphase2;
wire invertphase3;
wire invertphase4;
wire invertsignal1;
wire invertsignal2;
wire invertsignal3;
wire invertsignal4;
reg invertsignal1_reg;
reg invertsignal2_reg;
reg invertsignal3_reg;
reg invertsignal4_reg;
reg invertsignal1_reg_reg;
reg invertsignal2_reg_reg;
reg invertsignal3_reg_reg;
reg invertsignal4_reg_reg;
reg [LUTBITS-1:0] sin_reg;
reg [LUTBITS-1:0] cos_reg;
reg [LUTBITS-1:0] sin_shifted_reg;
reg [LUTBITS-1:0] cos_shifted_reg;

reg [LUTBITS-1:0] phase1;
reg [LUTBITS-1:0] phase2;
reg [LUTBITS-1:0] phase3;
reg [LUTBITS-1:0] phase4;

// in clock cycle n-1, phase is incremented (see far below in the else-block
// in clock cycle n,
//     - we assign wwphase1-4, the phase of the 4 subgenerators (possibly doubled phase counter for generation at 2f)
//     - we assign wphase1-4,
//     - we assign invertphase1-4, the flag telling if we are in the rising or falling quadrant of the sine
//     - we assign invertsignal1-4, the flag telling if we in the positive or negative sign of the sine

assign wwphase1 = sin_at_2f ? {phase[PHASEBITS-1:0],1'b0} : phase; // wwphase1 ready in cycle n
assign wwphase2 = cos_at_2f ? {phase[PHASEBITS-1:0],1'b0} + QSHIFT : phase + QSHIFT;
assign wwphase3 = sin_shifted_at_2f ? {phase[PHASEBITS-1:0],1'b0} + start_phase : phase + start_phase;
assign wwphase4 = cos_shifted_at_2f ? {phase[PHASEBITS-1:0],1'b0} + start_phase + QSHIFT : phase + start_phase + QSHIFT;
assign wphase1 = wwphase1[PHASEBITS-2-1:PHASEBITS-2-LUTSZ]; //wwphase1 ready in cycle n
assign wphase2 = wwphase2[PHASEBITS-2-1:PHASEBITS-2-LUTSZ];
assign wphase3 = wwphase3[PHASEBITS-2-1:PHASEBITS-2-LUTSZ];
assign wphase4 = wwphase4[PHASEBITS-2-1:PHASEBITS-2-LUTSZ];
assign invertphase1  = wwphase1[PHASEBITS-1-1];  //invertphase1 ready in cycle n
assign invertphase2  = wwphase2[PHASEBITS-1-1]; 
assign invertphase3  = wwphase3[PHASEBITS-1-1]; 
assign invertphase4  = wwphase4[PHASEBITS-1-1]; 
assign invertsignal1 = wwphase1[PHASEBITS-1]; //invertsignal1 ready in cycle n
assign invertsignal2 = wwphase2[PHASEBITS-1];
assign invertsignal3 = wwphase3[PHASEBITS-1];
assign invertsignal4 = wwphase4[PHASEBITS-1];

assign phase_o = phase_o_reg;

//main loop
always @(posedge clk_i) begin
    // 1) Next-phase computations:
    phase1 <= invertphase1 ? (~wphase1) : wphase1;  //phase1 ready in cycle n+1
    phase2 <= invertphase2 ? (~wphase2) : wphase2;
    phase3 <= invertphase3 ? (~wphase3) : wphase3;
    phase4 <= invertphase4 ? (~wphase4) : wphase4;

    // 2) Register the sign bits
    invertsignal1_reg <= invertsignal1; //invertsignal1_reg ready in cycle n+1
    invertsignal2_reg <= invertsignal2;
    invertsignal3_reg <= invertsignal3;
    invertsignal4_reg <= invertsignal4;
    invertsignal1_reg_reg <= invertsignal1_reg; //invertsignal1_reg_reg ready in cycle n+2
    invertsignal2_reg_reg <= invertsignal2_reg;
    invertsignal3_reg_reg <= invertsignal3_reg;
    invertsignal4_reg_reg <= invertsignal4_reg;

    // 3) Read LUT outputs for the next cycle
    sin_reg <= lutrom[phase1];    //sin_reg ready in cycle n+2
    cos_reg <= lutrom[phase2];
    sin_shifted_reg <= lutrom[phase3];
    cos_shifted_reg <= lutrom[phase4];

    // 4) Reset vs. Running State
    if (on==1'b0) begin // TODO: additional reset condition for syncing with ASG period possibly
        phase <= {PHASEBITS{1'b0}};
        phase_o_reg <= {PHASEBITS{1'b0}};
        sin <= {LUTBITS{1'b0}};
        cos <= {LUTBITS{1'b0}};
        sin_shifted <= {LUTBITS{1'b0}};
        cos_shifted <= {LUTBITS{1'b0}};
    end 
    else begin
        // 5) Advance the phase accumulator 
        phase <= phase + shift_phase; // new phase is ready in (arbitrary) cycle n

        // 6) Invert sign if needed
        sin <= invertsignal1_reg_reg ? ((~sin_reg)+'b1) : sin_reg;    //sin ready in cycle n+3  - based purely on signals that are ready in cycle n+2 -> timing correct, latency 3 cycles
        cos <= invertsignal2_reg_reg ? ((~cos_reg)+'b1) : cos_reg; 
        sin_shifted <= invertsignal3_reg_reg ? ((~sin_shifted_reg)+'b1) : sin_shifted_reg;  
        cos_shifted <= invertsignal4_reg_reg ? ((~cos_shifted_reg)+'b1) : cos_shifted_reg;
        phase_o_reg <= phase + shift_phase;
    end
end

//LUT ROM
//created by this (unpolished) python code:
/*
LUTSZ = 11     #number of LUT entries
LUTBITS = 17   #LUT word size
def from_pyint(v,bitlength=14):
    v = int(v)
    if v < 0:
        v = v + 2**bitlength
    v= (v & (2**bitlength-1))
    return int(v)
data = np.zeros(2**LUTSZ,dtype=np.long)
for i in range(len(data)):
    data[i] = np.long(np.round((2**(LUTBITS-1)-1)*np.sin(((float(i)+0.5)/len(data))*0.5*np.pi)))
data = [from_pyint(v,bitlength=LUTBITS) for v in data]
plot(data)

print "reg [LUTBITS-1-1:0] lutrom [0:(1<<LUTSZ)-1];"
print ""
print "initial begin"
for i in range(len(data)):
    string = "   lutrom["+str(i)+"] = "+str(LUTBITS-1)+"'d"+str(data[i])+";"
    print string
print "end"
*/ 


initial begin
   lutrom[0] = 16'd25;
   lutrom[1] = 16'd75;
   lutrom[2] = 16'd126;
   lutrom[3] = 16'd176;
   lutrom[4] = 16'd226;
   lutrom[5] = 16'd276;
   ....
   lutrom[2037] = 16'd65533;
   lutrom[2038] = 16'd65533;
   lutrom[2039] = 16'd65534;
   lutrom[2040] = 16'd65534;
   lutrom[2041] = 16'd65534;
   lutrom[2042] = 16'd65534;
   lutrom[2043] = 16'd65535;
   lutrom[2044] = 16'd65535;
   lutrom[2045] = 16'd65535;
   lutrom[2046] = 16'd65535;
   lutrom[2047] = 16'd65535;
end

endmodule
