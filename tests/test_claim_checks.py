#!/usr/bin/env python3
"""Assert that each small claim check passes, flags, or reports what it could not check.

Covers baseline, seeds, caption, measurement and interval, and the cases the
registry handles for every check: a missing key, a claim that was not recomputed,
a gate that failed, and a check that is switched off. Comparability has its own
test. The checks are run directly with a hand-built claim and gate result, so every
case, including an interval, is set exactly. Returns 1 if any case fails, so it can gate a commit.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.analysis import claim_checker  # noqa: E402
from core.registry import checks, extensions  # noqa: E402
from schema import inputs, provenance, recompute  # noqa: E402
from schema.primitives import Flagged, NotChecked, Off, Passed, Skipped  # noqa: E402

BUILT_IN_CHECKS = extensions.builtin_check_registry()   # the registry of the built-in checks

INTACT = provenance.Intact(path="runs/main/results.json")


def matches(value: float, n: int, ci: tuple[float, float] | None = None) -> recompute.Matches:
    """The result recompute gives a claim that matched."""
    return recompute.Matches(value=value, n=n, interval=ci, tolerance=0.0005)


NOT_RECOMPUTED = Skipped(because="not recomputed: the claim register gives no source")
NO_SOURCE = (provenance.NoSource(), NOT_RECOMPUTED)

# (name, check, claim fields, gate result, switched off, expected result type)
CASES = [
    ("baseline: worse than baseline, lower is better", "baseline",
     {"baseline": 0.25, "lower_is_better": True}, (INTACT, matches(0.30, 100)), (), Flagged),
    ("baseline: beats baseline", "baseline",
     {"baseline": 0.25, "lower_is_better": True}, (INTACT, matches(0.10, 100)), (), Passed),
    ("baseline: no baseline declared", "baseline",
     {}, (INTACT, matches(0.10, 100)), (), NotChecked),
    ("baseline: below baseline, higher is better", "baseline",
     {"baseline": 0.25, "lower_is_better": False}, (INTACT, matches(0.10, 100)), (), Flagged),
    ("baseline: above baseline, higher is better", "baseline",
     {"baseline": 0.25, "lower_is_better": False}, (INTACT, matches(0.40, 100)), (), Passed),
    ("baseline: equal to baseline, lower is better", "baseline",
     {"baseline": 0.25, "lower_is_better": True}, (INTACT, matches(0.25, 100)), (), Flagged),
    ("baseline: equal to baseline, higher is better", "baseline",
     {"baseline": 0.25, "lower_is_better": False}, (INTACT, matches(0.25, 100)), (), Flagged),
    ("baseline: direction not declared", "baseline",
     {"baseline": 0.25}, (INTACT, matches(0.10, 100)), (), NotChecked),
    ("baseline: not recomputed", "baseline",
     {"baseline": 0.25, "lower_is_better": True}, NO_SOURCE, (), Skipped),

    ("seeds: one seed", "seeds", {"seeds": 1}, NO_SOURCE, (), Flagged),
    ("seeds: three seeds", "seeds", {"seeds": 3}, NO_SOURCE, (), Passed),
    ("seeds: not declared", "seeds", {}, NO_SOURCE, (), NotChecked),
    ("seeds: gate failed", "seeds", {"seeds": 1},
     (provenance.Missing(path="gone.json"), Skipped(because="not recomputed: provenance failed")),
     (), Skipped),
    ("seeds: switched off", "seeds", {"seeds": 1}, (INTACT, matches(0.1, 10)), ("seeds",), Off),

    ("caption: names another model", "caption",
     {"model": "Qwen3-8B", "caption_model": "Llama-3-8B"}, (INTACT, matches(0.1, 10)), (), Flagged),
    ("caption: names the run's model", "caption",
     {"model": "Qwen3-8B", "caption_model": "Qwen3-8B"}, (INTACT, matches(0.1, 10)), (), Passed),
    ("caption: the run's model with an organisation prefix and another case", "caption",
     {"model": "Qwen/Qwen3-8B", "caption_model": "qwen3_8b"}, (INTACT, matches(0.1, 10)), (),
     Passed),
    ("caption: run model not declared", "caption",
     {"caption_model": "Qwen3-8B"}, (INTACT, matches(0.1, 10)), (), NotChecked),

    ("measurement: sample shrank by more than 10%", "measurement",
     {"baseline_n": 200}, (INTACT, matches(0.1, 150)), (), Flagged),
    ("measurement: sample within 10%", "measurement",
     {"baseline_n": 200}, (INTACT, matches(0.1, 190)), (), Passed),
    ("measurement: baseline count not declared", "measurement",
     {}, (INTACT, matches(0.1, 150)), (), NotChecked),

    ("interval: excludes zero only barely", "interval",
     {}, (INTACT, matches(0.1, 100, ci=(0.01, 0.2))), (), Flagged),
    ("interval: spans zero but called significant", "interval",
     {"claimed_significant": True}, (INTACT, matches(0.1, 100, ci=(-0.1, 0.3))), (), Flagged),
    ("interval: clearly excludes zero", "interval",
     {"claimed_significant": True}, (INTACT, matches(0.1, 100, ci=(0.05, 0.2))), (), Passed),
    ("interval: the method gives no interval", "interval",
     {"claimed_significant": True}, (INTACT, matches(0.1, 100)), (), Skipped),
    ("interval: not recomputed", "interval",
     {}, NO_SOURCE, (), Skipped),
    ("interval: recompute switched off", "interval", {}, (INTACT, Off()), (), Skipped),
]


def main() -> int:
    """Run every case and report which ones did not give the expected result."""
    failed = 0
    for name, check_name, fields, gate, off, want in CASES:
        claim = inputs.Claim(id="c", value=0.1, fields=fields)
        context = checks.ClaimContext(register={"c": claim})
        got = claim_checker.ClaimCheckRunner().run_check_on_claim(
            BUILT_IN_CHECKS.claim_checks[check_name], claim, *gate, context, frozenset(off))
        ok = isinstance(got, want)
        failed += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"        expected {want.__name__}, got {got}")
    if failed:
        print(f"\nFAILED: {failed} of {len(CASES)} case(s).")
        return 1
    print(f"\nAll {len(CASES)} cases behaved as expected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
