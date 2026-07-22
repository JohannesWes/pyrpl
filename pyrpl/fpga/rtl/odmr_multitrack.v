/**
 * @file odmr_multitrack.v
 * @brief Per-channel phase-continuous modulation oscillator + freeze controller
 *        for multi-resonance ODMR tracking.
 *
 * This block provides the modulation and demodulation references for tracking
 * multiple ODMR resonances with one shared modulation frequency f_m. It keeps one
 * phase accumulator per resonance channel, but all accumulators use the same
 * frequency tuning word (FTW). In multitrack mode, only the selected channel's
 * accumulator advances; parked channels hold their phase and resume without a
 * phase discontinuity when selected again.
 *
 * The selected channel is normally current_step_i from the scan block. For bench
 * bring-up and diagnostics, software can override that selection with sw_sel.
 * The active channel's phase drives one shared pair of quarter-wave sine/cosine
 * LUT instances, producing sin_o/cos_o and the demodulation-phase-shifted
 * sin_shifted_o/cos_shifted_o references. The LUTs are shared across channels;
 * only the per-channel phase accumulators are replicated.
 *
 * After each channel hop, a programmable settle counter can freeze the signal
 * processing path for T_SETTLE FPGA clock cycles. While settling, no channel
 * receives aclken_o. Once the window expires, aclken_o is asserted only for the
 * selected channel. The same per-channel clock enable gates the accumulator and
 * the replicated lock-in chain for that channel, so parked and settling chains
 * hold their internal state and resume cleanly.
 *
 * The active reference set is shared by the fgen3 FM drive and by all lock-in
 * instances. This does not mean that all resonance channels share one phase
 * state: each channel has its own phase accumulator, and the shared LUT/output
 * bus is driven from the selected channel's accumulator. Only the selected,
 * non-settling lock-in chain has aclken_o asserted, so only that chain advances
 * its CIC/FIR state and emits valid demod samples. Parked chains may still see
 * the shared reference at their input registers, but their filtered state and
 * output-valid strobes are held until the channel becomes active again.
 *
 * When enable is low, this module is transparent to the legacy single-resonance
 * path: osc_active_o is low, valid_window_o is high, and aclken_o is asserted for
 * every channel so downstream logic can free-run as before.
 *
 * REGISTER MAP (Region 9):
 *   0x00 CTRL       [RW] bit0 enable, bit1 sw_src (0=current_step, 1=sw_sel), bits[..]=sw_sel
 *   0x04 FTW        [RW] f_m frequency tuning word (phase increment / clock)
 *   0x08 DEMOD_PHASE[RW] phase offset for sin_shifted/cos_shifted (demod 2*pi*f_m*tau)
 *   0x0C T_SETTLE   [RW] physical-settle hold-off after each hop, in 125 MHz cycles
 *   0x10 STATUS     [R ] {run_mask, in_settle, sel}
 *   0x14 NCH        [R ] number of channels implemented (HW constant)
 */
module odmr_multitrack #(
  parameter integer NCH       = 2,    // number of resonance channels
  parameter integer PHASEBITS = 32,   // phase accumulator width (matches iq/fgen3)
  parameter integer LUTBITS   = 17,   // reference amplitude bits (matches lock-in refs)
  parameter integer LUTSZ     = 11    // quarter-wave LUT address bits
)(
  input  wire                        clk_i,
  input  wire                        rstn_i,

  // "Which resonance is live" index from the scan block (region 5).
  input  wire        [7:0]           current_step_i,

  // Active-channel modulation/demodulation references (LUTBITS, signed).
  output wire signed [LUTBITS-1:0]   sin_o,
  output wire signed [LUTBITS-1:0]   cos_o,
  output wire signed [LUTBITS-1:0]   sin_shifted_o,
  output wire signed [LUTBITS-1:0]   cos_shifted_o,

  // Per-channel clock-enable (freeze gate) for the replicated lock-in chains.
  output wire        [NCH-1:0]       aclken_o,
  // Selected channel + valid-window strobe (for the odmr error mux / diagnostics).
  output wire        [7:0]           sel_o,
  output wire                        valid_window_o,
  // High when this oscillator drives the signal path (reg_enable). When low the top
  // level keeps the legacy iq0 references (single-resonance backward compatibility).
  output wire                        osc_active_o,

  // System bus interface (Region 9)
  input  wire        [31:0]          sys_addr,
  input  wire        [31:0]          sys_wdata,
  input  wire        [ 3:0]          sys_sel,
  input  wire                        sys_wen,
  input  wire                        sys_ren,
  output reg         [31:0]          sys_rdata,
  output reg                         sys_err,
  output reg                         sys_ack
);

localparam integer SELBITS = (NCH <= 1) ? 1 : $clog2(NCH);
localparam [PHASEBITS-1:0] QUARTER = {2'b01, {(PHASEBITS-2){1'b0}}}; // +90 deg = 2^(PHASEBITS-2)

localparam ADDR_CTRL        = 20'h00000;
localparam ADDR_FTW         = 20'h00004;
localparam ADDR_DEMOD_PHASE = 20'h00008;
localparam ADDR_T_SETTLE    = 20'h0000C;
localparam ADDR_STATUS      = 20'h00010;
localparam ADDR_NCH         = 20'h00014;

//-----------------------------------------------------------------------------
// Configuration registers
//-----------------------------------------------------------------------------
reg                    reg_enable;
reg                    reg_sw_src;       // 0 = use current_step_i, 1 = use reg_sw_sel
reg [SELBITS-1:0]      reg_sw_sel;
reg [PHASEBITS-1:0]    reg_ftw;
reg [PHASEBITS-1:0]    reg_demod_phase;
reg [31:0]             reg_t_settle;

wire [19:0] addr = sys_addr[19:0];

//-----------------------------------------------------------------------------
// Channel selection + physical-settle freeze window
//-----------------------------------------------------------------------------
wire [SELBITS-1:0] sel = reg_sw_src ? reg_sw_sel : current_step_i[SELBITS-1:0];
reg  [SELBITS-1:0] sel_r;
reg  [31:0]        settle_cnt;
wire               sel_changed = (sel != sel_r);
wire               in_settle   = (settle_cnt != 32'd0);
// Include the selector-change cycle itself so the new channel cannot advance
// once with the previous channel's LUT output before settle_cnt is loaded.
wire               freeze_window = sel_changed || in_settle;
wire               valid_window = !freeze_window;

always @(posedge clk_i) begin
  if (!rstn_i) begin
    sel_r      <= {SELBITS{1'b0}};
    settle_cnt <= 32'd0;
  end else begin
    sel_r <= sel;
    if (sel_changed)            settle_cnt <= reg_t_settle;   // restart hold-off on each hop
    else if (settle_cnt != 0)   settle_cnt <= settle_cnt - 32'd1;
  end
end

// Per-channel run-enable. When disabled (reg_enable=0) the block is transparent:
// every channel runs (the top level keeps the legacy iq0 path, and all accumulators
// share the same FTW so they stay phase-identical = one oscillator). When enabled,
// only the active resonance past the settle window runs; the others freeze in place.
reg [NCH-1:0] run;
integer c;
always @(*) begin
  for (c = 0; c < NCH; c = c + 1)
    run[c] = reg_enable ? (valid_window && (sel == c[SELBITS-1:0])) : 1'b1;
end


assign aclken_o       = run;
assign sel_o          = {{(8-SELBITS){1'b0}}, sel};
assign valid_window_o = reg_enable ? valid_window : 1'b1;
assign osc_active_o   = reg_enable;

//-----------------------------------------------------------------------------
// Per-channel phase accumulators (only the running channel increments)
//-----------------------------------------------------------------------------
reg [PHASEBITS-1:0] phase_acc [NCH-1:0];
integer k;
always @(posedge clk_i) begin
  if (!rstn_i) begin
    for (k = 0; k < NCH; k = k + 1)
      phase_acc[k] <= {PHASEBITS{1'b0}};
  end else begin
    for (k = 0; k < NCH; k = k + 1)
      if (run[k])
        phase_acc[k] <= phase_acc[k] + reg_ftw;
  end
end

wire [PHASEBITS-1:0] active_phase = phase_acc[sel];

//-----------------------------------------------------------------------------
// Shared quarter-wave LUT set (active channel): sin/cos and shifted sin/cos
//-----------------------------------------------------------------------------
red_pitaya_quarter_wave_lut17 #(.LUTSZ(LUTSZ), .LUTBITS(LUTBITS), .PHASEBITS(PHASEBITS))
  lut_main (
    .clk_i(clk_i), .rstn_i(rstn_i),
    .phase_a_in(active_phase),
    .phase_b_in(active_phase + QUARTER),       // cos = sin(phase + 90 deg)
    .sin_a_o(sin_o),
    .sin_b_o(cos_o)
  );

red_pitaya_quarter_wave_lut17 #(.LUTSZ(LUTSZ), .LUTBITS(LUTBITS), .PHASEBITS(PHASEBITS))
  lut_shifted (
    .clk_i(clk_i), .rstn_i(rstn_i),
    .phase_a_in(active_phase + reg_demod_phase),
    .phase_b_in(active_phase + reg_demod_phase + QUARTER),
    .sin_a_o(sin_shifted_o),
    .sin_b_o(cos_shifted_o)
  );

//-----------------------------------------------------------------------------
// System bus
//-----------------------------------------------------------------------------
always @(posedge clk_i) begin
  if (!rstn_i) begin
    sys_ack         <= 1'b0;
    sys_err         <= 1'b0;
    sys_rdata       <= 32'h0;
    reg_enable      <= 1'b0;
    reg_sw_src      <= 1'b0;
    reg_sw_sel      <= {SELBITS{1'b0}};
    reg_ftw         <= {PHASEBITS{1'b0}};
    reg_demod_phase <= {PHASEBITS{1'b0}};
    reg_t_settle    <= 32'd0;
  end else begin
    sys_ack <= sys_wen | sys_ren;
    sys_err <= 1'b0;

    if (sys_wen) begin
      case (addr)
        ADDR_CTRL: begin
          reg_enable <= sys_wdata[0];
          reg_sw_src <= sys_wdata[1];
          reg_sw_sel <= sys_wdata[8 +: SELBITS];
        end
        ADDR_FTW:         reg_ftw         <= sys_wdata[PHASEBITS-1:0];
        ADDR_DEMOD_PHASE: reg_demod_phase <= sys_wdata[PHASEBITS-1:0];
        ADDR_T_SETTLE:    reg_t_settle    <= sys_wdata;
        default: sys_err <= 1'b1;
      endcase
    end

    if (sys_ren) begin
      case (addr)
        ADDR_CTRL:        sys_rdata <= {23'h0, reg_sw_sel, {(7-SELBITS){1'b0}}, reg_sw_src, reg_enable};
        ADDR_FTW:         sys_rdata <= reg_ftw;
        ADDR_DEMOD_PHASE: sys_rdata <= reg_demod_phase;
        ADDR_T_SETTLE:    sys_rdata <= reg_t_settle;
        ADDR_STATUS:      sys_rdata <= {{(32-8-NCH){1'b0}}, run, {(7-SELBITS){1'b0}}, in_settle, sel};
        ADDR_NCH:         sys_rdata <= NCH;
        default: begin sys_rdata <= 32'h0; sys_err <= 1'b1; end
      endcase
    end
  end
end

endmodule
