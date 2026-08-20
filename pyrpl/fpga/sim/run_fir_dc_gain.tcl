set script_dir [file dirname [file normalize [info script]]]
set fpga_dir [file normalize [file join $script_dir ..]]
set project_dir [file join $script_dir [format ".fir_dc_gain_sim_%d" [clock seconds]]]
create_project -force fir_dc_gain $project_dir -part xc7z010clg400-1
read_ip [file join $fpga_dir ip fir_lowpass_2000Hz fir_lowpass_2000Hz.xci]
read_ip [file join $fpga_dir ip fir_linear_phase_2000Hz fir_linear_phase_2000Hz.xci]
generate_target simulation [get_ips]
add_files -fileset sim_1 [file join $script_dir tb_fir_dc_gain.v]
add_files -fileset sim_1 [file join $fpga_dir rtl fir_gain_compensation.v]
set_property top tb_fir_dc_gain [get_filesets sim_1]
launch_simulation
run all
close_sim
close_project
