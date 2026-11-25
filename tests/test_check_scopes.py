#!/usr/bin/env python3
"""Check that each built-in claim check reads only what its input scope declares.

The cases check that:
  - each file a check opens is in the files of its scope, under an audit hook on `open`;
  - a check gets only the claims and the options of its scope, and a read of another
    claim raises KeyError;
  - only acyclic_support reads the whole register, so every other check can be cached.
Returns 1 if any case fails.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_run_checks  # noqa: E402

from core.analysis import claim_checker  # noqa: E402
from core.registry import checks, extensions  # noqa: E402
from schema import inputs, provenance, recompute  # noqa: E402
from schema.claim import claim_register_parser  # noqa: E402
from schema.primitives import CheckResult, Passed, Skipped  # noqa: E402

FAILED: list[str] = []
BUILT_IN_CHECKS = extensions.builtin_check_registry()
OPENED: list[str] = []              # the files opened while the hook records


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def record_open(event: str, args: tuple) -> None:
    """The audit hook: keep the path of each file that is opened."""
    if event == "open" and isinstance(args[0], str | Path):
        OPENED.append(os.path.realpath(args[0]))


def run_watched(name: str, claim: inputs.Claim, context: checks.ClaimContext,
                gate: tuple) -> tuple[CheckResult, set[str], set[str]]:
    """Run one check on one claim; give its result, the files it opened and those declared.

    Only the files in the folder of the claim count, so the imports of Python do not.
    """
    check = BUILT_IN_CHECKS.claim_checks[name]
    folder = os.path.realpath(tempfile.gettempdir())
    OPENED.clear()
    result = claim_checker.ClaimCheckRunner().run_check_on_claim(check, claim, *gate, context,
                                                                 frozenset())
    opened = {p for p in OPENED if p.startswith(folder)}
    scoped = check.input_scope.restricted_context(claim, context)
    declared = {os.path.realpath(p) for p in check.input_scope.files_read(claim, scoped.options)}
    return result, opened, declared


def parse(raw: dict, folder: str) -> inputs.Claim:
    """One claim entry, parsed as the claim register parser reads it."""
    parser = claim_register_parser.ClaimRegisterParser((), BUILT_IN_CHECKS.claim_field_types())
    return parser.parse_claim_entry(raw, folder)


def test_files(tmp: Path) -> None:
    """The run checks and claim_uncertainty open only the files of their scope."""
    folder = str(tmp)
    test_run_checks.clean_run(folder)
    claim = parse({"id": "c", "value": 0.1, "run": "run.json"}, folder)
    gate = (provenance.NoSource(), Skipped(because="not recomputed"))
    for name in ("run_integrity", "run_outputs", "run_data"):
        result, opened, declared = run_watched(
            name, claim, checks.ClaimContext(register={"c": claim}), gate)
        expect(f"files: {name} opens only the run record and the files it names",
               opened and opened <= declared, f"{sorted(opened - declared)} {type(result)}")
    readings = tmp / "readings.json"
    readings.write_text(json.dumps({"readings": {}}))
    premise = parse({"id": "p", "value": 0.1, "statement": "The premise holds."}, folder)
    derived = parse({"id": "d", "statement": "So the claim holds.",
                     "source": {"kind": "claims", "claims": ["p"]}}, folder)
    context = checks.ClaimContext(
        register={"p": premise, "d": derived},
        options={"want_claim_uncertainty": False, "claim_uncertainty_readings": str(readings)},
        full_paper_text="The paper: the premise holds, so the claim holds.")
    gate = (provenance.Derived(claims=("p",)), Skipped(because="a derived claim"))
    result, opened, declared = run_watched("claim_uncertainty", derived, context, gate)
    expect("files: claim_uncertainty opens its readings file, which its scope declares "
           "with the scenarios file beside it",
           opened == {os.path.realpath(readings)} and opened <= declared
           and len(declared) == 2, f"{opened} {declared} {result}")


class ReadsAnotherClaim(checks.ClaimCheck):
    """A check that declares only its own claim, but reads another one."""
    name = "reads_another_claim"
    title = "Reads another claim"
    why = "A test of the restricted context."
    problem_kinds = (BUILT_IN_CHECKS.claim_checks["seeds"].problem_kinds[0],)
    input_scope = checks.CheckInputScope(claims=checks.OWN_CLAIM, option_keys=("kept",))

    def check(self, claim: inputs.Claim, recomputed: recompute.Matches | None,
              context: checks.ClaimContext) -> CheckResult:
        """Read the options, then the other claim, which raises KeyError."""
        assert dict(context.options) == {"kept": 1}, dict(context.options)
        return Passed(measured=float(context.register["other"].value or 0))


def test_restricted_context(tmp: Path) -> None:
    """A check sees only the claims and options of its scope."""
    own = parse({"id": "own", "value": 0.1}, str(tmp))
    other = parse({"id": "other", "value": 0.2}, str(tmp))
    context = checks.ClaimContext(register={"own": own, "other": other},
                                  options={"kept": 1, "dropped": 2})
    gate = (provenance.NoSource(), Skipped(because="not recomputed"))
    try:
        claim_checker.ClaimCheckRunner().run_check_on_claim(ReadsAnotherClaim(), own, *gate,
                                                            context, frozenset())
        expect("scope: a read of a claim outside the scope raises KeyError", False,
               "nothing raised")
    except KeyError as e:
        expect("scope: a read of a claim outside the scope raises KeyError",
               e.args == ("other",), repr(e))
    named = checks.CheckInputScope(claims=checks.NAMED_CLAIM, named_by="compare_to")
    with_partner = parse({"id": "own", "value": 0.1, "compare_to": "other"}, str(tmp))
    expect("scope: a named claim is in the context of its scope",
           set(named.restricted_context(with_partner, context).register) == {"own", "other"})


def test_scopes_declared() -> None:
    """Only acyclic_support reads the whole register."""
    whole = sorted(name for name, check in BUILT_IN_CHECKS.claim_checks.items()
                   if not check.input_scope.can_be_cached)
    expect("scopes: only acyclic_support reads the whole register",
           whole == ["acyclic_support"], repr(whole))


def main() -> int:
    """Run every case, with the audit hook on."""
    sys.addaudithook(record_open)
    test_scopes_declared()
    for case in (test_files, test_restricted_context):
        print(case.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            case(Path(tmp))
    if FAILED:
        print(f"\n{len(FAILED)} case(s) failed: {FAILED}")
        return 1
    print("\nAll check scope cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
