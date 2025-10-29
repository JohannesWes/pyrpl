"""
Example Hardware Module Template

This module demonstrates how to create a new hardware module using one of the
newly available address regions (modules 8-15).

This serves as a minimal boilerplate/template for creating custom FPGA modules.

SCOPE: This template is for standalone HardwareModule instances that connect
directly to the system bus (like Scope, Scan, HK, AMS). It is NOT for DSP
modules with signal routing (like PID, IQ, IIR) which use the DspModule base
class and different address space.
"""

from ..attributes import (IntRegister, FloatRegister, BoolRegister,
                          SelectRegister, FrequencyRegister, LongRegister)
from ..modules import HardwareModule


class ExampleModule(HardwareModule):
    """
    Example hardware module occupying address region 8 (0x40800000)

    This minimal module demonstrates the standard PyRPL hardware module patterns:
    - Register mapping to FPGA addresses
    - Different register types (Int, Float, Bool, Select, Frequency)
    - Setup and GUI attribute configuration
    - Auto-generated setup() method via metaclass

    To use this module with actual FPGA hardware, you would need to:
    1. Create corresponding Verilog module in pyrpl/fpga/rtl/
    2. Instantiate it in red_pitaya_top.v
    3. Connect to system bus using sys_wen[8], sys_ren[8]
    4. Compile FPGA bitfile

    Example
    -------
    >>> from pyrpl import Pyrpl
    >>> p = Pyrpl('config')
    >>> ex = p.rp.example_module
    >>>
    >>> # Set parameters
    >>> ex.enable = True
    >>> ex.gain = 0.5
    >>> ex.threshold = 100
    >>> ex.mode = 'continuous'
    >>> ex.frequency = 10e6  # 10 MHz
    >>>
    >>> # Read status
    >>> print(f"Counter value: {ex.counter}")
    >>> print(f"Overflow detected: {ex.overflow}")
    >>>
    >>> # Use setup() method (auto-generated from _setup_attributes)
    >>> ex.setup(enable=True, gain=0.8, mode='triggered')
    """

    # Base address for module 8 (newly available slot)
    # Module address calculation: 0x40000000 + module_num * 0x100000
    # Module 8: 0x40000000 + 8 * 0x100000 = 0x40800000
    addr_base = 0x40800000

    # Optional: Custom GUI widget class (uncomment if you create one)
    # _widget_class = ExampleModuleWidget
    # If not specified, PyRPL auto-generates a basic widget from _gui_attributes

    # Attributes that are saved/restored in config file
    # These become parameters for the auto-generated setup() method
    _setup_attributes = ['enable', 'gain', 'threshold', 'mode', 'frequency']

    # Attributes shown in GUI (if widget is created)
    _gui_attributes = _setup_attributes + ['counter', 'overflow', 'reset_counter']

    # ==================== Control Registers ====================

    enable = BoolRegister(
        0x00,
        doc="Enable/disable module operation",
        default=False
    )

    gain = FloatRegister(
        0x04,
        bits=14,
        norm=2**13,  # Normalize to ±1.0 range for 14-bit signed
        doc="Signal gain (range: -1.0 to +1.0)"
    )

    threshold = IntRegister(
        0x08,
        bits=16,
        doc="Threshold value (0-65535)",
        min=0,
        max=2**16 - 1
    )

    mode = SelectRegister(
        0x0C,
        options=['off', 'continuous', 'triggered', 'gated'],
        doc="Operating mode selection"
    )

    frequency = FrequencyRegister(
        0x10,
        bits=32,
        doc="Operating frequency in Hz (uses parent.frequency_correction)"
    )

    # ==================== Status Registers (Read-Only) ====================

    counter = IntRegister(
        0x20,
        bits=32,
        doc="Event counter (read-only, increments on each trigger)"
    )

    overflow = BoolRegister(
        0x24,
        doc="Overflow flag (read-only, set when counter saturates)"
    )

    # ==================== Action Registers (Write-Only) ====================

    reset_counter = BoolRegister(
        0x30,
        doc="Write 1 to reset counter to zero"
    )

    # ==================== Configuration Register (Advanced) ====================

    # Example of multi-field register using IntRegister with bit manipulation
    _config = IntRegister(
        0x40,
        bits=32,
        doc="Configuration register (internal use)"
    )

    # Example of 64-bit register (uncomment if needed)
    # timestamp = LongRegister(
    #     0x50,
    #     bits=64,
    #     doc="64-bit timestamp counter (occupies addresses 0x50 and 0x54)"
    # )

    # ==================== Module Methods ====================

    def _setup(self):
        """
        Internal setup method called by auto-generated setup() method.

        The HardwareModule metaclass automatically generates a setup() method
        with keyword arguments for each attribute in _setup_attributes.

        This _setup() method is called at the end of setup() for custom
        initialization logic.
        """
        # Custom initialization if needed
        # For example, reset counter on setup
        if self.enable:
            self.reset_counter = True
            self.reset_counter = False  # Pulse the reset

    def reset(self):
        """
        Reset the module to default state.

        Example usage:
        >>> ex.reset()
        """
        self.enable = False
        self.gain = 0.0
        self.threshold = 0
        self.mode = 'off'
        self.frequency = 0
        self.reset_counter = True
        self.reset_counter = False

    def get_status(self):
        """
        Get current module status as a dictionary.

        Returns
        -------
        dict
            Dictionary containing all status information

        Example
        -------
        >>> status = ex.get_status()
        >>> print(f"Counter: {status['counter']}, Overflow: {status['overflow']}")
        """
        return {
            'enable': self.enable,
            'gain': self.gain,
            'threshold': self.threshold,
            'mode': self.mode,
            'frequency': self.frequency,
            'counter': self.counter,
            'overflow': self.overflow
        }

    def wait_for_trigger(self, timeout=1.0):
        """
        Wait for counter to increment (indicating trigger event).

        Parameters
        ----------
        timeout : float
            Maximum wait time in seconds

        Returns
        -------
        bool
            True if trigger occurred, False if timeout

        Example
        -------
        >>> ex.mode = 'triggered'
        >>> if ex.wait_for_trigger(timeout=5.0):
        ...     print("Trigger received!")
        """
        import time
        start_count = self.counter
        start_time = time.time()

        while time.time() - start_time < timeout:
            if self.counter != start_count:
                return True
            time.sleep(0.001)  # 1ms polling interval

        return False


# ============================================================================
# Verilog Template (for reference - create as separate .v file)
# ============================================================================
"""
To implement this module in FPGA, create a file:
pyrpl/fpga/rtl/example_module.v

module example_module (
    // Clock and reset
    input wire clk_i,
    input wire rstn_i,

    // System bus interface
    input wire [31:0] sys_addr,
    input wire [31:0] sys_wdata,
    input wire [3:0]  sys_sel,
    input wire        sys_wen,
    input wire        sys_ren,
    output reg [31:0] sys_rdata,
    output reg        sys_err,
    output reg        sys_ack
);

// Register addresses (match Python definitions above)
localparam ADDR_ENABLE     = 16'h0000;
localparam ADDR_GAIN       = 16'h0004;
localparam ADDR_THRESHOLD  = 16'h0008;
localparam ADDR_MODE       = 16'h000C;
localparam ADDR_FREQUENCY  = 16'h0010;
localparam ADDR_COUNTER    = 16'h0020;
localparam ADDR_OVERFLOW   = 16'h0024;
localparam ADDR_RESET      = 16'h0030;
localparam ADDR_CONFIG     = 16'h0040;

// Internal registers
reg         enable_reg;
reg [13:0]  gain_reg;
reg [15:0]  threshold_reg;
reg [1:0]   mode_reg;  // 2 bits for 4 modes
reg [31:0]  frequency_reg;
reg [31:0]  counter_reg;
reg         overflow_reg;

// Write logic
always @(posedge clk_i) begin
    if (!rstn_i) begin
        enable_reg <= 1'b0;
        gain_reg <= 14'b0;
        threshold_reg <= 16'b0;
        mode_reg <= 2'b0;
        frequency_reg <= 32'b0;
    end
    else if (sys_wen) begin
        case (sys_addr[15:0])
            ADDR_ENABLE:    enable_reg <= sys_wdata[0];
            ADDR_GAIN:      gain_reg <= sys_wdata[13:0];
            ADDR_THRESHOLD: threshold_reg <= sys_wdata[15:0];
            ADDR_MODE:      mode_reg <= sys_wdata[1:0];
            ADDR_FREQUENCY: frequency_reg <= sys_wdata;
            ADDR_RESET:     if (sys_wdata[0]) counter_reg <= 32'b0;
        endcase
    end
end

// Read logic
always @(posedge clk_i) begin
    if (!rstn_i) begin
        sys_ack <= 1'b0;
        sys_err <= 1'b0;
    end
    else begin
        sys_ack <= sys_wen | sys_ren;
        sys_err <= 1'b0;

        case (sys_addr[15:0])
            ADDR_ENABLE:    sys_rdata <= {31'b0, enable_reg};
            ADDR_GAIN:      sys_rdata <= {{18{gain_reg[13]}}, gain_reg};
            ADDR_THRESHOLD: sys_rdata <= {16'b0, threshold_reg};
            ADDR_MODE:      sys_rdata <= {30'b0, mode_reg};
            ADDR_FREQUENCY: sys_rdata <= frequency_reg;
            ADDR_COUNTER:   sys_rdata <= counter_reg;
            ADDR_OVERFLOW:  sys_rdata <= {31'b0, overflow_reg};
            default:        sys_rdata <= 32'b0;
        endcase
    end
end

// Example logic: increment counter on some condition
// (Replace with actual module functionality)
always @(posedge clk_i) begin
    if (!rstn_i || !enable_reg) begin
        counter_reg <= 32'b0;
        overflow_reg <= 1'b0;
    end
    else begin
        // Example: increment counter every cycle when enabled
        if (counter_reg == 32'hFFFFFFFF) begin
            overflow_reg <= 1'b1;
        end
        else begin
            counter_reg <= counter_reg + 1;
        end
    end
end

endmodule

// ============================================================================
// Then instantiate in red_pitaya_top.v (around line 770):
// ============================================================================

// Remove the unused assignment for module 8:
// assign sys_rdata[ 8*32+:32] = 32'h0;  // DELETE THIS
// assign sys_err  [ 8       ] =  1'b0;  // DELETE THIS
// assign sys_ack  [ 8       ] =  1'b1;  // DELETE THIS

// Add the module instantiation:
example_module i_example (
    .clk_i      (adc_clk),
    .rstn_i     (adc_rstn),
    .sys_addr   (sys_addr),
    .sys_wdata  (sys_wdata),
    .sys_sel    (sys_sel),
    .sys_wen    (sys_wen[8]),
    .sys_ren    (sys_ren[8]),
    .sys_rdata  (sys_rdata[8*32+31 : 8*32]),
    .sys_err    (sys_err[8]),
    .sys_ack    (sys_ack[8])
);
"""
