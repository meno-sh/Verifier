"""Run integrity: is the run behind a claim the run it says it was?

Reads the record of the run the claim names with `run` (see run_record.py). It flags
a setting the code applied differently from the config file, a model other than the
declared one, and a declared adapter the run says it did not load, the failure that
motivated this check. Any of these means the number may come from a different run
than described, so it is a soundness problem. So is a record, or a file it names,
that is gone or cannot be read: the run's evidence is missing. A record that does
not say what was loaded, or gives no seed, only weakens the result, so it is
disclosed.
"""
import os

from checks import interface, run_record
from schema import inputs, recompute
from schema.primitives import (
    CheckResult,
    Problem,
    ProblemKind,
    Severity,
    passed_or_flagged,
    read_only,
)
from utils import model_names, run_configs

RUN_UNREADABLE = ProblemKind(code="run-unreadable", severity=Severity.SOUNDNESS)
CONFIG_NOT_APPLIED = ProblemKind(code="config-not-applied", severity=Severity.SOUNDNESS)
WRONG_MODEL_LOADED = ProblemKind(code="wrong-model-loaded", severity=Severity.SOUNDNESS)
ADAPTER_NOT_LOADED = ProblemKind(code="adapter-not-loaded", severity=Severity.SOUNDNESS)
LOAD_NOT_RECORDED = ProblemKind(code="load-not-recorded", severity=Severity.STRENGTH)
SEED_NOT_SET = ProblemKind(code="seed-not-set", severity=Severity.STRENGTH)

# Config keys naming the run itself, which the code may rewrite at load time.
RUN_IDENTITY = tuple(k for k in run_configs.DEFAULT_IGNORE if k not in ("seed", "seeds"))

# How each file the record names is read, to confirm it is there and well formed.
READERS = {"config": lambda p: run_record.load_json(p, dict),
           "applied_config": lambda p: run_record.load_json(p, dict),
           "train_ids": run_record.load_ids, "eval_ids": run_record.load_ids,
           "outputs": run_record.load_outputs}


class RunIntegrityCheck(interface.ClaimCheck):
    """Flag a run whose config, model or adapter is not what its record declares."""
    name = "run_integrity"
    title = "Run integrity"
    why = ("Is the run behind the claim the one described: its config applied, "
           "its declared model and adapter loaded?")
    problem_kinds = ((RUN_UNREADABLE, CONFIG_NOT_APPLIED, WRONG_MODEL_LOADED,
                     ADAPTER_NOT_LOADED, LOAD_NOT_RECORDED, SEED_NOT_SET))
    fields = read_only(run_record.FIELDS)
    needs = ("run",)
    input_scope = interface.CheckInputScope(claims=interface.OWN_CLAIM,
                                            other_files_read=run_record.files_named_in_run_record)

    def check(self, claim: inputs.Claim, recomputed: recompute.Matches | None,
              context: interface.ClaimContext) -> CheckResult:
        """Flag a run whose config, model or adapter is not what its record declares."""
        try:
            run = run_record.load(claim.fields["run"])
            for key, read in READERS.items():
                if getattr(run, key) is not None:
                    read(getattr(run, key))
            if run.adapter is not None and not os.path.exists(run.adapter):
                raise run_record.RunRecordError("the declared adapter is gone")
        except run_record.RunRecordError as e:
            return passed_or_flagged([Problem(kind=RUN_UNREADABLE, detail=str(e))])
        problems = _config_not_applied(run)
        unrecorded = []
        if run.model is not None:
            if run.loaded_model is None:
                unrecorded.append("which model it loaded")
            elif not model_names.same_model(run.model, run.loaded_model):
                problems.append(Problem(kind=WRONG_MODEL_LOADED, detail=(
                    f"the run declared model {run.model!r} but loaded {run.loaded_model!r}")))
        if run.adapter is not None:
            if run.adapter_loaded is None:
                unrecorded.append("whether it loaded its adapter")
            elif not run.adapter_loaded:
                problems.append(Problem(kind=ADAPTER_NOT_LOADED, detail=(
                    "the run declared an adapter but did not load it")))
        if unrecorded:
            problems.append(Problem(kind=LOAD_NOT_RECORDED, detail=(
                f"the run record does not say {' or '.join(unrecorded)}")))
        if run.seed is None:
            problems.append(Problem(kind=SEED_NOT_SET, detail=(
                "the run record gives no seed, so the run cannot be repeated exactly")))
        return passed_or_flagged(problems)


def _config_not_applied(run: run_record.RunRecord) -> list[Problem]:
    """One problem per setting of the config file the code applied differently.

    Only what the config file sets is compared, so defaults the code adds at load time
    are not differences. Neither are keys naming the run, such as an output folder the
    code stamps with the time; the seed is still compared.
    """
    if run.config is None or run.applied_config is None:
        return []
    declared = run_record.load_json(run.config, dict)
    applied = run_record.load_json(run.applied_config, dict)
    set_by_file = run_configs.flatten(declared).keys()
    return [Problem(kind=CONFIG_NOT_APPLIED, detail=(
        f"the run applied {key} = {got!r}, but its config file says {want!r}"))
        for key, got, want in run_configs.diff(applied, declared, ignore=RUN_IDENTITY)
        if key in set_by_file]
