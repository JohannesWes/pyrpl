"""
GUI Widget for the scan module
"""

from .base_module_widget import ModuleWidget

class ScanWidget(ModuleWidget):
    """
    Widget for the Fgen3 module. Inherits basic functionality
    from ModuleWidget, which automatically populates controls
    based on the module's _gui_attributes list.
    """
    def __init__(self, *args, **kwargs):
        super(ScanWidget, self).__init__(*args, **kwargs)
        # Add any Scan widget connections or customizations here
        # For now, none seem necessary, similar to AsgWidget needing
        # only the trigger_source connection.
        # If changing certain settings requires calling module.setup(),
        # connect the corresponding attribute_widget's value_changed signal here.
        # Example (if changing output routing needed setup):
        # self.attribute_widgets['output_to_dsp_enable'].value_changed.connect(self.module.setup)