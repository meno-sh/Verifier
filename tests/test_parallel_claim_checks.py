#!/usr/bin/env python3
"""Check the parallel claim checks of core/analysis/claim_checker.py (`claim_check_workers`).

  - the findings with 1 worker and with 4 workers are the same, in register order;
  - with a check that reads the findings of the claims a claim rests on, no claim starts before
    them, and a cycle breaks at one claim without a block;
  - with no such check, no claim waits, and no claim gets the findings of others;
  - with a slow check, 4 workers take less time than 1;
  - each thread keeps the caller's spending purpose;
  - 16 threads that save uncertainty readings at the same time keep every reading;
  - a pool size below 1 is refused.

No model is called. Returns 1 if any case fails, so it can gate a commit.
"""
import concurrent.futures
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from checks.uncertainty import uncertainty  # noqa: E402
from core.analysis import claim_checker  # noqa: E402
from core.gate import artifact_pins, provenance, recompute  # noqa: E402
from core.registry import checks  # noqa: E402
from schema import inputs  # noqa: E402
from schema.claim import claim_register_parser  # noqa: E402
from schema.primitives import CheckResult, Passed, ProblemKind, Severity  # noqa: E402
from utils import spending  # noqa: E402

FAILED: list[str] = []
SECONDS = 0.2           # the time of one slow check


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


class SlowCheckOfInferredClaims(checks.ClaimCheck):
    """Waits SECONDS on each inferred claim, and records when it started and ended."""
    name = "slow_check_of_inferred_claims"
    title = why = "Waits."
    problem_kinds = (ProblemKind(code="slow-check-of-inferred-claims",
                                 severity=Severity.PRESENTATION),)
    needs_dependencies = True

    def __init__(self, reads_findings: bool) -> None:
        """Read the findings of the claims a claim rests on only when `reads_findings`."""
        self.reads_findings = reads_findings
        self.started: dict[str, float] = {}
        self.ended: dict[str, float] = {}
        self.dependencies_seen: dict[str, list[str]] = {}
        self.purposes: set[str | None] = set()
        self.lock = threading.Lock()

    def reads_findings_of_claims_it_rests_on(self, options) -> bool:
        """As the test asks."""
        return self.reads_findings

    def check(self, claim, recomputed, context) -> CheckResult:
        """Record the start, the findings it got and the purpose, wait, then record the end."""
        with self.lock:
            self.started[claim.id] = time.monotonic()
            self.dependencies_seen[claim.id] = sorted(context.dependencies)
            self.purposes.add(spending.current_purpose())
        time.sleep(SECONDS)
        with self.lock:
            self.ended[claim.id] = time.monotonic()
        return Passed(measured=float(len(context.dependencies)))


def register() -> tuple[inputs.Claim, ...]:
    """A leaf, a chain of 3 inferred claims on it, 3 independent ones, and a cycle of 2."""
    parser = claim_register_parser.ClaimRegisterParser((), {})

    def derived(cid: str, *deps: str) -> dict:
        return {"id": cid, "statement": f"{cid} holds.",
                "source": {"kind": "claims", "claims": list(deps)}}
    raw = [{"id": "leaf", "statement": "The leaf holds."},
           derived("c1", "leaf"), derived("c2", "c1"), derived("c3", "c2"),
           derived("i1", "leaf"), derived("i2", "leaf"), derived("i3", "leaf"),
           derived("p", "q"), derived("q", "p")]
    return tuple(parser.parse_claim_entry(r, ".") for r in raw)


def run(workers: int, reads_findings: bool) -> tuple[tuple, SlowCheckOfInferredClaims, float]:
    """Check the register once; give the findings, the check, and the seconds it took."""
    reg = checks.CheckRegistry()
    check = SlowCheckOfInferredClaims(reads_findings)
    reg.add_claim_check(check)
    checker = claim_checker.ClaimChecker(
        reg, {"claim_check_workers": workers}, frozenset(),
        provenance.ProvenanceChecker(artifact_pins.ArtifactPins()),
        recompute.ClaimRecomputer({}), claim_checker.ClaimCheckRunner())
    began = time.monotonic()
    with spending.purpose("parallel_test"):
        out = checker.check_claims_in_dependency_order(register())
    return out, check, time.monotonic() - began


def test_waiting() -> None:
    """With a check that reads dependency findings, the order holds and cycles do not block."""
    one, _, _ = run(1, reads_findings=True)
    four, check, _ = run(4, reads_findings=True)
    expect("the findings with 1 and 4 workers are the same, in register order",
           [f.claim.id for f in four] == [f.claim.id for f in one]
           and [f.checks for f in four] == [f.checks for f in one],
           repr([(f.claim.id, f.checks) for f in four]))
    expect("no claim starts before a claim it rests on has ended",
           check.started["c2"] >= check.ended["c1"] and check.started["c3"] >= check.ended["c2"],
           repr((check.started, check.ended)))
    expect("each claim gets the findings of the claims it rests on that come before it",
           check.dependencies_seen["c3"] == ["c2"] and check.dependencies_seen["c1"] == ["leaf"],
           repr(check.dependencies_seen))
    expect("a cycle breaks at one claim: one of p and q gets the other's findings",
           sorted(len(check.dependencies_seen[c]) for c in ("p", "q")) == [0, 1],
           repr({c: check.dependencies_seen[c] for c in ("p", "q")}))
    expect("each thread keeps the caller's spending purpose",
           check.purposes == {"parallel_test"}, repr(check.purposes))


def test_no_waiting() -> None:
    """With no check that reads dependency findings, no claim waits."""
    one, _, slow = run(1, reads_findings=False)
    four, check, fast = run(4, reads_findings=False)
    expect("no claim gets the findings of others",
           all(seen == [] for seen in check.dependencies_seen.values()),
           repr(check.dependencies_seen))
    expect("the findings with 1 and 4 workers are the same",
           [f.checks for f in four] == [f.checks for f in one])
    expect("c3 starts before c2 has ended, because nothing waits",
           check.started["c3"] < check.ended["c2"], repr((check.started, check.ended)))
    expect("4 workers take less time than 1 (8 slow claims)",
           fast < slow / 2, f"1 worker {slow:.2f} s, 4 workers {fast:.2f} s")


def test_parallel_saves() -> None:
    """16 threads that save readings to one file at the same time lose none of them."""
    reading = uncertainty.Reading(local=uncertainty.Local(value=0.5, worst=None), assessed=(),
                                  red_team="r@1", quantifier="q@1", calls=0,
                                  without_attack=0.5, attacked=False)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "claim_uncertainty.json"
        with concurrent.futures.ThreadPoolExecutor(16) as pool:
            list(pool.map(lambda n: uncertainty.save_reading(path, f"key-{n}", reading),
                          range(64)))
        saved = uncertainty.load_readings(path)
    expect("64 parallel saves keep all 64 readings", len(saved) == 64, repr(len(saved)))


def test_bad_size() -> None:
    """A pool size below 1 is refused."""
    try:
        run(0, reads_findings=False)
    except ValueError as e:
        expect("a pool size of 0 is refused", "at least 1" in str(e), str(e))
    else:
        expect("a pool size of 0 is refused", False, "no error raised")


def main() -> int:
    """Run every group of cases and report the failures."""
    for group in (test_waiting, test_no_waiting, test_parallel_saves, test_bad_size):
        group()
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll parallel claim check cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
