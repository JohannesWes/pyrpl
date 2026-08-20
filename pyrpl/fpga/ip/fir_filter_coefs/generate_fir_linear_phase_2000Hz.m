function generate_fir_linear_phase_2000Hz()
%GENERATE_FIR_LINEAR_PHASE_2000HZ Generate the CIC-compensating linear FIR.
%
% This is the linear-phase spectral counterpart of fir_lowpass_2000Hz.coe.
% It deliberately follows the "Convolved Half-Filter" construction in
% minphase_FIR_generator.m: h_half is linear phase, while firminphase of
% conv(h_half, h_half) has (within numerical precision) the same magnitude.

output_dir = fileparts(mfilename('fullpath'));
output_file = fullfile(output_dir, 'fir_linear_phase_2000Hz.coe');
minphase_file = fullfile(output_dir, 'fir_lowpass_2000Hz.coe');

fs_adc = 125e6;
r_cic = 4096;
n_cic = 5;
m_cic = 2;
fs = fs_adc / r_cic;

f_pass = 2000;
f_stop = 2500;
passband_ripple_db = 0.5;
stopband_attenuation_db = 43;

delta_p = (10^(passband_ripple_db / 20) - 1) / ...
          (10^(passband_ripple_db / 20) + 1);
delta_s = 10^(-stopband_attenuation_db / 20);
[estimated_order, ~, ~, ~] = firpmord( ...
    [f_pass f_stop], [1 0], [delta_p delta_s], fs);

f_grid_pass = linspace(0, f_pass, 100);
w_grid_pass = 2 * pi * f_grid_pass / fs;
h_cic_pass = abs(sin(m_cic * w_grid_pass / 2) ./ ...
                 (sin(w_grid_pass / (2 * r_cic)) + eps)).^n_cic / ...
             (r_cic * m_cic)^n_cic;
h_cic_pass(1) = 1;
cic_compensation = 1 ./ (h_cic_pass + eps);

linear_order = estimated_order + 12;
if mod(linear_order, 2) ~= 0
    linear_order = linear_order + 1;
end

normalized_frequency = [f_grid_pass / (fs / 2), f_stop / (fs / 2), 1];
desired_magnitude = [cic_compensation, 0, 0];
h_linear = fir2(linear_order, normalized_frequency, desired_magnitude);
h_linear = h_linear / sum(h_linear);

% Recreate the spectral factor to verify that this is the magnitude-matched
% linear-phase partner, rather than the uncompensated firpm reference filter.
h_minphase_float = firminphase(conv(h_linear, h_linear));
h_minphase_float = h_minphase_float / sum(h_minphase_float);
[h_linear_response, frequency] = freqz(h_linear, 1, 65536, fs);
h_minphase_response = freqz(h_minphase_float, 1, 65536, fs);
float_magnitude_error = max(abs(abs(h_linear_response) - ...
                                abs(h_minphase_response)));
assert(float_magnitude_error < 1e-6, ...
       'Linear/minimum-phase magnitude mismatch: %.3g', float_magnitude_error);
assert(max(abs(h_linear - fliplr(h_linear))) < 1e-12, ...
       'Generated coefficients are not symmetric.');

% The symmetric filter's centre tap cannot fit in 18 bits at the existing
% minimum-phase coefficient scale. Use an 18-bit scale selected so a cheap
% exact shift/add correction in lock_in.v restores the established raw gain:
%   gain = 1 + 1/8 + 1/32 + 1/128 + 1/256 = 1.16796875.
% This saves four DSP48E1 blocks per instantiated linear FIR versus 19-bit
% coefficients while keeping the two phase responses independently filtered.
coefficient_scale = 802861;
coefficient_width = 18;
hardware_gain_correction = 1 + 2^-3 + 2^-5 + 2^-7 + 2^-8;
h_linear_integer = round(h_linear * coefficient_scale);
maximum_integer = 2^(coefficient_width - 1) - 1;
minimum_integer = -2^(coefficient_width - 1);
assert(all(h_linear_integer <= maximum_integer & ...
           h_linear_integer >= minimum_integer), ...
       'Generated coefficients do not fit in %d signed bits.', coefficient_width);

fid = fopen(output_file, 'w');
assert(fid >= 0, 'Could not open %s for writing.', output_file);
cleanup = onCleanup(@() fclose(fid));
fprintf(fid, 'radix=10;\ncoefdata=\n');
fprintf(fid, '%d,\n', h_linear_integer(1:end-1));
fprintf(fid, '%d;\n', h_linear_integer(end));
clear cleanup;

% Compare the quantized candidate with the coefficient set actually used by
% the existing minimum-phase IP, including the CIC response.
h_minphase_integer = read_coe_coefficients(minphase_file);
h_linear_quantized = h_linear_integer / coefficient_scale;
h_minphase_scale = 937718;
h_minphase_quantized = h_minphase_integer / h_minphase_scale;
h_linear_quantized_response = freqz(h_linear_quantized, 1, frequency, fs);
h_minphase_quantized_response = freqz(h_minphase_quantized, 1, frequency, fs);

w = 2 * pi * frequency / fs;
h_cic = abs(sin(m_cic * w / 2) ./ ...
            (sin(w / (2 * r_cic)) + eps)).^n_cic / ...
        (r_cic * m_cic)^n_cic;
h_cic(1) = 1;
combined_linear_db = 20 * log10(h_cic .* ...
    abs(h_linear_quantized_response) + eps);
combined_minphase_db = 20 * log10(h_cic .* ...
    abs(h_minphase_quantized_response) + eps);
passband = frequency > 10 & frequency <= 0.9 * f_pass;
stopband = frequency >= f_stop;

linear_mean = mean(combined_linear_db(passband));
minphase_mean = mean(combined_minphase_db(passband));
linear_ripple = max(abs(combined_linear_db(passband) - linear_mean));
minphase_ripple = max(abs(combined_minphase_db(passband) - minphase_mean));
linear_stopband = -max(combined_linear_db(stopband));
minphase_stopband = -max(combined_minphase_db(stopband));
quantized_passband_error_db = max(abs( ...
    combined_linear_db(frequency <= f_pass) - ...
    combined_minphase_db(frequency <= f_pass)));
quantized_magnitude_error = max(abs( ...
    abs(h_linear_quantized_response) - ...
    abs(h_minphase_quantized_response)));

fprintf('Generated %s\n', output_file);
fprintf('Taps: %d; coefficient width: %d bits; scale: %d\n', ...
        length(h_linear_integer), coefficient_width, coefficient_scale);
fprintf('Linear-phase group delay: %.1f samples (%.3f ms)\n', ...
        linear_order / 2, linear_order / 2 / fs * 1e3);
fprintf('Maximum coefficient: %d; DC sum: %d\n', ...
        max(abs(h_linear_integer)), sum(h_linear_integer));
fprintf('Raw DC sum after hardware correction: %.3f (minphase: %d)\n', ...
        sum(h_linear_integer) * hardware_gain_correction, ...
        sum(h_minphase_integer));
fprintf('CIC+FIR ripple (linear/minphase): %.5f / %.5f dB\n', ...
        linear_ripple, minphase_ripple);
fprintf('CIC+FIR stopband attenuation (linear/minphase): %.2f / %.2f dB\n', ...
        linear_stopband, minphase_stopband);
fprintf('Float magnitude mismatch: %.3g\n', float_magnitude_error);
fprintf('Quantized magnitude mismatch: %.3g (%.5f dB in passband)\n', ...
        quantized_magnitude_error, quantized_passband_error_db);
end


function coefficients = read_coe_coefficients(filename)
text = fileread(filename);
start_index = regexpi(text, 'coefdata\s*=\s*', 'end', 'once');
assert(~isempty(start_index), 'No coefdata field found in %s.', filename);
tokens = regexp(text(start_index + 1:end), '-?\d+', 'match');
coefficients = cellfun(@str2double, tokens);
end
