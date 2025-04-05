/**
 * $Id: red_pitaya_hk.v 961 2014-01-21 11:40:39Z matej.oblak $
 *
 * @brief Red Pitaya house keeping.
 *
 * @Author Matej Oblak
 *
 * (c) Red Pitaya  http://www.redpitaya.com
 *
 * This part of code is written in Verilog hardware description language (HDL).
 * Please visit http://en.wikipedia.org/wiki/Verilog
 * for more details on the language used herein.
 */

/**
 * GENERAL DESCRIPTION:
 *
 * House keeping module takes care of system identification.
 *
 *
 * This module takes care of system identification via DNA readout at startup and
 * ID register which user can define at compile time.
 *
 * Beside that it is currently also used to test expansion connector and for
 * driving LEDs.
 *
 */

module red_pitaya_hk #(
  parameter DWL = 8, // data width for LED
  parameter DWE = 8, // data width for extension
  parameter [57-1:0] DNA = 57'h0823456789ABCDE
)(
  // system signals
  input                clk_i      ,  // clock
  input                rstn_i     ,  // reset - active low
  // LED
  output reg [DWL-1:0] led_o      ,  // LED output
  // global configuration
  output reg           digital_loop,
  // Expansion connector
  input      [DWE-1:0] exp_p_dat_i,  // exp. con. input data
  output reg [DWE-1:0] exp_p_dat_o,  // exp. con. output data
  output reg [DWE-1:0] exp_p_dir_o,  // exp. con. 1-output enable
  input      [DWE-1:0] exp_n_dat_i,  //
  output reg [DWE-1:0] exp_n_dat_o,  //
  output reg [DWE-1:0] exp_n_dir_o,  //
  input      [4-1  :0] digital_pwm, // Digital PWM values
  // System bus
  input      [ 32-1:0] sys_addr   ,  // bus address
  input      [ 32-1:0] sys_wdata  ,  // bus write data
  input      [  4-1:0] sys_sel    ,  // bus write byte select
  input                sys_wen    ,  // bus write enable
  input                sys_ren    ,  // bus read enable
  output reg [ 32-1:0] sys_rdata  ,  // bus read data
  output reg           sys_err    ,  // bus error indicator
  output reg           sys_ack       // bus acknowledge signal
);

// Flag to control whether PWM signals are directly routed to expansion pins
reg            pwm_direct_output;

// Store system bus writes to exp_p_dat_o separately
reg [DWE-1:0]  sys_exp_p_dat;
reg [DWE-1:0]  sys_exp_p_dir;

//---------------------------------------------------------------------------------
//
//  Read device DNA

wire           dna_dout ;
reg            dna_clk  ;
reg            dna_read ;
reg            dna_shift;
reg  [ 9-1: 0] dna_cnt  ;
reg  [57-1: 0] dna_value;
reg            dna_done ;

always @(posedge clk_i)
if (rstn_i == 1'b0) begin
  dna_clk   <=  1'b0;
  dna_read  <=  1'b0;
  dna_shift <=  1'b0;
  dna_cnt   <=  9'd0;
  dna_value <= 57'd0;
  dna_done  <=  1'b0;
end else begin
  if (!dna_done)
    dna_cnt <= dna_cnt + 1'd1;

  dna_clk <= dna_cnt[2] ;
  dna_read  <= (dna_cnt < 9'd10);
  dna_shift <= (dna_cnt > 9'd18);

  if ((dna_cnt[2:0]==3'h0) && !dna_done)
    dna_value <= {dna_value[57-2:0], dna_dout};

  if (dna_cnt > 9'd465)
    dna_done <= 1'b1;
end

// parameter specifies a sample 57-bit DNA value for simulation
DNA_PORT #(.SIM_DNA_VALUE (DNA)) i_DNA (
  .DOUT  ( dna_dout   ), // 1-bit output: DNA output data.
  .CLK   ( dna_clk    ), // 1-bit input: Clock input.
  .DIN   ( 1'b0       ), // 1-bit input: User data input pin.
  .READ  ( dna_read   ), // 1-bit input: Active high load DNA, active low read input.
  .SHIFT ( dna_shift  )  // 1-bit input: Active high shift enable input.
);

//---------------------------------------------------------------------------------
//
//  Design identification

wire [32-1: 0] id_value;

assign id_value[31: 4] = 28'h0; // reserved
assign id_value[ 3: 0] =  4'h1; // board type   1 - release 1

//---------------------------------------------------------------------------------
//
//  System bus connection - handle register writes

always @(posedge clk_i)
if (rstn_i == 1'b0) begin
  led_o            <= {DWL{1'b0}};
  sys_exp_p_dat    <= {DWE{1'b0}};
  sys_exp_p_dir    <= {DWE{1'b0}};
  exp_n_dat_o      <= {DWE{1'b0}};
  exp_n_dir_o      <= {DWE{1'b0}};
  pwm_direct_output <= 1'b0;  // Default to disabled
  digital_loop     <= 1'b0;
end else begin
  // Handle system bus writes
  if (sys_wen) begin
    if (sys_addr[19:0]==20'h0c)   digital_loop      <= sys_wdata[0];
    if (sys_addr[19:0]==20'h10)   sys_exp_p_dir     <= sys_wdata[DWE-1:0];
    if (sys_addr[19:0]==20'h14)   exp_n_dir_o       <= sys_wdata[DWE-1:0];
    if (sys_addr[19:0]==20'h18)   sys_exp_p_dat     <= sys_wdata[DWE-1:0];
    if (sys_addr[19:0]==20'h1C)   exp_n_dat_o       <= sys_wdata[DWE-1:0];
    if (sys_addr[19:0]==20'h28)   pwm_direct_output <= sys_wdata[0];
    if (sys_addr[19:0]==20'h30)   led_o             <= sys_wdata[DWL-1:0];
  end
end

//---------------------------------------------------------------------------------
//
// Continuous PWM routing - separate from system bus handling
// This ensures PWM signals are always routed to outputs when enabled

always @(posedge clk_i) begin
  if (pwm_direct_output) begin
    // In PWM direct mode:
    // - Bits 3:0 are controlled by PWM
    // - Bits 7:4 are controlled by system bus
    exp_p_dat_o[3:0] <= digital_pwm[3:0];
    exp_p_dat_o[DWE-1:4] <= sys_exp_p_dat[DWE-1:4];
    
    // Force directions for PWM pins to output
    exp_p_dir_o[3:0] <= 4'b1111;
    exp_p_dir_o[DWE-1:4] <= sys_exp_p_dir[DWE-1:4];
  end else begin
    // In normal mode, use system bus values
    exp_p_dat_o <= sys_exp_p_dat;
    exp_p_dir_o <= sys_exp_p_dir;
  end
end

wire sys_en;
assign sys_en = sys_wen | sys_ren;

always @(posedge clk_i)
if (rstn_i == 1'b0) begin
  sys_err <= 1'b0;
  sys_ack <= 1'b0;
  sys_rdata <= 32'h0;
end else begin
  sys_err <= 1'b0;
  sys_ack <= sys_en;

  casez (sys_addr[19:0])
    20'h00000: begin sys_rdata <= {                id_value          }; end
    20'h00004: begin sys_rdata <= {                dna_value[32-1: 0]}; end
    20'h00008: begin sys_rdata <= {{32-25{1'b0}}, dna_value[57-1:32]}; end
    20'h0000c: begin sys_rdata <= {{32-  1{1'b0}}, digital_loop      }; end

    20'h00010: begin sys_rdata <= {{32-DWE{1'b0}}, exp_p_dir_o}       ; end
    20'h00014: begin sys_rdata <= {{32-DWE{1'b0}}, exp_n_dir_o}       ; end
    20'h00018: begin sys_rdata <= {{32-DWE{1'b0}}, exp_p_dat_o}       ; end
    20'h0001C: begin sys_rdata <= {{32-DWE{1'b0}}, exp_n_dat_o}       ; end
    20'h00020: begin sys_rdata <= {{32-DWE{1'b0}}, exp_p_dat_i}       ; end
    20'h00024: begin sys_rdata <= {{32-DWE{1'b0}}, exp_n_dat_i}       ; end
    
    20'h00028: begin sys_rdata <= {{32-  1{1'b0}}, pwm_direct_output} ; end

    20'h00030: begin sys_rdata <= {{32-DWL{1'b0}}, led_o}             ; end

    default: begin sys_rdata <=  32'h0                              ; end
  endcase
end

endmodule