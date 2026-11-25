"""Checks that the run of a claim produced real outputs.

Truncated outputs only make a result weaker, so they are disclosed. The same-as-base and
collapsed checks need 20 items, because a few outputs can match by chance.
"""
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

OUTPUTS_EMPTY = ProblemKind(code="outputs-empty", severity=Severity.SOUNDNESS)
OUTPUTS_SAME_AS_BASE = ProblemKind(code="outputs-same-as-base", severity=Severity.SOUNDNESS)
OUTPUTS_COLLAPSED = ProblemKind(code="outputs-collapsed", severity=Severity.SOUNDNESS)
OUTPUTS_TRUNCATED = ProblemKind(code="outputs-truncated", severity=Severity.STRENGTH)

MAX_EMPTY = 0.05
MAX_SAME_AS_BASE = 0.9
MIN_DISTINCT = 0.05
MAX_TRUNCATED = 0.05
MIN_ITEMS = 20          # fewer items than this can match or repeat by chance


class RunOutputsCheck(interface.ClaimCheck):
    """Flag outputs showing that the run, not the model, produced the result."""
    name = "run_outputs"
    title = "Run outputs are real"
    why = ("Flags empty, collapsed or truncated outputs, and outputs identical "
           "to the untrained model's.")
    problem_kinds = ((OUTPUTS_EMPTY, OUTPUTS_SAME_AS_BASE, OUTPUTS_COLLAPSED,
                     OUTPUTS_TRUNCATED))
    fields = read_only(run_record.FIELDS)
    needs = ("run",)
    input_scope = interface.CheckInputScope(claims=interface.OWN_CLAIM,
                                            other_files_read=run_record.files_named_in_run_record)

    def check(self, claim: inputs.Claim, recomputed: recompute.Matches | None,
              context: interface.ClaimContext) -> CheckResult:
        """Flag outputs showing that the run, not the model, produced the result."""
        try:
            run = run_record.load(claim.fields["run"])
            if run.outputs is None:
                return NotChecked(missing="outputs in the run record")
            outputs = run_record.load_outputs(run.outputs)
        except run_record.RunRecordError as e:
            return Skipped(because=f"the run's outputs cannot be read: {e}")
        if not outputs:
            return passed_or_flagged([Problem(kind=OUTPUTS_EMPTY, detail=(
                "the run's outputs file holds no items"))])
        texts = [o.output.strip() for o in outputs]
        n = len(texts)
        problems = []
        if (empty := sum(not t for t in texts) / n) > MAX_EMPTY:
            problems.append(Problem(kind=OUTPUTS_EMPTY,
                                    detail=f"{empty:.0%} of {n} outputs are empty"))
        pairs = [(t, o.base_output.strip()) for t, o in zip(texts, outputs, strict=True)
                 if o.base_output is not None]
        same = sum(t == b for t, b in pairs) / len(pairs) if pairs else 0.0
        if len(pairs) >= MIN_ITEMS and same > MAX_SAME_AS_BASE:
            problems.append(Problem(kind=OUTPUTS_SAME_AS_BASE, detail=(
                f"{same:.0%} of outputs equal the untrained model's, so the trained model may "
                "not be in use")))
        nonempty = [t for t in texts if t]
        distinct = len(set(nonempty))
        if len(nonempty) >= MIN_ITEMS and distinct / len(nonempty) < MIN_DISTINCT:
            problems.append(Problem(kind=OUTPUTS_COLLAPSED, detail=(
                f"only {distinct} distinct outputs across {len(nonempty)} items")))
        if (truncated := sum(o.truncated for o in outputs) / n) > MAX_TRUNCATED:
            problems.append(Problem(kind=OUTPUTS_TRUNCATED, detail=(
                f"{truncated:.0%} of {n} outputs hit the length limit")))
        return passed_or_flagged(problems)
