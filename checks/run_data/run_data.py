"""Checks that the run of a claim used clean and complete evaluation data.

Unscored items make the sample smaller, and missing id files stop the overlap check.
Both are disclosed.
"""
from collections import Counter

from checks import interface, run_record
from schema import inputs, recompute
from schema.primitives import (
    CheckResult,
    NotChecked,
    Problem,
    ProblemKind,
    Severity,
    Skipped,
    passed_or_flagged,
    read_only,
)

TRAIN_EVAL_OVERLAP = ProblemKind(code="train-eval-overlap", severity=Severity.SOUNDNESS)
ITEMS_OUTSIDE_EVAL = ProblemKind(code="items-outside-eval", severity=Severity.SOUNDNESS)
ITEMS_DUPLICATED = ProblemKind(code="items-duplicated", severity=Severity.SOUNDNESS)
ITEMS_NOT_SCORED = ProblemKind(code="items-not-scored", severity=Severity.STRENGTH)
IDS_NOT_RECORDED = ProblemKind(code="ids-not-recorded", severity=Severity.STRENGTH)

EXAMPLES = 3        # item ids quoted in a problem


class RunDataCheck(interface.ClaimCheck):
    """Flag a run evaluated on leaked, repeated or incomplete items."""
    name = "run_data"
    title = "Run data is clean"
    why = ("Flags training items in the evaluation, and evaluation items scored "
           "twice or never.")
    problem_kinds = ((TRAIN_EVAL_OVERLAP, ITEMS_OUTSIDE_EVAL, ITEMS_DUPLICATED,
                     ITEMS_NOT_SCORED, IDS_NOT_RECORDED))
    fields = read_only(run_record.FIELDS)
    needs = ("run",)
    input_scope = interface.CheckInputScope(claims=interface.OWN_CLAIM,
                                            other_files_read=run_record.files_named_in_run_record)

    def check(self, claim: inputs.Claim, recomputed: recompute.Matches | None,
              context: interface.ClaimContext) -> CheckResult:
        """Flag a run evaluated on leaked, repeated or incomplete items."""
        try:
            run = run_record.load(claim.fields["run"])
            if run.eval_ids is None and run.outputs is None:
                return NotChecked(missing="eval_ids or outputs in the run record")
            train = set(run_record.load_ids(run.train_ids)) if run.train_ids else None
            test = set(run_record.load_ids(run.eval_ids)) if run.eval_ids else None
            scored = ([o.id for o in run_record.load_outputs(run.outputs)] if run.outputs
                      else None)
        except run_record.RunRecordError as e:
            return Skipped(because=f"the run's data cannot be read: {e}")
        problems = []
        if train is None or test is None:
            problems.append(Problem(kind=IDS_NOT_RECORDED, detail=(
                "the run record does not name both training and evaluation ids, so their "
                "overlap was not checked")))
        elif overlap := sorted(train & test):
            problems.append(_problem(TRAIN_EVAL_OVERLAP, overlap,
                                     "items are in both training and evaluation"))
        if scored is not None:
            if repeated := sorted(i for i, count in Counter(scored).items() if count > 1):
                problems.append(_problem(ITEMS_DUPLICATED, repeated,
                                         "items were scored more than once"))
            if test is not None and (outside := sorted(set(scored) - test)):
                problems.append(_problem(ITEMS_OUTSIDE_EVAL, outside,
                                         "scored items are not in the evaluation set"))
            if test is not None and (unscored := sorted(test - set(scored))):
                problems.append(_problem(ITEMS_NOT_SCORED, unscored,
                                         "evaluation items were never scored"))
        return passed_or_flagged(problems)


def _problem(kind: ProblemKind, ids: list[str], what: str) -> Problem:
    """A problem naming how many items and a few of their ids."""
    return Problem(kind=kind, detail=f"{len(ids)} {what}, e.g. {', '.join(ids[:EXAMPLES])}")
