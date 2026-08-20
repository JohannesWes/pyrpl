################################################################################
# Vivado tcl script for building RedPitaya FPGA in non project mode
#
# Usage:
# vivado -mode tcl -source red_pitaya_vivado.tcl
################################################################################

################################################################################
# define paths
################################################################################

set path_rtl rtl
set path_ip  ip
set path_sdc sdc

set path_out out
set path_sdk sdk

file mkdir $path_out
file mkdir $path_sdk
if {$tcl_platform(platform) eq "windows"} {
    file attributes $path_out -readonly 0
    file attributes $path_sdk -readonly 0
}

# Vivado 2024.x on Windows can create read-only worker directories under .Xil
# during parallel synthesis. Single-threaded mode avoids that worker-directory
# failure and matches the IP-generation scripts in this repository.
set_param general.maxThreads 1

################################################################################
# setup an in memory project
################################################################################

set part xc7z010clg400-1

create_project -in_memory -part $part

# experimental attempts to avoid a warning
#get_projects
#get_designs
#list_property  [current_project]
#set_property FAMILY 7SERIES [current_project]
#set_property SIM_DEVICE 7SERIES [current_project]

################################################################################
# create PS BD (processing system block design)
################################################################################

# file was created from GUI using "write_bd_tcl -force ip/system_bd.tcl"
# create PS BD
source                            $path_ip/system_bd.tcl

# generate SDK files
generate_target all [get_files    system.bd]

################################################################################
# read files:
# 1. IP database files
# 2. RTL design sources
# 3. constraints
################################################################################

# template
#read_verilog                      $path_rtl/...

read_verilog                      .gen/sources_1/bd/system/hdl/system_wrapper.v
read_ip                           $path_ip/fir_lowpass_500Hz/fir_lowpass_500Hz.xci
read_ip                           $path_ip/fir_lowpass_2000Hz/fir_lowpass_2000Hz.xci
read_ip                           $path_ip/fir_linear_phase_2000Hz/fir_linear_phase_2000Hz.xci
read_ip                           $path_ip/fir_lowpass_5000Hz/fir_lowpass_5000Hz.xci
read_ip                           $path_ip/cic_decimate_by_4096/cic_decimate_by_4096.xci

read_verilog                      $path_rtl/axi_master.v
read_verilog                      $path_rtl/axi_slave.v
read_verilog                      $path_rtl/axi_wr_fifo.v

read_verilog                      $path_rtl/red_pitaya_ams.v
read_verilog                      $path_rtl/red_pitaya_asg_ch.v
read_verilog                      $path_rtl/red_pitaya_asg.v
read_verilog                      $path_rtl/red_pitaya_dfilt1.v
read_verilog                      $path_rtl/red_pitaya_hk.v
read_verilog                      $path_rtl/red_pitaya_pid_block.v
read_verilog                      $path_rtl/red_pitaya_dsp.v
read_verilog                      $path_rtl/red_pitaya_pll.sv
read_verilog                      $path_rtl/red_pitaya_ps.v
read_verilog                      $path_rtl/red_pitaya_pwm.sv
read_verilog                      $path_rtl/red_pitaya_scope.v
read_verilog                      $path_rtl/red_pitaya_top.v

#custom modules
read_verilog                      $path_rtl/red_pitaya_adv_trigger.v
read_verilog                      $path_rtl/red_pitaya_saturate.v
read_verilog                      $path_rtl/red_pitaya_product_sat.v
read_verilog                      $path_rtl/red_pitaya_iir_block.v
read_verilog                      $path_rtl/red_pitaya_iq_modulator_block.v
read_verilog                      $path_rtl/red_pitaya_lpf_block.v
read_verilog                      $path_rtl/red_pitaya_filter_block.v
#read_verilog                     $path_rtl/red_pitaya_iq_lpf_block.v
read_verilog                      $path_rtl/red_pitaya_iq_demodulator_block.v
read_verilog                      $path_rtl/red_pitaya_pfd_block.v
#read_verilog                     $path_rtl/red_pitaya_iq_hpf_block.v
read_verilog                      $path_rtl/red_pitaya_iq_fgen_block.v
read_verilog                      $path_rtl/red_pitaya_iq_block.v
read_verilog                      $path_rtl/red_pitaya_trigger_block.v
read_verilog                      $path_rtl/red_pitaya_prng.v
read_verilog                      $path_rtl/red_pitaya_quarter_wave_lut.v
read_verilog                      $path_rtl/red_pitaya_3fgen.v
read_verilog                      $path_rtl/scan_new.v
read_verilog                      $path_rtl/fir_gain_compensation.v
read_verilog                      $path_rtl/lock_in.v
read_verilog                      $path_rtl/odmr_freq_lock_1f.v
read_verilog                      $path_rtl/red_pitaya_quarter_wave_lut17.v
read_verilog                      $path_rtl/odmr_multitrack.v

#constraints
read_xdc                          $path_sdc/red_pitaya.xdc
read_xdc                          $path_ip/fir_lowpass_500Hz/constraints/fir_compiler_v7_2.xdc
read_xdc                          $path_ip/fir_lowpass_2000Hz/constraints/fir_compiler_v7_2.xdc
read_xdc                          $path_ip/fir_linear_phase_2000Hz/constraints/fir_compiler_v7_2.xdc
read_xdc                          $path_ip/fir_lowpass_5000Hz/constraints/fir_compiler_v7_2.xdc
read_xdc                          $path_ip/cic_decimate_by_4096/cic_decimate_by_4096_ooc.xdc

################################################################################
# run synthesis
# report utilization and timing estimates
# write checkpoint design
################################################################################

#synth_design -top red_pitaya_top
synth_design -top red_pitaya_top -flatten_hierarchy none -bufg 16 -keep_equivalent_registers

write_checkpoint         -force   $path_out/post_synth
report_timing_summary    -file    $path_out/post_synth_timing_summary.rpt
report_power             -file    $path_out/post_synth_power.rpt

################################################################################
# run placement and logic optimization
# report utilization and timing estimates
# write checkpoint design
################################################################################

opt_design
power_opt_design
place_design
phys_opt_design
write_checkpoint         -force   $path_out/post_place
report_timing_summary    -file    $path_out/post_place_timing_summary.rpt
#write_hwdef              -file    $path_sdk/red_pitaya.hwdef

################################################################################
# run router
# report actual utilization and timing,
# write checkpoint design
# run drc, write verilog and xdc out
################################################################################

route_design
write_checkpoint         -force   $path_out/post_route
report_timing_summary    -file    $path_out/post_route_timing_summary.rpt
report_timing            -file    $path_out/post_route_timing.rpt -sort_by group -max_paths 100 -path_type summary
report_clock_utilization -file    $path_out/clock_util.rpt
report_utilization       -file    $path_out/post_route_util.rpt
report_power             -file    $path_out/post_route_power.rpt
report_drc               -file    $path_out/post_imp_drc.rpt
#write_verilog            -force   $path_out/bft_impl_netlist.v
#write_xdc -no_fixed_only -force   $path_out/bft_impl.xdc

################################################################################
# generate a bitstream
################################################################################

set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]
write_bitstream -force $path_out/red_pitaya.bit

################################################################################
# generate the .bin file for flashing
################################################################################

set_property BITSTREAM.GENERAL.COMPRESS FALSE [current_design]
write_bitstream -force $path_out/red_pitaya_uncompressed.bit
write_cfgmem -force -format BIN -size 2 -interface SMAPx32 -disablebitswap -loadbit "up 0x0 $path_out/red_pitaya_uncompressed.bit" $path_out/red_pitaya.bin

################################################################################
# generate system definition
################################################################################

write_hw_platform -include_bit -fixed -force $path_sdk/red_pitaya.xsa
validate_hw_platform $path_sdk/red_pitaya.xsa

file copy -force .gen/sources_1/bd/system/hw_handoff/system.hwh $path_sdk/red_pitaya.hwh

exit
