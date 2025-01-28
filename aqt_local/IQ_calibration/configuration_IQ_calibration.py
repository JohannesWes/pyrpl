import numpy as np

class Parameters:
    def __init__(self):
        self.f_mod = 5.0e4
        self.f_dev = 200.00e3
        self.f_base = 1.0e6
        self.f_lo = 2.0e9

        # For measuring repeated times:
        self.optimization_repititions = 3

        # SCPI or VISA addresses:
        self.osci_address = "TCPIP0::10.203.129.15::inst0::INSTR"

        # If you want to do wide sweeps:
        self.bDoSweeps = True
        # 1 => channel power method, 2 => marker method
        self.method = 2

        # scope measurement parameter
        self.measNumPoints = 101

        # wideband sweep parameters
        self.sweepBW = 1e5
        self.fullNumPoints = 1201

        self.microwave_max_power = 13  # dBm

        # Minimization control
        self.xatol = 1e-4
        self.fatol = 3
        self.maxiter = 50

    @property
    def measBW(self):
        return self.f_lo * 2

    @property
    def fullSpan(self, scaling=16.1):
        return int(abs(self.f_base * scaling))

    @property
    def startFreq(self):
        return self.f_lo - self.fullSpan / 2

    @property
    def stopFreq(self):
        return self.f_lo + self.fullSpan / 2
