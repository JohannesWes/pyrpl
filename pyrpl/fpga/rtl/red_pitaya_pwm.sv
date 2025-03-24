/**
 * @brief Red Pitaya PWM module
 *
 * @Author Matej Oblak
 *
 * (c) Red Pitaya  http://www.redpitaya.com
 *
 * This part of code is written in Verilog hardware description language (HDL).
 * Please visit http://en.wikipedia.org/wiki/Verilog
 * for more details on the language used herein.
 */
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

module red_pitaya_pwm #(
  int unsigned CCW = 24,  // configuration counter width (resolution)
  bit  [8-1:0] FULL = 8'd255 // 100% value - we increase it for total 12bit resolution
)(
  // system signals
  input  logic           clk ,  // clock
  input  logic           rstn,  // reset
  // configuration
  input  logic [CCW-1:0] cfg ,  // duty cycle configuration / signal
  input  logic [16-1:0]  freq_div,
  input  logic           mode_select,  // 0 = normal mode, 1 = elaborate mode 
  
  // PWM outputs
  output logic           pwm_o ,  // PWM output - driving RC
  output logic           pwm_s    // PWM synchronization
);

// Clock divider - for frequency control
reg  [16-1:0] div_counter;
reg           clk_enable;

// main part of PWM
reg  [ 4-1: 0] bcnt  ;
reg  [16-1: 0] b     ;
reg  [ 8-1: 0] vcnt, vcnt_r;
reg  [ 8-1: 0] v   ;
reg  [ 9-1: 0] v_r ; // needs an extra bit to avoid overflow

// Clock divider logic
always @(posedge clk)
if (~rstn) begin
  div_counter <= 16'h0;
  clk_enable <= 1'b0;
end else begin
  if (div_counter >= freq_div - 1) begin
    div_counter <= 16'h0;
    clk_enable <= 1'b1;
  end else begin
    div_counter <= div_counter + 1;
    clk_enable <= 1'b0;
  end
end

// short description of what is going on (implementing intersective PWM)

// vcnt counts from 0, 1, 2, 3, ..., 255, 0, 1, 2, 3
// vcnt_r = last cycle's vcnt

// bcnt goes like {FULL x 0}, {FULL x 1},..., {FULL x 15}, {FULL x 0}
// i.e. counts the number of cycles that vcnt has performed

// v gets updated every metacycle (when bcnt is 15 and VCNT is FULL)
// with the upper 8 bits of the set register

// b[0] = cfg[bcnt], i.e. changes every FULL cycles

// v_r is the sum of v and b[0], i.e. v_r alternates between upper 8 bits of config and that value +1  

// PWM logic with mode selection
always @(posedge clk)
if (~rstn) begin
   vcnt  <=  8'h0 ;
   bcnt  <=  4'h0 ;
   pwm_o <=  1'b0 ;
end else if (clk_enable) begin
   if (mode_select) begin
      // Dithered PWM mode
      vcnt   <= vcnt + 8'd1 ;
      vcnt_r <= vcnt;
      v_r    <= (v + b[0]) ; // add decimal bit to current value
      if (vcnt == FULL) begin
         bcnt <=  bcnt + 4'h1 ;
         v    <= (bcnt == 4'hF) ? cfg[24-1:16] : v ; // new value on 16*FULL
         b    <= (bcnt == 4'hF) ? cfg[16-1:0] : {1'b0,b[15:1]} ; // shift right
      end
      // make PWM duty cycle
      pwm_o <= ({1'b0,vcnt_r} < v_r) ;
   end else begin
      // Normal mode - simple PWM without dithered modulation
      vcnt   <= vcnt + 8'd1 ;
      if (vcnt == FULL) begin
         // Reset counter when needed
         vcnt <= 8'h0;
      end
      // Simple PWM - just compare counter with threshold
      pwm_o <= (vcnt < cfg[24-1:16]) ;
   end
end

// Modify sync signal based on mode
assign pwm_s = mode_select ? 
               ((bcnt == 4'hF) && (vcnt == (FULL-1)) && clk_enable) : // dithered mode
               ((vcnt == (FULL-1)) && clk_enable);                     // normal mode

endmodule: red_pitaya_pwm
