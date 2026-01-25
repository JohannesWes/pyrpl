/**
 * @file odmr_freq_lock_1f.v
 * @brief ODMR Frequency-Locked Loop (1f-I Component with PI Control)
 *
 * Implements a configurable frequency-locked loop (I-only or PI mode) that drives
 * a DDS frequency correction based on demodulated lock-in I-quadrature signal.
 *
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
 * 0x0000  CTRL        [RW]  Control bits (enable, invert, hold, clr, deadband_en, prop_enable)
 * 0x0004  MU_Q        [RW]  Integral gain μ_FTW in Q8.24 format
 * 0x0008  DEADBAND    [RW]  Error deadband threshold (unsigned LSB)
 * 0x000C  FTW_LIM     [RW]  Saturation limit for FTW correction (unsigned)
 * 0x0010  STATUS      [R ]  Status flags (locked, saturated, saturated_i, saturated_pi)
 * 0x0014  ERR_LATCH   [R ]  Last error value that produced update
 * 0x0018  FTW_INT     [R ]  Integrator state (for diagnostics)
 * 0x001C  KP_Q        [RW]  Proportional gain K_p,FTW in Q8.24 format
 * 0x0020  FTW_OUT     [R ]  Actual FTW correction output to DDS (includes P term in PI mode)
 */

module odmr_freq_lock_1f #(
  parameter integer PHASEBITS = 32,    // DDS phase accumulator width (must match 3FGEN)
  parameter integer MU_QFRAC  = 24     // Q-format fractional bits for gain (Q8.24)
)(
  // Clock and reset (125 MHz domain)
  input  wire                       clk_i,
  input  wire                       rstn_i,

  // Demodulated error from lock_in.v (Channel 1, I component)
  input  wire signed [31:0]         err_i,          // Demodulated I quadrature (LSB)
  input  wire                       err_valid_i,    // Valid strobe at ~30.5 kS/s

  // Output to red_pitaya_3fgen
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
// REGISTER ADDRESSES
//-----------------------------------------------------------------------------
localparam ADDR_CTRL      = 20'h00000;  // Control register
localparam ADDR_MU_Q      = 20'h00004;  // Gain μ_FTW (Q8.24)
localparam ADDR_DEADBAND  = 20'h00008;  // Deadband threshold
localparam ADDR_FTW_LIM   = 20'h0000C;  // FTW saturation limit
localparam ADDR_STATUS    = 20'h00010;  // Status flags
localparam ADDR_ERR_LATCH = 20'h00014;  // Latched error
localparam ADDR_FTW_INT   = 20'h00018;  // Integrator state (for diagnostics)
localparam ADDR_KP_Q      = 20'h0001C;  // Proportional gain (Q8.24)
localparam ADDR_FTW_OUT   = 20'h00020;  // Actual FTW correction output to DDS

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

// Lock detector: consecutive samples below threshold
localparam integer LOCK_COUNT_THRESH = 256;  // ~8.4 ms at 30.5 kS/s

//-----------------------------------------------------------------------------
// CONTROL & CONFIGURATION REGISTERS
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

//-----------------------------------------------------------------------------
// STATE REGISTERS
//-----------------------------------------------------------------------------
reg signed [PHASEBITS-1:0] ftw_corr;       // Integrator accumulator
reg signed [31:0]          err_latch;      // Last error processed
reg                        flag_saturated;  // Saturation indicator (any saturation, legacy)
reg                        flag_i_saturated;   // Integrator-only saturation
reg                        flag_pi_saturated;  // PI sum saturation
reg        [15:0]          lock_counter;    // Lock detector counter

//-----------------------------------------------------------------------------
// SYSTEM BUS INTERFACE
//-----------------------------------------------------------------------------
wire [19:0] sys_addr_local = sys_addr[19:0];  // Extract local address

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

        default: sys_err <= 1'b1;
      endcase
    end

    // Read operations
    if (sys_ren) begin
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

        ADDR_STATUS: begin
          sys_rdata <= {28'h0,
                        flag_pi_saturated,            // Bit 3: PI sum saturated
                        flag_i_saturated,             // Bit 2: Integrator saturated
                        flag_saturated,               // Bit 1: Any saturation (legacy)
                        (lock_counter == LOCK_COUNT_THRESH)}; // Bit 0: locked
        end

        ADDR_ERR_LATCH: sys_rdata <= err_latch;
        ADDR_FTW_INT:   sys_rdata <= ftw_corr;
        ADDR_FTW_OUT:   sys_rdata <= ftw_correction_o;

        default: begin
          sys_rdata <= 32'h0;
          sys_err   <= 1'b1;
        end
      endcase
    end
  end
end

//-----------------------------------------------------------------------------
// INTEGRAL CONTROLLER DATAPATH
//-----------------------------------------------------------------------------

// Error conditioning: apply inversion if requested
wire signed [31:0] err_conditioned = ctrl_invert ? -err_i : err_i;

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
wire signed [PHASEBITS:0] sum_extended = $signed(ftw_corr) + $signed(delta_ftw);

// Saturation limit (signed interpretation)
wire signed [PHASEBITS-1:0] ftw_lim_signed = $signed(reg_ftw_lim[PHASEBITS-1:0]);

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
// Note: Use ftw_corr (x[n]), not sum_extended (x[n+1]), for correct PI formula:
//   u[n] = x[n] - K_p * e[n]
wire signed [PHASEBITS:0] ftw_pi_sum = $signed({ftw_corr[PHASEBITS-1], ftw_corr}) + $signed(p_term);

// Check saturation on full PI sum
wire sat_pi_pos = (ftw_pi_sum > ftw_lim_signed);
wire sat_pi_neg = (ftw_pi_sum < -ftw_lim_signed);
wire pi_saturated = sat_pi_pos | sat_pi_neg;

// Saturated PI output
wire signed [PHASEBITS-1:0] ftw_pi_final =
    sat_pi_pos ? ftw_lim_signed :
    sat_pi_neg ? -ftw_lim_signed :
    ftw_pi_sum[PHASEBITS-1:0];

// For I-only mode: separate saturation check on integrator path alone
wire sat_i_pos = (sum_extended > ftw_lim_signed);
wire sat_i_neg = (sum_extended < -ftw_lim_signed);
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

//-----------------------------------------------------------------------------
// INTEGRATOR UPDATE (Clocked Process)
//-----------------------------------------------------------------------------
always @(posedge clk_i) begin
  if (!rstn_i) begin
    ftw_corr                <= {PHASEBITS{1'b0}};
    err_latch               <= 32'h0;
    flag_saturated          <= 1'b0;
    flag_i_saturated        <= 1'b0;
    flag_pi_saturated       <= 1'b0;
    lock_counter            <= 16'h0;
    ftw_correction_o        <= {PHASEBITS{1'b0}};
    ftw_correction_valid_o  <= 1'b0;

  end else begin

    // Clear command overrides everything
    if (ctrl_clr) begin
      ftw_corr           <= {PHASEBITS{1'b0}};
      lock_counter       <= 16'h0;
      flag_saturated     <= 1'b0;
      flag_i_saturated   <= 1'b0;
      flag_pi_saturated  <= 1'b0;
    end

    // Update on valid strobe when enabled and not held
    else if (err_valid_i) begin

      if (ctrl_enable && !ctrl_hold) begin

        if (!deadband_skip) begin
          // Directional anti-windup: freeze integrator only if update would
          // worsen saturation. Allow updates that help recovery from saturation.
          // This enables PI mode to acquire lock from large initial errors.
          if (!freeze_integrator) begin
            ftw_corr         <= sum_extended[PHASEBITS-1:0];
            flag_i_saturated <= saturated;
          end else begin
            // Integrator frozen (update would worsen saturation)
            ftw_corr         <= ftw_corr;
            flag_i_saturated <= saturated;
          end
          flag_pi_saturated <= pi_saturated;

        end else begin
          // Deadband active: hold integrator, clear saturation flags
          flag_i_saturated  <= 1'b0;
          flag_pi_saturated <= 1'b0;
        end

        // Update combined saturation flag (any saturation, for legacy compatibility)
        flag_saturated <= saturated | pi_saturated;

        // Always latch current error when valid (regardless of deadband)
        // This allows monitoring the error signal even when deadband is active
        err_latch <= err_conditioned;

        // Lock detector
        if (in_lock_range) begin
          if (lock_counter < LOCK_COUNT_THRESH)
            lock_counter <= lock_counter + 16'd1;
        end else begin
          lock_counter <= 16'h0;
        end

      end else if (!ctrl_enable) begin
        // When disabled, reset to zero
        ftw_corr           <= {PHASEBITS{1'b0}};
        lock_counter       <= 16'h0;
        flag_saturated     <= 1'b0;
        flag_i_saturated   <= 1'b0;
        flag_pi_saturated  <= 1'b0;
      end

      // Output: Use PI sum when prop enabled AND updating, else integrator only
      if (ctrl_enable && !ctrl_hold && !deadband_skip) begin
        if (ctrl_prop_enable) begin
          ftw_correction_o <= ftw_pi_final;  // PI mode
        end else begin
          ftw_correction_o <= ftw_corr_saturated;  // I-only mode (backward compatible)
        end
      end else begin
        ftw_correction_o <= ftw_corr;  // Held or disabled
      end
      ftw_correction_valid_o <= 1'b1;

    end else begin
      // De-assert valid when not updating
      ftw_correction_valid_o <= 1'b0;
    end
  end
end

endmodule
