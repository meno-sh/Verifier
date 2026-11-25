#!/usr/bin/env python3
"""Assert that the schema parses claim registers strictly and computes the verdict as designed.

Covers:
  - the parser accepts the fixture's well-formed claims and rejects malformed ones
    with an error naming the claim;
  - relative paths are read from the register's folder, and an absolute path is refused;
  - the registry refuses a problem kind two checks claim, and a check reporting a
    kind it did not declare;
  - every trace reason has a description in the report;
  - the tolerance comes from the decimals the claim register wrote, unless `tol` is set;
  - two printed numbers match when the values that round to them overlap, at a scale of
    1, 100 or 1/100;
  - every declared ProblemKind has a fixed Severity;
  - a saved model reading becomes one result, and an unusable one is Skipped;
  - a provider call that gives no reading is Skipped with its reason, and a requested
    review that gave no reading is listed under missing inputs;
  - verdict() on hand-built trees, one tree per outcome.

Returns 1 if any case fails, so it can gate a commit.
"""
import io
import json
import operator
import os
import re
import sys
import tempfile
import types
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checks.model_review import model_review  # noqa: E402
from core.analysis import claim_checker, findings_json, verdict_policy  # noqa: E402
from core.methods import computational_methods
from core.registry import checks, extensions, plugin_checks, run_options
from core.report_model import triage_board, verdict_explainer  # noqa: E402
from schema import findings, inputs, provenance, recompute, verdict
from schema.claim import claim_links, claim_register_parser
from schema.primitives import (  # noqa: E402
    Flagged,
    NotChecked,
    Off,
    Passed,
    Problem,
    ProblemKind,
    Severity,
    Skipped,
    TraceReason,
)
from tests import result_classes  # noqa: E402
from utils import alerts, models  # noqa: E402

BUILT_IN_CHECKS = extensions.builtin_check_registry()   # the registry of the built-in checks

VERDICT_POLICY = verdict_policy.VerdictPolicy()

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "workdir", "claims.json")
SPEC = os.path.join(os.path.dirname(HERE), "docs", "SPEC.md")
# the computational methods the parser accepts: the fixture's method files, and the one
# the SPEC's example claim register names
SIG = {*computational_methods.MethodFolderLoader().registry_with_methods(
             BUILT_IN_CHECKS,
             Path(HERE) / "fixtures" / "workdir" / "methods").computational_methods,
       "accuracy_with_retrieval"}
FIELDS = BUILT_IN_CHECKS.claim_field_types()        # the claim keys the built-in claim checks read
PARSER = claim_register_parser.ClaimRegisterParser(SIG, FIELDS)
# every built-in problem kind, by code
KIND = {k.code: k for c in (*BUILT_IN_CHECKS.claim_checks.values(),
                            *BUILT_IN_CHECKS.paper_check_groups.values()) for k in c.problem_kinds}

FAILED = []


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(name)
        if detail:
            print(f"        {detail}")


def spec_register() -> dict:
    """The example claim register in SPEC.md: its first JSON block with a claims list."""
    blocks = re.findall(r"```json\n(.*?)```", Path(SPEC).read_text(), re.S)
    return next(json.loads(b) for b in blocks if '"claims"' in b)


def raises(raw: dict, fragment: str) -> tuple[bool, str]:
    """True if parsing `raw` raises a ClaimRegisterError whose message contains `fragment`."""
    try:
        PARSER.parse_claim_entry(raw, HERE)
    except inputs.ClaimRegisterError as e:
        return fragment in str(e), str(e)
    return False, "no error raised"


def test_parser() -> None:
    """Well-formed claims parse; a malformed entry is set aside, naming the claim and key."""
    raw = json.loads(Path(FIXTURE).read_text())
    register = PARSER.parse_json(raw, HERE)
    expect("fixture: the 13 well-formed claims parse", len(register.claims) == 13)
    expect("fixture: the 2 malformed entries are set aside, not fatal",
           [(u.entry, u.id) for u in register.unreadable]
           == [(8, "bad-reason"), (11, "unknown-method")],
           repr(register.unreadable))
    first = register.claims[0]
    expect("fixture: artifact source is typed",
           isinstance(first.source, inputs.ArtifactSource)
           and first.source.method == "trained_brier_post")
    expect("fixture: optional lists become tuples", first.fields["varies"] == ("training",))

    example = PARSER.parse_json(spec_register(), HERE)
    expect("the SPEC's example claim register parses with no unreadable entries",
           len(example.claims) == 3 and not example.unreadable, repr(example.unreadable))

    by_id = {c["id"]: c for c in raw["claims"]}
    cases = [
        ("unknown trace reason", by_id["bad-reason"], "'inconvenient' is not one of"),
        ("unknown computational method", by_id["unknown-method"], "'ms' is not a known"),
        ("the old recipe key", {"id": "c", "value": 0.1, "source": {
            "kind": "artifact", "recipe": "mean_post", "path": "x"}}, "recipe"),
        ("unknown key", {"id": "c", "value": 0.1, "seed": 3}, "seed"),
        ("missing value", {"id": "c", "source": {
            "kind": "artifact", "method": "mean_post", "path": "x"}}, "`value` is required"),
        ("value as a string", {"id": "c", "value": "0.1"}, "`value` must be int or float"),
        ("seeds as a boolean", {"id": "c", "value": 0.1, "seeds": True}, "`seeds` must be int"),
        ("a where filter, which belongs inside a method", {"id": "c", "value": 0.1, "source": {
            "kind": "artifact", "method": "mean_post", "path": "x",
            "where": {"condition": "x"}}}, "where"),
        ("unknown source kind", {"id": "c", "value": 0.1, "source": {"kind": "log", "path": "x"}},
         "'artifact', 'document' or 'claims'"),
        ("traced without anchor", {"id": "c", "value": 0.1, "source": {
            "kind": "document", "path": "x", "why": "parser-superseded"}}, "`must_contain`"),
        ("an infinite value", {"id": "c", "value": float("inf")}, "finite number"),
        ("a tol that is not a number", {"id": "c", "value": 0.1, "tol": float("nan")},
         "finite number"),
        ("a tol of zero or less", {"id": "c", "value": 0.1, "tol": -0.01}, "above zero"),
        ("a record field another method reads", {"id": "c", "value": 0.1, "source": {
            "kind": "artifact", "method": "mean_post", "path": "x", "post": "p"}}, "post"),
        ("an absolute artifact path", {"id": "c", "value": 0.1, "source": {
            "kind": "artifact", "method": "mean_post", "path": "/data/jobs/x.json"}},
         "absolute path"),
        ("a config given as a file, not inline",
         {"id": "c", "value": 0.1, "config": "runs/cfg.json"}, "`config` must be dict"),
        ("a method no method file defines", {"id": "c", "value": 0.1, "source": {
            "kind": "artifact", "method": "brier_post", "path": "x"}}, "not a known"),
    ]
    for name, claim, fragment in cases:
        ok, message = raises(claim, fragment)
        expect(f"rejects {name}", ok and f"claim {claim['id']!r}" in message, message)

    repeated = PARSER.parse_json(
        {"claims": [{"id": "a", "value": 1}, {"id": "a", "value": 2}]}, HERE)
    expect("a repeated id keeps the first entry and sets the second aside",
           [c.value for c in repeated.claims] == [1.0]
           and [(u.entry, u.id) for u in repeated.unreadable] == [(2, "a")])

    try:
        PARSER.parse_json({"claim": []}, HERE)
        expect("a file with no claims list stops the run", False, "no error raised")
    except inputs.ClaimRegisterError:
        expect("a file with no claims list stops the run", True)

    relative = PARSER.parse_claim_entry({"id": "c", "value": 0.1, "source": {
        "kind": "artifact", "method": "mean_post", "path": "x"}}, HERE)
    expect("a relative path is read from the base folder",
           relative.source.path == os.path.join(HERE, "x"), relative.source.path)

    frozen = PARSER.parse_claim_entry({"id": "c", "value": 0.1, "setup": {"model": "Qwen3-8B"},
                                       "config": {"judge": {"model": "x"}, "seeds": [0, 1]}},
                                      HERE)
    for name, change in [("setup", lambda: operator.setitem(frozen.setup, "model", "y")),
                         ("nested config", lambda: operator.setitem(
                             frozen.fields["config"]["judge"], "model", "y"))]:
        try:
            change()
            expect(f"{name} cannot be changed after parsing", False, "change went through")
        except TypeError:
            expect(f"{name} cannot be changed after parsing", True)


def test_claims_source() -> None:
    """A claim inferred from other claims parses, and every broken link sets it aside."""
    def register(*entries: dict) -> inputs.ClaimRegister:
        return PARSER.parse_json({"claims": list(entries)}, HERE)

    def leaf(cid: str, **more: object) -> dict:
        return {"id": cid, "value": 0.1, "statement": f"{cid} holds.", **more}

    def derived(cid: str, *deps: str, **more: object) -> dict:
        return {"id": cid, "statement": f"{cid} follows.",
                "source": {"kind": "claims", "claims": list(deps)}, **more}

    ok = register(leaf("a"), leaf("b"), derived("h", "a", "b"))
    h = ok.claims[-1]
    expect("a derived claim parses with no value",
           not ok.unreadable and isinstance(h.source, inputs.ClaimsSource)
           and h.source.claims == ("a", "b") and h.value is None, repr(ok))
    expect("a derived claim may still give a value",
           register(leaf("a"), derived("h", "a", value=0.5)).claims[-1].value == 0.5)
    expect("a claim with no source still needs no value",
           not register({"id": "q", "statement": "Words only."}).unreadable)

    def unreadable(reg: inputs.ClaimRegister) -> dict[str, str]:
        return {u.id: u.reason for u in reg.unreadable}

    cases = [
        ("a dependency that is not in the register", (derived("h", "nope"),), "h",
         "'nope', which is not a readable claim"),
        ("a dependency that is unreadable", ({"id": "bad", "value": "x"}, derived("h", "bad")),
         "h", "'bad', which is not a readable claim"),
        ("a dependency that is superseded",
         (leaf("old", superseded_by="new"), leaf("new"), derived("h", "old")), "h",
         "superseded by 'new'"),
        ("a dependency with no statement", ({"id": "a", "value": 0.1}, derived("h", "a")), "h",
         "gives no `statement`"),
        ("a derived claim with no statement",
         (leaf("a"), {"id": "h", "source": {"kind": "claims", "claims": ["a"]}}), "h",
         "`statement` is required"),
        ("an empty list", (derived("h"),), "h", "at least one claim"),
        ("a dependency named twice", (leaf("a"), derived("h", "a", "a")), "h", "more than once"),
        ("an empty statement", ({"id": "a", "value": 0.1, "statement": " "},), "a",
         "must not be empty"),
        ("a document source with no value", ({"id": "t", "source": {
            "kind": "document", "path": "x", "must_contain": "y",
            "why": "parser-superseded"}},), "t", "`value` is required"),
        ("an unknown key in a claims source",
         (leaf("a"), {"id": "h", "statement": "s", "source": {
             "kind": "claims", "claims": ["a"], "why": "x"}}), "h", "unknown key"),
    ]
    for name, entries, cid, fragment in cases:
        why = unreadable(register(*entries)).get(cid, "")
        expect(f"sets aside {name}", fragment in why, why or "not set aside")

    chain = register(leaf("a", superseded_by="b"), leaf("b"), derived("m", "b"),
                     derived("h", "m"), {"id": "x", "value": "bad"}, derived("y", "x"),
                     derived("z", "y"))
    expect("a claim resting on a set-aside claim is set aside too, however deep",
           set(unreadable(chain)) == {"x", "y", "z"} and
           [c.id for c in chain.claims] == ["a", "b", "m", "h"], repr(chain))

    looped = register(derived("p", "q"), derived("q", "p"), derived("r", "r"))
    expect("the parser leaves cycles to the acyclic_support check",
           not looped.unreadable and len(looped.claims) == 3, repr(looped.unreadable))

    in_order = claim_links.ClaimLinks.order_by_dependency(
        register(derived("top", "mid"), derived("mid", "a"), leaf("a"),
                 derived("p", "q"), derived("q", "p"), derived("after", "p")).claims)
    ordered, early = in_order.claims, in_order.ids_before_a_claim_they_rest_on
    expect("dependency order puts each claim after the claims it rests on, and breaks a cycle "
           "greedily at the first claim of the fewest unplaced claims",
           [c.id for c in ordered] == ["a", "mid", "top", "p", "q", "after"]
           and early == {"p"}, f"{[c.id for c in ordered]} {early}")
    tangled = claim_links.ClaimLinks.order_by_dependency(
        register(derived("u", "v", "w"), derived("v", "u"), derived("w", "v")).claims)
    expect("in a tangle, the claim with the fewest unplaced claims comes first",
           [c.id for c in tangled.claims] == ["v", "w", "u"]
           and tangled.ids_before_a_claim_they_rest_on == {"v"},
           f"{[c.id for c in tangled.claims]} {tangled.ids_before_a_claim_they_rest_on}")

    for bad in (float("nan"), True, "0.1"):
        try:
            Passed(measured=bad)
            expect(f"Passed refuses a measured value of {bad!r}", False, "no error raised")
        except ValueError:
            expect(f"Passed refuses a measured value of {bad!r}", True)


def test_registry() -> None:
    """Every kind has a severity; the registry refuses clashes and undeclared kinds."""
    expect("every TraceReason has a meaning for the reader",
           all(reason.meaning_for_reader for reason in TraceReason))
    broken = (*result_classes.broken_provenance_classes(),
              *result_classes.broken_recompute_classes())
    expect("every broken result has a code on the integrity axis of the board",
           all(c.board_code for c in broken), repr([c for c in broken if not c.board_code]))
    expect("every declared ProblemKind has a Severity",
           all(isinstance(k.severity, Severity) for k in KIND.values()))
    try:
        KIND["too-few-seeds"].severity = Severity.PRESENTATION  # type: ignore[misc]
        expect("a ProblemKind's severity cannot be changed", False, "change went through")
    except (AttributeError, TypeError):
        expect("a ProblemKind's severity cannot be changed", True)
    try:
        ProblemKind(code="no-severity", severity="strength")  # type: ignore[arg-type]
        expect("a ProblemKind without a real Severity is refused", False, "no error raised")
    except TypeError:
        expect("a ProblemKind without a real Severity is refused", True)

    reg = BUILT_IN_CHECKS.copy_for_one_run()
    try:
        reg.add_claim_check(claim_check_for_test(problem_kinds=(KIND["too-few-seeds"],)))
        expect("a check claiming another check's kind is refused", False, "no error raised")
    except run_options.RegistryError:
        expect("a check claiming another check's kind is refused", True)

    mine = ProblemKind(code="my-kind", severity=Severity.STRENGTH)

    def liar(self, claim, recomputed, context):
        return Flagged(problems=(Problem(kind=KIND["too-few-seeds"], detail="x"),))
    check = claim_check_for_test(name="liar", problem_kinds=(mine,), check=liar)
    reg.add_claim_check(check)
    c = inputs.Claim(id="c", value=0.1)
    gate = (provenance.NoSource(), Skipped(because="no source"))
    try:
        claim_checker.ClaimCheckRunner().run_check_on_claim(
            check, c, *gate, checks.ClaimContext(register={"c": c}), frozenset())
        expect("a built-in check reporting an undeclared kind stops the run", False, "ran")
    except run_options.RegistryError:
        expect("a built-in check reporting an undeclared kind stops the run", True)
    untrusted = claim_check_for_test(base=plugin_checks.PluginCheck, name="liar",
                                     problem_kinds=(mine,), check=liar)
    got = claim_checker.ClaimCheckRunner().run_check_on_claim(
        untrusted, c, *gate, checks.ClaimContext(register={"c": c}), frozenset())
    expect("a check written by the agent reporting an undeclared kind is Skipped",
           isinstance(got, Skipped) and "undeclared" in got.because, repr(got))
    try:
        checks.PaperCheckGroupResultBuilder((mine,)).results_from_problems(
            [Problem(kind=KIND["too-few-seeds"], detail="x")])
        expect("a paper check group reporting an undeclared kind stops the run", False, "ran")
    except run_options.RegistryError:
        expect("a paper check group reporting an undeclared kind stops the run", True)


def test_fields() -> None:
    """Claim keys belong to the checks that declare them; the schema knows none of them."""
    expect("the claim schema has no key of a built-in claim check",
           not set(FIELDS) & claim_register_parser.ClaimRegisterParser.CLAIM_KEYS
           and not any(hasattr(inputs.Claim(id="c", value=0.1), key) for key in FIELDS),
           repr(set(FIELDS) & claim_register_parser.ClaimRegisterParser.CLAIM_KEYS))
    entry = {"id": "c", "value": 0.1, "seeds": 3, "leak_ids": ["a", "b"]}
    try:
        claim_register_parser.ClaimRegisterParser(SIG, {}).parse_claim_entry(entry, HERE)
        expect("with no check registered, a check's key is an unknown key", False, "parsed")
    except inputs.ClaimRegisterError as e:
        expect("with no check registered, a check's key is an unknown key", "seeds" in str(e))
    mine = {**FIELDS, "leak_ids": inputs.FieldType.TEXT_LIST}
    parsed = claim_register_parser.ClaimRegisterParser(SIG, mine).parse_claim_entry(entry, HERE)
    expect("a key a new check declares is parsed with its declared type",
           dict(parsed.fields) == {"seeds": 3, "leak_ids": ("a", "b")}, repr(parsed.fields))
    try:
        claim_register_parser.ClaimRegisterParser(SIG, mine).parse_claim_entry(
            {**entry, "leak_ids": "a"}, HERE)
        ok, message = False, "no error raised"
    except inputs.ClaimRegisterError as e:
        ok, message = "`leak_ids`" in str(e), str(e)
    expect("a declared key of the wrong type makes the claim unreadable", ok, message)

    kind = ProblemKind(code="field-test", severity=Severity.STRENGTH)
    for name, attributes in [
            ("a key another check reads as a different type",
             {"fields": {"seeds": inputs.FieldType.TEXT}}),
            ("a key the gate already uses", {"fields": {"value": inputs.FieldType.NUMBER}}),
            ("a key named fields", {"fields": {"fields": inputs.FieldType.TEXT}}),
            ("a key whose type is not a FieldType", {"fields": {"n_rows": int}}),
            ("a needed key it does not declare", {"needs": ("seeds",)}),
            ("no problem kinds", {"problem_kinds": ()}),
            ("both a recomputed number and dependencies",
             {"needs_recompute": True, "needs_dependencies": True})]:
        try:
            BUILT_IN_CHECKS.copy_for_one_run().add_claim_check(
                claim_check_for_test(**{"problem_kinds": (kind,), **attributes}))
            expect(f"the registry refuses {name}", False, "no error raised")
        except run_options.RegistryError:
            expect(f"the registry refuses {name}", True)


def claim_check_for_test(base: type = checks.ClaimCheck,
                         **attributes: object) -> checks.ClaimCheck:
    """One claim check of a new subclass of `base`, with these class attributes.

    It is named "test_check", and passes every claim, unless the attributes say otherwise.
    """
    declared = {"name": "test_check", "title": "t", "why": "w",
                "check": lambda self, claim, recomputed, context: Passed(), **attributes}
    return type("ClaimCheckForTest", (base,), declared)()


def problem(kind: ProblemKind) -> Flagged:
    """A Flagged result holding one problem of the given kind."""
    return Flagged(problems=(Problem(kind=kind, detail="test"),))


def paper(unreadable_inputs: tuple[str, ...] = (), **results) -> findings.PaperFindings:
    """Paper findings where every built-in paper check passed, with some results replaced."""
    names = [checks.PaperCheckGroupResultBuilder.check_name_for_kind(k)
             for g in BUILT_IN_CHECKS.paper_check_groups.values()
             if g.name not in ("model_review", "setup_review") for k in g.problem_kinds] \
        + ["model_review", "setup_review"]
    return findings.PaperFindings(unreadable_inputs=unreadable_inputs, facts={},
                                  checks={**{n: Passed() for n in names}, **results})


GATE_FIELDS = ("claim", "provenance", "recompute")


def claim(**overrides) -> findings.ClaimFindings:
    """One claim that is intact, matches, and passes every built-in claim check, with some
    gate fields or check results replaced."""
    gate = {
        "claim": inputs.Claim(id="c", value=0.075),
        "provenance": provenance.Intact(path="runs/main/results.json"),
        "recompute": recompute.Matches(value=0.075, n=4, interval=None, tolerance=0.0005),
        **{k: v for k, v in overrides.items() if k in GATE_FIELDS},
    }
    results = {**{name: Passed() for name, check in BUILT_IN_CHECKS.claim_checks.items()
                  if check.applies_to_claim(gate["claim"])},
               **{k: v for k, v in overrides.items() if k not in GATE_FIELDS}}
    return findings.ClaimFindings(**gate, checks=results)


def tree(paper_findings: findings.PaperFindings | None = None,
         *claim_findings: findings.ClaimFindings,
         unreadable: tuple[inputs.UnreadableClaim, ...] = ()) -> findings.Findings:
    """A Findings tree around the given claim findings and one document's paper findings."""
    run_inputs = inputs.Inputs(paper_paths=("paper.tex",), paper_sha256="0" * 64,
                               claim_register_path=None, claim_register_sha256=None,
                               switched_off=frozenset(), tool_commit="test")
    return findings.Findings(inputs=run_inputs, papers={"paper.tex": paper_findings or paper()},
                             claims=claim_findings, unreadable_claims=unreadable,
                             must_run=BUILT_IN_CHECKS.checks_that_must_not_be_skipped(frozenset()))


def test_verdict() -> None:
    """One hand-built tree per verdict, worst condition winning when several hold."""
    V = verdict.Verdict
    K = types.SimpleNamespace(**{c.upper().replace("-", "_"): k for c, k in KIND.items()})
    cases = [
        ("clean paper and claim", tree(paper(), claim()), V.PASS),
        ("a claim check could not run",
         tree(paper(), claim(baseline=NotChecked(missing="baseline"))), V.PASS_WITH_CAVEATS),
        ("a claim with no source", tree(paper(), claim(provenance=provenance.NoSource(),
                                                       recompute=Skipped(because="no source"))),
         V.PASS_WITH_CAVEATS),
        ("a traced claim", tree(paper(), claim(
            provenance=provenance.Traced(path="logs/run.md", anchor="brier 0.257",
                                         reason=TraceReason.PARSER_SUPERSEDED),
            recompute=Skipped(because="traced on purpose"))), V.PASS),
        ("too few seeds", tree(paper(), claim(seeds=problem(K.TOO_FEW_SEEDS))),
         V.PASS_WITH_DISCLOSURE),
        ("a run whose adapter was not loaded, with a weak result too",
         tree(paper(), claim(seeds=problem(K.TOO_FEW_SEEDS),
                             run_integrity=problem(K.ADAPTER_NOT_LOADED))),
         V.BLOCK_UNTIL_FIXED),
        ("model review not asked for", tree(paper(model_review=Off()), claim()), V.PASS),
        ("model review asked for but not run, with a weak result too",
         tree(paper(model_review=Skipped(because="model review was not run: no key")),
              claim(seeds=problem(K.TOO_FEW_SEEDS))), V.MISSING_INPUTS),
        ("model review not run does not hide a broken claim",
         tree(paper(model_review=Skipped(because="model review was not run: no key")),
              claim(provenance=provenance.Missing(path="runs/gone/*.json"),
                    recompute=Skipped(because="provenance failed"))), V.REJECT),
        ("model review not run does not hide a presentation problem",
         tree(paper(model_review=Skipped(because="model review was not run: no key")),
              claim(caption=problem(K.CAPTION_NAMES_OTHER_MODEL))), V.BLOCK_UNTIL_FIXED),
        ("a consistency problem is a caveat",
         tree(paper(dangling_ref=problem(K.DANGLING_REF))), V.PASS_WITH_CAVEATS),
        ("caption names another model",
         tree(paper(), claim(caption=problem(K.CAPTION_NAMES_OTHER_MODEL))), V.BLOCK_UNTIL_FIXED),
        ("number in no table",
         tree(paper(number_not_in_any_table=problem(K.NUMBER_NOT_IN_ANY_TABLE))),
         V.PASS_WITH_CAVEATS),
        ("artifact missing, with a presentation problem too",
         tree(paper(dangling_ref=problem(K.DANGLING_REF)),
              claim(provenance=provenance.Missing(path="runs/gone/*.json"),
                    recompute=Skipped(because="provenance failed"))), V.REJECT),
        ("recompute off, so three claim checks were skipped",
         tree(paper(), claim(recompute=Off(),
                             **{name: Skipped(because="recompute off")
                                for name in ("baseline", "measurement", "interval")})),
         V.PASS_WITH_CAVEATS),
        ("one unreadable claim register entry",
         tree(paper(), claim(),
              unreadable=(inputs.UnreadableClaim(entry=2, id="x", reason="test"),)),
         V.MISSING_INPUTS),
        ("an unreadable claim register entry outranks a rejected claim",
         tree(paper(), claim(recompute=recompute.NotANumber(method="brier_post")),
              unreadable=(inputs.UnreadableClaim(entry=2, id="x", reason="test"),)),
         V.MISSING_INPUTS),
        ("number disagrees",
         tree(paper(), claim(recompute=recompute.Disagrees(paper_value=0.2, value=0.075, n=4,
                                                            tolerance=0.0005))), V.REJECT),
        ("unreadable input outranks a rejected claim",
         tree(paper(unreadable_inputs=("tables/main.tex",)),
              claim(recompute=recompute.NotANumber(method="brier_post"))),
         V.MISSING_INPUTS),
    ]
    for name, t, want in cases:
        got = VERDICT_POLICY.verdict_for(t)
        expect(f"verdict: {name} -> {want.value}", got is want, f"got {got.value}")


def test_report_model_review_not_run() -> None:
    """The board lists a requested review that gave no reading once, under missing inputs."""
    t = tree(paper(model_review=Skipped(because="model review was not run: no key")), claim())
    board = triage_board.TriageBoardBuilder().build_board(t)
    explainer = verdict_explainer.VerdictExplainer()
    expect("a review that gave no reading is on the missing inputs axis, and only there",
           [e.code for e in board.entries_on_axis("missing_inputs")] == ["model-review-not-run"]
           and not any("model_review" in e.detail for e in board.entries_on_axis("caveat")),
           repr(board))
    reason = explainer.explain_verdict(verdict.Verdict.MISSING_INPUTS, board, t)
    expect("the reason names the model review", "model review" in reason, reason)
    unresolved = triage_board.TriageBoardEntry(code="unresolved-input", subject="p.tex", detail="x")
    both = triage_board.TriageBoard(entries_by_axis={
        **board.entries_by_axis,
        "missing_inputs": (unresolved, *board.entries_on_axis("missing_inputs"))})
    reason = explainer.explain_verdict(verdict.Verdict.MISSING_INPUTS, both, t)
    expect("the reason names both causes when both hold",
           "files" in reason and "model review" in reason, reason)
    off = triage_board.TriageBoardBuilder().build_board(tree(paper(model_review=Off()), claim()))
    expect("a review that was not asked for is not on the board",
           not off.entries_on_axis("missing_inputs"), repr(off))


def test_types() -> None:
    """Results refuse impossible values, and the tree turns into tagged JSON."""
    try:
        Flagged(problems=())
        expect("Flagged refuses an empty problem list", False, "no error raised")
    except ValueError:
        expect("Flagged refuses an empty problem list", True)

    off = claim(recompute=Off())
    expect("a claim's checks never include recompute, even when it is Off",
           "recompute" not in off.checks
           and list(off.checks) == ["baseline", "seeds", "caption", "measurement", "interval",
                                    "comparability", "run_integrity", "run_outputs",
                                    "run_data"])

    t = tree(paper(), claim(seeds=problem(KIND["too-few-seeds"])))
    out = findings_json.FindingsJsonCodec().encode(t)
    json.dumps(out)
    c = out["claims"][0]
    expect("to_json tags sum types and leaves records untagged",
           c["provenance"]["type"] == "Intact" and c["checks"]["seeds"]["type"] == "Flagged"
           and c["checks"]["seeds"]["problems"][0]["kind"] == "too-few-seeds"
           and "type" not in c["claim"] and "type" not in out["papers"]["paper.tex"])


def test_tolerance() -> None:
    """The tolerance comes from the decimals the claim register wrote, unless `tol` is set."""
    for text, want in (("0.127", 3), ("0.50", 2), ("0.5", 1), ("1e-3", 3)):
        got = inputs.PrintedFloat.count_printed_decimals(inputs.PrintedFloat(text))
        expect(f"{text} was written with {want} decimals", got == want, f"got {got}")
    expect("an integer value has no decimals", inputs.PrintedFloat.count_printed_decimals(1) == 0)

    register = PARSER.parse_file(FIXTURE)
    good = next(c for c in register.claims if c.id == "good")
    expect("a loaded claim keeps its printed decimals",
           isinstance(good.value, inputs.PrintedFloat) and good.value.text == "0.075")
    expect("tolerance is one unit in the last printed decimal",
           good.recompute_tolerance() == 0.001)
    loose = inputs.Claim(id="c", value=inputs.PrintedFloat("0.127"), tol=0.01)
    expect("tol replaces the tolerance when set", loose.recompute_tolerance() == 0.01)


def test_printed_number_matching() -> None:
    """Two printed numbers match when the values that round to them overlap, at 3 scales."""
    printed = inputs.PrintedFloat
    expect("an interval is half a unit of the last digit, and 0.5 for an integer",
           printed("71.3").interval() == (71.25, 71.35) and printed("71").interval()
           == (70.5, 71.5), repr((printed("71.3").interval(), printed("71").interval())))
    for a, b, want in (("0.713", "71.3", True), ("0.7128", "71.3", True),
                       ("0.7136", "71.3", False), ("71.3", "0.713", True),
                       ("0.200", "0.201", False), ("-0.5", "-0.50", True),
                       ("-0.5", "0.5", False), ("71", "71.4", True), ("71", "71.6", False)):
        got = printed(a).could_be_same_value(printed(b))
        expect(f"{a} and {b} {'can' if want else 'cannot'} be the same value", got == want,
               repr(got))
    for a, b, want in (("0.7128", "71.3", True), ("0.713", "1", False), ("71.32", "1", False),
                       ("0.71", "71", True)):
        got = printed(a).could_be_same_value_at_similar_precision(printed(b))
        expect(f"{a} and {b} {'can' if want else 'cannot'} be the same value at a similar "
               "precision", got == want, repr(got))


def test_model_review_reply() -> None:
    """A saved model reading becomes one result, and an unusable one is Skipped, not a problem."""
    reading = ('{"findings": [{"code": "claim-exceeds-evidence", "where": "abstract", '
               '"quote": "q", "why": "w"}, {"code": "made-up", "why": "x"}]}')
    got = model_review.read_reply(reading)
    kinds = [p.kind for p in got.problems] if isinstance(got, Flagged) else []
    unusable = model_review.UNUSABLE_FINDING
    expect("a reading's findings become Problems, and an unknown code is kept so it blocks",
           kinds == [model_review.CLAIM_EXCEEDS_EVIDENCE, unusable]
           and unusable.severity is Severity.PRESENTATION, repr(got))
    mixed = model_review.read_reply(
        '{"findings": [{"code": "method-drift", "where": null, "why": "w"}, '
        '{"code": "term-drift", "quote": 5}, "loose text", {"why": "no code"}]}')
    kinds = [p.kind for p in mixed.problems] if isinstance(mixed, Flagged) else []
    expect("a null field counts as absent, and each unusable finding is kept next to the "
           "usable ones", kinds == [model_review.METHOD_DRIFT, unusable, unusable, unusable],
           repr(mixed))
    expect("claim-exceeds-evidence is a strength problem",
           model_review.CLAIM_EXCEEDS_EVIDENCE.severity is Severity.STRENGTH)
    expect("an empty reading passes",
           isinstance(model_review.read_reply('{"findings": []}'), Passed))
    for text in ("no JSON here", '{"findings": null}', "[1, 2]", "{}",
                 '{"error": "rate limited"}', '[{"code": "term-drift", "why": "x"}]'):
        expect(f"an unusable reading is Skipped: {text}",
               isinstance(model_review.read_reply(text), Skipped))


def test_model_review_provider_errors() -> None:
    """The model review calls its model through the shared call in utils/models.py.

    A provider error that a retry cannot fix is raised, and so is a second bad reply.
    A bad reply and then a good one gives the reading. The provider is a fake, so no
    model is called and nothing is paid for.
    """
    class Reply(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    def chat(content: str) -> bytes:
        return json.dumps({"choices": [{"finish_reason": "stop",
                                        "message": {"content": content}}],
                           "usage": {"prompt_tokens": 20, "completion_tokens": 5}}).encode()

    def provider(*answers: object) -> tuple[object, list[dict]]:
        sent: list[dict] = []
        left = list(answers)

        def fake(req, timeout):
            sent.append(json.loads(req.data))
            answer = left.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return Reply(answer)
        return fake, sent

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        log = alerts.configure(folder / "alerts.jsonl", "test", folder)
        out = folder / "reading.json"
        saved = (urllib.request.urlopen, os.environ.get(models.KEYS["openrouter.ai"]))
        os.environ[models.KEYS["openrouter.ai"]] = "test"
        try:
            urllib.request.urlopen, sent = provider(urllib.error.HTTPError(
                model_review.MODEL, 401, "Unauthorized", None, None))
            try:
                model_review.run("text", out_path=str(out))
                expect("HTTP 401 is raised", False, "no error raised")
            except models.ProviderError as e:
                expect("HTTP 401 is raised at once, with no retry, and no reading is written",
                       "401" in str(e) and len(sent) == 1 and not out.exists(), repr(e))

            urllib.request.urlopen, sent = provider(chat("no JSON here"),
                                                    chat('```json\n{"findings": []}\n```'))
            got = model_review.run("text", out_path=str(out))
            records = alerts.read(log.path)
            expect("a bad reply and then a good one gives the reading, from two calls",
                   isinstance(got, Passed) and len(sent) == 2
                   and out.read_text() == '```json\n{"findings": []}\n```', repr(got))
            expect("the second call says which rule the first reply broke",
                   "YOUR LAST REPLY BROKE A RULE" in sent[1]["messages"][-1]["content"]
                   and "not one JSON value" in sent[1]["messages"][-1]["content"])
            expect("the bad reply is a warn in the alert log, with the whole reply",
                   [(r["level"], r["key"]) for r in records] == [("warn", "models.bad_reply")]
                   and records[0]["details"]["reply"] == "no JSON here", repr(records))

            out.unlink()
            urllib.request.urlopen, sent = provider(chat("```json\n{}\n```"),
                                                    chat('Here: {"findings": []}'))
            try:
                model_review.run("text", out_path=str(out))
                expect("two bad replies are raised", False, "no error raised")
            except models.BadReply:
                expect("two bad replies are raised, and no reading is written",
                       len(sent) == 2 and not out.exists())
        finally:
            urllib.request.urlopen = saved[0]
            if saved[1] is None:
                os.environ.pop(models.KEYS["openrouter.ai"], None)
            else:
                os.environ[models.KEYS["openrouter.ai"]] = saved[1]


def main() -> int:
    """Run every group of cases and report the failures."""
    for group in (test_parser, test_claims_source, test_registry, test_fields, test_tolerance,
                  test_printed_number_matching,
                  test_model_review_reply, test_model_review_provider_errors,
                  test_report_model_review_not_run, test_verdict, test_types):
        print(f"{group.__name__}")
        group()
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll schema cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
