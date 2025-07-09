from ..attributes import IntRegister, SelectRegister, IORegister, BoolProperty
from ..modules import HardwareModule
from ..widgets.module_widgets.hk_widget import HkWidget


class ExpansionDirection(BoolProperty):
    """Property for controlling expansion pin direction"""

    def set_value(self, obj, val):
        obj._set_expansion_direction(self.name.strip('_output'), val)

    def get_value(self, obj):
        return obj._get_expansion_direction(self.name.strip('_output'))


class SourceSelect(BoolProperty):
    """Property for controlling pin source (system bus vs external module input)"""

    def set_value(self, obj, val):
        obj._set_source_select(self.name.strip('_src'), val)

    def get_value(self, obj):
        return obj._get_source_select(self.name.strip('_src'))


class HK(HardwareModule):
    """
    HouseKeeping module for RedPitaya

    Controls digital I/O expansion connectors, LEDs, and provides system identification.
    Each expansion pin can be controlled either by the system bus (Python) or by
    other FPGA modules, selectable on a per-pin basis.
    """
    _widget_class = HkWidget

    _setup_attributes = ["led"] + \
                        ['expansion_P' + str(i) for i in range(8)] + \
                        ['expansion_P' + str(i) + '_output' for i in range(8)] + \
                        ['expansion_P' + str(i) + '_src' for i in range(8)] + \
                        ['expansion_N' + str(i) for i in range(8)] + \
                        ['expansion_N' + str(i) + '_output' for i in range(8)] + \
                        ['expansion_N' + str(i) + '_src' for i in range(8)]

    _gui_attributes = _setup_attributes
    addr_base = 0x40000000

    # System identification
    id = SelectRegister(0x0, doc="Device ID")
    dna_low = IntRegister(0x04, doc="Device DNA (low 32 bits)")
    dna_high = IntRegister(0x08, doc="Device DNA (high 25 bits)", bits=25)

    # Configuration
    digital_loop = IntRegister(0x0C, doc="Enables digital loop", bits=1, min=0, max=1)

    # LED control
    led = IntRegister(0x30, doc="LED control with bits 0:7", min=0, max=2 ** 8 - 1)

    # Expansion connector registers - dynamically created
    # Create P and N expansion pin registers
    for i in range(8):
        locals()['expansion_P' + str(i)] = IORegister(0x20, 0x18, 0x10, bit=i,
                                                      outputmode=True,
                                                      doc=f"Positive expansion pin {i}")
        locals()['expansion_P' + str(i) + '_output'] = ExpansionDirection(
            doc=f"Direction of positive expansion pin {i} (True=output, False=input)")
        locals()['expansion_P' + str(i) + '_src'] = SourceSelect(
            doc=f"Source select for positive expansion pin {i} (True=module, False=system)")

        # N expansion
        locals()['expansion_N' + str(i)] = IORegister(0x24, 0x1C, 0x14, bit=i,
                                                      outputmode=True,
                                                      doc=f"Negative expansion pin {i}")
        locals()['expansion_N' + str(i) + '_output'] = ExpansionDirection(
            doc=f"Direction of negative expansion pin {i} (True=output, False=input)"
        )
        locals()['expansion_N' + str(i) + '_src'] = SourceSelect(
            doc=f"Source select for negative expansion pin {i} (True=module, False=system)"
        )

    # Source selection registers (full byte access)
    _p_source_select = IntRegister(0x40, doc="P expansion source select (bit per pin)", min=0, max=255)
    _n_source_select = IntRegister(0x44, doc="N expansion source select (bit per pin)", min=0, max=255)

    # Current output values (read-only, shows values after mux)
    _p_current_output = IntRegister(0x48, doc="Current P expansion output values")
    _n_current_output = IntRegister(0x4C, doc="Current N expansion output values")

    def _setup(self): # the function is here for its docstring to be used by the metaclass.
        """
        Sets the HouseKeeping module of the redpitaya up. (just setting the attributes is OK)
        """
        pass

    def _set_expansion_direction(self, name, val):
        """Set the direction for an expansion pin"""
        getattr(HK, name).direction(self, val)

    def _get_expansion_direction(self, name):
        """Get the direction for an expansion pin"""
        # Get the IORegister instance
        io_register = getattr(self.__class__, name)
        # Return the current outputmode setting
        return io_register.outputmode

    def _set_source_select(self, name, val):
        """Set source selection for a single pin"""
        # Extract pin info from name (e.g., "expansion_P3" -> type="P", index=3)
        parts = name.split('_')
        exp_type = parts[1][0]  # 'P' or 'N'
        index = int(parts[1][1:])  # pin number

        # Get current register value
        if exp_type == 'P':
            current = self._p_source_select
        else:
            current = self._n_source_select

        # Set or clear the bit
        if val:
            new_value = current | (1 << index)
        else:
            new_value = current & ~(1 << index)

        # Write back
        if exp_type == 'P':
            self._p_source_select = new_value
        elif exp_type == 'N':
            self._n_source_select = new_value
        else:
            raise ValueError(f"Invalid pin type in name: {name}. Expected 'P' or 'N'.")

    def _get_source_select(self, name):
        """Get source selection for a single pin"""
        # Extract pin info from name
        parts = name.split('_')
        exp_type = parts[1][0]  # 'P' or 'N'
        index = int(parts[1][1:])  # pin number

        # Read register and extract bit
        if exp_type == 'P':
            return bool((self._p_source_select >> index) & 1)
        elif exp_type == 'N':
            return bool((self._n_source_select >> index) & 1)
        else :
            raise ValueError(f"Invalid pin type in name: {name}. Expected 'P' or 'N'.")


    def configure_pin(self, pin_name, direction='output', source='system'):
        """
        Configure a single pin's direction and source.

        Parameters
        ----------
        pin_name : str
            Pin name (e.g., 'P0', 'N7', 'expansion_P0')
        direction : str
            'input' or 'output'
        source : str
            'system' or 'module'

        Example
        -------
        >>> hk.configure_pin('P0', direction='output', source='module')
        >>> hk.configure_pin('N3', direction='input')
        """
        # Normalize pin name
        if not pin_name.startswith('expansion_'):
            if pin_name[0] in ['P', 'N']:
                pin_name = f'expansion_{pin_name}'
            else:
                raise ValueError(f"Invalid pin name: {pin_name}")

        # Set direction
        dir_attr = pin_name + '_output'
        if hasattr(self, dir_attr):
            setattr(self, dir_attr, direction == 'output')

        # Set source
        src_attr = pin_name + '_src'
        if hasattr(self, src_attr):
            setattr(self, src_attr, source == 'module')