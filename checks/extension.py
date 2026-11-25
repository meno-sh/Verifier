"""The built-in checks, as the extension that the core always loads first.

The core names this extension only by its name (core/registry/extensions.py BUILTIN_CHECKS), so
the core imports no check.
"""
from pathlib import Path

from checks import interface
from checks.baseline import baseline
from checks.caption import caption
from checks.comparability import comparability
from checks.consistency import consistency
from checks.interval import interval
from checks.measurement import measurement
from checks.model_review import model_review
from checks.run_data import run_data
from checks.run_integrity import run_integrity
from checks.run_outputs import run_outputs
from checks.science import science
from checks.seeds import seeds
from checks.setup_review import setup_review
from checks.support import support
from checks.uncertainty import quantifier, red_team, uncertainty


class BuiltinChecks(interface.Extension):
    """Adds every built-in check and its run options to a registry, in report order."""

    def extend(self, check_registry: interface.CheckRegistry, root: Path,
               state_dir: Path) -> None:
        """Add the paper check groups, then the claim checks, then the run options.

        The built-in checks read no project folder, so `root` and `state_dir` are not used.
        """
        for group in (consistency.ConsistencyChecks(), science.ScienceChecks(),
                      model_review.ModelReviewChecks(), setup_review.SetupReviewChecks()):
            check_registry.add_paper_check_group(group)
        for check in (baseline.BaselineCheck(), seeds.SeedsCheck(), caption.CaptionCheck(),
                      measurement.MeasurementCheck(), interval.IntervalCheck(),
                      comparability.ComparabilityCheck(), run_integrity.RunIntegrityCheck(),
                      run_outputs.RunOutputsCheck(), run_data.RunDataCheck(),
                      support.AcyclicSupportCheck(), uncertainty.ClaimUncertaintyCheck()):
            check_registry.add_claim_check(check)
        # the run options of the checks and of the parts of claim_uncertainty, and their rules
        for module in (uncertainty, red_team, quantifier, model_review, setup_review):
            check_registry.add_run_options_of_module(module)
