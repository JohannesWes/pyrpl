import numpy as np
import pyvisa as visa
from RsInstrument import *
from abc import ABC, abstractmethod
from time import sleep

class VisaRS(ABC):
    """
    Base class for controlling an R&S device via VISA.
    """

    def __init__(self, address='TCPIP0::192.168.88.12::INSTR'):
        super().__init__()
        self.resource = address
        self.sa = RsInstrument(self.resource, True, True, "SelectVisa='rs'")
        sleep(1)
        print('\n', f'VISA Manufacturer: {self.sa.visa_manufacturer}', '\n')
        self.sa.visa_timeout = 5000
        self.sa.opc_timeout = 5000
        self.sa.instrument_status_checking = True
        self.sa.clear_status()
        idnResponse = self.sa.query_str('*IDN?')
        print('Hello, I am ' + idnResponse + '\n')
        # Instrument preset or reset
        self.sa.write_str_with_opc('*RST')
        self.sa.write_str_with_opc('SYST:DISP:UPD ON')
        # Example setup for RTO scope:
        self.sa.write_str_with_opc('CALC:MATH1 "FFTmag(Ch1)"')
        self.sa.write_str_with_opc('CALC:MATH1:STATE ON')
        self.sa.write_str_with_opc('TIM:SCAL 5E-9')
        self.sa.write_str_with_opc('CHAN1:COUP DC')

    def __del__(self):
        self.sa.clear_status()
        self.sa.close()

    @abstractmethod
    def get_amp(self):
        """Return the amplitude measurement (dBm) from the instrument."""
        pass

    @abstractmethod
    def set_automatic_video_bandwidth(self, state: int):
        pass

    @abstractmethod
    def set_automatic_bandwidth(self, state: int):
        pass

    @abstractmethod
    def set_bandwidth(self, bw: int):
        pass

    @abstractmethod
    def set_sweep_points(self, n_points: int):
        pass

    @abstractmethod
    def set_center_freq(self, freq: int):
        pass

    @abstractmethod
    def set_span(self, span: int):
        pass

    @abstractmethod
    def set_cont_off(self):
        pass

    @abstractmethod
    def set_cont_on(self):
        pass

    @abstractmethod
    def get_single_trigger(self):
        pass

    @abstractmethod
    def active_marker(self, marker: int):
        pass

    @abstractmethod
    def set_marker_freq(self, marker: int, freq: int):
        pass

    @abstractmethod
    def query_marker(self, marker: int):
        pass

    @abstractmethod
    def get_full_trace(self):
        pass

    @abstractmethod
    def enable_measurement(self):
        pass

    @abstractmethod
    def disables_measurement(self):
        pass

    @abstractmethod
    def sets_measurement_integration_bw(self, ibw: int):
        pass

    @abstractmethod
    def disables_measurement_averaging(self):
        pass

    @abstractmethod
    def get_measurement_data(self):
        pass


class RhodeSchwarzRTO6(VisaRS):
    """
    Derived class specialized for R&S RTO6 scope.
    """

    def get_amp(self):
        """
        Acquire a single trigger, then read out amplitude
        either from channel power or from a marker measurement.
        """
        self.get_single_trigger()
        if self.method == 1:  # Channel power
            sig = self.get_measurement_data()
        elif self.method == 2:  # Marker
            sig = self.query_marker(1)
        else:
            sig = float("NaN")
        return sig

    def set_automatic_video_bandwidth(self, state: int):
        pass

    def set_automatic_bandwidth(self, state: int):
        pass

    def set_bandwidth(self, bw: int):
        self.sa.write_str_with_opc(f'CALC:MATH1:FFT:BAND {int(bw)}')

    def set_sweep_points(self, n_points: int):
        pass

    def set_center_freq(self, freq: int):
        self.sa.write_str_with_opc(f"CALC:MATH1:FFT:CFR {int(freq)}")

    def set_span(self, span: int):
        self.sa.write_str_with_opc(f"CALC:MATH1:FFT:SPAN {int(span)}")

    def set_cont_off(self):
        return self.sa.write_str_with_opc("STOP")

    def set_cont_on(self):
        return self.sa.write_str_with_opc("RUN")

    def get_single_trigger(self):
        return self.sa.write_str_with_opc('SING')

    def active_marker(self, marker: int):
        self.sa.write_str_with_opc(f"CURS{int(marker)}:STAT ON")
        self.sa.write_str_with_opc(f"CURS{int(marker)}:SOUR M1")
        self.sa.write_str_with_opc(f"CURS{int(marker)}:TRAC ON")

    def set_marker_freq(self, marker: int, freq: int):
        self.get_single_trigger()
        self.sa.write(f"CURS{int(marker)}:X1P {int(freq)}")

    def query_marker(self, marker: int):
        return float(self.sa.query_float(f"CURS{int(marker)}:Y1P?"))

    def get_full_trace(self):
        amp = self.sa.query_bin_or_ascii_float_list('FORM REAL,32;CALC:MATH1:DATA?')
        btrace = self.sa.query_bin_or_ascii_float_list('CALC:MATH1:DATA:HEAD?')
        f_vec = np.linspace(btrace[0], btrace[1], int(btrace[2]))
        return f_vec, amp

    def enable_measurement(self):
        print('NOT ENABLING ANYTHING (override if needed)')

    def disables_measurement(self):
        print('NOT DISABLING ANYTHING (override if needed)')

    def sets_measurement_integration_bw(self, ibw: int):
        print('NOT SETTING MEASUREMENT INTEGRATION BW')

    def disables_measurement_averaging(self):
        pass

    def get_measurement_data(self):
        print('NOT GETTING MEASUREMENT DATA')
        return None
