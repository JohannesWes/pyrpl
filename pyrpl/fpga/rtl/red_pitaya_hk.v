/**
 * @brief Red Pitaya house keeping module
 *
 * This module handles system identification and digital IO control.
 * Digital outputs can be controlled via system bus or from other FPGA modules.
 * Source selection (system bus vs module input) is configurable per pin.
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

  // Expansion connector - direct connection to physical pins
  input      [DWE-1:0] exp_p_dat_i,  // exp. con. P input data
  output reg [DWE-1:0] exp_p_dat_o,  // exp. con. P output data
  output reg [DWE-1:0] exp_p_dir_o,  // exp. con. P direction (1=output, 0=input)
  input      [DWE-1:0] exp_n_dat_i,  // exp. con. N input data
  output reg [DWE-1:0] exp_n_dat_o,  // exp. con. N output data
  output reg [DWE-1:0] exp_n_dir_o,  // exp. con. N direction (1=output, 0=input)

  // Module inputs - data from other FPGA modules
  input      [DWE-1:0] mod_exp_p_dat,  // exp. con. P data from modules
  input      [DWE-1:0] mod_exp_n_dat,  // exp. con. N data from modules

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

//---------------------------------------------------------------------------------
// Internal registers for system bus control

// Data and direction from system bus
reg [DWE-1:0] sys_exp_p_dat;
reg [DWE-1:0] sys_exp_p_dir;
reg [DWE-1:0] sys_exp_n_dat;
reg [DWE-1:0] sys_exp_n_dir;

// Inversion control registers (1=invert output, 0=normal)
reg [DWE-1:0] exp_p_inv_sel;  // Inversion select for P expansion
reg [DWE-1:0] exp_n_inv_sel;  // Inversion select for N expansion

// Source selection registers (0=system bus, 1=module input)
reg [DWE-1:0] exp_p_src_sel;  // Source select for P expansion
reg [DWE-1:0] exp_n_src_sel;  // Source select for N expansion

//---------------------------------------------------------------------------------
// Read device DNA

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
//  Design identification

wire [32-1: 0] id_value;

assign id_value[31: 4] = 28'h0; // reserved
assign id_value[ 3: 0] =  4'h1; // board type   1 - release 1

//---------------------------------------------------------------------------------
// Output multiplexing - select between system bus and module inputs

integer i;

reg [DWE-1:0] exp_p_dat_mux;
reg [DWE-1:0] exp_n_dat_mux;

always @(posedge clk_i) begin
  if (rstn_i == 1'b0) begin
    exp_p_dat_o <= {DWE{1'b0}};
    exp_p_dir_o <= {DWE{1'b0}};
    exp_n_dat_o <= {DWE{1'b0}};
    exp_n_dir_o <= {DWE{1'b0}};
  end else begin
    // Multiplexing for P expansion
    for (i = 0; i < DWE; i = i + 1) begin
      // select the data source
      exp_p_dat_mux[i] = exp_p_src_sel[i] ? mod_exp_p_dat[i] : sys_exp_p_dat[i];
      // apply inversion if enabled
      exp_p_dat_o[i] <= exp_p_inv_sel[i] ? ~exp_p_dat_mux[i] : exp_p_dat_mux[i];

      exp_p_dir_o[i] <= exp_p_src_sel[i] ? 1'b1 : sys_exp_p_dir[i]; // Module data is always output, not input
    end
    
    // Multiplexing for N expansion
    for (i = 0; i < DWE; i = i + 1) begin
      // select the data source
      exp_n_dat_mux[i] = exp_n_src_sel[i] ? mod_exp_n_dat[i] : sys_exp_n_dat[i];
      // apply inversion if enabled
      exp_n_dat_o[i] <= exp_n_inv_sel[i] ? ~exp_n_dat_mux[i] : exp_n_dat_mux[i];

      exp_n_dir_o[i] <= exp_n_src_sel[i] ? 1'b1 : sys_exp_n_dir[i]; // Module data is always output, not input
    end
  end
end

//---------------------------------------------------------------------------------
// System bus interface

always @(posedge clk_i)
if (rstn_i == 1'b0) begin
  led_o            <= {DWL{1'b0}};
  digital_loop     <= 1'b0;

  sys_exp_p_dat    <= {DWE{1'b0}};
  sys_exp_p_dir    <= {DWE{1'b0}};
  sys_exp_n_dat    <= {DWE{1'b0}};
  sys_exp_n_dir    <= {DWE{1'b0}};
  exp_p_src_sel    <= {DWE{1'b0}};
  exp_n_src_sel    <= {DWE{1'b0}};
  exp_p_inv_sel    <= {DWE{1'b0}};
  exp_n_inv_sel    <= {DWE{1'b0}};

end else begin
  // Handle system bus writes
  if (sys_wen) begin
    case (sys_addr[19:0])
      20'h0C: digital_loop  <= sys_wdata[0];
      
      // Direction control
      20'h10: sys_exp_p_dir <= sys_wdata[DWE-1:0];
      20'h14: sys_exp_n_dir <= sys_wdata[DWE-1:0];
      
      // Data output
      20'h18: sys_exp_p_dat <= sys_wdata[DWE-1:0];
      20'h1C: sys_exp_n_dat <= sys_wdata[DWE-1:0];
      
      // Source selection (0=system, 1=module)
      20'h40: exp_p_src_sel <= sys_wdata[DWE-1:0];
      20'h44: exp_n_src_sel <= sys_wdata[DWE-1:0];

      20'h50: exp_p_inv_sel <= sys_wdata[DWE-1:0];
      20'h54: exp_n_inv_sel <= sys_wdata[DWE-1:0];
      
      // LED control
      20'h30: led_o         <= sys_wdata[DWL-1:0];
    endcase
  end
end

// System bus read logic
wire sys_en;
assign sys_en = sys_wen | sys_ren;

always @(posedge clk_i)
if (rstn_i == 1'b0) begin
  sys_err   <= 1'b0;
  sys_ack   <= 1'b0;
  sys_rdata <= 32'h0;
end else begin
  sys_err <= 1'b0;
  sys_ack <= sys_en;

  case (sys_addr[19:0])
    // Identification
    20'h00000: sys_rdata <= id_value;
    20'h00004: sys_rdata <= dna_value[32-1:0];
    20'h00008: sys_rdata <= {{32-25{1'b0}}, dna_value[57-1:32]};
    
    // Configuration
    20'h0000c: sys_rdata <= {{32-1{1'b0}}, digital_loop};
    
    // Direction registers
    20'h00010: sys_rdata <= {{32-DWE{1'b0}}, sys_exp_p_dir};
    20'h00014: sys_rdata <= {{32-DWE{1'b0}}, sys_exp_n_dir};
    
    // Output data registers - values set by the system bus
    20'h00018: sys_rdata <= {{32-DWE{1'b0}}, sys_exp_p_dat};
    20'h0001C: sys_rdata <= {{32-DWE{1'b0}}, sys_exp_n_dat};
    
    // Input data (read actual pin values)
    20'h00020: sys_rdata <= {{32-DWE{1'b0}}, exp_p_dat_i};
    20'h00024: sys_rdata <= {{32-DWE{1'b0}}, exp_n_dat_i};
    
    // LED register
    20'h00030: sys_rdata <= {{32-DWL{1'b0}}, led_o};
    
    // Source selection registers
    20'h00040: sys_rdata <= {{32-DWE{1'b0}}, exp_p_src_sel};
    20'h00044: sys_rdata <= {{32-DWE{1'b0}}, exp_n_src_sel};
    
    // Current output values (after mux)
    20'h00048: sys_rdata <= {{32-DWE{1'b0}}, exp_p_dat_o};
    20'h0004C: sys_rdata <= {{32-DWE{1'b0}}, exp_n_dat_o};

    20'h00050: sys_rdata <= {{32-DWE{1'b0}}, exp_p_inv_sel};
    20'h00054: sys_rdata <= {{32-DWE{1'b0}}, exp_n_inv_sel};
    
    default: sys_rdata <= 32'h0;
  endcase
end

endmodule