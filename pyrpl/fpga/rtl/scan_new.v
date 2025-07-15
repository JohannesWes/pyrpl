/**
 * @brief Scan Module for Pyrpl
 *
 * Performs a sweep across a defined number of steps. For each step:
 * 1. Outputs a trigger pulse.
 * 2. Waits for a settling time.
 * 3. Acquires and accumulates data from input channel for a dwell time.
 * 4. Stores the 64-bit accumulated sum in BRAM (split into LSB/MSB banks).
 *
 * Configuration and data readout via System Bus.
 * BRAM banks are mapped to separate address regions for scope-like reading:
 * - LSB Bank: Base + 0x10000 - 0x1FFFF
 * - MSB Bank: Base + 0x20000 - 0x2FFFF
 */
module scan #(
    parameter MAX_STEPS_BITS    = 12,                 // Maximum number of steps = 2^12 = 4096
    parameter DATA_WIDTH        = 14,                 // Input data width from ADC/DSP
    parameter ACCUM_WIDTH       = 64,                 // Accumulator width - 64 bits should be sufficient for our acquisition times
    parameter BUS_DATA_WIDTH    = 32,                 // System bus data width
    parameter ADDR_WIDTH        = 32,                 // System bus address width
    parameter PIN_SELECT_BITS   = 3                   // Allows selecting 1 of 8 pins - selection not implemented yet.
)(
    // System Clock and Reset
    input wire                          clk,
    input wire                          rstn,          // Active low reset

    // Data Input 
    input wire signed [DATA_WIDTH-1:0]  input_i,

    // Trigger Output
    output wire                         trigger_o,

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

// BRAM Address Mapping
// LSB Bank: Module Base + 0x10000 - 0x1FFFF (Relative Addr: 20'h1????)
// MSB Bank: Module Base + 0x20000 - 0x2FFFF (Relative Addr: 20'h2????)
localparam BRAM_ADDR_OFFSET     = 2;
localparam BRAM_LSB_MAP_PATTERN = 20'h1????; // Relative addr pattern for LSB
localparam BRAM_MSB_MAP_PATTERN = 20'h2????; // Relative addr pattern for MSB

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
reg                         reg_start_cmd;
reg                         reg_stop_cmd;
reg                         reg_reset_cmd;

// Status Registers/Signals
reg                         reg_busy_flag;
reg                         reg_done_flag;
reg [MAX_STEPS_BITS-1:0]    reg_current_step; // Read only

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

// BRAM signals (Read Port B)
wire [BRAM_ADDR_BITS-1:0]   bram_rd_addr_in;        // Combinatorial BRAM address from sys_addr
reg  [BRAM_ADDR_BITS-1:0]   bram_rd_addr_p1;        // Pipelined BRAM address stage 1
reg  [BRAM_ADDR_BITS-1:0]   bram_rd_addr_p2;        // Pipelined BRAM address stage 2 (used for BRAM read)
wire [BUS_DATA_WIDTH-1:0]   bram_rd_data_lsb_raw;   // Raw data output from LSB BRAM read
wire [BUS_DATA_WIDTH-1:0]   bram_rd_data_msb_raw;   // Raw data output from MSB BRAM read
reg  [BUS_DATA_WIDTH-1:0]   bram_rd_data_lsb_reg;   // Registered BRAM data (after BRAM read latency)
reg  [BUS_DATA_WIDTH-1:0]   bram_rd_data_msb_reg;   // Registered BRAM data (after BRAM read latency)

// BRAM Read Acknowledge Delay Pipe (Scope Style)
reg  [4-1:0]                bram_ack_delay_pipe;    // 4-stage pipeline for ack generation
wire                        bram_read_ack_delayed;  // Delayed ack signal (output of pipe)

// Trigger logic
reg                         trigger_pulse_active;

// System bus signals
wire [19:0]                 reg_addr = sys_addr[19:0];
wire                        sys_en = sys_wen || sys_ren;

// BRAM Access Detection (combinatorial based on current sys_addr)
wire                        bram_access_lsb = (reg_addr == BRAM_LSB_MAP_PATTERN); // Use == for exact pattern check
wire                        bram_access_msb = (reg_addr == BRAM_MSB_MAP_PATTERN); // Use == for exact pattern check
wire                        is_bram_access = bram_access_lsb || bram_access_msb;

// Pipelined BRAM Access Control Signals
reg                         sys_ren_p1, sys_ren_p2;
reg                         is_bram_access_p1, is_bram_access_p2, is_bram_access_p3;
reg                         bram_access_lsb_p1, bram_access_lsb_p2;
reg                         bram_access_msb_p1, bram_access_msb_p2;
reg                         select_lsb_for_rdata_p3; // Delayed select for sys_rdata muxing

//-----------------------------------------------------------------------------
// Configuration Register Logic
//-----------------------------------------------------------------------------
always @(posedge clk) begin
    if (!rstn) begin
        reg_num_steps          <= {MAX_STEPS_BITS{1'b0}};
        reg_dwell_time         <= 32'd125000; // Default 1ms
        reg_settling_time      <= 32'd12500;  // Default 0.1ms
        reg_trigger_length     <= 32'd125;    // Default 1us
        reg_trigger_pin_select <= {PIN_SELECT_BITS{1'b0}};
        reg_start_cmd          <= 1'b0;
        reg_stop_cmd           <= 1'b0;
        reg_reset_cmd          <= 1'b0;
    end else begin
        // Clear command flags after one cycle
        reg_start_cmd <= 1'b0;
        reg_stop_cmd  <= 1'b0;
        reg_reset_cmd <= 1'b0;

        if (sys_wen && !reg_busy_flag) begin // Only allow config writes when not busy
            case (reg_addr)
                ADDR_CONTROL: begin
                    if (sys_wdata[0]) reg_start_cmd <= 1'b1;
                    // Stop and Reset can be asserted while busy
                    // if(sys_wdata[1]) reg_stop_cmd  <= 1'b1; // Stop handled directly in FSM
                    // if(sys_wdata[2]) reg_reset_cmd <= 1'b1; // Reset handled directly in FSM
                end
                ADDR_NUM_STEPS:       reg_num_steps          <= sys_wdata[MAX_STEPS_BITS-1:0];
                ADDR_DWELL_TIME:      reg_dwell_time         <= sys_wdata;
                ADDR_SETTLING_TIME:   reg_settling_time      <= sys_wdata;
                ADDR_TRIGGER_LENGTH:  reg_trigger_length     <= sys_wdata;
                ADDR_TRIGGER_PIN_SEL: reg_trigger_pin_select <= sys_wdata[PIN_SELECT_BITS-1:0];
                default: ;
            endcase
        end
        // Allow Stop/Reset commands even when busy
         if (sys_wen && reg_addr == ADDR_CONTROL) begin
              if(sys_wdata[1]) reg_stop_cmd  <= 1'b1;
              if(sys_wdata[2]) reg_reset_cmd <= 1'b1;
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

// Combinational next state logic - calculates what next_state should be
// State transition logic
always @(*) begin
    next_state = current_state;
    case (current_state)
        S_IDLE:         if (reg_start_cmd && reg_num_steps > 0) next_state = S_START_STEP;
        
        S_START_STEP:   next_state = S_TRIGGERING;
        
        S_TRIGGERING:   if (trigger_counter >= reg_trigger_length) next_state = S_SETTLING;
        
        S_SETTLING:     if (settling_counter >= reg_settling_time) next_state = S_ACQUIRING;
        
        S_ACQUIRING:    if (dwell_counter >= reg_dwell_time) next_state = S_STORING_REQ;
        
        // Always wait one cycle for write
        S_STORING_REQ:  next_state = S_STORING_WAIT;
        
        S_STORING_WAIT: next_state = S_FINISHING;
        
        // Continue or complete based on step counter
        S_FINISHING:    if (step_counter >= reg_num_steps - 1) next_state = S_DONE;
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
         end

// Increment logic based on current state
case (current_state)
    // Increment trigger counter until length reached
    S_TRIGGERING:   if (trigger_counter < reg_trigger_length) trigger_counter <= trigger_counter + 1;
    
    // Increment settling counter until settling time reached
    S_SETTLING:     if (settling_counter < reg_settling_time) settling_counter <= settling_counter + 1;
    
    S_ACQUIRING:    begin
                        // Update current step register for readout
                        reg_current_step <= step_counter;
                        if (dwell_counter < reg_dwell_time) begin
                            dwell_counter <= dwell_counter + 1;
                            // Accumulate sign-extended input data
                            accum <= accum + $signed({{(ACCUM_WIDTH-DATA_WIDTH){input_i[DATA_WIDTH-1]}}, input_i});
                        end
                    end
    
    // Increment step counter after storing
    S_FINISHING:    step_counter <= step_counter + 1;
    
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
reg [BUS_DATA_WIDTH-1:0] ram_lsb [0:BRAM_DEPTH-1];
reg [BUS_DATA_WIDTH-1:0] ram_msb [0:BRAM_DEPTH-1];

// Port A: Write Port (Synchronous) - Controlled by State Machine
assign bram_wr_addr     = step_counter;
assign bram_wr_data_lsb = accum[BUS_DATA_WIDTH-1:0];
assign bram_wr_data_msb = accum[ACCUM_WIDTH-1:BUS_DATA_WIDTH];

always @(posedge clk) begin
    if (bram_wr_en) begin
        ram_lsb[bram_wr_addr] <= bram_wr_data_lsb;
        ram_msb[bram_wr_addr] <= bram_wr_data_msb;
    end
end

// Generate Write Enable signal from State Machine
always @(posedge clk) begin
    if (!rstn) begin
        bram_wr_en <= 1'b0;
    end else begin
         // Assert write enable only during the STORING_REQ state
        bram_wr_en <= (current_state == S_STORING_REQ);
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
assign bram_rd_data_lsb_raw = ram_lsb[bram_rd_addr_p2]; // Combinatorial read based on p2 address
assign bram_rd_data_msb_raw = ram_msb[bram_rd_addr_p2];

always @(posedge clk) begin
    if (sys_ren_p2) begin // Use delayed ren to enable data capture
        if(bram_access_lsb_p2)      bram_rd_data_lsb_reg <= bram_rd_data_lsb_raw;
        else if(bram_access_msb_p2) bram_rd_data_msb_reg <= bram_rd_data_msb_raw;
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
        select_lsb_for_rdata_p3 <= 1'b0;
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

        // Pipeline the LSB select signal to align with final data stage
        select_lsb_for_rdata_p3 <= bram_access_lsb_p2;

        // Pipeline for acknowledge generation (4 cycles total delay)
        bram_ack_delay_pipe <= {bram_ack_delay_pipe[2:0], (sys_ren && is_bram_access)};
    end
end
// Delayed ack is the output of the 4th stage
assign bram_read_ack_delayed = bram_ack_delay_pipe[3];

//-----------------------------------------------------------------------------
// System Bus Interface Logic
//-----------------------------------------------------------------------------
assign sys_err = 1'b0; // No error conditions implemented

always @(posedge clk) begin
    if (!rstn) begin
        sys_ack <= 1'b0;
        sys_rdata <= 32'h0;
    end else begin
        // Default assignments (will be overridden below if conditions match)
        sys_ack <= 1'b0;
        sys_rdata <= 32'h0; // Default to 0 if no valid read target

        // Handle Register Accesses (Immediate Ack, Registered Data)
        if (sys_en && !is_bram_access) begin // Active and NOT a BRAM access
            sys_ack <= 1'b1; // Immediate ack for registers
            if (sys_ren) begin // Only update rdata on read
                case (reg_addr) // Use current address for decoding
                    ADDR_STATUS:          sys_rdata <= { {32-2{1'b0}}, reg_done_flag, reg_busy_flag };
                    ADDR_NUM_STEPS:       sys_rdata <= { {(32-MAX_STEPS_BITS){1'b0}}, reg_num_steps };
                    ADDR_DWELL_TIME:      sys_rdata <= reg_dwell_time;
                    ADDR_SETTLING_TIME:   sys_rdata <= reg_settling_time;
                    ADDR_TRIGGER_LENGTH:  sys_rdata <= reg_trigger_length;
                    ADDR_TRIGGER_PIN_SEL: sys_rdata <= { {(32-PIN_SELECT_BITS){1'b0}}, reg_trigger_pin_select };
                    ADDR_CURRENT_STEP:    sys_rdata <= { {(32-MAX_STEPS_BITS){1'b0}}, reg_current_step };
                    default:              sys_rdata <= 32'hBADADD05; // Bad register address
        endcase
            end else begin
                 sys_rdata <= 32'h0; // Don't drive data bus during register write
            end
        end

        // Handle BRAM Read Data/Ack Output (Delayed & Registered)
        // Gated by is_bram_access_p3 to ensure data pipeline is complete
        if (is_bram_access_p3) begin
            sys_ack <= bram_read_ack_delayed; // Use delayed acknowledge (4 cycles)
            if (select_lsb_for_rdata_p3) begin // Use delayed select (3 cycles)
                sys_rdata <= bram_rd_data_lsb_reg; // Use delayed data (3 cycles)
            end else begin
                sys_rdata <= bram_rd_data_msb_reg; // Use delayed data (3 cycles)
            end
        end

        // Handle Write Acknowledge (Immediate) - Overrides BRAM ack if concurrent
        if (sys_wen) begin
             sys_ack <= 1'b1;
             // Note: If sys_wen is asserted in the same cycle is_bram_access_p3 becomes true,
             // the immediate write ack takes precedence over the delayed read ack.
             // This is typical for simple AXI-lite implementations.
        end
    end
end

endmodule