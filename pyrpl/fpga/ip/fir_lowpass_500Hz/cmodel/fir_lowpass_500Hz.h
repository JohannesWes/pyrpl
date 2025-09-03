
//------------------------------------------------------------------------------
// (c) Copyright 2023 Advanced Micro Devices. All rights reserved.
//
// This file contains confidential and proprietary information
// of AMD, Inc. and is protected under U.S. and
// international copyright and other intellectual property
// laws.
//
// DISCLAIMER
// This disclaimer is not a license and does not grant any
// rights to the materials distributed herewith. Except as
// otherwise provided in a valid license issued to you by
// AMD, and to the maximum extent permitted by applicable
// law: (1) THESE MATERIALS ARE MADE AVAILABLE "AS IS" AND
// WITH ALL FAULTS, AND AMD HEREBY DISCLAIMS ALL WARRANTIES
// AND CONDITIONS, EXPRESS, IMPLIED, OR STATUTORY, INCLUDING
// BUT NOT LIMITED TO WARRANTIES OF MERCHANTABILITY, NON-
// INFRINGEMENT, OR FITNESS FOR ANY PARTICULAR PURPOSE; and
// (2) AMD shall not be liable (whether in contract or tort,
// including negligence, or under any other theory of
// liability) for any loss or damage of any kind or nature
// related to, arising under or in connection with these
// materials, including for any direct, or any indirect,
// special, incidental, or consequential loss or damage
// (including loss of data, profits, goodwill, or any type of
// loss or damage suffered as a result of any action brought
// by a third party) even if such damage or loss was
// reasonably foreseeable or AMD had been advised of the
// possibility of the same.
//
// CRITICAL APPLICATIONS
// AMD products are not designed or intended to be fail-
// safe, or for use in any application requiring fail-safe
// performance, such as life-support or safety devices or
// systems, Class III medical devices, nuclear facilities,
// applications related to the deployment of airbags, or any
// other applications that could lead to death, personal
// injury, or severe property or environmental damage
// (individually and collectively, "Critical
// Applications"). Customer assumes the sole risk and
// liability of any use of AMD products in Critical
// Applications, subject only to applicable laws and
// regulations governing limitations on product liability.
//
// THIS COPYRIGHT NOTICE AND DISCLAIMER MUST BE RETAINED AS
// PART OF THIS FILE AT ALL TIMES.
//------------------------------------------------------------------------------ 
//
// C Model configuration for the "fir_lowpass_500Hz" instance.
//
//------------------------------------------------------------------------------
//
// coefficients: 4475,3567,4912,6549,8512,10802,13432,16442,19840,23617,27773,32305,37191,42414,47940,53702,59669,65818,72045,78260,84428,90465,96259,101734,106815,111402,115419,118807,121476,123344,124376,124517,123734,122033,119390,115800,111300,105927,99729,92779,85144,76914,68199,59101,49724,40200,30666,21222,11996,3120,-5311,-13195,-20417,-26918,-32626,-37456,-41397,-44430,-46527,-47704,-47978,-47381,-45983,-43834,-41001,-37585,-33664,-29339,-24725,-19911,-14995,-10087,-5267,-628,3736,7770,11411,14602,17316,19504,21154,22293,22904,23000,22632,21819,20602,19051,17206,15119,12870,10511,8084,5656,3286,1011,-1127,-3082,-4829,-6343,-7606,-8625,-9387,-9872,-10115,-10130,-9925,-9546,-9005,-8306,-7495,-6607,-5655,-4677,-3714,-2763,-1835,-991,-234,468,1080,1575,1972,2275,2483,2604,2641,2599,2501,2364,2175,1945,1710,1472,3671
// chanpats: 173
// name: fir_lowpass_500Hz
// filter_type: 0
// rate_change: 0
// interp_rate: 1
// decim_rate: 1
// zero_pack_factor: 1
// coeff_padding: 0
// num_coeffs: 135
// coeff_sets: 1
// reloadable: 0
// is_halfband: 0
// quantization: 0
// coeff_width: 18
// coeff_fract_width: 0
// chan_seq: 0
// num_channels: 1
// num_paths: 1
// data_width: 40
// data_fract_width: 0
// output_rounding_mode: 4
// output_width: 32
// output_fract_width: 0
// config_method: 0

const double fir_lowpass_500Hz_coefficients[135] = {4475,3567,4912,6549,8512,10802,13432,16442,19840,23617,27773,32305,37191,42414,47940,53702,59669,65818,72045,78260,84428,90465,96259,101734,106815,111402,115419,118807,121476,123344,124376,124517,123734,122033,119390,115800,111300,105927,99729,92779,85144,76914,68199,59101,49724,40200,30666,21222,11996,3120,-5311,-13195,-20417,-26918,-32626,-37456,-41397,-44430,-46527,-47704,-47978,-47381,-45983,-43834,-41001,-37585,-33664,-29339,-24725,-19911,-14995,-10087,-5267,-628,3736,7770,11411,14602,17316,19504,21154,22293,22904,23000,22632,21819,20602,19051,17206,15119,12870,10511,8084,5656,3286,1011,-1127,-3082,-4829,-6343,-7606,-8625,-9387,-9872,-10115,-10130,-9925,-9546,-9005,-8306,-7495,-6607,-5655,-4677,-3714,-2763,-1835,-991,-234,468,1080,1575,1972,2275,2483,2604,2641,2599,2501,2364,2175,1945,1710,1472,3671};

const xip_fir_v7_2_pattern fir_lowpass_500Hz_chanpats[1] = {P_BASIC};

static xip_fir_v7_2_config gen_fir_lowpass_500Hz_config() {
  xip_fir_v7_2_config config;
  config.name                = "fir_lowpass_500Hz";
  config.filter_type         = 0;
  config.rate_change         = XIP_FIR_INTEGER_RATE;
  config.interp_rate         = 1;
  config.decim_rate          = 1;
  config.zero_pack_factor    = 1;
  config.coeff               = &fir_lowpass_500Hz_coefficients[0];
  config.coeff_padding       = 0;
  config.num_coeffs          = 135;
  config.coeff_sets          = 1;
  config.reloadable          = 0;
  config.is_halfband         = 0;
  config.quantization        = XIP_FIR_INTEGER_COEFF;
  config.coeff_width         = 18;
  config.coeff_fract_width   = 0;
  config.chan_seq            = XIP_FIR_BASIC_CHAN_SEQ;
  config.num_channels        = 1;
  config.init_pattern        = fir_lowpass_500Hz_chanpats[0];
  config.num_paths           = 1;
  config.data_width          = 40;
  config.data_fract_width    = 0;
  config.output_rounding_mode= XIP_FIR_CONVERGENT_EVEN;
  config.output_width        = 32;
  config.output_fract_width  = 0,
  config.config_method       = XIP_FIR_CONFIG_SINGLE;
  return config;
}

const xip_fir_v7_2_config fir_lowpass_500Hz_config = gen_fir_lowpass_500Hz_config();

