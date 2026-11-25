"""Caption: does the caption name the model the run actually used?

A caption naming a different model from the run's setup misdescribes the result,
which is a presentation problem. Reads `caption_model` and `model` from the claim.
"""
from checks import interface
from schema import inputs, recompute
from schema.primitives import (
    CheckResult,
    Problem,
    ProblemKind,
    Severity,
    passed_or_flagged,
    read_only,
)
from utils import model_names

# The problem kinds this check reports, each with its severity.
# the caption names another model than the run used
CAPTION_NAMES_OTHER_MODEL = ProblemKind(code="caption-names-other-model",
                                        severity=Severity.PRESENTATION)


class CaptionCheck(interface.ClaimCheck):
    """Flag a caption model that differs from the model the run used."""
    name = "caption"
    title = "Caption matches the run"
    why = ("Flags a caption naming a different model from the one the run "
           "actually used.")
    problem_kinds = (CAPTION_NAMES_OTHER_MODEL,)
    fields = (read_only({"model": inputs.FieldType.TEXT,
              "caption_model": inputs.FieldType.TEXT}))
    needs = ("model",)
    input_scope = interface.CheckInputScope(claims=interface.OWN_CLAIM)

    def check(self, claim: inputs.Claim, recomputed: recompute.Matches | None,
              context: interface.ClaimContext) -> CheckResult:
        """Flag a caption model that differs from the model the run used."""
        model, named = claim.fields["model"], claim.fields.get("caption_model")
        problems = []
        if named and not model_names.same_model(named, model):
            problems.append(Problem(kind=CAPTION_NAMES_OTHER_MODEL, detail=(
                f"caption says model {named!r}, run setup says {model!r}")))
        return passed_or_flagged(problems)
