# TCL script to regenerate an IP from an existing XCI file
# Usage: vivado -mode batch -source regenerate_xci_ip.tcl -tclargs <xci_file> <ip_name> <output_dir>

if {$argc < 3} {
    puts "ERROR: Missing arguments"
    puts "Usage: vivado -mode batch -source regenerate_xci_ip.tcl -tclargs <xci_file> <ip_name> <output_dir>"
    exit 1
}

set xci_file [lindex $argv 0]
set ip_name [lindex $argv 1]
set output_dir [lindex $argv 2]

# FPGA part
set part "xc7z010clg400-1"

set_param general.maxThreads 1

puts "============================================================"
puts "Regenerating IP from XCI: $ip_name"
puts "  XCI file: $xci_file"
puts "  Output directory: $output_dir"
puts "============================================================"

# Create in-memory project
create_project -in_memory -part $part

# Read the IP
# We need to copy the XCI to the output directory first, because Vivado likes to work in-place
# or we read it and then set the output directory?
# Better: copy the XCI to the temp output dir, then read it.

file mkdir $output_dir
set local_xci_path [file join $output_dir [file tail $xci_file]]

# If the XCI is not already in the output dir, copy it
if {[file normalize $xci_file] != [file normalize $local_xci_path]} {
    puts "  Copying XCI to build directory..."
    file copy -force $xci_file $local_xci_path
}

# Read the IP from the local copy
read_ip $local_xci_path

# Check if locked/upgrade needed
set is_locked [get_property IS_LOCKED [get_ips $ip_name]]
if {$is_locked} {
    puts "  IP is locked. Attempting upgrade..."
    upgrade_ip [get_ips $ip_name] -log_ip_upgrade [file join $output_dir "ip_upgrade.log"]
}

# Generate output products
puts "  Generating output products..."
generate_target all [get_ips $ip_name]

# Run synthesis
puts "  Running synthesis..."
synth_ip [get_ips $ip_name]

# Verify DCP
set dcp_file [file join $output_dir "$ip_name.dcp"]
if {[file exists $dcp_file]} {
    puts "  SUCCESS: IP $ip_name regenerated!"
    exit 0
} else {
    puts "  ERROR: DCP file not created!"
    exit 1
}
