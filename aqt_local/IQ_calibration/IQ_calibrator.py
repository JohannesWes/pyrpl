import time
import numpy as np
import scipy.optimize as opti
import matplotlib.pyplot as plt
from scipy.signal import find_peaks
import pandas as pd

from configuration_IQ_calibration import Parameters
from auto_mixer_tools_visa import RhodeSchwarzRTO6
from rpy_control import MyIQDevice

################
# Main Calibrator
################

class IQMixerCalibrator:
    """
    A class to calibrate an IQ mixer using an oscilloscope (RhodeSchwarzRTO6)
    and a custom IQ device (MyIQDevice).
    """

    def __init__(self, i_amp=0.0, q_amp=0.0, i_offset=0.0, q_offset=0.0):
        self.parameters = Parameters()
        self.i_amp = i_amp
        self.q_amp = q_amp
        self.i_offset = i_offset
        self.q_offset = q_offset

        # Initialize references to hardware (None until connect)
        self.oscilloscope = None
        self.iq_device = None

    def connect_instruments(self):
        """
        Connect to the new IQ device and the RTO scope.
        """
        try:
            # 1. Connect to your custom IQ device
            self.iq_device = MyIQDevice(address="10.203.129.28")
            # Example: set initial amplitudes, offsets, phases, etc.
            self.iq_device.set_i_amplitude(self.i_amp)
            self.iq_device.set_q_amplitude(self.q_amp)
            self.iq_device.set_i_offset(self.i_offset)
            self.iq_device.set_q_offset(self.q_offset)
            self.iq_device.set_frequency(self.parameters.f_base)

            # 2. Connect to Oscilloscope
            self.oscilloscope = RhodeSchwarzRTO6(address=self.parameters.osci_address)
            self.oscilloscope.method = self.parameters.method
            self.oscilloscope.set_cont_off()

            # Possibly set auto/ manual bandwidth
            # self.oscilloscope.set_automatic_video_bandwidth(1)
            # self.oscilloscope.set_automatic_bandwidth(0)

        except Exception as e:
            raise RuntimeError(f"Error connecting to instruments: {e}")

    def disconnect_instruments(self):
        """
        Disconnect from the new IQ device and RTO scope.
        """
        try:
            if self.oscilloscope:
                self.oscilloscope.set_cont_on()
                self.oscilloscope.__del__()  # or self.oscilloscope.sa.close()

            if self.iq_device:
                self.iq_device.close()

        except Exception as e:
            print(f"Error during instrument disconnection: {e}")

    def _setup_oscilloscope_measurement(self):
        """
        Configure the scope for power measurement or marker measurement.
        """
        if self.parameters.method == 1:  # channel power
            self.oscilloscope.enable_measurement()
            self.oscilloscope.sets_measurement_integration_bw(10 * self.parameters.measBW)
            self.oscilloscope.disables_measurement_averaging()
        elif self.parameters.method == 2:  # marker
            self.oscilloscope.get_single_trigger()
            self.oscilloscope.active_marker(1)

        self.oscilloscope.set_sweep_points(self.parameters.measNumPoints)
        self.oscilloscope.set_span(self.parameters.fullSpan)
        self.oscilloscope.set_bandwidth(1e6) # fixme: Why is this hardcoded here?

    def _get_signal_power(self):
        """
        Measures the power at the desired IF frequency
        (LO + IF or LO - IF).
        """
        freq = self.parameters.f_lo + self.parameters.f_base
        self.oscilloscope.set_center_freq(freq)
        if self.parameters.method == 2:
            self.oscilloscope.set_marker_freq(1, freq)
        return int(self.oscilloscope.get_amp())

    ###########################
    # LO LEAKAGE OPTIMIZATION
    ###########################
    def _measure_lo_leakage(self, i_offset, q_offset):
        """
        Sets the DC offsets on the IQ device, then measures amplitude at LO freq.
        """
        self.iq_device.set_offsets(i_offset, q_offset)
        return self.oscilloscope.get_amp()

    def optimize_lo_leakage(self, x0=(0.0, 0.0)):
        """
        Minimizes the amplitude at the LO freq by varying the I/Q DC offsets.
        """
        # Scope marker/center freq at LO:
        lo_freq = self.parameters.f_lo
        self.oscilloscope.set_center_freq(lo_freq)
        if self.parameters.method == 2:
            self.oscilloscope.set_marker_freq(1, lo_freq)

        def fun_lo_leakage(x):
            return self._measure_lo_leakage(x[0], x[1])

        res = opti.minimize(
            fun_lo_leakage,
            x0,
            method="Nelder-Mead",
            options={
                "xatol": self.parameters.xatol,
                "fatol": self.parameters.fatol,
                "maxiter": self.parameters.maxiter,
            },
        )
        print(f"LO-Leakage Min: {res.fun:.3f} dBm at I={res.x[0]:.5f}, Q={res.x[1]:.5f}")
        return res.x, res.fun

    ###########################
    # IMAGE REJECTION OPTIMIZATION
    ###########################
    def _measure_image(self, g, phi):
        """
        Sets the (g, phi) imbalance on the IQ device, then reads amplitude
        at the image freq: (LO - IF).
        """
        self.iq_device.set_gain_phase(g, phi)
        return self.oscilloscope.get_amp()

    def optimize_image(self, x0=(0.0, 0.0)):
        """
        Minimizes amplitude at LO - IF by varying gain-phase imbalance (g, phi).
        """
        image_freq = self.parameters.f_lo - self.parameters.f_base
        self.oscilloscope.set_center_freq(image_freq)
        if self.parameters.method == 2:
            self.oscilloscope.set_marker_freq(1, image_freq)

        def fun_image(x):
            return self._measure_image(x[0], x[1])

        res = opti.minimize(
            fun_image,
            x0,
            method="Nelder-Mead",
            options={
                "xatol": self.parameters.xatol,
                "fatol": self.parameters.fatol,
                "maxiter": self.parameters.maxiter,
            },
        )
        print(f"Image Rejection Min: {res.fun:.3f} dBm at g={res.x[0]:.5f}, phi={res.x[1]:.5f}")
        return res.x, res.fun

    ###########################
    # WIDEBAND SWEEP
    ###########################
    def _perform_spectrum_sweep(self):
        """
        Runs a wideband sweep for debugging/visualization.
        """
        self.oscilloscope.set_bandwidth(self.parameters.sweepBW)
        self.oscilloscope.set_sweep_points(self.parameters.fullNumPoints)
        self.oscilloscope.set_center_freq(self.parameters.f_lo)
        self.oscilloscope.set_span(self.parameters.fullSpan)
        self.oscilloscope.get_single_trigger()
        freq_vec, amp = self.oscilloscope.get_full_trace()
        return freq_vec, amp

    def _plot_spectrum(self, freq_vec, amp_before, amp_after):
        plt.figure("Full Spectrum")
        plt.xlabel("Frequency (Hz)")
        plt.ylabel("Amplitude (dBm)")
        plt.plot(freq_vec, amp_before, alpha=0.5, label="Before")
        plt.plot(freq_vec, amp_after, alpha=0.5, label="After")
        plt.title(f"IQ-Calibration around LO={self.parameters.f_lo / 1e9:.3f} GHz")
        plt.legend()
        plt.show()

    def _measure_spurious_sum(self, params):
        """
        params is [I_offset, Q_offset, g, phi].
        Sets the corresponding parameters on the hardware,
        performs a wideband sweep, then returns the sum of spurious peaks
        above -75 dBm *in linear scale* (mW), or whichever metric you prefer.
        """
        (i_offset, q_offset, g, phi) = params

        # 1. Update offsets (in V)
        self.iq_device.set_offsets(i_offset, q_offset)

        # 2. Update gain/phase imbalance (and internally amplitude or phases).
        #    Depending on your device logic, you might pass in the amplitude or
        #    set them first.  For example, if your base amplitude is 0.3 V:
        base_amp = 0.3
        self.iq_device.set_gain_phase(g, phi, base_amplitude=base_amp)

        # 3. Perform wideband sweep
        self.oscilloscope.set_bandwidth(self.parameters.sweepBW)
        self.oscilloscope.set_sweep_points(self.parameters.fullNumPoints)
        self.oscilloscope.set_center_freq(self.parameters.f_lo)
        self.oscilloscope.set_span(self.parameters.fullSpan)
        self.oscilloscope.get_single_trigger()

        freq_vec, amp_dbm = self.oscilloscope.get_full_trace()  # amp in dBm

        # 4. Compute spurious metric
        spurs_mW = self.sum_peak_spurs(freq_vec, amp_dbm, threshold_dbm=-75.0, distance_in_MHz=0.5)
        # or use spurious_fraction(...) if you want a fraction

        return spurs_mW

    def sum_peak_spurs(self, freq_vec, amp_dbm, threshold_dbm=-75.0, distance_in_MHz=0.0, **find_peaks_kwargs):

        # 1) If user specified a minimum distance in MHz, convert it to # of samples.
        if distance_in_MHz > 0.0:
            # frequency spacing in Hz per sample (approx)
            df = (freq_vec[-1] - freq_vec[0]) / (len(freq_vec) - 1)

            # Convert distance_in_MHz to Hz, then to # of samples
            distance_in_samples = int(round((distance_in_MHz * 1e6) / df))

            # Put it into the find_peaks_kwargs dict
            # so it gets passed to find_peaks as 'distance'
            find_peaks_kwargs["distance"] = distance_in_samples

        # 2) find_peaks with desired threshold
        #    Setting 'height=threshold_dbm' ensures only peaks above threshold_dbm
        #    are considered.
        peaks, props = find_peaks(amp_dbm, height=threshold_dbm, **find_peaks_kwargs)

        # 3) Convert those peak amplitudes from dBm to mW
        peak_amp_dbm = props["peak_heights"]  # the amplitude of each peak in dBm
        peak_amp_mW = 10 ** (peak_amp_dbm / 10.0)

        # 4) Sum them
        total_peak_spur_mW = np.sum(peak_amp_mW)

        return total_peak_spur_mW

    def calibrate_spurs(self, x0=(0.0, 0.0, 0.0, 0.0)):
        """
        Minimizes the sum of spurious peaks across the entire
        wideband spectrum, above -75 dBm. The parameter vector is:
           x = [I_offset, Q_offset, g, phi].

        Also plots a 'Before' and 'After' spectrum comparison.
        """
        # Make sure instruments are connected, etc.
        if not (self.oscilloscope and self.iq_device):
            raise RuntimeError("Instruments not connected. Call connect_instruments() first.")

        # Possibly set up the scope
        # (Though you'll do a wide sweep for each measurement anyway)
        self._setup_oscilloscope_measurement()

        #
        # 1) Measure and store the initial 'Before' spectrum
        #
        freq_vec_before, amp_before_dbm = self._perform_spectrum_sweep()

        def fun_spurious_sum(x):
            return self._measure_spurious_sum(x)

        #
        # 2) Perform the minimization
        #
        res = opti.minimize(
            fun_spurious_sum,
            x0,
            method="Nelder-Mead",
            options={
                "xatol": 1e-4,
                "fatol": 3,
                "maxiter": 500,
            },
        )

        best_x = res.x
        print(f"Result of global spurious minimization: {res.fun:.5f} mW "
              f"at I_offset={best_x[0]:.4f} V, Q_offset={best_x[1]:.4f} V, "
              f"g={best_x[2]:.4f}, phi={best_x[3]:.4f} rad")

        #
        # 3) Measure the 'After' spectrum
        #
        freq_vec_after, amp_after_dbm = self._perform_spectrum_sweep()

        #
        # 4) Plot the comparison of Before vs After
        #
        self._plot_spectrum(freq_vec_before, amp_before_dbm, amp_after_dbm)

        return res

    ###########################
    # MAIN ROUTINE
    ###########################
    def calibrate(self):
        """
        Full calibration:
          - optional wideband sweep
          - LO leakage minimization
          - image rejection minimization
          - repeated N times
          - final wideband sweep
        """
        if not (self.oscilloscope and self.iq_device):
            raise RuntimeError("Instruments not connected. Call connect_instruments() first.")

        # Setup scope measurement
        self._setup_oscilloscope_measurement()
        # Possibly measure initial signal power
        init_power = self._get_signal_power()
        print(f"Initial Power at LO+IF: {init_power} dBm")

        if self.parameters.bDoSweeps:
            freq_vec, amp_before = self._perform_spectrum_sweep()

        # Repetitions
        best_lo_offsets = (0, 0)
        best_image_gphi = (0, 0)

        for i in range(self.parameters.optimization_repititions):
            print("\n====================================")
            print(f"Optimization Repetition {i+1}")
            print("====================================")

            # LO LEAKAGE
            lo_x, lo_fun = self.optimize_lo_leakage(x0=best_lo_offsets)
            best_lo_offsets = (lo_x[0], lo_x[1])

            # IMAGE REJECTION
            img_x, img_fun = self.optimize_image(x0=best_image_gphi)
            best_image_gphi = (img_x[0], img_x[1])

        if self.parameters.bDoSweeps:
            _, amp_after = self._perform_spectrum_sweep()
            self._plot_spectrum(freq_vec, amp_before, amp_after)

        print("\nFinal Results:")
        print(f"  LO leakage offsets = {best_lo_offsets}")
        print(f"  Image rejection (g,phi) = {best_image_gphi}")
        return best_lo_offsets, best_image_gphi


if __name__ == "__main__":
    # Example usage:
    # We can sweep different amplitude settings, or just do a single calibration.


    calibrator = IQMixerCalibrator(i_amp=0.3, q_amp=0.3)
    calibrator.connect_instruments()
    result = calibrator.calibrate_spurs()
    calibrator.disconnect_instruments()
    #
    print("Calibration done.")
    print(result)
    # print(f"Best LO offsets: {best_lo}")
    # print(f"Best (g, phi): {best_img}")
