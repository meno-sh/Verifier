"""Everything a check uses from the core, in one place.

A check imports this module, its own folder, `checks/run_record.py`,
`checks/model_reading.py`, `schema/` and
`utils/`, and nothing else of the Verifier (tests/test_layers.py). A plug-in check in
`.verifier/checks/` imports `PluginCheck` from here. A method file in `.verifier/methods/`
imports `Computed` and `method` from here; core/methods/method_runner.py gives them in the process
that runs the method. checks/extension.py, the extension of the built-in checks, imports
`Extension` from here.
"""
from core.methods.computational_methods import Computed
from core.registry.checks import (
    NAMED_CLAIM,
    OWN_CLAIM,
    PREMISES,
    WHOLE_REGISTER,
    Check,
    CheckInputScope,
    CheckRegistry,
    ClaimCheck,
    ClaimContext,
    PaperCheckGroup,
    PaperCheckGroupResultBuilder,
    PaperContext,
    PaperOutcome,
)
from core.registry.extensions import Extension
from core.registry.plugin_checks import PluginCheck
from core.registry.run_options import OptionsError, RegistryError, RunOption, options_rule

__all__ = [
    "NAMED_CLAIM",
    "OWN_CLAIM",
    "PREMISES",
    "WHOLE_REGISTER",
    "Check",
    "CheckInputScope",
    "CheckRegistry",
    "ClaimCheck",
    "ClaimContext",
    "Computed",
    "Extension",
    "OptionsError",
    "PaperCheckGroup",
    "PaperCheckGroupResultBuilder",
    "PaperContext",
    "PaperOutcome",
    "PluginCheck",
    "RegistryError",
    "RunOption",
    "options_rule",
]
