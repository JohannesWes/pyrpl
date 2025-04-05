from ..attributes import IntRegister, SelectRegister, IORegister, BoolProperty
from ..modules import HardwareModule
from ..widgets.module_widgets.hk_widget import HkWidget
import numpy as np


class ExpansionDirection(BoolProperty):
    def set_value(self, obj, val):
        obj._set_expansion_direction(self.name.strip('_output'), val)

    def get_value(self, obj):
        return obj._get_expansion_direction(self.name.strip('_output'))


class HK(HardwareModule):
    _widget_class = HkWidget

    _setup_attributes = ["led", "pwm_direct_output"] + \
                        ['expansion_P' + str(i) for i in range(8)] + \
                        ['expansion_P' + str(i) + '_output' for i in range(8)] + \
                        ['expansion_N' + str(i) for i in range(8)] + \
                        ['expansion_N' + str(i) + '_output' for i in range(8)]
    _gui_attributes = _setup_attributes
    addr_base = 0x40000000
    # We need all attributes to be there when the interpreter is done reading the class (for metaclass to workout)
    # see http://stackoverflow.com/questions/2265402/adding-class-attributes-using-a-for-loop-in-python
    for i in range(8):
        locals()['expansion_P' + str(i)] = IORegister(0x20, 0x18, 0x10, bit=i,
                                                      outputmode=True,
                                                      doc="positive digital io")
        locals()['expansion_P' + str(i) + '_output'] = ExpansionDirection(
            doc="direction of the "
                "port")
        locals()['expansion_N' + str(i)] = IORegister(0x24, 0x1C, 0x14, bit=i,
                                                      outputmode=True,
                                                      doc="negative digital io")
        locals()['expansion_N' + str(i) + '_output'] = ExpansionDirection(
            doc="direction of the "
                "port")

    id = SelectRegister(0x0, doc="device ID", options={"prototype0": 0,
                                                       "release1": 1})
    digital_loop = IntRegister(0x0C, doc="enables digital loop")

    # Add PWM direct output flag
    pwm_direct_output = IntRegister(0x28, bits=1,
                                    doc="When 1, routes PWM signals directly to expansion pins 0-3",
                                    min=0, max=1, default=0)

    led = IntRegister(0x30, doc="LED control with bits 1:8", min=0, max=2 ** 8)

    # another option: access led as array of bools
    # led = [BoolRegister(0x30,bit=i,doc="LED "+str(i)) for i in range(8)]

    def set_expansion_direction(self, index, val):
        """Sets the output mode of expansion index (both for P and N expansions)"""
        if not index in range(8):
            raise ValueError("Index from 0 to 7 expected")
        for name in ["expansion_P", "expansion_N"]:
            getattr(HK, name + str(index)).direction(self, val)

    def _setup(self):  # the function is here for its docstring to be used by the metaclass.
        """
        Sets the HouseKeeping module of the redpitaya up. (just setting the attributes is OK)
        """
        pass

    def _set_expansion_direction(self, name, val):
        """Sets the output mode of expansion index (both for P and N expansions)"""
        # if not index in range(8):
        #    raise ValueError("Index from 0 to 7 expected")
        # for name in ["expansion_P", "expansion_N"]:
        getattr(HK, name).direction(self, val)

    def _get_expansion_direction(self, name):
        """Sets the output mode of expansion index (both for P and N expansions)"""
        # if not index in range(8):
        # raise ValueError("Index from 0 to 7 expected")
        return getattr(HK, name).outputmode  # direction(self,
        # val)

    def enable_pwm_direct_output(self, enable=True):
        """
        Enable or disable direct routing of PWM signals to expansion pins 0-3.

        When enabled, PWM signals will be routed directly to expansion pins 0-3,
        overriding any values set via the expansion_P0 through expansion_P3 registers.

        Parameters:
        -----------
        enable : bool
            True to route PWM signals to pins, False to use normal expansion control

        Returns:
        --------
        int
            Current state of pwm_direct_output (1 if enabled, 0 if disabled)
        """
        self.pwm_direct_output = 1 if enable else 0
        return self.pwm_direct_output