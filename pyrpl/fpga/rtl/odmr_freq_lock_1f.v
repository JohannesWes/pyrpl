/**
 * @file odmr_freq_lock_1f.v
 * @brief ODMR Frequency-Locked Loop (1f-I Component with PI Control) — N-slot multi-resonance
 *
 * Implements a configurable frequency-locked loop (I-only or PI mode) that drives
 * a DDS frequency correction based on demodulated lock-in I-quadrature signal.
 *
 * MULTI-RESONANCE (Phase B, NSLOTS integrators):
 * ==============================================
 * For multi-resonance ODMR tracking (see docs/developer_guide/multi_resonance_tracking.md)
 * the loop carries NSLOTS independent integrator states — one per tracked resonance.
 * A slot selector (software `active_slot`, or the hardware `current_step_i` index from
 * the scan block when `active_slot_src=1`) picks the *active* resonance:
 *   - only the active slot's integrator updates on a valid strobe;
 *   - the inactive slots HOLD their converged value (bumpless across an LO hop);
 *   - `ftw_correction_o` is driven CONTINUOUSLY from the active slot's stored output,
 *     so it follows the slot the instant `current_step_i` changes (fgen3 samples this
 *     bus every clock — there is no valid-gating on the fgen3 side).
 * Integrators are NEVER reset on a hop (only on explicit `clr` or `enable=0`). With
 * NSLOTS=1 the behaviour is identical to the legacy single-resonance loop.
 *
 * The error `err_i` is the demodulated signal of whichever resonance is physically
 * live (one shared lock-in chain until Phase C), so it is attributed to the active
 * slot. (Group-delay / valid-window alignment around a hop is a Phase C concern —
 * Phase B follows `current_step` directly.)
 *
 * THEORY OF OPERATION:
 * ====================
 * Around resonance, demodulated 1f-I output is proportional to detuning:
 *   e[n] ≈ K * (f0[n] - fr[n])
 *
 * Control modes (selected by prop_enable bit):
 *
 * INTEGRAL-ONLY (prop_enable=0, default for backward compatibility):
 *   f0[n+1] = f0[n] - μ * e[n]  (μ = K_i * T_s with integral gain K_i and sample time T_s)
 *
 * PI CONTROL (prop_enable=1, faster acquisition and improved phase margin):
 *   u[n] = K_p * e[n] + x[n]    (parallel PI form)
 *   x[n+1] = x[n] - μ * e[n]    (integral state update)
 *
 * Implemented in DDS units (Frequency Tuning Words):
 *   FTW_corr[n+1] = FTW_corr[n] - μ_FTW * e[n]           (I-only mode)
 *   FTW_out[n] = FTW_corr[n] - K_p,FTW * e[n]            (PI mode)
 *
 * Where:
 *   μ_FTW = μ * (2^PHASEBITS / f_clk)      [FTW/LSB, stored in Q8.24 format]
 *   K_p,FTW = K_p * (2^PHASEBITS / f_clk)  [FTW/LSB, stored in Q8.24 format]
 *
 * DESIGN PARAMETERS (from planning doc):
 * ======================================
 * - Clock: 125 MHz
 * - Update rate: ~30.5 kS/s (when err_valid_i strobes)
 * - PHASEBITS: 32 (matches 3FGEN)
 * - Gain format: Q8.24 fixed-point (8 integer bits, 24 fractional bits)
 * - Default μ_FTW: 0x01EDE8D0 ≈ 1.929 FTW/LSB (for 300 Hz BW, K=1.1 LSB/Hz)
 * - Default FTW_LIM: 34359738 (±1 MHz correction range)
 * - Loop gains (mu, kp), deadband, and ftw_lim are GLOBAL (shared by all slots).
 *
 * ANTI-WINDUP STRATEGY (Directional):
 * ===================================
 * In PI mode, the proportional term can cause output saturation even when the
 * integrator is small (especially during acquisition from large errors).
 *
 * Standard anti-windup (freeze when saturated) would permanently lock the
 * integrator at zero, preventing acquisition.
 *
 * This implementation uses DIRECTIONAL anti-windup:
 * - If PI sum saturates positive AND integrator update is positive: FREEZE
 * - If PI sum saturates negative AND integrator update is negative: FREEZE
 * - Otherwise: ALLOW (integrator can recover from saturation)
 *
 * This enables PI mode to acquire lock from large initial errors while still
 * preventing classical integrator wind-up.
 *
 * REGISTER MAP (System Bus Region 8):
 * ====================================
 * GLOBAL control / configuration:
 *   0x0000  CTRL        [RW]  Control bits (enable, invert, hold, clr, deadband_en, prop_enable)
 *   0x0004  MU_Q        [RW]  Integral gain μ_FTW in Q8.24 format
 *   0x0008  DEADBAND    [RW]  Error deadband threshold (unsigned LSB)
 *   0x000C  FTW_LIM     [RW]  Saturation limit for FTW correction (unsigned)
 *   0x001C  KP_Q        [RW]  Proportional gain K_p,FTW in Q8.24 format
 *   0x0024  SLOT_CTRL   [RW]  bit[SLOTSEL_BITS-1:0]=active_slot (sw), bit[8]=active_slot_src
 *   0x0028  NSLOTS      [R ]  Number of integrator slots implemented (read from HW)
 *
 * PER-SLOT readback (slot 0 also aliased at the legacy addresses for back-compat):
 *   0x0010  STATUS      [R ]  slot 0: {pi_sat, i_sat, sat, locked}
 *   0x0014  ERR_LATCH   [R ]  slot 0: last error value that produced an update
 *   0x0018  FTW_INT     [R ]  slot 0: integrator state (diagnostics)
 *   0x0020  FTW_OUT     [R ]  slot 0: actual FTW correction output (incl. P term in PI mode)
 *
 *   Packed per-slot bank at 0x0040, stride 0x10 (slot s at 0x40 + s*0x10):
 *     +0x00  STATUS[s]   [R ]
 *     +0x04  ERR_LATCH[s][R ]
 *     +0x08  FTW_INT[s]  [R ]
 *     +0x0C  FTW_OUT[s]  [R ]
 */

module odmr_freq_lock_1f #(
  parameter integer PHASEBITS = 32,    // DDS phase accumulator width (must match 3FGEN)
  parameter integer MU_QFRAC  = 24,    // Q-format fractional bits for gain (Q8.24)
  parameter integer NSLOTS    = 2      // Number of per-resonance integrator slots (N=2 now, generalises to 8)
)(
  // Clock and reset (125 MHz domain)
  input  wire                       clk_i,
  input  wire                       rstn_i,

  // Demodulated error from lock_in.v (Channel 1, I component)
  input  wire signed [31:0]         err_i,          // Demodulated I quadrature (LSB)
  input  wire                       err_valid_i,    // Valid strobe at ~30.5 kS/s

  // Hardware "which resonance is live" index (from the scan block, region 5).
  // Selects the active integrator slot when active_slot_src=1. Tie to 0 if unrouted.
  input  wire        [7:0]          current_step_i,

  // Output to red_pitaya_3fgen (driven continuously from the active slot)
  output reg  signed [PHASEBITS-1:0] ftw_correction_o,
  output reg                         ftw_correction_valid_o,

  // System bus interface (Region 8)
  input  wire        [31:0]         sys_addr,
  input  wire        [31:0]         sys_wdata,
  input  wire        [ 3:0]         sys_sel,
  input  wire                       sys_wen,
  input  wire                       sys_ren,
  output reg         [31:0]         sys_rdata,
  output reg                        sys_err,
  output reg                        sys_ack
);

//-----------------------------------------------------------------------------
// SLOT SELECTION
//-----------------------------------------------------------------------------
localparam integer SLOTSEL_BITS = (NSLOTS <= 1) ? 1 : $clog2(NSLOTS);

//-----------------------------------------------------------------------------
// REGISTER ADDRESSES
//-----------------------------------------------------------------------------
localparam ADDR_CTRL      = 20'h00000;  // Control register
localparam ADDR_MU_Q      = 20'h00004;  // Gain μ_FTW (Q8.24)
localparam ADDR_DEADBAND  = 20'h00008;  // Deadband threshold
localparam ADDR_FTW_LIM   = 20'h0000C;  // FTW saturation limit
localparam ADDR_STATUS    = 20'h00010;  // Status flags (slot 0 alias)
localparam ADDR_ERR_LATCH = 20'h00014;  // Latched error (slot 0 alias)
localparam ADDR_FTW_INT   = 20'h00018;  // Integrator state (slot 0 alias)
localparam ADDR_KP_Q      = 20'h0001C;  // Proportional gain (Q8.24)
localparam ADDR_FTW_OUT   = 20'h00020;  // FTW correction output (slot 0 alias)
localparam ADDR_SLOT_CTRL = 20'h00024;  // Slot selection control
localparam ADDR_NSLOTS    = 20'h00028;  // NSLOTS readback (HW constant)

// Packed per-slot readback bank
localparam [19:0] BANK_BASE   = 20'h00040;
localparam [19:0] BANK_STRIDE = 20'h00010;
localparam [19:0] BANK_END    = BANK_BASE + NSLOTS*BANK_STRIDE;  // one past the last slot (compile-time const)

//-----------------------------------------------------------------------------
// DEFAULT VALUES (Pre-computed from planning doc)
//-----------------------------------------------------------------------------
// Default gain for 300 Hz BW with K=1.1 LSB/Hz:
//   μ ≈ 0.05615 Hz/LSB
//   μ_FTW = μ * (2^32 / 125 MHz) ≈ 1.929333 FTW/LSB
//   Q8.24: round(1.929333 * 2^24) = 0x01EDE8D0
localparam [31:0] MU_Q_DEFAULT = 32'h01EDE8D0;

// Default saturation: ±1 MHz
//   FTW/Hz = 2^32 / 125 MHz ≈ 34.359738
//   1 MHz → round(1e6 * 34.359738) = 34359738
localparam [31:0] FTW_LIM_DEFAULT = 32'd34359738;

// Default proportional gain for 300 Hz BW with K=1.1 LSB/Hz, zero at BW/3:
//   K_p = α/K = 3/1.1 ≈ 2.7273 Hz/LSB
//   K_p,FTW = 2.7273 * (2^32 / 125 MHz) ≈ 93.7084 FTW/LSB
//   Q8.24: round(93.7084 * 2^24) = 0x5DB55838
localparam [31:0] KP_Q_DEFAULT = 32'h5DB55838;

// Lock detector: consecutive samples below threshold (sized to lock_counter width)
localparam [15:0] LOCK_COUNT_THRESH = 16'd256;  // ~8.4 ms at 30.5 kS/s

//-----------------------------------------------------------------------------
// CONTROL & CONFIGURATION REGISTERS (GLOBAL — shared by all slots)
//-----------------------------------------------------------------------------
reg        ctrl_enable;       // Enable loop
reg        ctrl_invert;       // Invert error sign
reg        ctrl_hold;         // Freeze integrator
reg        ctrl_deadband_en;  // Enable deadband check
reg        ctrl_clr;          // Clear integrator (self-clearing)
reg        ctrl_prop_enable;  // Enable proportional path (PI mode)

reg signed [31:0] reg_mu_q;      // Integral gain (Q8.24)
reg        [31:0] reg_deadband;  // Deadband threshold (unsigned)
reg        [31:0] reg_ftw_lim;   // Saturation limit (unsigned)
reg signed [31:0] reg_kp_q;      // Proportional gain (Q8.24)

// Active-slot selection
reg [SLOTSEL_BITS-1:0] active_slot;      // sw-selected slot
reg                    active_slot_src;  // 0 = use active_slot (sw); 1 = use current_step_i (hw)
wire [SLOTSEL_BITS-1:0] slot_sel = active_slot_src ? current_step_i[SLOTSEL_BITS-1:0]
                                                   : active_slot;
reg [SLOTSEL_BITS-1:0] slot_sel_r;       // registered selector (glitch-free swap into datapath)

//-----------------------------------------------------------------------------
// PER-SLOT STATE REGISTERS
//-----------------------------------------------------------------------------
reg signed [PHASEBITS-1:0] ftw_corr   [NSLOTS-1:0];  // Integrator accumulator per slot
reg signed [PHASEBITS-1:0] ftw_out_r  [NSLOTS-1:0];  // Last applied correction per slot (readback + follow)
reg signed [31:0]          err_latch  [NSLOTS-1:0];  // Last error processed per slot
reg                        flag_saturated    [NSLOTS-1:0];  // Any saturation (legacy)
reg                        flag_i_saturated  [NSLOTS-1:0];  // Integrator-only saturation
reg                        flag_pi_saturated [NSLOTS-1:0];  // PI sum saturation
reg        [15:0]          lock_counter      [NSLOTS-1:0];  // Lock detector counter per slot

// Active slot's integrator (datapath operand). slot_sel_r is registered, so the
// datapath operates on the slot selected last cycle (glitch-free; the 1-cycle lag
// is negligible vs the ~4096-cycle update period).
//-----------------------------------------------------------------------------
// MULTICYCLE SETTLE FOR THE INTEGRATOR MAC (+ hop-safe operand snapshot)
//-----------------------------------------------------------------------------
// The integrator datapath (reg_mu_q*err -> +ftw_corr_active -> saturate) is a deep
// unpipelined 32x32 multiply + 64-bit accumulate + saturate that CANNOT settle in
// one 8 ns clock on the 7z010-1 (post-route ~21 ns). So we delay the integrator
// CAPTURE by MAC_DELAY clocks (upd = err_valid_i delayed) and declare a matching
// `set_multicycle_path -setup MAC_DELAY` on ftw_corr/ftw_out_r/flag_* in
// red_pitaya.xdc, turning it into a TRUE multicycle path that closes timing on every
// build. (A bare xdc constraint WITHOUT this capture delay would be unsound: the
// original capture was single-cycle, so STA would pass while silicon still failed.)
//
// HOP-SAFE SNAPSHOT: the datapath must use the error AND slot that were live at the
// err_valid strobe, NOT the (possibly different) live values MAC_DELAY clocks later.
// During an LO hop the live slot (slot_sel_r) and the active demod (err_i) switch to
// the other resonance; a capture launched just before the hop would otherwise write
// the NEW slot with the OLD slot's stale error and saturate it to +/-ftw_lim
// (observed: one slot railing to -1e6 Hz intermittently). We therefore snapshot
// {err, slot} at err_valid_i into err_upd/slot_upd, hold them across the delay, and
// run the whole MAC + capture off the snapshot. Only ONE update is ever in flight
// (err_valid is ~4096 clk apart, MAC_DELAY=4), so a single snapshot pair suffices.
// The output bus ftw_correction_o still follows the LIVE slot (slot_sel_r) so it
// tracks the hop immediately.
localparam integer MAC_DELAY = 4;
reg  [MAC_DELAY-1:0]   upd_pipe;          // err_valid_i delayed MAC_DELAY clocks
wire                   upd = upd_pipe[MAC_DELAY-1];
reg signed [31:0]      err_upd;           // err_i  snapshot at err_valid_i
reg [SLOTSEL_BITS-1:0] slot_upd;          // slot   snapshot at err_valid_i

wire signed [PHASEBITS-1:0] ftw_corr_active = ftw_corr[slot_upd];

//-----------------------------------------------------------------------------
// SYSTEM BUS INTERFACE
//-----------------------------------------------------------------------------
wire [19:0] sys_addr_local = sys_addr[19:0];  // Extract local address

// Per-slot readback bank decode. Use (addr - BANK_BASE) so the slot index is correct
// for any NSLOTS regardless of whether BANK_BASE is stride-aligned.
wire bank_sel = (sys_addr_local >= BANK_BASE) && (sys_addr_local < BANK_END);
wire [19:0] bank_rel  = sys_addr_local - BANK_BASE;
wire [SLOTSEL_BITS-1:0] bank_slot = bank_rel[3+SLOTSEL_BITS -: SLOTSEL_BITS]; // stride 0x10 -> slot bits start at bit 4
wire [3:0]              bank_off  = bank_rel[3:0];

always @(posedge clk_i) begin
  if (!rstn_i) begin
    sys_ack   <= 1'b0;
    sys_err   <= 1'b0;
    sys_rdata <= 32'h0;

    // Reset configuration to defaults
    ctrl_enable      <= 1'b0;
    ctrl_invert      <= 1'b0;
    ctrl_hold        <= 1'b0;
    ctrl_deadband_en <= 1'b0;
    ctrl_clr         <= 1'b0;
    ctrl_prop_enable <= 1'b0;  // PI mode disabled by default (integral-only)

    reg_mu_q      <= MU_Q_DEFAULT;
    reg_deadband  <= 32'd100;  // Default: lock when |error| < 100 LSB
    reg_ftw_lim   <= FTW_LIM_DEFAULT;
    reg_kp_q      <= KP_Q_DEFAULT;

    active_slot     <= {SLOTSEL_BITS{1'b0}};
    active_slot_src <= 1'b0;

  end else begin
    // Default ack behavior
    sys_ack <= sys_wen | sys_ren;
    sys_err <= 1'b0;

    // Self-clearing control bits
    ctrl_clr <= 1'b0;

    // Write operations
    if (sys_wen) begin
      case (sys_addr_local)
        ADDR_CTRL: begin
          ctrl_enable      <= sys_wdata[0];
          ctrl_invert      <= sys_wdata[1];
          ctrl_hold        <= sys_wdata[2];
          ctrl_clr         <= sys_wdata[3];  // Self-clearing
          ctrl_deadband_en <= sys_wdata[4];
          ctrl_prop_enable <= sys_wdata[5];
        end

        ADDR_MU_Q:     reg_mu_q     <= sys_wdata;
        ADDR_DEADBAND: reg_deadband <= sys_wdata;
        ADDR_FTW_LIM:  reg_ftw_lim  <= sys_wdata;
        ADDR_KP_Q:     reg_kp_q     <= sys_wdata;

        ADDR_SLOT_CTRL: begin
          active_slot     <= sys_wdata[SLOTSEL_BITS-1:0];
          active_slot_src <= sys_wdata[8];
        end

        default: sys_err <= 1'b1;
      endcase
    end

    // Read operations
    if (sys_ren) begin
      if (bank_sel) begin
        // Packed per-slot readback bank
        case (bank_off)
          4'h0: sys_rdata <= {28'h0,
                              flag_pi_saturated[bank_slot],
                              flag_i_saturated[bank_slot],
                              flag_saturated[bank_slot],
                              (lock_counter[bank_slot] == LOCK_COUNT_THRESH)};
          4'h4: sys_rdata <= err_latch[bank_slot];
          4'h8: sys_rdata <= ftw_corr[bank_slot];
          4'hC: sys_rdata <= ftw_out_r[bank_slot];
          default: begin sys_rdata <= 32'h0; sys_err <= 1'b1; end
        endcase
      end else begin
        case (sys_addr_local)
          ADDR_CTRL: begin
            sys_rdata <= {26'h0,
                          ctrl_prop_enable,
                          ctrl_deadband_en,
                          ctrl_clr,         // Will read as 0 (self-clearing)
                          ctrl_hold,
                          ctrl_invert,
                          ctrl_enable};
          end

          ADDR_MU_Q:      sys_rdata <= reg_mu_q;
          ADDR_DEADBAND:  sys_rdata <= reg_deadband;
          ADDR_FTW_LIM:   sys_rdata <= reg_ftw_lim;
          ADDR_KP_Q:      sys_rdata <= reg_kp_q;

          ADDR_SLOT_CTRL: sys_rdata <= {23'h0, active_slot_src, {(8-SLOTSEL_BITS){1'b0}}, active_slot};
          ADDR_NSLOTS:    sys_rdata <= NSLOTS;

          // Slot-0 aliases (back-compat with the legacy single-resonance map)
          ADDR_STATUS: begin
            sys_rdata <= {28'h0,
                          flag_pi_saturated[0],
                          flag_i_saturated[0],
                          flag_saturated[0],
                          (lock_counter[0] == LOCK_COUNT_THRESH)};
          end
          ADDR_ERR_LATCH: sys_rdata <= err_latch[0];
          ADDR_FTW_INT:   sys_rdata <= ftw_corr[0];
          ADDR_FTW_OUT:   sys_rdata <= ftw_out_r[0];

          default: begin
            sys_rdata <= 32'h0;
            sys_err   <= 1'b1;
          end
        endcase
      end
    end
  end
end

//-----------------------------------------------------------------------------
// INTEGRAL CONTROLLER DATAPATH (operates on the ACTIVE slot)
//-----------------------------------------------------------------------------

// Error conditioning: apply inversion if requested. Uses the hop-safe SNAPSHOT
// err_upd (the error live at err_valid_i), not the current err_i, so a delayed
// capture straddling an LO hop applies the correct resonance's error.
wire signed [31:0] err_conditioned = ctrl_invert ? -err_upd : err_upd;

// Deadband check: compute absolute value
wire [31:0] err_abs = (err_conditioned[31]) ? -err_conditioned : err_conditioned;
wire deadband_skip = ctrl_deadband_en && (err_abs < reg_deadband);

// DSP48 multiply: delta_ftw = -μ_FTW * e
// Note: We negate the product to get correct loop polarity
wire signed [63:0] product = -($signed(reg_mu_q) * $signed(err_conditioned));

// Align to integer FTW/LSB by arithmetic right shift
// Add rounding: (product + 2^(MU_QFRAC-1)) >>> MU_QFRAC
wire signed [63:0] product_rounded = product + (64'sd1 << (MU_QFRAC - 1));
wire signed [PHASEBITS-1:0] delta_ftw = product_rounded[MU_QFRAC +: PHASEBITS];

// Compute unsaturated integrator update (extended precision for overflow detection)
wire signed [PHASEBITS:0] sum_extended = $signed(ftw_corr_active) + $signed(delta_ftw);

// Saturation limit (signed interpretation)
wire signed [PHASEBITS-1:0] ftw_lim_signed = $signed(reg_ftw_lim[PHASEBITS-1:0]);
// 33-bit sign-extended copy for width-matched comparison against the extended sums
wire signed [PHASEBITS:0]   ftw_lim_signed_ext = {ftw_lim_signed[PHASEBITS-1], ftw_lim_signed};

//-----------------------------------------------------------------------------
// PROPORTIONAL PATH (PI CONTROL EXTENSION)
//-----------------------------------------------------------------------------

// Proportional multiply: delta_p_ftw = -KP_Q * e (same Q8.24 mechanics as integral)
wire signed [63:0] product_p = -($signed(reg_kp_q) * $signed(err_conditioned));
wire signed [63:0] product_p_rounded = product_p + (64'sd1 << (MU_QFRAC - 1));
wire signed [PHASEBITS-1:0] delta_p_ftw = product_p_rounded[MU_QFRAC +: PHASEBITS];

// Gate P term: zero when deadband active OR prop_enable=0
wire signed [PHASEBITS-1:0] p_term = (ctrl_prop_enable && !deadband_skip)
                                     ? delta_p_ftw
                                     : {PHASEBITS{1'b0}};

// PI saturation
// Compute full PI sum: current integrator state + proportional term
// Note: Use ftw_corr_active (x[n]), not sum_extended (x[n+1]), for correct PI formula:
//   u[n] = x[n] - K_p * e[n]
wire signed [PHASEBITS:0] ftw_pi_sum = $signed({ftw_corr_active[PHASEBITS-1], ftw_corr_active}) + $signed(p_term);

// Check saturation on full PI sum
wire sat_pi_pos = (ftw_pi_sum > ftw_lim_signed_ext);
wire sat_pi_neg = (ftw_pi_sum < -ftw_lim_signed_ext);
wire pi_saturated = sat_pi_pos | sat_pi_neg;

// Saturated PI output
wire signed [PHASEBITS-1:0] ftw_pi_final =
    sat_pi_pos ? ftw_lim_signed :
    sat_pi_neg ? -ftw_lim_signed :
    ftw_pi_sum[PHASEBITS-1:0];

// For I-only mode: separate saturation check on integrator path alone
wire sat_i_pos = (sum_extended > ftw_lim_signed_ext);
wire sat_i_neg = (sum_extended < -ftw_lim_signed_ext);
wire saturated = sat_i_pos | sat_i_neg;

// Saturated integrator value (used for I-only output and status reporting)
wire signed [PHASEBITS-1:0] ftw_corr_saturated =
    sat_i_pos ? ftw_lim_signed :
    sat_i_neg ? -ftw_lim_signed :
    sum_extended[PHASEBITS-1:0];

// Lock detector: count consecutive samples below deadband
wire in_lock_range = (err_abs < reg_deadband);

// Directional anti-windup: only freeze integrator if update would worsen saturation
// If PI sum saturates positive AND integrator would increase (delta > 0): freeze
// If PI sum saturates negative AND integrator would decrease (delta < 0): freeze
// Otherwise: allow integrator to update (helps recover from saturation)
wire freeze_integrator = (sat_pi_pos && !delta_ftw[PHASEBITS-1]) ||
                         (sat_pi_neg && delta_ftw[PHASEBITS-1]);

// New applied output for the active slot this update (held value if disabled/held/deadband)
wire signed [PHASEBITS-1:0] new_output =
    (ctrl_enable && !ctrl_hold && !deadband_skip)
        ? (ctrl_prop_enable ? ftw_pi_final : ftw_corr_saturated)
        : ftw_corr_active;  // held (deadband or hold); zeroed below when disabled

//-----------------------------------------------------------------------------
// INTEGRATOR UPDATE (Clocked Process)
//-----------------------------------------------------------------------------
integer si;
always @(posedge clk_i) begin
  if (!rstn_i) begin
    slot_sel_r              <= {SLOTSEL_BITS{1'b0}};
    ftw_correction_o        <= {PHASEBITS{1'b0}};
    ftw_correction_valid_o  <= 1'b0;
    upd_pipe                <= {MAC_DELAY{1'b0}};
    err_upd                 <= 32'h0;
    slot_upd                <= {SLOTSEL_BITS{1'b0}};
    for (si = 0; si < NSLOTS; si = si + 1) begin
      ftw_corr[si]          <= {PHASEBITS{1'b0}};
      ftw_out_r[si]         <= {PHASEBITS{1'b0}};
      err_latch[si]         <= 32'h0;
      flag_saturated[si]    <= 1'b0;
      flag_i_saturated[si]  <= 1'b0;
      flag_pi_saturated[si] <= 1'b0;
      lock_counter[si]      <= 16'h0;
    end

  end else begin
    // Register the slot selector (glitch-free datapath mux + bumpless output follow)
    slot_sel_r <= slot_sel;

    // Advance the MAC capture-delay pipeline (err_valid_i is a 1-clk strobe) and
    // snapshot the operands (error + slot) at the strobe so the delayed capture uses
    // the resonance that was live then, even if an LO hop switches the live slot/demod
    // during the MAC_DELAY window.
    upd_pipe <= {upd_pipe[MAC_DELAY-2:0], err_valid_i};
    if (err_valid_i) begin
      err_upd  <= err_i;
      slot_upd <= slot_sel_r;
    end

    // Default: de-assert valid; output continuously follows the active slot's stored
    // correction so it tracks LO hops the instant current_step changes (bumpless).
    ftw_correction_valid_o <= 1'b0;
    ftw_correction_o       <= ftw_out_r[slot_sel_r];

    // Clear command overrides everything: clear ALL slots
    if (ctrl_clr) begin
      for (si = 0; si < NSLOTS; si = si + 1) begin
        ftw_corr[si]          <= {PHASEBITS{1'b0}};
        ftw_out_r[si]         <= {PHASEBITS{1'b0}};
        lock_counter[si]      <= 16'h0;
        flag_saturated[si]    <= 1'b0;
        flag_i_saturated[si]  <= 1'b0;
        flag_pi_saturated[si] <= 1'b0;
      end
      ftw_correction_o <= {PHASEBITS{1'b0}};
    end

    // Disabled: hold output at zero and keep ALL slots cleared every cycle. This is
    // NOT gated on err_valid_i: a parked/frozen chain (multi-resonance) produces no
    // valid strobe, so the reset must not depend on one.
    else if (!ctrl_enable) begin
      for (si = 0; si < NSLOTS; si = si + 1) begin
        ftw_corr[si]          <= {PHASEBITS{1'b0}};
        ftw_out_r[si]         <= {PHASEBITS{1'b0}};
        lock_counter[si]      <= 16'h0;
        flag_saturated[si]    <= 1'b0;
        flag_i_saturated[si]  <= 1'b0;
        flag_pi_saturated[si] <= 1'b0;
      end
      ftw_correction_o <= {PHASEBITS{1'b0}};
    end

    // Update on the DELAYED valid strobe (`upd` = err_valid_i delayed MAC_DELAY clks),
    // so the deep integrator MAC has settled. All reads/writes use the SNAPSHOT slot
    // (slot_upd) and snapshot error (via err_conditioned/ftw_corr_active), so a capture
    // straddling an LO hop lands on the slot that was live at its err_valid -- never the
    // newly-selected slot. ctrl_enable is implied true here.
    else if (upd) begin

      if (!ctrl_hold) begin

        if (!deadband_skip) begin
          // Directional anti-windup: freeze integrator only if update would
          // worsen saturation. Allow updates that help recovery from saturation.
          if (!freeze_integrator) begin
            ftw_corr[slot_upd]         <= sum_extended[PHASEBITS-1:0];
          end
          flag_i_saturated[slot_upd] <= saturated;
          flag_pi_saturated[slot_upd] <= pi_saturated;
        end else begin
          // Deadband active: hold integrator, clear saturation flags
          flag_i_saturated[slot_upd]  <= 1'b0;
          flag_pi_saturated[slot_upd] <= 1'b0;
        end

        // Update combined saturation flag (any saturation, for legacy compatibility)
        flag_saturated[slot_upd] <= saturated | pi_saturated;

        // Always latch current error when valid (regardless of deadband)
        err_latch[slot_upd] <= err_conditioned;

        // Lock detector
        if (in_lock_range) begin
          if (lock_counter[slot_upd] < LOCK_COUNT_THRESH)
            lock_counter[slot_upd] <= lock_counter[slot_upd] + 16'd1;
        end else begin
          lock_counter[slot_upd] <= 16'h0;
        end

      end

      // Store the snapshot slot's applied correction (= held integrator when ctrl_hold).
      // This (and ftw_corr/flag_*) is the multicycle-constrained MAC capture. The
      // output bus ftw_correction_o is NOT driven here: it follows ftw_out_r[slot_sel_r]
      // (the LIVE slot) every clock (above), a short single-cycle path, so it stays off
      // the multicycle path and tracks the hop immediately.
      ftw_out_r[slot_upd] <= new_output;
      ftw_correction_valid_o <= 1'b1;
    end
  end
end

endmodule
