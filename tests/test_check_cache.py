#!/usr/bin/env python3
"""Check the result cache of the claim checks (core/analysis/check_result_cache.py).

The cases count the runs of each check, and check that:
  - a second run with no change runs no check, and gives the same results;
  - a change to one claim runs again only the checks of that claim, and of the claims
    inferred from it;
  - a change to a file that a check reads runs the check again;
  - a change to the source file of a check runs it again;
  - a broken cache file raises CheckCacheError;
  - a Skipped that a check gives is stored, and a crash of an untrusted check is not;
  - a computational method runs again only when its file, its environment, its source
    or its data change, and a failure is not stored.
Returns 1 if any case fails.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.analysis import check_result_cache, claim_checker  # noqa: E402
from core.gate import artifact_pins, provenance, recompute  # noqa: E402
from core.methods import computational_methods  # noqa: E402
from core.registry import checks  # noqa: E402
from schema import findings, inputs  # noqa: E402
from schema import provenance as provenance_module  # noqa: E402
from schema import recompute as recompute_module  # noqa: E402
from schema.claim import claim_register_parser  # noqa: E402
from schema.primitives import (  # noqa: E402
    CheckCrashed,
    CheckResult,
    ModelFailedAfterRetries,
    Passed,
    ProblemKind,
    Severity,
    Skipped,
)
from utils import checksums  # noqa: E402

FAILED: list[str] = []


def kind(code: str) -> ProblemKind:
    """A problem kind of one test check; each check needs its own."""
    return ProblemKind(code=code, severity=Severity.STRENGTH)


RUNS: dict[str, list[str]] = {}         # the claims each check ran on, by check name
DATA = Path(tempfile.gettempdir())      # the data file of CountsOwnClaim; set by main


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


class CountsOwnClaim(checks.ClaimCheck):
    """Reads its own claim and one data file, and counts its runs."""
    name = "counts_own_claim"
    title = why = "Counts its runs."
    problem_kinds = (kind("counts-own-claim"),)
    input_scope = checks.CheckInputScope(
        claims=checks.OWN_CLAIM, other_files_read=lambda claim, options: (str(DATA),))

    def check(self, claim, recomputed, context) -> CheckResult:
        """Count the run, and pass."""
        RUNS.setdefault(self.name, []).append(claim.id)
        return Passed(measured=len(DATA.read_text()))


class CountsPremises(checks.ClaimCheck):
    """Reads its claim and its premises, and counts its runs."""
    name = "counts_premises"
    title = why = "Counts its runs."
    problem_kinds = (kind("counts-premises"),)
    needs_dependencies = True
    input_scope = checks.CheckInputScope(claims=checks.PREMISES)

    def check(self, claim, recomputed, context) -> CheckResult:
        """Count the run, and pass with the sum of the premise values."""
        RUNS.setdefault(self.name, []).append(claim.id)
        return Passed(measured=sum(context.register[d].value or 0
                                   for d in claim.ids_of_claims_it_is_inferred_from()))


class WritesItsFile(checks.ClaimCheck):
    """Reads and writes one file, as claim_uncertainty does with its readings."""
    name = "writes_its_file"
    title = why = "Writes its file."
    problem_kinds = (kind("writes-its-file"),)
    input_scope = checks.CheckInputScope(
        claims=checks.OWN_CLAIM,
        other_files_read=lambda claim, options: (str(DATA.with_name("readings.json")),))

    def check(self, claim, recomputed, context) -> CheckResult:
        """Count the run, and write the file."""
        RUNS.setdefault(self.name, []).append(claim.id)
        DATA.with_name("readings.json").write_text("{}")
        return Passed()


class SkipsItself(checks.ClaimCheck):
    """A check that gives Skipped itself, as `interval` does for a claim with no interval."""
    name = "skips_itself"
    title = why = "Skips."
    problem_kinds = (kind("skips-itself"),)
    input_scope = checks.CheckInputScope(claims=checks.OWN_CLAIM)

    def check(self, claim, recomputed, context) -> CheckResult:
        """Count the run, and give Skipped."""
        RUNS.setdefault(self.name, []).append(claim.id)
        return Skipped(because="the claim gives no interval")


class ModelFails(checks.ClaimCheck):
    """A check whose model fails after its retries, as claim uncertainty can."""
    name = "model_fails"
    title = why = "Model fails."
    problem_kinds = (kind("model-fails"),)
    input_scope = checks.CheckInputScope(claims=checks.OWN_CLAIM)

    def check(self, claim, recomputed, context) -> CheckResult:
        """Count the run, and give ModelFailedAfterRetries."""
        RUNS.setdefault(self.name, []).append(claim.id)
        return ModelFailedAfterRetries(because="a model failed after its retries")


class CrashesUntrusted(checks.ClaimCheck):
    """An untrusted check that crashes."""
    name = "crashes_untrusted"
    title = why = "Crashes."
    problem_kinds = (kind("crashes-untrusted"),)
    trusted = False
    input_scope = checks.CheckInputScope(claims=checks.OWN_CLAIM)

    def check(self, claim, recomputed, context) -> CheckResult:
        """Count the run, and crash."""
        RUNS.setdefault(self.name, []).append(claim.id)
        raise RuntimeError("a crash")


def register(values: dict[str, float]) -> tuple[inputs.Claim, ...]:
    """The claims a and b with these values, and d inferred from a."""
    parser = claim_register_parser.ClaimRegisterParser((), {})
    raw = [{"id": "a", "value": values["a"], "statement": "A holds."},
           {"id": "b", "value": values["b"], "statement": "B holds."},
           {"id": "d", "statement": "So D holds.", "source": {"kind": "claims", "claims": ["a"]}}]
    return tuple(parser.parse_claim_entry(r, str(DATA.parent)) for r in raw)


def run(state: Path, claims: tuple[inputs.Claim, ...], *extra: checks.ClaimCheck,
        workers: int = 16) -> tuple[findings.ClaimFindings, ...]:
    """Check the claims once, with a new cache of `state`, as one `vcheck run` does."""
    reg = checks.CheckRegistry()
    for check in (CountsOwnClaim(), CountsPremises(), *extra):
        reg.add_claim_check(check)
    cache = check_result_cache.CheckResultCache(state, "the standard library only")
    checker = claim_checker.ClaimChecker(
        reg, {"claim_check_workers": workers}, frozenset(),
        provenance.ProvenanceChecker(artifact_pins.ArtifactPins()),
        recompute.ClaimRecomputer({}), claim_checker.ClaimCheckRunner(cache))
    out = checker.check_claims_in_dependency_order(claims)
    cache.finish_run()
    return out


def ran() -> dict[str, list[str]]:
    """The runs since the last call, and forget them."""
    out = {k: sorted(v) for k, v in RUNS.items() if v}
    RUNS.clear()
    return out


def test_counts(tmp: Path) -> None:
    """Only the checks whose inputs changed run again."""
    state = tmp / "state"
    values = {"a": 0.5, "b": 0.25}
    first = run(state, register(values))
    expect("cache: the first run runs every check",
           ran() == {"counts_own_claim": ["a", "b", "d"], "counts_premises": ["d"]})
    second = run(state, register(values))
    expect("cache: a second run with no change runs no check, with the same results",
           ran() == {} and [c.checks for c in second] == [c.checks for c in first])
    run(state, register({**values, "b": 0.75}))
    expect("cache: a change to b runs only the checks of b", ran() == {"counts_own_claim": ["b"]})
    run(state, register({"a": 0.125, "b": 0.75}))
    expect("cache: a change to a runs the checks of a, and of d, which is inferred from a",
           ran() == {"counts_own_claim": ["a"], "counts_premises": ["d"]})
    DATA.write_text("changed data")
    run(state, register({"a": 0.125, "b": 0.75}))
    expect("cache: a change to a file a check reads runs it again, and so the checks that "
           "read its results", ran() == {"counts_own_claim": ["a", "b", "d"],
                                         "counts_premises": ["d"]})


def test_check_source(tmp: Path) -> None:
    """A change to the source file of a check runs it again."""
    source = tmp / "plugin_check.py"
    text = ("import __main__ as t\n"
            "class FromFile(t.CountsOwnClaim):\n    name = 'from_file'\n"
            "    problem_kinds = (t.kind('from-file'),)\n")

    def load() -> checks.ClaimCheck:
        """The check of the file, loaded again, as core/registry/plugin_checks.py loads one."""
        spec = importlib.util.spec_from_file_location("plugin_check", source)
        module = importlib.util.module_from_spec(spec)
        sys.modules["plugin_check"] = module
        spec.loader.exec_module(module)
        return module.FromFile()

    source.write_text(text)
    state = tmp / "state"
    values = {"a": 0.5, "b": 0.25}
    run(state, register(values), load())
    ran()
    run(state, register(values), load())
    expect("cache: the same source file runs nothing again", ran() == {})
    source.write_text(text + "# a change\n")
    run(state, register(values), load())
    expect("cache: a changed source file runs its check again",
           ran() == {"from_file": ["a", "b", "d"]})


def test_broken_and_crash(tmp: Path) -> None:
    """A broken cache file raises; an untrusted crash is not stored."""
    state = tmp / "state"
    values = {"a": 0.5, "b": 0.25}
    out = run(state, register(values), CrashesUntrusted())
    crashed = [c.checks["crashes_untrusted"] for c in out]
    run(state, register(values), CrashesUntrusted())
    expect("cache: a crash of an untrusted check is not stored, so it runs again",
           all(isinstance(r, CheckCrashed) for r in crashed)
           and ran()["crashes_untrusted"] == ["a", "a", "b", "b", "d", "d"])
    results = state / check_result_cache.CACHE_DIR / "results"
    for stored in results.iterdir():
        stored.write_text(json.dumps({"type": "Passed"}))
    try:
        run(state, register(values))
        expect("cache: a broken cache file raises CheckCacheError", False, "nothing raised")
    except check_result_cache.CheckCacheError as e:
        expect("cache: a broken cache file raises CheckCacheError, and says to delete the cache",
               "delete" in str(e) and "without 'measured'" in str(e), str(e))


class CountedMethod(computational_methods.SubprocessComputationalMethod):
    """A method from a method file that counts its calls."""
    calls = 0

    def __call__(self, path: str) -> computational_methods.Computed:
        """Count the call, and run the method."""
        CountedMethod.calls += 1
        return super().__call__(path)


METHOD = ("from checks.interface import Computed, method\n"
          "import json\n"
          "@method('mean')\n"
          "def mean(path):\n"
          "    values = json.load(open(path))\n"
          "    return Computed(value=sum(values) / len(values), n=len(values))\n")


def recompute_once(tmp: Path, methods_ran_in: str = "the standard library only",
                   pattern: str = "data.json") -> object:
    """Recompute the claim of `pattern` once, with a new cache, as one `vcheck run` does.

    The claim pins the first file that `pattern` matches, with one checksum.
    """
    first = sorted(tmp.glob(pattern))[0]
    source = inputs.ArtifactSource(path=str(tmp / pattern), method="mean",
                                   sha256=checksums.sha256_of_file(str(first)))
    claim = inputs.Claim(id="c", value=inputs.PrintedFloat("0.25"), source=source)
    entry = computational_methods.ComputationalMethodEntry(
        name="mean", function=CountedMethod(sys.executable, str(tmp / "methods.py"), "mean"))
    cache = check_result_cache.CheckResultCache(tmp / "state", methods_ran_in)
    out = recompute.ClaimRecomputer({"mean": entry}, cache).recompute_claim(
        claim, provenance.ProvenanceChecker(artifact_pins.ArtifactPins()).check_claim_source(
            claim))
    cache.finish_run()
    return out


def calls() -> int:
    """The calls of the method since the last call, and forget them."""
    n, CountedMethod.calls = CountedMethod.calls, 0
    return n


def test_recompute(tmp: Path) -> None:
    """A method runs again only when what it reads changed."""
    (tmp / "methods.py").write_text(METHOD)
    (tmp / "data.json").write_text("[0.25, 0.25]")
    first = recompute_once(tmp)
    second = recompute_once(tmp)
    expect("recompute: a second run with no change calls no method, with the same result",
           calls() == 1 and second == first and type(first).__name__ == "Matches",
           repr((first, second)))
    (tmp / "data.json").write_text("[0.5, 0.0]")
    recompute_once(tmp)
    expect("recompute: new data with a new pin calls the method again", calls() == 1)
    (tmp / "methods.py").write_text(METHOD + "# a change\n")
    recompute_once(tmp)
    expect("recompute: a changed method file calls the method again", calls() == 1)
    recompute_once(tmp, "the environment uv.lock pins (sha256 abc)")
    expect("recompute: a new environment calls the method again", calls() == 1)
    (tmp / "helper.py").write_text("WEIGHT = 1\n")
    recompute_once(tmp, "the environment uv.lock pins (sha256 abc)")
    expect("recompute: a new helper module beside the method file calls the method again",
           calls() == 1)
    (tmp / "methods.py").write_text(METHOD.replace("len(values))", "len(values)) + 1/0"))
    failed = [recompute_once(tmp), recompute_once(tmp)]
    expect("recompute: a failure is not stored, so the method runs again",
           calls() == 2 and all(type(r).__name__ == "MethodFailed" for r in failed),
           repr(failed))


def test_unpinned_file(tmp: Path) -> None:
    """A change to a matched file that the single pin does not cover calls the method again."""
    (tmp / "methods.py").write_text(METHOD.replace("json.load(open(path))",
                                                   "[v for f in sorted(__import__('glob')"
                                                   ".glob(path)) for v in json.load(open(f))]"))
    (tmp / "data1.json").write_text("[0.25]")
    (tmp / "data2.json").write_text("[0.25]")
    first = recompute_once(tmp, pattern="data*.json")
    (tmp / "data2.json").write_text("[0.75]")
    second = recompute_once(tmp, pattern="data*.json")
    expect("recompute: a changed file that no pin covers calls the method again",
           calls() == 2 and type(first).__name__ == "Matches"
           and type(second).__name__ == "Disagrees", repr((first, second)))


def test_written_file_and_nan(tmp: Path) -> None:
    """A check that writes its file is found by the file as it is after; NaN is not stored."""
    state = tmp / "state"
    values = {"a": 0.5, "b": 0.25}
    run(state, register(values), WritesItsFile())
    ran()
    run(state, register(values), WritesItsFile())
    expect("cache: a check that wrote its file gives its stored result in the next run",
           "writes_its_file" not in ran())
    DATA.with_name("readings.json").unlink()
    # 1 worker: the run on a writes the file again before b and d look up their results;
    # with parallel workers, b and d can run before the file exists again
    run(state, register(values), WritesItsFile(), workers=1)
    expect("cache: when the file it wrote is deleted, the check runs again",
           ran().get("writes_its_file") == ["a"])
    run(state, register(values), SkipsItself())
    ran()
    second = run(state, register(values), SkipsItself())
    expect("cache: a Skipped that the check gives is stored, and given again",
           "skips_itself" not in ran()
           and all(type(c.checks["skips_itself"]) is Skipped for c in second))
    run(state, register(values), ModelFails())
    ran()
    run(state, register(values), ModelFails())
    expect("cache: a model failure is not stored, so the next run asks the model again",
           sorted(ran().get("model_fails", [])) == sorted(c.id for c in register(values)))
    cache = check_result_cache.CheckResultCache(state, "the standard library only")
    claim = register(values)[0]
    nan_gate = recompute_module.Matches(value=0.5, n=1, interval=(float("nan"), 1.0),
                                        tolerance=0.001)
    expect("cache: a gate result with NaN gives no fingerprint, so nothing is stored",
           cache.fingerprint(CountsOwnClaim(), claim,
                             checks.ClaimContext(register={"a": claim}),
                             provenance_module.NoSource(), nan_gate) is None)


def main() -> int:
    """Run every case in a new temporary folder."""
    global DATA
    for case in (test_counts, test_check_source, test_broken_and_crash, test_recompute,
                 test_unpinned_file, test_written_file_and_nan):
        print(case.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            DATA = Path(tmp) / "data.txt"
            DATA.write_text("data")
            case(Path(tmp))
            RUNS.clear()
    if FAILED:
        print(f"\n{len(FAILED)} case(s) failed: {FAILED}")
        return 1
    print("\nAll check cache cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
