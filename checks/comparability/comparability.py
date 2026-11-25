"""Checks that two compared runs differ only where the claim says.

The check compares the two run configs. The claim must declare each difference in `varies`.
There is no fixed list of fields, because unlike comparisons hide in many places: the judge
model, the harness version, a stale baseline, the data. A run without a config is NotChecked.
"""
from collections.abc import Iterable

from checks import interface
from schema import inputs, recompute
from schema.primitives import (
    CheckResult,
    NotChecked,
    Problem,
    ProblemKind,
    Severity,
    passed_or_flagged,
    read_only,
)
from utils import run_configs


def _load_config(claim: inputs.Claim) -> dict | None:
    """Return the claim's run config as a dict, or None if it gives none.

    A `commit` field is folded in under the key `_commit`, so a different code
    version is a difference.
    """
    cfg = claim.fields.get("config")
    if cfg is None:
        return None
    cfg = dict(cfg)
    if claim.fields.get("commit"):
        cfg["_commit"] = claim.fields["commit"]
    return cfg


def _declared(key: str, varies: Iterable[str]) -> bool:
    """True if a dotted key falls under one of the declared varying keys (exact or as a prefix)."""
    return any(key == v or key.startswith(v + ".") for v in varies)


# The problem kinds this check reports, each with its severity.
# the compared claim is not in the register
PARTNER_MISSING = ProblemKind(code="partner-missing", severity=Severity.STRENGTH)
# the comparison does not say what it varies
VARIES_NOT_DECLARED = ProblemKind(code="varies-not-declared", severity=Severity.STRENGTH)
# the two runs differ somewhere undeclared
UNDECLARED_DIFFERENCE = ProblemKind(code="undeclared-difference", severity=Severity.STRENGTH)
# the declared difference is not there
DECLARED_DIFFERENCE_ABSENT = ProblemKind(code="declared-difference-absent",
                                         severity=Severity.STRENGTH)


# The claim register keys that this check reads.
FIELDS = {"compare_to": inputs.FieldType.TEXT, "config": inputs.FieldType.OBJECT,
          "commit": inputs.FieldType.TEXT, "varies": inputs.FieldType.TEXT_LIST,
          "ignore": inputs.FieldType.TEXT_LIST}


class ComparabilityCheck(interface.ClaimCheck):
    """Flag undeclared differences, declared ones that are absent, and a missing partner."""
    name = "comparability"
    title = "Like-for-like comparison"
    why = ("Diffs the full configs of the two compared runs and flags any "
           "difference the comparison does not declare.")
    problem_kinds = ((PARTNER_MISSING, VARIES_NOT_DECLARED, UNDECLARED_DIFFERENCE,
                     DECLARED_DIFFERENCE_ABSENT))
    fields = read_only(FIELDS)
    needs = ("compare_to",)
    input_scope = interface.CheckInputScope(claims=interface.NAMED_CLAIM, named_by="compare_to")

    def check(self, claim: inputs.Claim, recomputed: recompute.Matches | None,
              context: interface.ClaimContext) -> CheckResult:
        """Flag undeclared differences, declared ones that are absent, and a missing partner.

        A run without a config is NotChecked, never a pass.
        """
        partner = claim.fields["compare_to"]
        other = context.register.get(partner)
        if other is None:
            return passed_or_flagged([Problem(kind=PARTNER_MISSING, detail=(
                f"compared against {partner!r}, "
                f"which is not a claim in this claim register"))])
        cfg_a, cfg_b = _load_config(claim), _load_config(other)
        missing = [c.id for c, cfg in ((claim, cfg_a), (other, cfg_b)) if cfg is None]
        if missing:
            return NotChecked(missing=f"a config for {' and '.join(missing)}")
        varies = claim.fields.get("varies", ())
        ignore = (*run_configs.DEFAULT_IGNORE, *claim.fields.get("ignore", ()))
        diffs = run_configs.diff(cfg_a, cfg_b, ignore=ignore)
        undeclared = [d for d in diffs if not _declared(d[0], varies)]
        problems = [Problem(kind=UNDECLARED_DIFFERENCE, detail=(
            f"compared against {other.id} but {key} also differs ({va!r} vs {vb!r})"))
            for key, va, vb in undeclared]
        if undeclared and not varies:
            problems.append(Problem(kind=VARIES_NOT_DECLARED, detail=(
                f"compared against {other.id} without declaring what the comparison "
                "varies (`varies`), so every difference above is unexplained")))
        for v in varies:
            if not any(_declared(d[0], [v]) for d in diffs):
                problems.append(Problem(kind=DECLARED_DIFFERENCE_ABSENT, detail=(
                    f"compared against {other.id} to vary {v!r}, "
                    "but the two configs do not differ there")))
        return passed_or_flagged(problems)
