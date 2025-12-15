
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
// C Model configuration for the "fir_lowpass_5000Hz" instance.
//
//------------------------------------------------------------------------------
//
// coefficients: 485,3678,14591,39037,76805,114021,124517,86641,6717,-73428,-100893,-55075,28358,81654,62374,-7922,-63308,-55470,1860,49816,44393,-2275,-39594,-32778,4652,30942,22096,-6749,-23124,-13200,7612,16092,6543,-7139,-10144,-2194,5736,5627,-74,-3892,-2536,924,2236,801,-915,-1037,-52,581,351,-135,-261,-62,102,78,-11,-39,-13,10,9,-1,-2,-1,1,0,0
// chanpats: 173
// name: fir_lowpass_5000Hz
// filter_type: 0
// rate_change: 0
// interp_rate: 1
// decim_rate: 1
// zero_pack_factor: 1
// coeff_padding: 0
// num_coeffs: 65
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

const double fir_lowpass_5000Hz_coefficients[65] = {485,3678,14591,39037,76805,114021,124517,86641,6717,-73428,-100893,-55075,28358,81654,62374,-7922,-63308,-55470,1860,49816,44393,-2275,-39594,-32778,4652,30942,22096,-6749,-23124,-13200,7612,16092,6543,-7139,-10144,-2194,5736,5627,-74,-3892,-2536,924,2236,801,-915,-1037,-52,581,351,-135,-261,-62,102,78,-11,-39,-13,10,9,-1,-2,-1,1,0,0};

const xip_fir_v7_2_pattern fir_lowpass_5000Hz_chanpats[1] = {P_BASIC};

static xip_fir_v7_2_config gen_fir_lowpass_5000Hz_config() {
  xip_fir_v7_2_config config;
  config.name                = "fir_lowpass_5000Hz";
  config.filter_type         = 0;
  config.rate_change         = XIP_FIR_INTEGER_RATE;
  config.interp_rate         = 1;
  config.decim_rate          = 1;
  config.zero_pack_factor    = 1;
  config.coeff               = &fir_lowpass_5000Hz_coefficients[0];
  config.coeff_padding       = 0;
  config.num_coeffs          = 65;
  config.coeff_sets          = 1;
  config.reloadable          = 0;
  config.is_halfband         = 0;
  config.quantization        = XIP_FIR_INTEGER_COEFF;
  config.coeff_width         = 18;
  config.coeff_fract_width   = 0;
  config.chan_seq            = XIP_FIR_BASIC_CHAN_SEQ;
  config.num_channels        = 1;
  config.init_pattern        = fir_lowpass_5000Hz_chanpats[0];
  config.num_paths           = 1;
  config.data_width          = 40;
  config.data_fract_width    = 0;
  config.output_rounding_mode= XIP_FIR_CONVERGENT_EVEN;
  config.output_width        = 32;
  config.output_fract_width  = 0,
  config.config_method       = XIP_FIR_CONFIG_SINGLE;
  return config;
}

const xip_fir_v7_2_config fir_lowpass_5000Hz_config = gen_fir_lowpass_5000Hz_config();

