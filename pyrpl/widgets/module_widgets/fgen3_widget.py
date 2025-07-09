"""
GUI Widget for the 3-Frequency FM Generator (Fgen3) module.
"""

from .base_module_widget import ModuleWidget

class Fgen3Widget(ModuleWidget):
    """
    Widget for the Fgen3 module. Inherits basic functionality
    from ModuleWidget, which automatically populates controls
    based on the module's _gui_attributes list.
    """
    def __init__(self, *args, **kwargs):
        super(Fgen3Widget, self).__init__(*args, **kwargs)
        # Add any Fgen3-specific widget connections or customizations here
        # For now, none seem necessary, similar to AsgWidget needing
        # only the trigger_source connection.
        # If changing certain settings requires calling module.setup(),
        # connect the corresponding attribute_widget's value_changed signal here.
        # Example (if changing output routing needed setup):
        # self.attribute_widgets['output_to_dsp_enable'].value_changed.connect(self.module.setup)