/**
 * SCAN MODULE
 *
 * This module steps through frequencies on an external microwave generator
 * by outputting a low-going pulse on mw_trig_o at the start of each
 * averaging interval.  The ADC data is averaged over 'set_dec' samples
 * and stored in RAM at the next index.  We then repeat, sending another
 * pulse, collecting the next average, and storing it.  Once we fill
 * up the memory (or reach a desired stop condition), we can stop.
 *
 * Default state of mw_trig_o is HIGH (idle).  A 30 us (or longer) low pulse
 * ensures the MW generator steps to the next frequency.  The pulse length
 * is controlled by 'set_pulse_len'.
 *
 * The interface is similar to the original Red Pitaya 'scope' module.
 *
 */

module scan #(
  parameter RSZ = 14  // RAM size 2^RSZ
)(
   // ADC
   input                    adc_clk_i          , // ADC clock
   input                    scan_rstn_i        , // reset - active low
   input      [ 14-1: 0]    adc_a_i            , // ADC data CHA
   input      [ 14-1: 0]    adc_b_i            , // ADC data CHB

   // Output pulse to the microwave generator - Idle = 1, Active pulse = 0 
   output reg               mw_trig_o       ,

   // System bus
   input      [ 32-1: 0]    sys_addr        ,  // bus saddress
   input      [ 32-1: 0]    sys_wdata       ,  // bus write data
   input      [  4-1: 0]    sys_sel         ,  // bus write byte select
   input                    sys_wen         ,  // bus write enable
   input                    sys_ren         ,  // bus read enable
   output reg [ 32-1: 0]    sys_rdata       ,  // bus read data
   output reg               sys_ack          // bus acknowledge signal
);


//---------------------------------------------------------------------------------
// Internal register signals / settings

reg             start_scan;     //
reg  [31:0]     set_pulse_len;  // # of clock cycles for mw_trig_o to be held low
reg             adc_rst_do;     // Reset (forced disarm)
reg             adc_we_keep;    // Keep writing after some condition (unused if you want a single pass)
reg             set_avg_en;     // If 1, we do bit-shift divide by set_dec
reg  [17-1:0]   set_dec;        // # of samples to accumulate per frequency step


//---------------------------------------------------------------------------------
// Memory buffers for final averaged data

// Each address holds one sample for channel A or B.  We'll write them in lockstep.
reg  [14-1:0]   adc_a_buf [0 : (1<<RSZ)-1];
reg  [14-1:0]   adc_b_buf [0 : (1<<RSZ)-1];

// RAM read side
reg  [RSZ-1:0]  adc_raddr;
reg  [RSZ-1:0]  adc_a_raddr;
reg  [RSZ-1:0]  adc_b_raddr;
reg  [14-1:0]   adc_a_rd;
reg  [14-1:0]   adc_b_rd;
reg  [3:0]      adc_rval;
wire            adc_rd_dv;

// RAM write side
reg  [RSZ-1:0]  adc_wp;        // write pointer

//---------------------------------------------------------------------------------
// Summation and decimation logic (like the scope code) for each step

reg  [31:0]     adc_a_sum;
reg  [31:0]     adc_b_sum;
reg  [14:0]     adc_a_dat;
reg  [14:0]     adc_b_dat;
reg  [16:0]     adc_dec_cnt;
reg             adc_dv;  // signals "just finished set_dec samples => can store average"

//---------------------------------------------------------------------------------
// State machine for stepping frequency / collecting data

localparam ST_IDLE  = 3'd0;
localparam ST_PULSE = 3'd1;  // hold mw_trig_o low for set_pulse_len cycles
localparam ST_MEAS  = 3'd2;  // measure/accumulate for set_dec cycles
localparam ST_DONE  = 3'd3;  // optional done state, or go back to IDLE

reg [2:0] scan_state;
reg [31:0] pulse_cnt;    // counts down from set_pulse_len
reg [31:0] freq_steps_cnt;    // how many steps so far (if you want a limit)
reg [31:0] set_steps_max; // user can set how many total steps to do if desired

//---------------------------------------------------------------------------------
// MAIN LOGIC

always @(posedge adc_clk_i)
begin
   if (!scan_rstn_i) begin
      scan_state     <= ST_IDLE;
      mw_trig_o      <= 1'b1;       // idle = high
      pulse_cnt      <= 32'h0;
      freq_steps_cnt <= 32'h0;

      adc_dec_cnt    <= 17'h0;
      adc_dv         <= 1'b0;
      adc_a_sum      <= 32'h0;
      adc_b_sum      <= 32'h0;

      adc_wp         <= {RSZ{1'b0}};
   end
   else begin

      // If the user or some other logic sets "adc_rst_do", then forcibly return to idle
      if (adc_rst_do) begin
         scan_state <= ST_IDLE;
         mw_trig_o  <= 1'b1;
      end


      // Basic state machine
      case (scan_state)
      
      //--------------------------------------------------------------------------
      ST_IDLE: begin
         mw_trig_o <= 1'b1;   // hold output high in idle
         // Wait for ARM from software
         if (start_scan) begin
            // Initialize everything for first step
            freq_steps_cnt  <= 32'h0;
            adc_wp      <= {RSZ{1'b0}};
            // You could also reset or preload sums here if you want
            // Start the first pulse
            pulse_cnt   <= set_pulse_len;
            mw_trig_o   <= 1'b0; // start the low pulse
            scan_state  <= ST_PULSE;
         end
      end

      //--------------------------------------------------------------------------
      ST_PULSE: begin
         // We are holding mw_trig_o LOW for set_pulse_len cycles
         if (pulse_cnt > 0) begin
            pulse_cnt <= pulse_cnt - 32'd1;
         end
         else begin
            // Once we've hit 0, return mw_trig_o high
            mw_trig_o <= 1'b1;
            // Start measuring/accumulating - maybe nicer to shift this into the ST_MEAS section
            // Reset the decimation counter
            adc_dec_cnt <= 17'h1;
            adc_a_sum   <= $signed(adc_a_i);
            adc_b_sum   <= $signed(adc_b_i);
            adc_dv      <= 1'b0;

            scan_state <= ST_MEAS;
         end
      end

      //--------------------------------------------------------------------------
      ST_MEAS: begin
         // We accumulate for set_dec samples
         // Then produce "adc_dv=1" for one cycle to store the average
         if (adc_dec_cnt < set_dec) begin
            adc_dec_cnt <= adc_dec_cnt + 17'd1;
            adc_a_sum   <= adc_a_sum + $signed(adc_a_i);
            adc_b_sum   <= adc_b_sum + $signed(adc_b_i);
            adc_dv      <= 1'b0;
         end
         else begin
            // Done collecting set_dec samples => compute average => store
            // We will set adc_dv=1 for a single clock, so that the store logic
            // can increment the memory pointer and store data at [adc_wp].
            adc_dv      <= 1'b1;
            adc_dec_cnt <= 17'h0;
            
            if (freq_steps_cnt >= set_steps_max) begin
               scan_state <= ST_DONE;
            end
            else begin
               scan_state <= ST_PULSE;
               pulse_cnt  <= set_pulse_len;
               mw_trig_o  <= 1'b0;
               freq_steps_cnt <= freq_steps_cnt + 1;
            end
         end
      end

      //--------------------------------------------------------------------------
      ST_DONE: begin
         // Potential place for a "finished" condition, or return to IDLE, etc.
         mw_trig_o <= 1'b1;
      end

      endcase
   end
end

//---------------------------------------------------------------------------------
// Decimation dividing (like original scope's big switch-case).
// Because we only produce final data when adc_dv=1, we do that division just
// before or at the moment we store to memory.

always @(posedge adc_clk_i) begin
   // By default, each clock we update the "adc_a_dat" and "adc_b_dat"
   // to the "average" in case we want to store it at that moment.  However, we only care about its final
   // value the moment that adc_dv=1 goes true.
   if (!scan_rstn_i) begin
      adc_a_dat <= 14'd0;
      adc_b_dat <= 14'd0;
   end
   else begin
      // dividing/averaging by bit shifting, per set_dec
      // exactly as in the scope code
   // dividing/averaging by bit shifting would be better than via this selection approach
   case (set_dec & {17{set_avg_en}})
      17'h0     : begin adc_a_dat <= adc_a_i             ;      adc_b_dat <= adc_b_i             ;  end
      17'h1     : begin adc_a_dat <= adc_a_sum[15+0 :  0];      adc_b_dat <= adc_b_sum[15+0 :  0];  end
      17'h2     : begin adc_a_dat <= adc_a_sum[15+1 :  1];      adc_b_dat <= adc_b_sum[15+1 :  1];  end
      17'h4     : begin adc_a_dat <= adc_a_sum[15+2 :  2];      adc_b_dat <= adc_b_sum[15+2 :  2];  end
      17'h8     : begin adc_a_dat <= adc_a_sum[15+3 :  3];      adc_b_dat <= adc_b_sum[15+3 :  3];  end
      17'h10    : begin adc_a_dat <= adc_a_sum[15+4 :  4];      adc_b_dat <= adc_b_sum[15+4 :  4];  end
      17'h20    : begin adc_a_dat <= adc_a_sum[15+5 :  5];      adc_b_dat <= adc_b_sum[15+5 :  5];  end
      17'h40    : begin adc_a_dat <= adc_a_sum[15+6 :  6];      adc_b_dat <= adc_b_sum[15+6 :  6];  end
      17'h80    : begin adc_a_dat <= adc_a_sum[15+7 :  7];      adc_b_dat <= adc_b_sum[15+7 :  7];  end
      17'h100   : begin adc_a_dat <= adc_a_sum[15+8 :  8];      adc_b_dat <= adc_b_sum[15+8 :  8];  end
      17'h200   : begin adc_a_dat <= adc_a_sum[15+9 :  9];      adc_b_dat <= adc_b_sum[15+9 :  9];  end
      17'h400   : begin adc_a_dat <= adc_a_sum[15+10: 10];      adc_b_dat <= adc_b_sum[15+10: 10];  end
      17'h800   : begin adc_a_dat <= adc_a_sum[15+11: 11];      adc_b_dat <= adc_b_sum[15+11: 11];  end
      17'h1000  : begin adc_a_dat <= adc_a_sum[15+12: 12];      adc_b_dat <= adc_b_sum[15+12: 12];  end
      17'h2000  : begin adc_a_dat <= adc_a_sum[15+13: 13];      adc_b_dat <= adc_b_sum[15+13: 13];  end
      17'h4000  : begin adc_a_dat <= adc_a_sum[15+14: 14];      adc_b_dat <= adc_b_sum[15+14: 14];  end
      17'h8000  : begin adc_a_dat <= adc_a_sum[15+15: 15];      adc_b_dat <= adc_b_sum[15+15: 15];  end
      17'h10000 : begin adc_a_dat <= adc_a_sum[15+16: 16];      adc_b_dat <= adc_b_sum[15+16: 16];  end
      default   : begin adc_a_dat <= adc_a_sum[15+0 :  0];      adc_b_dat <= adc_b_sum[15+0 :  0];  end
   endcase
   end
end

//---------------------------------------------------------------------------------
// Write to the memory buffer on the one cycle after finishing accumulation

always @(posedge adc_clk_i) begin
   if (!scan_rstn_i) begin
      adc_wp <= {RSZ{1'b0}};
   end
   else begin
      if (adc_dv) begin
         // store the average to RAM at the current write pointer
         adc_a_buf[adc_wp] <= adc_a_dat;
         adc_b_buf[adc_wp] <= adc_b_dat;

         // advance pointer
         adc_wp <= adc_wp + 1'b1;

      end
   end
end

//---------------------------------------------------------------------------------
// Simple read side for system bus addressing

always @(posedge adc_clk_i) begin
   if (!scan_rstn_i)
      adc_rval <= 4'h0;
   else
      adc_rval <= {adc_rval[2:0], (sys_ren || sys_wen)};
end

assign adc_rd_dv = adc_rval[3];

always @(posedge adc_clk_i) begin
   adc_raddr   <= sys_addr[RSZ+1:2]; // address synchronous to clock
   adc_a_raddr <= adc_raddr;        // double register to avoid timing issues
   adc_b_raddr <= adc_raddr;
   adc_a_rd    <= adc_a_buf[adc_a_raddr];
   adc_b_rd    <= adc_b_buf[adc_b_raddr];
end

//---------------------------------------------------------------------------------
// System-bus register interface
// Very similar to the original scope module.  We have added e.g. set_pulse_len.

wire sys_en;
assign sys_en = sys_wen | sys_ren;

always @(posedge adc_clk_i) begin
   if(!scan_rstn_i) begin
      sys_ack       <= 1'b0;
      start_scan    <= 1'b0;
      adc_rst_do    <= 1'b0;
      adc_we_keep   <= 1'b0;
      set_dec       <= 17'h2000;      // default
      set_avg_en    <= 1'b0;
      set_pulse_len <= 32'd3750;      // example: 30us @125MHz => 3750 cycles
      set_steps_max <= 32'd100;       // example max steps
   end
   else begin
      sys_ack <= sys_en;

      // Defaults each cycle # TODO: Should this default each cycle?
      start_scan <= 1'b0;
      adc_rst_do <= 1'b0;

      if (sys_wen) begin
         case (sys_addr[19:0])
           // 0x00: bit 0 => ARM, bit 1 => RESET, bit 3 => keep writing, etc.
           20'h00000: begin
             start_scan  <= sys_wdata[0];
             adc_rst_do  <= sys_wdata[1];
             adc_we_keep <= sys_wdata[3];
           end

           // set_dec => # of samples
           20'h00014: set_dec     <= sys_wdata[16:0];

           // average enable
           20'h00028: set_avg_en  <= sys_wdata[0];

           // Pulse length for MW generator
           20'h000A0: set_pulse_len <= sys_wdata[31:0];

           // Max steps if you want a limit
           20'h000A4: set_steps_max <= sys_wdata[31:0];

           default: /* no-op */;
         endcase
      end
   end
end

//---------------------------------------------------------------------------------
// Reading back registers/data

always @(posedge adc_clk_i) begin
   if(!scan_rstn_i) begin
      sys_rdata <= 32'h0;
   end
   else if(sys_ren) begin
      casez (sys_addr[19:0])
        20'h00000: sys_rdata <= {28'd0, adc_we_keep, 1'b0 /*(some trigger status)*/, adc_rst_do, start_scan};
        20'h00014: sys_rdata <= {{32-17{1'b0}}, set_dec};
        20'h00028: sys_rdata <= {{32-1{1'b0}}, set_avg_en};
        20'h000A0: sys_rdata <= set_pulse_len;
        20'h000A4: sys_rdata <= set_steps_max;

        // If you want to read raw ADC inputs (like scope):
        20'h00154: sys_rdata <= {{18{1'b0}}, adc_a_i}; 
        20'h00158: sys_rdata <= {{18{1'b0}}, adc_b_i};

        // Reading from memory  addresses 0x1???? => channel A, 0x2???? => channel B
        20'h1????: sys_rdata <= {16'h0, 2'h0, adc_a_rd};
        20'h2????: sys_rdata <= {16'h0, 2'h0, adc_b_rd};

        default:   sys_rdata <= 32'h0;
      endcase
   end
end

endmodule
