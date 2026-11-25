#!/usr/bin/env python3
"""Assert that the comparability check fires, stays quiet, and gives caveats when it should.

The cases come from real unlike comparisons in the verifier investigation
(a judge-read Brier row next to stated-P rows, a different instructor model, a stale
baseline commit), and from the ways a fixed list of compared fields goes wrong
(missing fields passing silently, a dangling partner passing silently, cosmetic
model-name differences being flagged). Returns 1 if any case fails, so it can gate a commit.
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.analysis import claim_checker  # noqa: E402
from core.registry import checks, extensions  # noqa: E402
from schema import provenance  # noqa: E402
from schema.claim import claim_register_parser  # noqa: E402
from schema.primitives import Flagged, NotChecked, Skipped  # noqa: E402

BUILT_IN_CHECKS = extensions.builtin_check_registry()   # the registry of the built-in checks

GATE = (provenance.NoSource(),
        Skipped(because="not recomputed: the claim register gives no source"))

BASE = {"model": "Qwen/Qwen3-32B", "judge": {"model": "claude-haiku-4.5"},
        "instructor": {"model": "llama-3.1-70b"}, "readout": "stated_p",
        "questions": {"set": "q50", "language": "en"}, "training": "none",
        "seeds": [0, 1, 2], "output_dir": "/runs/a"}


def claim(cid, cfg=None, **kw):
    """Build a claim with an inline config, through the claim register parser."""
    parser = claim_register_parser.ClaimRegisterParser(set(), BUILT_IN_CHECKS.claim_field_types())
    return parser.parse_claim_entry({"id": cid, "value": 0.1, "config": cfg, **kw}, os.getcwd())


def variant(**changes):
    """Copy BASE with dotted-key changes applied, e.g. variant(**{"judge.model": "x"})."""
    cfg = copy.deepcopy(BASE)
    for key, value in changes.items():
        node = cfg
        parts = key.split(".")
        for p in parts[:-1]:
            node = node[p]
        node[parts[-1]] = value
    return cfg


def case(name, a, b, want_findings, want_caveats=0, contains=None):
    """Run one comparison and check whether it gives findings, and how many caveats."""
    result = claim_checker.ClaimCheckRunner().run_check_on_claim(
        BUILT_IN_CHECKS.claim_checks["comparability"], a, *GATE,
        checks.ClaimContext(register={a.id: a, b.id: b}), frozenset())
    findings = [p.detail for p in result.problems] if isinstance(result, Flagged) else []
    caveats = [result.missing] if isinstance(result, NotChecked) else []
    ok = (len(findings) > 0) == bool(want_findings) and len(caveats) == want_caveats
    if contains:
        ok = ok and any(contains in f for f in findings + caveats)
    print(f"{'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print("     findings:", findings, "\n     caveats:", caveats)
    return ok


def main():
    """Run every case and report."""
    trained = variant(training="sft", output_dir="/runs/b", seeds=[3, 4, 5, 6, 7])
    results = [
        case("only the declared variable differs -> pass",
             claim("trained", trained, compare_to="base", varies=["training"]),
             claim("base", BASE), want_findings=False),
        case("model id spelled differently -> not a difference",
             claim("a", variant(model="qwen3-32b", training="sft"), compare_to="b",
                   varies=["training"]),
             claim("b", BASE), want_findings=False),
        case("judge-read vs stated-P read-out (audit c08-18) -> finding",
             claim("row7", variant(training="sft", readout="judge_brier"), compare_to="row2",
                   varies=["training"]),
             claim("row2", BASE), want_findings=True, contains="readout"),
        case("different instructor model (audit x01-18) -> finding",
             claim("c3", variant(**{"training": "sft", "instructor.model": "qwen3-32b"}),
                   compare_to="ctrl", varies=["training"]),
             claim("ctrl", BASE), want_findings=True, contains="instructor.model"),
        case("stale baseline from older code (audit c02-41) -> finding",
             claim("new", trained, compare_to="old", varies=["training"], commit="e923676"),
             claim("old", BASE, commit="a73f8d7"), want_findings=True, contains="_commit"),
        case("question language differs (audit c05-67) -> finding",
             claim("zh", variant(**{"training": "sft", "questions.language": "zh"}),
                   compare_to="en", varies=["training"]),
             claim("en", BASE), want_findings=True, contains="questions.language"),
        case("judge named under a generic key is still compared",
             claim("a", dict(variant(training="sft"), grader={"name": "gpt-4o"}), compare_to="b",
                   varies=["training"]),
             claim("b", dict(BASE, grader={"name": "claude-haiku-4.5"})), want_findings=True,
             contains="grader.name"),
        case("declared variable does not actually differ -> finding",
             claim("a", BASE, compare_to="b", varies=["training"]),
             claim("b", BASE), want_findings=True, contains="do not differ"),
        case("differences but nothing declared -> finding",
             claim("a", trained, compare_to="b"),
             claim("b", BASE), want_findings=True, contains="without declaring"),
        case("partner id does not exist -> finding, not silence",
             claim("a", BASE, compare_to="zzz"), claim("b", BASE),
             want_findings=True, contains="not a claim"),
        case("partner has no config -> caveat, not a pass",
             claim("a", trained, compare_to="b", varies=["training"]), claim("b"),
             want_findings=False, want_caveats=1),
        case("no comparison declared -> caveat",
             claim("a", BASE), claim("b", BASE), want_findings=False, want_caveats=1),
    ]
    print(f"{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
