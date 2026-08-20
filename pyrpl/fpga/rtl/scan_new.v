/**
 * @brief Scan block for triggered sweeps, streaming, and marker streaming.
 *
 * This module has three operating configurations:
 *   1. Scan: step-by-step triggered acquisition with 64-bit accumulation.
 *   2. Stream: continuous 32-bit DEMOD or FTW_CORR samples into data3 BRAM.
 *   3. Marker stream: stream mode plus x/y position-marker capture.
 *
 * Scan mode and stream mode are mutually exclusive. Marker stream is not a
 * separate data engine; it is stream mode with STREAM_CONTROL[2] enabled.
 *
 * Scan mode:
 *   - CONTROL/STATUS at 0x00 starts/stops/resets the scan FSM and reports
 *     busy/done.
 *   - Each step emits trigger_o, waits SETTLING_TIME, accumulates for
 *     DWELL_TIME, then writes:
 *       ram_lsb   = accumulator[31:0]
 *       ram_msb   = accumulator[63:32]
 *       ram_data3 = valid sample count
 *
 * Stream mode:
 *   - INPUT_SELECT selects 2=DEMOD or 3=FTW_CORR.
 *   - STREAM_CONTROL[0] enables the stream engine.
 *   - STREAM_CONTROL[1] pulses a stream reset.
 *   - Valid samples are written to ram_data3 as a 4096-word ring.
 *   - STREAM_WR_PTR and STREAM_SAMPLES expose the producer state.
 *
 * Dual-quantity (self-describing) stream mode (multi-resonance, 4-trace high-rate):
 *   - STREAM_CONTROL[4] enables dual-quantity mode (overrides INPUT_SELECT for the
 *     stream engine; the legacy single-word path is bypassed).
 *   - On each demod strobe (demod_input_valid_i; demod & ftw valids are coincident)
 *     FOUR 32-bit words are written into the data3 ring on four consecutive clocks:
 *       word0 = demod_input_i   (live error,  the active resonance's lock-in ch1)
 *       word1 = ftw_correction_i(live correction, the active slot's FTW correction)
 *       word2 = cic_input_i     (same active chain immediately before its FIR)
 *       word3 = current_step    (which resonance this sample belongs to, zero-extended)
 *     The 4096-clk strobe gap makes the 4-cycle burst trivially safe on the single
 *     write port. STREAM_SAMPLES counts WORDS (4 per strobe), matching the generic
 *     ARM word-drainer (stream_server.c is unchanged). The PC reshapes the drained
 *     word stream to (-1,4) and groups the quantities by the inline step column -> the
 *     label travels with the value, so they can never desync (no hop-marker ring is
 *     needed in this mode; ram_lsb/ram_msb are unused).
 *
 * Marked-continuous (uniform-rate, dead-time-aware) stream mode (STREAM_CONTROL[5]):
 *   - A NEW mode that emits a sample EVERY demod period (125 MHz / 4096 ~= 30.5 kHz)
 *     from a FREE-RUNNING /4096 tick, NOT from the aclken-gated demod strobe. So the
 *     word stream is a perfectly uniform time grid regardless of the per-hop freeze.
 *   - Same self-describing 4-word layout as dual mode (so the ARM push server and
 *     ring plumbing are unchanged), but word3 carries a STATE tag instead of a step:
 *       word0 = demod_input_i    (err; latched/sampled at the free tick)
 *       word1 = ftw_correction_i (corr; latched at the free tick)
 *       word2 = cic_input_i      (pre-FIR CIC value at the same free tick)
 *       word3 = state            = current_step (zero-extended) when the sample is
 *                                  LIVE (stream_live_i high), else DEAD = 32'hFFFFFFFF
 *                                  (= -1 as int32; cannot be a real step, survives the
 *                                  int32->float64 PC path unambiguously).
 *   - During a live dwell the slot is tagged with the active resonance; during the
 *     per-hop settle/freeze dead-time the slot is tagged DEAD. So one demod-period of
 *     dead-time occupies exactly one slot: PC time = sample_index / (125e6/4096) is
 *     EXACT (uniform), the dead-time is explicit, and per-resonance visit timing is
 *     correct (the old dual mode, gated by the demod strobe, silently compressed the
 *     dead-time and over-estimated the visit rate). When the multitrack oscillator is
 *     disabled, stream_live_i is constant 1, so the mode degenerates to "all samples
 *     live, tagged current_step, no dead" (single-resonance backward compatibility).
 *   - Overrides dual mode (bit4) and INPUT_SELECT for the stream engine; the legacy
 *     single-word path and dual path are bypassed. STREAM_SAMPLES counts WORDS (4 per
 *     tick). Decode on the PC with scan.reconstruct_marked_series().
 *
 * Marker stream mode:
 *   - STREAM_CONTROL[2] enables marker capture while streaming.
 *   - x_pos_trig_i (DIO5_P / exp_p_in[5]) marks fast-axis bin boundaries.
 *   - y_pos_trig_i (DIO6_P / exp_p_in[6]) marks slow-axis line boundaries.
 *   - A rising edge writes the current reg_stream_sample_cnt into:
 *       ram_lsb for x markers, ram_msb for y markers.
 *   - Marker value m means stream sample index m is the first sample after that
 *     spatial boundary. This is the hardware contract used by the PC-side
 *     reconstruction.
 *
 * Continuous / loop mode (multi-resonance indefinite hopping):
 *   - CONTROL bit3 (written together with the start bit0) latches reg_continuous
 *     for that run. The scan FSM then, in S_FINISHING, wraps step_counter->0 and
 *     re-enters S_START_STEP instead of going to S_DONE, so it keeps emitting the
 *     LO-hop trigger and advancing current_step (0..num_steps-1) indefinitely
 *     until an explicit stop/reset (CONTROL bit1/bit2). reg_busy stays high and
 *     reg_done never asserts. Set num_steps = N (number of resonances). The push
 *     stream + hop markers (below) run concurrently and key off current_step, so a
 *     single start() drives continuous multi-resonance tracking with no software
 *     re-arming. STATUS bit2 reads back reg_continuous.
 *
 * Register map, relative to module base:
 *   0x00 CONTROL/STATUS       write: start(0)/stop(1)/reset(2)/continuous(3, with start)
 *                             read: busy(0)/done(1)/continuous(2)
 *   0x04 NUM_STEPS            12-bit scan step count
 *   0x08 DWELL_TIME           scan dwell in 125 MHz clock cycles
 *   0x0C SETTLING_TIME        scan settle delay in clock cycles
 *   0x10 TRIGGER_LENGTH       trigger pulse length in clock cycles
 *   0x14 TRIGGER_PIN_SEL      stored, not physically routed here
 *   0x18 CURRENT_STEP         current scan step
 *   0x1C INPUT_SELECT         0=ADC, 1=IQ, 2=DEMOD, 3=FTW_CORR
 *   0x20 STREAM_CONTROL       bit0 enable, bit1 reset, bit2 x/y-marker en, bit3 hop-marker en, bit4 dual-quantity en, bit5 marked-continuous en
 *   0x24 STREAM_STATUS        bit0 active
 *   0x28 STREAM_WR_PTR        data3 ring write pointer
 *   0x2C STREAM_SAMPLES       total stream samples since reset
 *   0x30 MARKER_X_WR_PTR      x-marker ring write pointer
 *   0x34 MARKER_X_COUNT       total x markers since reset
 *   0x38 MARKER_Y_WR_PTR      y-marker ring write pointer
 *   0x3C MARKER_Y_COUNT       total y markers since reset
 *   0x40 HOP_WR_PTR           hop-marker ring write pointer
 *   0x44 HOP_COUNT            total hop markers since reset
 *   0x48 STREAM_FORMAT        words per self-describing sample (4)
 *
 * Hop-marker stream mode (multi-resonance monitoring, Phase B-stream):
 *   - STREAM_CONTROL[3] enables hop-boundary marker capture while streaming.
 *   - An internal current_step CHANGE (and one event at stream start, for the
 *     initial segment) records the pair (tick, current_step):
 *       ram_lsb = reg_stream_sample_cnt (tick = stream sample index)
 *       ram_msb = current_step          (which resonance the new segment belongs to)
 *   - The scan FSM may run concurrently (it drives the LO-hop trigger + advances
 *     current_step) while the push stream owns data3; scan accumulator BRAM writes
 *     are suppressed during streaming. Marker m means stream sample index tick_m is
 *     the first sample of a segment with the recorded current_step.
 *   - Reuses the (otherwise free) position-marker banks in monitoring mode; mutually
 *     exclusive with x/y position markers (2D motor scan gets a dedicated 4th bank).
 *
 * BRAM map:
 *   0x10000 ram_lsb    scan accumulator LSBs | x-marker ring | hop-marker tick ring
 *   0x20000 ram_msb    scan accumulator MSBs | y-marker ring | hop-marker step ring
 *   0x30000 ram_data3  scan sample counts or stream sample ring
 *
 * Timing notes:
 *   - clk is 125 MHz.
 *   - DEMOD and FTW_CORR valid pulses are expected every 4096 clocks.
 *   - BRAM reads use the 4-cycle system-bus read pipeline below.
 *
 * Full motor-scan design notes live in:
 *   docs/developer_guide/motor_position_sync_scan.md
 */
module scan #(
    parameter MAX_STEPS_BITS    = 12,                 // Maximum number of steps = 2^12 = 4096
    parameter DATA_WIDTH_ADC    = 14,                 // Input data width from ADC/DSP,
    parameter DATA_WIDTH_IQ     = 24,                 // Input data width from IQ-module demodulated output
    parameter DATA_WIDTH_DEMOD  = 32,                 // Demodulated data width
    parameter ACCUM_WIDTH       = 64,                 // Accumulator width. Bus aligned - 32 bits would only allow for 2^(32-14) / 125e6 s = 2 ms acquisition time per sample
    parameter BUS_DATA_WIDTH    = 32,                 // System bus data width
    parameter ADDR_WIDTH        = 32,                 // System bus address width
    parameter PIN_SELECT_BITS   = 3                   // Allows selecting 1 of 8 pins - selection not implemented yet.
)(
    // System Clock and Reset
    input wire                          clk,
    input wire                          rstn,          // Active low reset

    // Data Inputs - four inputs and option for input selection
    input wire signed [DATA_WIDTH_ADC-1:0]  adc_input_i,   // ADC input
    input wire signed [DATA_WIDTH_IQ-1:0]   iq_input_i,    // IQ demodulator input
    input wire signed [DATA_WIDTH_DEMOD-1:0] demod_input_i,     // Demodulated input (32-bit)
    input wire                          demod_input_valid_i,    // Valid signal for demodulated data
    input wire signed [DATA_WIDTH_DEMOD-1:0] cic_input_i,       // Same demod chain, after CIC and before FIR
    input wire                          cic_input_valid_i,      // Valid strobe for the CIC value
    input wire signed [DATA_WIDTH_DEMOD-1:0] ftw_correction_i,  // FTW correction from ODMR tracker
    input wire                          ftw_correction_valid_i,  // Valid signal for FTW correction

    // Live/dead level for marked-continuous stream mode (STREAM_CONTROL[5]). High
    // while the active demod chain is unfrozen (producing fresh samples), low during
    // the per-hop physical-settle freeze. Sourced from odmr_multitrack.valid_window_o
    // (= 1 when the multitrack oscillator is disabled, so the mode degenerates to
    // "always live"). Used only to tag samples LIVE (current_step) vs DEAD.
    input wire                          stream_live_i,

    // External position-step triggers from KDC101 motor controllers (one per axis).
    // In marker-streaming mode a rising edge records the current demod sample index
    // into the x/y marker rings (see MODE 3 below).
    input wire                          x_pos_trig_i,            // x-axis position pulse (fast-axis bin boundary)
    input wire                          y_pos_trig_i,            // y-axis position pulse (slow-axis line boundary)

    // Trigger Output
    output wire                         trigger_o,

    // Live scan-step index (which sweep point / resonance is active). Exported so
    // fgen3 can hardware-select the matching SSB calibration slot in lockstep with
    // the LO hop (multi-resonance tracking).
    output wire [MAX_STEPS_BITS-1:0]    current_step_o,

    // System Bus Interface (AXI-Lite Slave)
    input wire [ADDR_WIDTH-1:0]         sys_addr,      // Address
    input wire [BUS_DATA_WIDTH-1:0]     sys_wdata,     // Write data
    input wire [BUS_DATA_WIDTH/8-1:0]   sys_sel,       // Write byte select (unused for 32-bit regs)
    input wire                          sys_wen,       // Write enable
    input wire                          sys_ren,       // Read enable
    output reg [BUS_DATA_WIDTH-1:0]     sys_rdata,     // Read data
    output reg                          sys_err,       // Error indicator
    output reg                          sys_ack        // Acknowledge signal
);

//-----------------------------------------------------------------------------
// Parameters and Localparams
//-----------------------------------------------------------------------------
localparam BRAM_ADDR_BITS = MAX_STEPS_BITS;
localparam BRAM_DEPTH     = 1 << BRAM_ADDR_BITS;

// Address Map (relative to module base 0x4050_0000)
localparam ADDR_CONTROL         = 20'h00000; // W: Start(0), Stop(1), Reset(2); R: Status
localparam ADDR_STATUS          = 20'h00000; // R: Busy(0), Done(1)
localparam ADDR_NUM_STEPS       = 20'h00004; // R/W
localparam ADDR_DWELL_TIME      = 20'h00008; // R/W (cycles)
localparam ADDR_SETTLING_TIME   = 20'h0000C; // R/W (cycles)
localparam ADDR_TRIGGER_LENGTH  = 20'h00010; // R/W (cycles)
localparam ADDR_TRIGGER_PIN_SEL = 20'h00014; // R/W
localparam ADDR_CURRENT_STEP    = 20'h00018; // Read only
localparam ADDR_INPUT_SELECT    = 20'h0001C; // R/W Input source selection
// Streaming control/status (demodulated data ring buffer in data3 BRAM)
localparam ADDR_STREAM_CONTROL  = 20'h00020; // W/R: bit0 enable, bit1 reset, bit2 xy-marker, bit3 hop-marker, bit4 dual, bit5 marked-continuous

// Marked-continuous stream tick: one sample per DEMOD_DECIMATION clocks (free-running)
localparam DEMOD_DECIMATION_DIV = 4096; // 125 MHz / 4096 ~= 30.5 kHz demod sample rate
localparam DECIM_BITS           = 12;   // log2(DEMOD_DECIMATION_DIV)
localparam ADDR_STREAM_STATUS   = 20'h00024; // R: bit0 active
localparam ADDR_STREAM_WR_PTR   = 20'h00028; // R: current write pointer (mod BRAM depth)
localparam ADDR_STREAM_SAMPLES  = 20'h0002C; // R: total samples written since last reset
// Position-marker streaming (MODE 3): x markers live in ram_lsb, y markers in ram_msb
localparam ADDR_MARKER_X_WR_PTR = 20'h00030; // R: x-marker ring write pointer (mod BRAM depth)
localparam ADDR_MARKER_X_COUNT  = 20'h00034; // R: total x markers written since last reset
localparam ADDR_MARKER_Y_WR_PTR = 20'h00038; // R: y-marker ring write pointer (mod BRAM depth)
localparam ADDR_MARKER_Y_COUNT  = 20'h0003C; // R: total y markers written since last reset
// Hop-boundary marker streaming (multi-resonance, Phase B-stream): internal
// current_step edge -> (tick=stream sample idx in ram_lsb, step=current_step in ram_msb)
localparam ADDR_HOP_WR_PTR      = 20'h00040; // R: hop-marker ring write pointer (mod BRAM depth)
localparam ADDR_HOP_COUNT       = 20'h00044; // R: total hop markers written since last reset
localparam ADDR_STREAM_FORMAT   = 20'h00048; // R: words per self-describing sample
localparam [BUS_DATA_WIDTH-1:0] STREAM_WORDS_PER_SAMPLE = 32'd4;

// Input selection values
localparam INPUT_SELECT_ADC      = 2'b00;
localparam INPUT_SELECT_IQ       = 2'b01;
localparam INPUT_SELECT_DEMOD    = 2'b10;
localparam INPUT_SELECT_FTW_CORR = 2'b11;

// BRAM Address Mapping
// LSB Bank: Module Base + 0x10000 - 0x1FFFF (Relative Addr: 20'h1????)
// MSB Bank: Module Base + 0x20000 - 0x2FFFF (Relative Addr: 20'h2????)
// Data3 Bank: Module Base + 0x30000 - 0x3FFFF (Relative Addr: 20'h3????)
localparam BRAM_ADDR_OFFSET     = 2;
localparam BRAM_LSB_MAP_PATTERN = 20'h1????; // Relative addr pattern for LSB
localparam BRAM_MSB_MAP_PATTERN = 20'h2????; // Relative addr pattern for MSB
localparam BRAM_DATA3_MAP_PATTERN = 20'h3????; // Relative addr pattern for data3 (counts/stream)

// State Machine States
localparam STATE_WIDTH    = 4;
localparam S_IDLE         = 4'b0000;
localparam S_START_STEP   = 4'b0001;
localparam S_TRIGGERING   = 4'b0010;
localparam S_SETTLING     = 4'b0011;
localparam S_ACQUIRING    = 4'b0100;
localparam S_STORING_REQ  = 4'b0101; // Request BRAM Write
localparam S_STORING_WAIT = 4'b0110; // Wait for BRAM Write (1 cycle)
localparam S_FINISHING    = 4'b0111;
localparam S_DONE         = 4'b1000;

//-----------------------------------------------------------------------------
// Internal Registers and Wires
//-----------------------------------------------------------------------------

// State Machine
reg [STATE_WIDTH-1:0]       current_state;
reg [STATE_WIDTH-1:0]       next_state;

// Configuration Registers
reg [MAX_STEPS_BITS-1:0]    reg_num_steps;
reg [32-1:0]                reg_dwell_time;
reg [32-1:0]                reg_settling_time;
reg [32-1:0]                reg_trigger_length;
reg [PIN_SELECT_BITS-1:0]   reg_trigger_pin_select; // For top-level routing
reg [1:0]                   reg_input_select;       // Input selection register
reg                         reg_start_cmd;
reg                         reg_stop_cmd;
reg                         reg_reset_cmd;
reg                         reg_continuous;         // Continuous/loop mode: wrap step_counter->0 instead of S_DONE (multi-resonance indefinite hopping)

// Streaming control/status
reg                         reg_stream_enable;       // Streaming enable (level)
reg                         reg_stream_reset_cmd;    // One-cycle reset pulse for streaming engine
reg                         reg_stream_active;       // Indicates streaming is active
reg [BRAM_ADDR_BITS-1:0]    reg_stream_wr_ptr;       // Write pointer into BRAM (count bank)
reg [32-1:0]                reg_stream_sample_cnt;   // Total samples written since last stream reset (= WORDS in dual mode)

// Dual-quantity (self-describing) stream mode (STREAM_CONTROL[4]). On each demod
// strobe, four words [err, corr, cic, step] are written to the data3 ring over four
// consecutive clocks via a tiny phase FSM. err is written directly from
// demod_input_i on the strobe cycle; corr/cic/step are latched then so cycles 1..3 do
// not depend on the inputs staying valid.
reg                         reg_dual_quantity;       // Dual-quantity mode enable (level, STREAM_CONTROL[4])
reg [1:0]                   dual_phase;              // 0=idle, 1=corr, 2=cic, 3=step
reg signed [DATA_WIDTH_DEMOD-1:0] dual_corr_lat;     // latched ftw_correction_i for word1
reg signed [DATA_WIDTH_DEMOD-1:0] dual_cic_lat;      // latched cic_input_i for word2
reg [MAX_STEPS_BITS-1:0]    dual_step_lat;           // latched current_step for word3

// Marked-continuous (uniform-rate, dead-time-aware) stream mode (STREAM_CONTROL[5]).
// A free-running /4096 counter (NOT the aclken-gated demod strobe) drives the record
// writer, so a sample is emitted every demod period regardless of the per-hop freeze.
// word0=err (direct on tick), word1=corr, word2=cic, word3=state (all latched;
// state is current_step when LIVE, else DEAD sentinel).
reg                         reg_marked_continuous;   // Marked-continuous enable (level, STREAM_CONTROL[5])
reg [DECIM_BITS-1:0]        marked_free_cnt;         // free-running /4096 sample tick counter
reg [1:0]                   marked_phase;            // 0=idle, 1=corr, 2=cic, 3=state
reg signed [DATA_WIDTH_DEMOD-1:0] marked_corr_lat;   // latched ftw_correction_i for word1
reg signed [DATA_WIDTH_DEMOD-1:0] marked_cic_lat;    // latched cic_input_i for word2
reg [BUS_DATA_WIDTH-1:0]    marked_state_lat;        // latched state word for word3 (step or DEAD sentinel)
localparam [BUS_DATA_WIDTH-1:0] MARKED_DEAD = 32'hFFFFFFFF; // DEAD tag (= -1 int32; not a valid step)

// Position-marker streaming control/status (MODE 3)
reg                         reg_marker_enable;       // Marker capture enable (level)
reg [BRAM_ADDR_BITS-1:0]    reg_marker_x_wr_ptr;     // x-marker ring write pointer (into ram_lsb)
reg [32-1:0]                reg_marker_x_count;      // Total x markers since last reset
reg [BRAM_ADDR_BITS-1:0]    reg_marker_y_wr_ptr;     // y-marker ring write pointer (into ram_msb)
reg [32-1:0]                reg_marker_y_count;      // Total y markers since last reset

// Hop-boundary marker streaming control/status (multi-resonance, Phase B-stream).
// An internal current_step change records (tick, current_step) into ram_lsb/ram_msb.
// Monitoring mode reuses the two free position-marker banks (x/y markers are not used
// during pure time monitoring); 2D motor scans get a dedicated 4th bank later.
reg                         reg_hop_marker_enable;   // Hop-marker capture enable (level, STREAM_CONTROL[3])
reg [BRAM_ADDR_BITS-1:0]    reg_hop_wr_ptr;          // hop-marker ring write pointer (shared lsb/msb index)
reg [32-1:0]                reg_hop_count;           // Total hop markers since last reset
reg                         hop_primed;              // 0 until the first marker (records the initial segment's step)
reg [MAX_STEPS_BITS-1:0]    hop_step_prev;           // last current_step recorded into a marker

// Valid sample counter for demodulated mode
reg [32-1:0]                reg_valid_samples;      // Count of valid samples accumulated

// Input multiplexer - handle different data widths
reg signed [ACCUM_WIDTH-1:0] selected_input_extended;
always @(*) begin
    case (reg_input_select)
        INPUT_SELECT_ADC:      selected_input_extended = {{(ACCUM_WIDTH-DATA_WIDTH_ADC){adc_input_i[DATA_WIDTH_ADC-1]}}, adc_input_i};
        INPUT_SELECT_IQ:       selected_input_extended = {{(ACCUM_WIDTH-DATA_WIDTH_IQ){iq_input_i[DATA_WIDTH_IQ-1]}}, iq_input_i};
        INPUT_SELECT_DEMOD:    selected_input_extended = {{(ACCUM_WIDTH-DATA_WIDTH_DEMOD){demod_input_i[DATA_WIDTH_DEMOD-1]}}, demod_input_i};
        INPUT_SELECT_FTW_CORR: selected_input_extended = {{(ACCUM_WIDTH-DATA_WIDTH_DEMOD){ftw_correction_i[DATA_WIDTH_DEMOD-1]}}, ftw_correction_i};
        default:               selected_input_extended = {ACCUM_WIDTH{1'b0}};
    endcase
end

// Determine if we should accumulate this cycle
wire accumulate_enable = (reg_input_select == INPUT_SELECT_DEMOD) ? demod_input_valid_i :
                         (reg_input_select == INPUT_SELECT_FTW_CORR) ? ftw_correction_valid_i : 1'b1;

// Status Registers/Signals
reg                         reg_busy_flag;
reg                         reg_done_flag;
reg [MAX_STEPS_BITS-1:0]    reg_current_step; // Read only

// Export the live step index for cross-module cal-slot selection (fgen3).
assign current_step_o = reg_current_step;

// Hop-marker event: fire once when current_step changes (and once at stream start,
// via !hop_primed, to record the initial segment's step). Single-cycle pulse, since
// hop_step_prev is updated to the captured value on the same cycle.
wire hop_event = reg_stream_enable && reg_hop_marker_enable &&
                 (!hop_primed || (reg_current_step != hop_step_prev));

// Dual-quantity write FSM helpers (combinational). dual_start fires on the strobe
// when idle (writes word0=err that cycle); phases 1..3 write corr/cic/step.
// dual_wr is high on every cycle a record word is being written
// (drives the ptr/word-counter increment and the data3 write-enable).
wire dual_mode  = reg_stream_enable && reg_dual_quantity;
wire dual_start = dual_mode && demod_input_valid_i && (dual_phase == 2'd0);
wire dual_wr    = dual_start || (dual_phase != 2'd0);

// Marked-continuous write FSM helpers (combinational). marked_mode takes priority
// over dual mode. The free-running counter marked_free_cnt wraps every
// DEMOD_DECIMATION_DIV clocks; marked_tick fires once per wrap (a uniform 30.5 kHz
// cadence INDEPENDENT of the aclken-gated demod strobe, so dead-time is represented).
// marked_start writes word0=err on the tick when idle; phases 1..3 write
// corr/cic/state. The 4096-clk gap makes the 4-cycle
// burst trivially safe on the single write port.
wire marked_mode  = reg_stream_enable && reg_marked_continuous;
wire marked_tick  = marked_mode && (marked_free_cnt == {DECIM_BITS{1'b0}});
wire marked_start = marked_tick && (marked_phase == 2'd0);
wire marked_wr    = marked_start || (marked_phase != 2'd0);

// Counters
reg [MAX_STEPS_BITS-1:0]    step_counter;
reg [32-1:0]                trigger_counter;
reg [32-1:0]                settling_counter;
reg [32-1:0]                dwell_counter;

// Accumulator
reg signed [ACCUM_WIDTH-1:0] accum;

// BRAM signals (Write Port A)
reg                         bram_wr_en;
wire [BRAM_ADDR_BITS-1:0]   bram_wr_addr;
wire [BUS_DATA_WIDTH-1:0]   bram_wr_data_lsb;
wire [BUS_DATA_WIDTH-1:0]   bram_wr_data_msb;
wire [BUS_DATA_WIDTH-1:0]   bram_wr_data_data3;     // Data3 bank data (sample counts or stream data)

// BRAM signals (Read Port B)
wire [BRAM_ADDR_BITS-1:0]   bram_rd_addr_in;        // Combinatorial BRAM address from sys_addr
reg  [BRAM_ADDR_BITS-1:0]   bram_rd_addr_p1;        // Pipelined BRAM address stage 1
reg  [BRAM_ADDR_BITS-1:0]   bram_rd_addr_p2;        // Pipelined BRAM address stage 2 (used for BRAM read)

reg  [BUS_DATA_WIDTH-1:0]   bram_rd_data_lsb_raw;
reg  [BUS_DATA_WIDTH-1:0]   bram_rd_data_msb_raw;
reg  [BUS_DATA_WIDTH-1:0]   bram_rd_data_data3_raw; // Raw data3 data from BRAM
reg  [BUS_DATA_WIDTH-1:0]   bram_rd_data_lsb_reg;   // Registered BRAM data (after BRAM read latency)
reg  [BUS_DATA_WIDTH-1:0]   bram_rd_data_msb_reg;   // Registered BRAM data (after BRAM read latency)
reg  [BUS_DATA_WIDTH-1:0]   bram_rd_data_data3_reg; // Registered data3 data


//-----------------------------------------------------------------------------
// BRAM Read Logic and System Bus Pipelining
//-----------------------------------------------------------------------------
// Reading from the BRAM is a multi-cycle operation due to the synchronous
// nature of BRAMs and the need to meet timing constraints. A full read
// transaction has a 4-cycle latency from request to acknowledge.
//
// The pipeline stages are as follows:
//
// Cycle 0: CPU asserts sys_ren & sys_addr.
//   - Combinatorial logic detects a BRAM access (is_bram_access).
//   - bram_rd_addr_in latches the address from the bus.
//
// Cycle 1:
//   - sys_ren_p1, is_bram_access_p1, and bram_rd_addr_p1 are registered.
//
// Cycle 2:
//   - sys_ren_p2, is_bram_access_p2, and bram_rd_addr_p2 are registered.
//   - The BRAM read is initiated using the address from the p1 stage.
//
// Cycle 3:
//   - is_bram_access_p3 is registered.
//   - BRAM data is available and is latched into bram_rd_data_*_reg.
//
// Cycle 4:
//   - The 4-stage bram_ack_delay_pipe asserts bram_read_ack_delayed.
//   - sys_ack is asserted, and sys_rdata is driven with the registered data.
//
// The Python framework's _reads() method handles this delay automatically
// by waiting for sys_ack.
//-----------------------------------------------------------------------------
always @(posedge clk) begin
    if (sys_ren_p1 && is_bram_access_p1) begin  // Read on cycle p1
        if (bram_access_lsb_p1) begin
            bram_rd_data_lsb_raw <= ram_lsb[bram_rd_addr_p1];
        end else if (bram_access_msb_p1) begin
            bram_rd_data_msb_raw <= ram_msb[bram_rd_addr_p1];
        end else if (bram_access_data3_p1) begin
            bram_rd_data_data3_raw <= ram_data3[bram_rd_addr_p1];
        end
    end
end



// Adjust the pipeline stages and acknowledge delay
reg  [4-1:0]  bram_ack_delay_pipe;    // Increase from 3 to 4 stages
wire bram_read_ack_delayed;

// Trigger logic
reg                         trigger_pulse_active;

// System bus signals
wire [19:0]                 reg_addr = sys_addr[19:0];
wire                        sys_en = sys_wen || sys_ren;

// BRAM Access Detection (combinatorial based on current sys_addr)
wire                        bram_access_lsb = (reg_addr[19:16] == 4'h1); // Check for address range 0x10000 - 0x1FFFF
wire                        bram_access_msb = (reg_addr[19:16] == 4'h2); // Check for address range 0x20000 - 0x2FFFF
wire                        bram_access_data3 = (reg_addr[19:16] == 4'h3); // Check for address range 0x30000 - 0x3FFFF
wire                        is_bram_access = bram_access_lsb || bram_access_msb || bram_access_data3;

// Pipelined BRAM Access Control Signals
reg                         sys_ren_p1, sys_ren_p2;
reg                         is_bram_access_p1, is_bram_access_p2, is_bram_access_p3;
reg                         bram_access_lsb_p1, bram_access_lsb_p2;
reg                         bram_access_msb_p1, bram_access_msb_p2;
reg                         bram_access_data3_p1, bram_access_data3_p2; // Pipeline for data3 BRAM
reg [1:0]                   select_bram_for_rdata_p3; // Delayed select for sys_rdata muxing (00=lsb, 01=msb, 10=data3)

//-----------------------------------------------------------------------------
// Configuration Register Logic
//-----------------------------------------------------------------------------
always @(posedge clk) begin
    if (!rstn) begin
        reg_num_steps          <= {MAX_STEPS_BITS{1'b0}};
        reg_dwell_time         <= 32'd125000; // Default 1ms
        reg_settling_time      <= 32'd12500;  // Default 0.1ms
        reg_trigger_length     <= 32'd6250;    // Default 50 us
        reg_trigger_pin_select <= {PIN_SELECT_BITS{1'b0}};
        reg_input_select       <= INPUT_SELECT_ADC; // Default to ADC input
        reg_start_cmd          <= 1'b0;
        reg_stop_cmd           <= 1'b0;
        reg_reset_cmd          <= 1'b0;
        reg_continuous         <= 1'b0;
    // Stream defaults
    reg_stream_enable      <= 1'b0;
    reg_stream_reset_cmd   <= 1'b0;
    reg_marker_enable      <= 1'b0;
    reg_hop_marker_enable  <= 1'b0;
    reg_dual_quantity      <= 1'b0;
    reg_marked_continuous  <= 1'b0;
    end else begin
        // Clear command flags after one cycle
        reg_start_cmd <= 1'b0;
        reg_stop_cmd  <= 1'b0;
        reg_reset_cmd <= 1'b0;
        reg_stream_reset_cmd <= 1'b0;

        if (sys_wen && !reg_busy_flag) begin // Only allow scan config writes when not busy
            case (reg_addr)
                ADDR_CONTROL: begin
                    if (sys_wdata[0]) begin
                        reg_start_cmd  <= 1'b1;
                        // bit3 latches continuous/loop mode for THIS run: in S_FINISHING
                        // the FSM wraps step_counter->0 and keeps emitting LO-hop triggers
                        // + advancing current_step forever instead of stopping at S_DONE.
                        reg_continuous <= sys_wdata[3];
                    end
                    // Stop and Reset can be asserted while busy
                    // if(sys_wdata[1]) reg_stop_cmd  <= 1'b1; // Stop handled directly in FSM
                    // if(sys_wdata[2]) reg_reset_cmd <= 1'b1; // Reset handled directly in FSM
                end
                ADDR_NUM_STEPS:       reg_num_steps          <= sys_wdata[MAX_STEPS_BITS-1:0];
                ADDR_DWELL_TIME:      reg_dwell_time         <= sys_wdata;
                ADDR_SETTLING_TIME:   reg_settling_time      <= sys_wdata;
                ADDR_TRIGGER_LENGTH:  reg_trigger_length     <= sys_wdata;
                ADDR_TRIGGER_PIN_SEL: reg_trigger_pin_select <= sys_wdata[PIN_SELECT_BITS-1:0];
                ADDR_INPUT_SELECT:    reg_input_select       <= sys_wdata[1:0];
                default: ;
            endcase
        end
        // Allow Stop/Reset commands even when busy
         if (sys_wen && reg_addr == ADDR_CONTROL) begin
              if(sys_wdata[1]) reg_stop_cmd  <= 1'b1;
              if(sys_wdata[2]) reg_reset_cmd <= 1'b1;
              // Leaving continuous mode: a stop/reset ends the loop cleanly.
              if(sys_wdata[1] || sys_wdata[2]) reg_continuous <= 1'b0;
         end
        // Streaming control is independent of scan acquisition, so it stays writable
        // even while the scan FSM is busy. This lets the scan FSM drive LO hops while
        // a push stream + hop markers run concurrently (multi-resonance monitoring).
        //   bit0 enable, bit1 reset (pulse), bit2 position(x/y)-marker en, bit3 hop-marker en
        if (sys_wen && reg_addr == ADDR_STREAM_CONTROL) begin
            reg_stream_enable     <= sys_wdata[0];
            reg_marker_enable     <= sys_wdata[2];
            reg_hop_marker_enable <= sys_wdata[3];
            reg_dual_quantity     <= sys_wdata[4];
            reg_marked_continuous <= sys_wdata[5];
            if (sys_wdata[1])     reg_stream_reset_cmd <= 1'b1;
        end
    end
end

//-----------------------------------------------------------------------------
// Status Register Logic
//-----------------------------------------------------------------------------
always @(posedge clk) begin
    if (!rstn) begin
        reg_busy_flag <= 1'b0;
        reg_done_flag <= 1'b0;
    end else begin
        if (reg_reset_cmd || reg_stop_cmd) begin
            reg_busy_flag <= 1'b0;
            reg_done_flag <= 1'b0; // Reset done flag on reset/stop
        end else if (reg_start_cmd && current_state == S_IDLE) begin
            reg_busy_flag <= 1'b1;
            reg_done_flag <= 1'b0;
        end else if (current_state == S_DONE && next_state == S_IDLE) begin // Exiting DONE state
             // Keep flags as they are until next start/reset
        end else if (current_state == S_FINISHING && next_state == S_DONE) begin
            reg_busy_flag <= 1'b0;
            reg_done_flag <= 1'b1;
        end
    end
end

// reg_current_step is updated within the state machine logic directly

//-----------------------------------------------------------------------------
// State Machine Logic - Two Process Style
//-----------------------------------------------------------------------------

// Combinational next state logic - calculates what next_state should be (Multiplexer)
// State transition logic
always @(*) begin
    // Default assignment for next_state - if the sub-conditions for the state-transitions are not met yet, stay in the current state
    next_state = current_state;
    
    case (current_state)
        // Scan may run concurrently with streaming: the scan FSM then drives LO-hop
        // triggers + advances current_step (for hop markers) while the push stream
        // owns the data3 ring. Its BRAM accumulator writes are suppressed during
        // streaming (see bram_wr_en), so there is no data3 contention.
        S_IDLE:         if (reg_start_cmd && reg_num_steps > 0) next_state = S_START_STEP;
        
        S_START_STEP:   next_state = S_TRIGGERING;
        
        S_TRIGGERING:   if (reg_trigger_length == 0) next_state = S_SETTLING;
                        else if (trigger_counter >= reg_trigger_length - 1) next_state = S_SETTLING;
        
        S_SETTLING:     if (reg_settling_time == 0) next_state = S_ACQUIRING;
                        else if (settling_counter >= reg_settling_time - 1) next_state = S_ACQUIRING;
        
        S_ACQUIRING:    if (reg_dwell_time == 0) next_state = S_STORING_REQ;
                        else if (dwell_counter >= reg_dwell_time - 1) next_state = S_STORING_REQ;
        
        // Always wait one cycle for write
        S_STORING_REQ:  next_state = S_STORING_WAIT;
        
        S_STORING_WAIT: next_state = S_FINISHING;
        
        // Continue or complete based on step counter. In continuous/loop mode the
        // last step wraps back to a new step (S_START_STEP) instead of finishing,
        // so the FSM keeps emitting LO-hop triggers + advancing current_step until
        // an explicit stop/reset (multi-resonance indefinite hopping).
        S_FINISHING:    if (step_counter >= reg_num_steps - 1)
                            next_state = reg_continuous ? S_START_STEP : S_DONE;
                        else next_state = S_START_STEP;
        
        // Allow restart from DONE
        S_DONE:         if (reg_reset_cmd || reg_stop_cmd || reg_start_cmd) next_state = S_IDLE;
        
        default:        next_state = S_IDLE;
    endcase
    
    // Global overrides for Stop/Reset
    if (reg_reset_cmd || reg_stop_cmd) next_state = S_IDLE;
end

// Sequential state register - sets state to next_state
always @(posedge clk) begin
    if (!rstn) current_state <= S_IDLE;
    else current_state <= next_state;
end

//-----------------------------------------------------------------------------
// External position-trigger edge detection (KDC101 -> expansion inputs)
//-----------------------------------------------------------------------------
// Two-FF synchronizers + rising-edge detect + per-axis holdoff. The KDC pulses
// are long (~10-100 us = thousands of 8 ns cycles) relative to the holdoff, so a
// single clean event is registered per encoder position. The holdoff rejects
// level-shifter ringing / contact bounce on the active edge. Detection runs
// continuously; the captured pulse is only consumed when marker mode is enabled.
localparam [15:0] MARKER_HOLDOFF = 16'd1250; // ~10 us at 125 MHz

reg        x_trig_s0, x_trig_s1, x_trig_s2;
reg        y_trig_s0, y_trig_s1, y_trig_s2;
reg [15:0] x_holdoff_cnt, y_holdoff_cnt;

wire x_edge  = x_trig_s1 & ~x_trig_s2;   // synchronized rising edge
wire y_edge  = y_trig_s1 & ~y_trig_s2;
wire x_pulse = x_edge & (x_holdoff_cnt == 16'd0);
wire y_pulse = y_edge & (y_holdoff_cnt == 16'd0);

always @(posedge clk) begin
    if (!rstn) begin
        x_trig_s0 <= 1'b0; x_trig_s1 <= 1'b0; x_trig_s2 <= 1'b0;
        y_trig_s0 <= 1'b0; y_trig_s1 <= 1'b0; y_trig_s2 <= 1'b0;
        x_holdoff_cnt <= 16'd0; y_holdoff_cnt <= 16'd0;
    end else begin
        x_trig_s0 <= x_pos_trig_i; x_trig_s1 <= x_trig_s0; x_trig_s2 <= x_trig_s1;
        y_trig_s0 <= y_pos_trig_i; y_trig_s1 <= y_trig_s0; y_trig_s2 <= y_trig_s1;

        if (x_pulse)                      x_holdoff_cnt <= MARKER_HOLDOFF;
        else if (x_holdoff_cnt != 16'd0)  x_holdoff_cnt <= x_holdoff_cnt - 16'd1;

        if (y_pulse)                      y_holdoff_cnt <= MARKER_HOLDOFF;
        else if (y_holdoff_cnt != 16'd0)  y_holdoff_cnt <= y_holdoff_cnt - 16'd1;
    end
end

//-----------------------------------------------------------------------------
// Counters and Accumulator Logic
//-----------------------------------------------------------------------------
always @(posedge clk) begin
    if (!rstn) begin
        step_counter     <= {MAX_STEPS_BITS{1'b0}};
        trigger_counter  <= 32'b0;
        settling_counter <= 32'b0;
        dwell_counter    <= 32'b0;
        accum            <= {ACCUM_WIDTH{1'b0}};
        reg_current_step <= {MAX_STEPS_BITS{1'b0}};
        reg_valid_samples <= 32'b0;
    // Streaming state
    reg_stream_active   <= 1'b0;
    reg_stream_wr_ptr   <= {BRAM_ADDR_BITS{1'b0}};
    reg_stream_sample_cnt <= 32'b0;
    // Marker state
    reg_marker_x_wr_ptr <= {BRAM_ADDR_BITS{1'b0}};
    reg_marker_x_count  <= 32'b0;
    reg_marker_y_wr_ptr <= {BRAM_ADDR_BITS{1'b0}};
    reg_marker_y_count  <= 32'b0;
    // Hop-marker state
    reg_hop_wr_ptr      <= {BRAM_ADDR_BITS{1'b0}};
    reg_hop_count       <= 32'b0;
    hop_primed          <= 1'b0;
    hop_step_prev       <= {MAX_STEPS_BITS{1'b0}};
    end else begin
        // Reset conditions
        if (current_state == S_IDLE) begin // Reset counters when idle
             step_counter     <= {MAX_STEPS_BITS{1'b0}};
             reg_current_step <= {MAX_STEPS_BITS{1'b0}};
             // Other counters reset in S_START_STEP
        end

         if (next_state == S_START_STEP) begin // beginning a new STEP, not a new SWEEP - Only reset per-step counters and accumulator here
              trigger_counter  <= 32'b0;
              settling_counter <= 32'b0;
              dwell_counter    <= 32'b0;
              accum            <= {ACCUM_WIDTH{1'b0}};
              reg_valid_samples <= 32'b0;  // Reset valid sample counter for new step
         end

        // Increment logic based on current state
        case (current_state)
            // Publish the live resonance index ONE state before the hop trigger.
            // step_counter was already incremented (or wrapped) in S_FINISHING, so by
            // S_START_STEP it holds the NEW step; latching reg_current_step here makes
            // current_step_o switch at the hop (before trigger_o fires in S_TRIGGERING)
            // instead of after the settle. Downstream consumers (odmr_multitrack freeze
            // window, odmr_freq_lock_1f slot select, fgen3 cal slot, and the marked-
            // continuous LIVE/DEAD stream tag) therefore all switch in lockstep with the
            // physical MW hop, so the parked chain + controller stay frozen across the
            // MW settle and the old resonance is never contaminated with transition data.
            S_START_STEP:   reg_current_step <= step_counter;

            // Increment trigger counter until length reached
            S_TRIGGERING:   if (trigger_counter < reg_trigger_length) trigger_counter <= trigger_counter + 1;
            
            // Increment settling counter until settling time reached
            S_SETTLING:     if (settling_counter < reg_settling_time) settling_counter <= settling_counter + 1;
            
            S_ACQUIRING:    begin
                                // reg_current_step already holds this step (latched in
                                // S_START_STEP, before the hop trigger); step_counter is
                                // unchanged until S_FINISHING, so no update is needed here.

                                // Increment dwell_counter (counts in clock cycles, not in sample cycles)
                                if (dwell_counter < reg_dwell_time) begin
                                    dwell_counter <= dwell_counter + 1;
                                    
                                    // Only accumulate samples when data is valid
                                    if (accumulate_enable) begin
                                        accum <= accum + selected_input_extended;
                                        reg_valid_samples <= reg_valid_samples + 1;  // Count valid samples
                                    end
                                end
                            end
            
            // Increment step counter after storing. In continuous/loop mode the
            // last step wraps the counter back to 0 (next_state is then S_START_STEP)
            // so current_step cycles 0..num_steps-1 indefinitely.
            S_FINISHING:    if (reg_continuous && (step_counter >= reg_num_steps - 1))
                                step_counter <= {MAX_STEPS_BITS{1'b0}};
                            else
                                step_counter <= step_counter + 1;
            
            default: ; // No counter updates in other states
        endcase

        // Handle external reset/stop
        if (reg_reset_cmd || reg_stop_cmd) begin
            step_counter     <= {MAX_STEPS_BITS{1'b0}};
            trigger_counter  <= 32'b0;
            settling_counter <= 32'b0;
            dwell_counter    <= 32'b0;
            accum            <= {ACCUM_WIDTH{1'b0}};
            reg_current_step <= {MAX_STEPS_BITS{1'b0}};
            reg_valid_samples <= 32'b0;
        end

        // ------------------------------------------------------------------
        // Streaming engine (demodulated input -> ring buffer in data3 BRAM)
        // ------------------------------------------------------------------
        // Reset streaming engine (also clears the position-marker rings)
        if (reg_stream_reset_cmd || !reg_stream_enable) begin
            reg_stream_active     <= 1'b0;
            reg_stream_wr_ptr     <= {BRAM_ADDR_BITS{1'b0}};
            reg_stream_sample_cnt <= 32'b0;
            reg_marker_x_wr_ptr   <= {BRAM_ADDR_BITS{1'b0}};
            reg_marker_x_count    <= 32'b0;
            reg_marker_y_wr_ptr   <= {BRAM_ADDR_BITS{1'b0}};
            reg_marker_y_count    <= 32'b0;
            reg_hop_wr_ptr        <= {BRAM_ADDR_BITS{1'b0}};
            reg_hop_count         <= 32'b0;
            hop_primed            <= 1'b0;
            hop_step_prev         <= {MAX_STEPS_BITS{1'b0}};
            dual_phase            <= 2'd0;
            marked_free_cnt       <= {DECIM_BITS{1'b0}};
            marked_phase          <= 2'd0;
        end else if (reg_stream_enable) begin
            reg_stream_active <= 1'b1;
            if (reg_marked_continuous) begin
                // Marked-continuous mode (priority over dual): a free-running /4096
                // counter drives the record writer, so a sample is emitted EVERY
                // demod period regardless of the per-hop freeze (uniform time grid).
                // ptr/word-counter advance on every written word (STREAM_SAMPLES =
                // WORDS = 4x ticks). corr/cic/state are latched at the tick so later cycles
                // are input-independent. state = current_step (LIVE, stream_live_i
                // high) or DEAD sentinel (frozen / in settle).
                marked_free_cnt <= (marked_free_cnt == DEMOD_DECIMATION_DIV - 1) ?
                                   {DECIM_BITS{1'b0}} : marked_free_cnt + 1'b1;
                if (marked_wr) begin
                    reg_stream_wr_ptr     <= reg_stream_wr_ptr + 1'b1;
                    reg_stream_sample_cnt <= reg_stream_sample_cnt + 1'b1;
                end
                case (marked_phase)
                    2'd0: if (marked_start) begin
                              marked_phase     <= 2'd1;
                              marked_corr_lat  <= ftw_correction_i;
                              marked_cic_lat   <= cic_input_i;
                              marked_state_lat <= stream_live_i ?
                                  { {(BUS_DATA_WIDTH-MAX_STEPS_BITS){1'b0}}, reg_current_step } :
                                  MARKED_DEAD;
                          end
                    2'd1: marked_phase <= 2'd2;
                    2'd2: marked_phase <= 2'd3;
                    2'd3: marked_phase <= 2'd0;
                    default: marked_phase <= 2'd0;
                endcase
            end else if (reg_dual_quantity) begin
                // Dual-quantity mode: write four words [err, corr, cic, step] per strobe
                // over four consecutive cycles. ptr/word-counter advance on every
                // written word (so STREAM_SAMPLES counts WORDS = 4x strobes). The data3
                // write itself is in the unified write block; here we run the phase FSM
                // and latch corr/step at the strobe so cycles 1/2 are input-independent.
                if (dual_wr) begin
                    reg_stream_wr_ptr     <= reg_stream_wr_ptr + 1'b1;
                    reg_stream_sample_cnt <= reg_stream_sample_cnt + 1'b1;
                end
                case (dual_phase)
                    2'd0: if (dual_start) begin
                              dual_phase    <= 2'd1;
                              dual_corr_lat <= ftw_correction_i;
                              dual_cic_lat  <= cic_input_i;
                              dual_step_lat <= reg_current_step;
                          end
                    2'd1: dual_phase <= 2'd2;
                    2'd2: dual_phase <= 2'd3;
                    2'd3: dual_phase <= 2'd0;
                    default: dual_phase <= 2'd0;
                endcase
            end else begin
                // Legacy single-word streaming (DEMOD or FTW_CORR)
                if (reg_input_select == INPUT_SELECT_DEMOD && demod_input_valid_i) begin
                    // Increment pointer and sample counter; actual memory write handled in unified write block
                    reg_stream_wr_ptr <= reg_stream_wr_ptr + 1'b1;
                    reg_stream_sample_cnt <= reg_stream_sample_cnt + 1'b1;
                end else if (reg_input_select == INPUT_SELECT_FTW_CORR && ftw_correction_valid_i) begin
                    // Increment pointer and sample counter for FTW correction streaming
                    reg_stream_wr_ptr <= reg_stream_wr_ptr + 1'b1;
                    reg_stream_sample_cnt <= reg_stream_sample_cnt + 1'b1;
                end
            end

            // Position-marker capture: on each external position pulse, record the
            // current demod sample index (reg_stream_sample_cnt) into the x/y marker
            // ring (write itself handled in the unified BRAM write block below). The
            // pre-increment pointer/count values are used so they stay aligned with
            // the write address, which also uses the pre-increment pointer.
            if (reg_marker_enable) begin
                if (x_pulse) begin
                    reg_marker_x_wr_ptr <= reg_marker_x_wr_ptr + 1'b1;
                    reg_marker_x_count  <= reg_marker_x_count + 1'b1;
                end
                if (y_pulse) begin
                    reg_marker_y_wr_ptr <= reg_marker_y_wr_ptr + 1'b1;
                    reg_marker_y_count  <= reg_marker_y_count + 1'b1;
                end
            end

            // Hop-boundary marker capture: on each current_step change (and once at
            // stream start to record the initial segment), record the current stream
            // sample index (reg_stream_sample_cnt, pre-increment, aligned with the
            // write address) and the new current_step. The BRAM write is in the
            // unified lsb/msb write block (tick -> ram_lsb, step -> ram_msb).
            if (hop_event) begin
                reg_hop_wr_ptr <= reg_hop_wr_ptr + 1'b1;
                reg_hop_count  <= reg_hop_count + 1'b1;
                hop_primed     <= 1'b1;
                hop_step_prev  <= reg_current_step;
            end
        end
    end
end

//-----------------------------------------------------------------------------
// Trigger Pulse Logic
//-----------------------------------------------------------------------------
always @(posedge clk) begin
     if (!rstn) begin
          trigger_pulse_active <= 1'b0;
     end else begin
          if (current_state == S_TRIGGERING) begin
               trigger_pulse_active <= 1'b1;
          end else begin
               trigger_pulse_active <= 1'b0;
          end
          if (reg_reset_cmd || reg_stop_cmd) begin // Ensure trigger goes low on stop/reset
                trigger_pulse_active <= 1'b0;
          end
     end
end
assign trigger_o = trigger_pulse_active; // Connect internal signal to output

//-----------------------------------------------------------------------------
// BRAM Implementation (Using inferred dual-port BRAM)
//-----------------------------------------------------------------------------
(* ram_style = "block" *) reg [BUS_DATA_WIDTH-1:0] ram_lsb [0:BRAM_DEPTH-1];
(* ram_style = "block" *) reg [BUS_DATA_WIDTH-1:0] ram_msb [0:BRAM_DEPTH-1];
(* ram_style = "block" *) reg [BUS_DATA_WIDTH-1:0] ram_data3 [0:BRAM_DEPTH-1];  // Dual-purpose: sample counts (scan) or stream data (stream)

// Port A: Write Port (Synchronous) - Controlled by State Machine
assign bram_wr_addr     = step_counter;
assign bram_wr_data_lsb = accum[BUS_DATA_WIDTH-1:0];
assign bram_wr_data_msb = accum[ACCUM_WIDTH-1:BUS_DATA_WIDTH];
assign bram_wr_data_data3 = reg_valid_samples;  // Assign valid sample count

// Streaming write enable (legacy single-word demod or FTW stream into data3 BRAM
// ring). Excluded in dual-quantity mode, which has its own 3-word writer (dual_wr).
wire stream_wr_en = reg_stream_enable && !reg_dual_quantity &&
                    ((reg_input_select == INPUT_SELECT_DEMOD && demod_input_valid_i) ||
                     (reg_input_select == INPUT_SELECT_FTW_CORR && ftw_correction_valid_i));
// Muxed write controls for data3 BRAM (single-port write template)
reg                       data3_we_mux;
reg [BRAM_ADDR_BITS-1:0]  data3_waddr_mux;
reg [BUS_DATA_WIDTH-1:0]  data3_wdata_mux;

// Muxed write controls for LSB/MSB BRAM (single-port write template).
// CRITICAL for block-RAM inference: each array must be written by EXACTLY ONE
// clocked statement with ONE address source. Writing ram_lsb/ram_msb directly
// from two different address signals (marker pointer vs. scan step counter) makes
// Vivado infer two write ports, which a simple-dual-port BRAM cannot provide, so
// it silently falls back to distributed RAM (LUTRAM) and replicates it -> ~17k
// LUTRAMs, over-utilizing the xc7z010. Pre-muxing here (identical to the data3
// template) keeps both arrays in block RAM.
reg                       lsb_we_mux;
reg [BRAM_ADDR_BITS-1:0]  lsb_waddr_mux;
reg [BUS_DATA_WIDTH-1:0]  lsb_wdata_mux;
reg                       msb_we_mux;
reg [BRAM_ADDR_BITS-1:0]  msb_waddr_mux;
reg [BUS_DATA_WIDTH-1:0]  msb_wdata_mux;

always @(*) begin
    // Default no write
    data3_we_mux    = 1'b0;
    data3_waddr_mux = {BRAM_ADDR_BITS{1'b0}};
    data3_wdata_mux = {BUS_DATA_WIDTH{1'b0}};
    // Priority: scan write > marked-continuous write > dual-quantity write > legacy
    // single-word stream write. (bram_wr_en is suppressed while streaming, so it never
    // collides with any of the stream writers.) marked and dual are mutually exclusive
    // bits, but marked is listed first so it wins if both were ever set.
    if (bram_wr_en) begin
        data3_we_mux    = 1'b1;
        data3_waddr_mux = bram_wr_addr;
        data3_wdata_mux = bram_wr_data_data3;
    end else if (marked_mode && marked_wr) begin
        // Marked-continuous record: word0=err (tick cycle), word1=corr,
        // word2=cic, word3=state (latched: step or DEAD). One word per cycle
        // at the running write pointer (advanced in lockstep in the streaming block).
        data3_we_mux    = 1'b1;
        data3_waddr_mux = reg_stream_wr_ptr;
        data3_wdata_mux = marked_start          ? demod_input_i[31:0] :
                          (marked_phase == 2'd1) ? marked_corr_lat[31:0] :
                          (marked_phase == 2'd2) ? marked_cic_lat[31:0] :
                          marked_state_lat;
    end else if (dual_mode && dual_wr) begin
        // Dual-quantity record: word0=err (strobe cycle), word1=corr,
        // word2=cic, word3=step (latched, zero-extended). One word per cycle at the
        // running write pointer, which advances in lockstep in the streaming block.
        data3_we_mux    = 1'b1;
        data3_waddr_mux = reg_stream_wr_ptr;
        data3_wdata_mux = dual_start          ? demod_input_i[31:0] :
                          (dual_phase == 2'd1) ? dual_corr_lat[31:0] :
                          (dual_phase == 2'd2) ? dual_cic_lat[31:0] :
                          { {(BUS_DATA_WIDTH-MAX_STEPS_BITS){1'b0}}, dual_step_lat };
    end else if (stream_wr_en) begin
        data3_we_mux    = 1'b1;
        data3_waddr_mux = reg_stream_wr_ptr;
        // Select streaming data source based on input mode
        data3_wdata_mux = (reg_input_select == INPUT_SELECT_FTW_CORR) ? ftw_correction_i[31:0] : demod_input_i[31:0];
    end
end

// LSB/MSB arrays are dual-purpose:
//  - scan mode: lower/upper 32 bits of the 64-bit accumulator (scan FSM)
//  - marker-stream mode: x markers in ram_lsb, y markers in ram_msb.
// The two are mutually exclusive (marker mode only runs while streaming, when the
// scan FSM is idle and bram_wr_en is low), so a simple priority mux is correct.
// Marker mode takes precedence, matching the original if/else ordering.
always @(*) begin
    // Default no write
    lsb_we_mux    = 1'b0;
    lsb_waddr_mux = {BRAM_ADDR_BITS{1'b0}};
    lsb_wdata_mux = {BUS_DATA_WIDTH{1'b0}};
    msb_we_mux    = 1'b0;
    msb_waddr_mux = {BRAM_ADDR_BITS{1'b0}};
    msb_wdata_mux = {BUS_DATA_WIDTH{1'b0}};
    if (reg_hop_marker_enable && reg_stream_enable) begin
        // Hop-marker mode (multi-resonance monitoring): on a current_step change,
        // tick -> ram_lsb, step -> ram_msb at the shared hop ring index. Mutually
        // exclusive with x/y position markers (monitoring vs 2D motor scan).
        lsb_we_mux    = hop_event;
        lsb_waddr_mux = reg_hop_wr_ptr;
        lsb_wdata_mux = reg_stream_sample_cnt;
        msb_we_mux    = hop_event;
        msb_waddr_mux = reg_hop_wr_ptr;
        msb_wdata_mux = { {(BUS_DATA_WIDTH-MAX_STEPS_BITS){1'b0}}, reg_current_step };
    end else if (reg_marker_enable && reg_stream_enable) begin
        // Marker mode: x -> ram_lsb, y -> ram_msb (independent per-axis writes)
        lsb_we_mux    = x_pulse;
        lsb_waddr_mux = reg_marker_x_wr_ptr;
        lsb_wdata_mux = reg_stream_sample_cnt;
        msb_we_mux    = y_pulse;
        msb_waddr_mux = reg_marker_y_wr_ptr;
        msb_wdata_mux = reg_stream_sample_cnt;
    end else if (bram_wr_en) begin
        // Scan mode: lower/upper 32 bits of the accumulator at the step address
        lsb_we_mux    = 1'b1;
        lsb_waddr_mux = bram_wr_addr;
        lsb_wdata_mux = bram_wr_data_lsb;
        msb_we_mux    = 1'b1;
        msb_waddr_mux = bram_wr_addr;
        msb_wdata_mux = bram_wr_data_msb;
    end
end

always @(posedge clk) begin
    // Each array: exactly one write statement, one muxed address -> block RAM.
    if (lsb_we_mux)   ram_lsb[lsb_waddr_mux]     <= lsb_wdata_mux;
    if (msb_we_mux)   ram_msb[msb_waddr_mux]     <= msb_wdata_mux;
    if (data3_we_mux) ram_data3[data3_waddr_mux] <= data3_wdata_mux;
end

// Generate Write Enable signal from State Machine
always @(posedge clk) begin
    if (!rstn) begin
        bram_wr_en <= 1'b0;
    end else begin
         // Assert write enable only during the STORING_REQ state. Suppressed while
         // streaming so a concurrently-running scan FSM (LO-hop trigger generator)
         // does not clobber the data3 ring / marker banks the stream owns.
        bram_wr_en <= (current_state == S_STORING_REQ) && !reg_stream_enable;
        if (reg_reset_cmd || reg_stop_cmd) begin // Deassert on reset/stop
             bram_wr_en <= 1'b0;
        end
    end
end

// == Port B: Read Port (Synchronous) - Controlled by System Bus ==

// Address Pipelining (Scope Style)
assign bram_rd_addr_in = sys_addr[BRAM_ADDR_OFFSET + BRAM_ADDR_BITS - 1 : BRAM_ADDR_OFFSET]; // Calculate index combinatorially
always @(posedge clk) begin
    bram_rd_addr_p1 <= bram_rd_addr_in;   // Stage 1 pipeline register
    bram_rd_addr_p2 <= bram_rd_addr_p1;   // Stage 2 pipeline register (Used for BRAM read)
end

// BRAM Read - data appears on the cycle AFTER the address is presented
// Note: Actual BRAM might behave slightly differently, but this models typical synchronous read
// assign bram_rd_data_lsb_raw = ram_lsb[bram_rd_addr_p2]; // Combinatorial read based on p2 address
// assign bram_rd_data_msb_raw = ram_msb[bram_rd_addr_p2];

always @(posedge clk) begin
    if (sys_ren_p2) begin // Use delayed ren to enable data capture
        if(bram_access_lsb_p2)          bram_rd_data_lsb_reg   <= bram_rd_data_lsb_raw;
        else if(bram_access_msb_p2)     bram_rd_data_msb_reg   <= bram_rd_data_msb_raw;
        else if(bram_access_data3_p2)   bram_rd_data_data3_reg <= bram_rd_data_data3_raw;
    end
end

//-----------------------------------------------------------------------------
// BRAM Read Control Signal Pipelining
//-----------------------------------------------------------------------------
always @(posedge clk) begin
    if (!rstn) begin
        bram_ack_delay_pipe <= 4'h0;
        sys_ren_p1 <= 1'b0;
        sys_ren_p2 <= 1'b0;
        is_bram_access_p1 <= 1'b0;
        is_bram_access_p2 <= 1'b0;
        is_bram_access_p3 <= 1'b0;
        bram_access_lsb_p1 <= 1'b0;
        bram_access_lsb_p2 <= 1'b0;
        bram_access_msb_p1 <= 1'b0;
        bram_access_msb_p2 <= 1'b0;
        bram_access_data3_p1 <= 1'b0;
        bram_access_data3_p2 <= 1'b0;
        select_bram_for_rdata_p3 <= 2'b00;
    end else begin
        // Pipeline sys_ren and access type signals
        sys_ren_p1 <= sys_ren;
        sys_ren_p2 <= sys_ren_p1;

        is_bram_access_p1 <= is_bram_access;
        is_bram_access_p2 <= is_bram_access_p1;
        is_bram_access_p3 <= is_bram_access_p2; // Pipeline one more for gating output

        bram_access_lsb_p1 <= bram_access_lsb;
        bram_access_lsb_p2 <= bram_access_lsb_p1;

        bram_access_msb_p1 <= bram_access_msb;
        bram_access_msb_p2 <= bram_access_msb_p1;

        bram_access_data3_p1 <= bram_access_data3;
        bram_access_data3_p2 <= bram_access_data3_p1;

        // Pipeline the select signal to align with final data stage
        if (bram_access_lsb_p2) select_bram_for_rdata_p3 <= 2'b00;      // LSB
        else if (bram_access_msb_p2) select_bram_for_rdata_p3 <= 2'b01; // MSB
        else if (bram_access_data3_p2) select_bram_for_rdata_p3 <= 2'b10; // Data3

        // Pipeline for acknowledge generation (4 cycles total delay)
        bram_ack_delay_pipe <= {bram_ack_delay_pipe[2:0], (sys_ren && is_bram_access)};
    end
end
// Delayed ack is the output of the 4th stage
assign bram_read_ack_delayed = bram_ack_delay_pipe[3];

//-----------------------------------------------------------------------------
// System Bus Interface Logic (read data + acknowledge)
//-----------------------------------------------------------------------------
// ROBUSTNESS FIX (2026-06-08): single registered case on the *current* bus
// address selects both sys_ack and sys_rdata, mirroring the proven
// red_pitaya_scope.v pattern (registers ack on sys_en, BRAM banks ack on a
// delayed valid).
//
// The previous version used three overlapping if-blocks (immediate register
// ack, a BRAM ack gated by the *lingering* is_bram_access_p3 pipeline, and a
// write ack) that all assigned the single sys_ack register with last-wins
// precedence. After a BRAM read the is_bram_access_p3 pipeline stays asserted
// for several drain cycles; if a register read was issued in that shadow -
// exactly what happens when the ARM push-stream server continuously reads the
// data3 BRAM while the PC interleaves marker/register reads - the stale BRAM
// block overrode the register read's ack. The CPU read then got no AXI
// acknowledge and faulted with an "external abort" (SIGBUS), intermittently
// killing the register server only during push streaming and only on this
// module. Selecting the ack source from the current address (held by the bus
// for the whole transaction) guarantees exactly one ack per transaction, so a
// lingering BRAM-pipeline signal can no longer suppress a following access.
//
// Reuses the existing read pipeline unchanged: registers are combinational from
// their holding regs (immediate ack via sys_en); the three BRAM banks present
// bram_rd_data_*_reg with the existing 4-cycle delayed ack (bram_read_ack_delayed).
// BRAM-range *writes* never occur from the CPU but are acked immediately for
// safety so no transaction can ever hang the bus.
//-----------------------------------------------------------------------------
always @(posedge clk) begin
    if (!rstn) begin
        sys_ack   <= 1'b0;
        sys_rdata <= 32'h0;
        sys_err   <= 1'b0;
    end else begin
        sys_err <= 1'b0;
        casez (reg_addr)
            // ---- configuration / status / stream / marker registers ----
            // immediate ack (1 cycle); rdata valid the same cycle as the ack.
            ADDR_STATUS:          begin sys_ack <= sys_en; sys_rdata <= { {(32-3){1'b0}}, reg_continuous, reg_done_flag, reg_busy_flag }; end
            ADDR_NUM_STEPS:       begin sys_ack <= sys_en; sys_rdata <= { {(32-MAX_STEPS_BITS){1'b0}}, reg_num_steps }; end
            ADDR_DWELL_TIME:      begin sys_ack <= sys_en; sys_rdata <= reg_dwell_time; end
            ADDR_SETTLING_TIME:   begin sys_ack <= sys_en; sys_rdata <= reg_settling_time; end
            ADDR_TRIGGER_LENGTH:  begin sys_ack <= sys_en; sys_rdata <= reg_trigger_length; end
            ADDR_TRIGGER_PIN_SEL: begin sys_ack <= sys_en; sys_rdata <= { {(32-PIN_SELECT_BITS){1'b0}}, reg_trigger_pin_select }; end
            ADDR_CURRENT_STEP:    begin sys_ack <= sys_en; sys_rdata <= { {(32-MAX_STEPS_BITS){1'b0}}, reg_current_step }; end
            ADDR_INPUT_SELECT:    begin sys_ack <= sys_en; sys_rdata <= { {30{1'b0}}, reg_input_select }; end
            ADDR_STREAM_CONTROL:  begin sys_ack <= sys_en; sys_rdata <= {26'b0, reg_marked_continuous, reg_dual_quantity, reg_hop_marker_enable, reg_marker_enable, reg_stream_reset_cmd, reg_stream_enable}; end
            ADDR_STREAM_STATUS:   begin sys_ack <= sys_en; sys_rdata <= {31'b0, reg_stream_active}; end
            ADDR_STREAM_WR_PTR:   begin sys_ack <= sys_en; sys_rdata <= { {(32-BRAM_ADDR_BITS){1'b0}}, reg_stream_wr_ptr }; end
            ADDR_STREAM_SAMPLES:  begin sys_ack <= sys_en; sys_rdata <= reg_stream_sample_cnt; end
            ADDR_MARKER_X_WR_PTR: begin sys_ack <= sys_en; sys_rdata <= { {(32-BRAM_ADDR_BITS){1'b0}}, reg_marker_x_wr_ptr }; end
            ADDR_MARKER_X_COUNT:  begin sys_ack <= sys_en; sys_rdata <= reg_marker_x_count; end
            ADDR_MARKER_Y_WR_PTR: begin sys_ack <= sys_en; sys_rdata <= { {(32-BRAM_ADDR_BITS){1'b0}}, reg_marker_y_wr_ptr }; end
            ADDR_MARKER_Y_COUNT:  begin sys_ack <= sys_en; sys_rdata <= reg_marker_y_count; end
            ADDR_HOP_WR_PTR:      begin sys_ack <= sys_en; sys_rdata <= { {(32-BRAM_ADDR_BITS){1'b0}}, reg_hop_wr_ptr }; end
            ADDR_HOP_COUNT:       begin sys_ack <= sys_en; sys_rdata <= reg_hop_count; end
            ADDR_STREAM_FORMAT:   begin sys_ack <= sys_en; sys_rdata <= STREAM_WORDS_PER_SAMPLE; end

            // ---- BRAM banks (offsets 0x10000/0x20000/0x30000 -> addr[19:16]=1/2/3) ----
            // delayed ack aligned with the 4-cycle read pipeline; writes (never
            // issued by the CPU) ack immediately so they can't hang the bus.
            20'h1????:            begin sys_ack <= (sys_wen ? 1'b1 : bram_read_ack_delayed); sys_rdata <= bram_rd_data_lsb_reg; end
            20'h2????:            begin sys_ack <= (sys_wen ? 1'b1 : bram_read_ack_delayed); sys_rdata <= bram_rd_data_msb_reg; end
            20'h3????:            begin sys_ack <= (sys_wen ? 1'b1 : bram_read_ack_delayed); sys_rdata <= bram_rd_data_data3_reg; end

            // unmapped register address: still ack so the bus never hangs.
            default:              begin sys_ack <= sys_en; sys_rdata <= 32'h0; end
        endcase
    end
end

// Add debug signals to check BRAM writes
(* mark_debug = "true" *) wire bram_write_valid = (current_state == S_STORING_REQ);
(* mark_debug = "true" *) wire [11:0] bram_write_addr = step_counter;
(* mark_debug = "true" *) wire [31:0] bram_write_data_lsb = accum[31:0];

endmodule
