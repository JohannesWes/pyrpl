################################################################################
# TCL script to generate FIR lowpass IP cores from coefficient files
#
# Usage:
#   cd pyrpl/fpga/ip
#   vivado -mode batch -source generate_fir_ips.tcl
#
# This script reads all .coe files from fir_filter_coefs/ and generates
# corresponding FIR Compiler IP cores with identical settings.
################################################################################

# Configuration - adjust these paths if needed
set script_dir [file dirname [info script]]
set coef_dir [file join $script_dir "fir_filter_coefs"]
set output_base_dir $script_dir

# FPGA part (Red Pitaya uses Zynq 7010)
set part "xc7z010clg400-1"

# Clean up any leftover .Xil directory
set xil_dir [file join $script_dir ".Xil"]
if {[file exists $xil_dir]} {
    puts "Cleaning up old .Xil directory..."
    file delete -force $xil_dir
}

# Find all .coe files in the coefficients directory
set coe_files [glob -nocomplain -directory $coef_dir *.coe]

if {[llength $coe_files] == 0} {
    puts "ERROR: No .coe files found in $coef_dir"
    exit 1
}

puts "Found [llength $coe_files] coefficient files:"
foreach coe_file $coe_files {
    puts "  - [file tail $coe_file]"
}

# Process each coefficient file using non-project mode
foreach coe_file $coe_files {
    # Extract IP name from filename (e.g., fir_lowpass_500Hz.coe -> fir_lowpass_500Hz)
    set ip_name [file rootname [file tail $coe_file]]
    set ip_dir [file join $output_base_dir $ip_name]

    puts "\n============================================================"
    puts "Generating IP: $ip_name"
    puts "  Coefficient file: $coe_file"
    puts "  Output directory: $ip_dir"
    puts "============================================================"

    # Remove existing IP directory if it exists
    if {[file exists $ip_dir]} {
        puts "  Removing existing IP directory..."
        file delete -force $ip_dir
    }

    # Create a temporary in-memory project for IP creation
    create_project -in_memory -part $part

    # Create the FIR Compiler IP with settings matching the 500Hz reference
    # These settings are extracted from fir_lowpass_500Hz.xci
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
        CONFIG.Coefficient_Width {18} \
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
        CONFIG.Has_ACLKEN {false} \
        CONFIG.Has_ARESETn {false} \
    ] [get_ips $ip_name]

    # Generate all output products (synthesis, simulation, etc.)
    puts "  Generating output products..."
    generate_target all [get_ips $ip_name]

    # Run out-of-context synthesis to generate .dcp file
    # This works in non-project mode (in-memory project)
    puts "  Running out-of-context synthesis..."
    synth_ip [get_ips $ip_name]

    puts "  IP $ip_name generated successfully!"

    # Close the in-memory project before processing next IP
    close_project

    # Clean up .Xil directory between runs to avoid conflicts
    if {[file exists $xil_dir]} {
        file delete -force $xil_dir
    }
}

# Now fix the XCI files to use relative paths
puts "\n============================================================"
puts "Post-processing: Fixing coefficient file paths in XCI files"
puts "============================================================"

foreach coe_file $coe_files {
    set ip_name [file rootname [file tail $coe_file]]
    set xci_file [file join $output_base_dir $ip_name "${ip_name}.xci"]

    if {[file exists $xci_file]} {
        puts "  Processing $xci_file..."

        # Read the XCI file
        set fp [open $xci_file r]
        set content [read $fp]
        close $fp

        # Replace the absolute coefficient path with a relative one
        # The relative path should be: ../fir_filter_coefs/<filename>.coe
        set coe_filename [file tail $coe_file]
        set relative_coe_path "../fir_filter_coefs/$coe_filename"

        # Use regex to replace the Coefficient_File value
        # Match the JSON pattern for Coefficient_File
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

        puts "    Fixed paths in $ip_name.xci"
    }
}

# Final cleanup
if {[file exists $xil_dir]} {
    file delete -force $xil_dir
}

puts "\n============================================================"
puts "FIR IP generation complete!"
puts "============================================================"
puts "\nGenerated IPs:"
foreach coe_file $coe_files {
    set ip_name [file rootname [file tail $coe_file]]
    set dcp_file [file join $output_base_dir $ip_name "${ip_name}.dcp"]
    if {[file exists $dcp_file]} {
        puts "  - $ip_name (DCP OK)"
    } else {
        puts "  - $ip_name (WARNING: DCP missing!)"
    }
}
puts "\nYou can now run the main FPGA build."

exit 0
