
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
// C Model configuration for the "fir_lowpass_2000Hz" instance.
//
//------------------------------------------------------------------------------
//
// coefficients: 160,643,1757,3918,7625,13398,21675,32696,46371,62175,79097,95660,110040,120269,124517,121402,110277,91443,66231,36925,6517,-21684,-44520,-59467,-65047,-61079,-48729,-30344,-9077,11622,28569,39351,42702,38662,28511,14482,-685,-14254,-23981,-28490,-27457,-21590,-12425,-1968,7720,14917,18538,18284,14627,8644,1745,-4637,-9330,-11630,-11385,-8966,-5133,-830,3025,5724,6890,6511,4889,2542,58,-2032,-3361,-3779,-3350,-2308,-976,315,1299,1826,1867,1507,901,227,-355,-738,-880,-801,-565,-259,34,253,366,372,294,169,36,-72,-138,-156,-133,-85,-30,19,49,59,51,32,11,-6,-15,-17,-13,-8,-4,-2,-1,1,5,8,8,2,-8,-13,9
// chanpats: 173
// name: fir_lowpass_2000Hz
// filter_type: 0
// rate_change: 0
// interp_rate: 1
// decim_rate: 1
// zero_pack_factor: 1
// coeff_padding: 0
// num_coeffs: 119
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

const double fir_lowpass_2000Hz_coefficients[119] = {160,643,1757,3918,7625,13398,21675,32696,46371,62175,79097,95660,110040,120269,124517,121402,110277,91443,66231,36925,6517,-21684,-44520,-59467,-65047,-61079,-48729,-30344,-9077,11622,28569,39351,42702,38662,28511,14482,-685,-14254,-23981,-28490,-27457,-21590,-12425,-1968,7720,14917,18538,18284,14627,8644,1745,-4637,-9330,-11630,-11385,-8966,-5133,-830,3025,5724,6890,6511,4889,2542,58,-2032,-3361,-3779,-3350,-2308,-976,315,1299,1826,1867,1507,901,227,-355,-738,-880,-801,-565,-259,34,253,366,372,294,169,36,-72,-138,-156,-133,-85,-30,19,49,59,51,32,11,-6,-15,-17,-13,-8,-4,-2,-1,1,5,8,8,2,-8,-13,9};

const xip_fir_v7_2_pattern fir_lowpass_2000Hz_chanpats[1] = {P_BASIC};

static xip_fir_v7_2_config gen_fir_lowpass_2000Hz_config() {
  xip_fir_v7_2_config config;
  config.name                = "fir_lowpass_2000Hz";
  config.filter_type         = 0;
  config.rate_change         = XIP_FIR_INTEGER_RATE;
  config.interp_rate         = 1;
  config.decim_rate          = 1;
  config.zero_pack_factor    = 1;
  config.coeff               = &fir_lowpass_2000Hz_coefficients[0];
  config.coeff_padding       = 0;
  config.num_coeffs          = 119;
  config.coeff_sets          = 1;
  config.reloadable          = 0;
  config.is_halfband         = 0;
  config.quantization        = XIP_FIR_INTEGER_COEFF;
  config.coeff_width         = 18;
  config.coeff_fract_width   = 0;
  config.chan_seq            = XIP_FIR_BASIC_CHAN_SEQ;
  config.num_channels        = 1;
  config.init_pattern        = fir_lowpass_2000Hz_chanpats[0];
  config.num_paths           = 1;
  config.data_width          = 40;
  config.data_fract_width    = 0;
  config.output_rounding_mode= XIP_FIR_CONVERGENT_EVEN;
  config.output_width        = 32;
  config.output_fract_width  = 0,
  config.config_method       = XIP_FIR_CONFIG_SINGLE;
  return config;
}

const xip_fir_v7_2_config fir_lowpass_2000Hz_config = gen_fir_lowpass_2000Hz_config();

