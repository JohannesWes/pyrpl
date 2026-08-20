################################################################################
# TCL script to generate a SINGLE FIR lowpass IP core
#
# Usage (called by generate_fir_ips.bat):
#   vivado -mode batch -source generate_single_fir_ip.tcl -tclargs <coe_file> <ip_name> ?<output_dir>? ?<coefficient_width>?
#
# This script is designed to be called in a fresh Vivado process for each IP
# to avoid .Xil directory lock issues on Windows.
################################################################################

# Get arguments
if {$argc < 2} {
    puts "ERROR: Missing arguments"
    puts "Usage: vivado -mode batch -source generate_single_fir_ip.tcl -tclargs <coe_file> <ip_name>"
    exit 1
}

set coe_file [lindex $argv 0]
set ip_name [lindex $argv 1]

# Optional: Output directory (default to script directory if not provided)
if {$argc > 2} {
    set output_base_dir [lindex $argv 2]
} else {
    set output_base_dir [file dirname [info script]]
}

# DSP48E1 coefficients are normally 18 bits.  A wider value may be supplied
# for a filter that needs it to preserve a shared fixed-point gain.
if {$argc > 3} {
    set coefficient_width [lindex $argv 3]
} else {
    set coefficient_width 18
}

set ip_dir [file join $output_base_dir $ip_name]

# FPGA part (Red Pitaya uses Zynq 7010)
set part "xc7z010clg400-1"

# WORKAROUND for Vivado 2024.x .Xil/realtime directory lock bug on Windows
# Disable multithreading completely
set_param general.maxThreads 1

puts "============================================================"
puts "Generating IP: $ip_name"
puts "  Coefficient file: $coe_file"
puts "  Output directory: $ip_dir"
puts "  Coefficient width: $coefficient_width"
puts "============================================================"

# Remove existing IP directory if it exists
if {[file exists $ip_dir]} {
    puts "  Removing existing IP directory..."
    file delete -force $ip_dir
    after 500
}

# Create a temporary in-memory project for IP creation
create_project -in_memory -part $part

# Create the FIR Compiler IP
create_ip -name fir_compiler -vendor xilinx.com -library ip -version 7.2 \
    -module_name $ip_name -dir $output_base_dir

# Configure the IP
# Use absolute path for coefficient file to avoid path issues during generation
set abs_coe_path [file normalize $coe_file]

set_property -dict [list \
    CONFIG.CoefficientSource {COE_File} \
    CONFIG.Coefficient_File $abs_coe_path \
    CONFIG.Coefficient_Sets {1} \
    CONFIG.Coefficient_Reload {false} \
    CONFIG.Filter_Type {Single_Rate} \
    CONFIG.Rate_Change_Type {Integer} \
    CONFIG.Interpolation_Rate {1} \
    CONFIG.Decimation_Rate {1} \
    CONFIG.Zero_Pack_Factor {1} \
    CONFIG.Channel_Sequence {Basic} \
    CONFIG.Number_Channels {1} \
    CONFIG.Select_Pattern {All} \
    CONFIG.Number_Paths {1} \
    CONFIG.RateSpecification {Frequency_Specification} \
    CONFIG.Sample_Frequency {0.030517578125} \
    CONFIG.Clock_Frequency {125} \
    CONFIG.Coefficient_Sign {Signed} \
    CONFIG.Quantization {Integer_Coefficients} \
    CONFIG.Coefficient_Width $coefficient_width \
    CONFIG.BestPrecision {false} \
    CONFIG.Coefficient_Fractional_Bits {0} \
    CONFIG.Coefficient_Structure {Inferred} \
    CONFIG.Data_Width {40} \
    CONFIG.Data_Fractional_Bits {0} \
    CONFIG.Output_Rounding_Mode {Convergent_Rounding_to_Even} \
    CONFIG.Output_Width {32} \
    CONFIG.Filter_Architecture {Systolic_Multiply_Accumulate} \
    CONFIG.Optimization_Goal {Area} \
    CONFIG.DATA_Has_TLAST {Not_Required} \
    CONFIG.M_DATA_Has_TREADY {false} \
    CONFIG.S_DATA_Has_FIFO {true} \
    CONFIG.S_DATA_Has_TUSER {Not_Required} \
    CONFIG.M_DATA_Has_TUSER {Not_Required} \
    CONFIG.Has_ACLKEN {true} \
    CONFIG.Has_ARESETn {false} \
] [get_ips $ip_name]

# Generate all output products (synthesis, simulation, etc.)
puts "  Generating output products..."
generate_target all [get_ips $ip_name]

# Run out-of-context synthesis to generate .dcp file
puts "  Running out-of-context synthesis..."
synth_ip [get_ips $ip_name]

# Close the project
close_project

# Fix the XCI file to use relative paths
set xci_file [file join $ip_dir "${ip_name}.xci"]
if {[file exists $xci_file]} {
    puts "  Fixing paths in XCI file..."

    # Read the XCI file
    set fp [open $xci_file r]
    set content [read $fp]
    close $fp

    # Replace the absolute coefficient path with a relative one
    set coe_filename [file tail $coe_file]
    set relative_coe_path "../fir_filter_coefs/$coe_filename"

    # Use regex to replace the Coefficient_File value
    set pattern {"Coefficient_File": \[ \{ "value": "[^"]*"}
    set replacement "\"Coefficient_File\": \[ \{ \"value\": \"$relative_coe_path\""
    set content [regsub $pattern $content $replacement]

    # Also ensure gen_directory is "."
    set pattern {"gen_directory": "[^"]*"}
    set replacement "\"gen_directory\": \".\""
    set content [regsub $pattern $content $replacement]

    # Write back
    set fp [open $xci_file w]
    puts -nonewline $fp $content
    close $fp
}

# Verify DCP was created
set dcp_file [file join $ip_dir "${ip_name}.dcp"]
if {[file exists $dcp_file]} {
    puts "  SUCCESS: IP $ip_name generated with DCP!"
    exit 0
} else {
    puts "  ERROR: DCP file not created!"
    exit 1
}
