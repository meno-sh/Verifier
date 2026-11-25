#!/usr/bin/env python3
"""Assert that each run check passes on a clean run and fires on a broken one.

Each case writes a clean run into a fresh folder, breaks one thing, and runs
run_integrity, run_outputs and run_data on a claim naming that run. A check that
never fires cannot be told apart from a check that is missing. Returns 1 if any
case fails, so it can gate a commit.
"""
import json
import os
import sys
import tempfile
from collections.abc import Callable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.analysis import claim_checker  # noqa: E402
from core.registry import checks, extensions  # noqa: E402
from schema import inputs, provenance  # noqa: E402
from schema.claim import claim_register_parser  # noqa: E402
from schema.primitives import CheckResult, Flagged, NotChecked, Passed, Skipped  # noqa: E402

BUILT_IN_CHECKS = extensions.builtin_check_registry()   # the registry of the built-in checks

CONFIG = {"model": "qwen3-32b", "seed": 1, "training": {"lr": 0.0001}}
RECORD = {"config": "config.json", "applied_config": "applied.json", "model": "qwen3-32b",
          "loaded_model": "Qwen/Qwen3-32B", "adapter": "adapter/", "adapter_loaded": True,
          "seed": 1, "train_ids": "train_ids.json", "eval_ids": "eval_ids.json",
          "outputs": "outputs.jsonl"}
OUTPUTS = [{"id": f"e{i}", "output": f"estimate {i}", "base_output": f"base {i}",
            "truncated": False} for i in range(25)]


def write(folder: str, name: str, content: object) -> None:
    """Write one file of the run: JSON, or JSON Lines for a list of outputs."""
    with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
        if name.endswith(".jsonl"):
            fh.write("".join(json.dumps(item) + "\n" for item in content))
        else:
            fh.write(content if isinstance(content, str) else json.dumps(content))


def clean_run(folder: str) -> None:
    """A run with nothing wrong: config applied, model and adapter loaded, data clean."""
    os.mkdir(os.path.join(folder, "adapter"))
    for name, content in (("config.json", CONFIG), ("applied.json", CONFIG),
                          ("train_ids.json", ["t0", "t1"]),
                          ("eval_ids.json", [o["id"] for o in OUTPUTS]),
                          ("outputs.jsonl", OUTPUTS), ("run.json", RECORD)):
        write(folder, name, content)


def results(folder: str, claim_raw: dict | None = None) -> dict[str, CheckResult]:
    """Every run check's result for one claim naming the run in `folder`."""
    raw = claim_raw or {"id": "c", "value": 0.1, "run": "run.json"}
    parser = claim_register_parser.ClaimRegisterParser((), BUILT_IN_CHECKS.claim_field_types())
    claim = parser.parse_claim_entry(raw, folder)
    gate = (provenance.NoSource(), Skipped(because="not recomputed"))
    context = checks.ClaimContext(register={"c": claim})
    runner = claim_checker.ClaimCheckRunner()
    return {name: runner.run_check_on_claim(BUILT_IN_CHECKS.claim_checks[name], claim, *gate,
                                            context, frozenset())
            for name in ("run_integrity", "run_outputs", "run_data")}


def record(**changes: object) -> Callable[[str], None]:
    """A case that rewrites run.json with some keys changed; None removes a key."""
    new = {k: v for k, v in {**RECORD, **changes}.items() if v is not None}
    return lambda folder: write(folder, "run.json", new)


def file(name: str, content: object) -> Callable[[str], None]:
    """A case that rewrites one file of the run."""
    return lambda folder: write(folder, name, content)


def outputs(**changes: object) -> Callable[[str], None]:
    """A case that rewrites every output with some fields changed."""
    return file("outputs.jsonl", [{**o, **changes} for o in OUTPUTS])


# (case, change, check, expected problem code or result type)
CASES: list[tuple[str, Callable[[str], None], str, str | type]] = [
    ("record is not JSON", file("run.json", "{"), "run_integrity", "run-unreadable"),
    ("record has an unknown key", record(heartbeat="hb"), "run_integrity", "run-unreadable"),
    ("record has an absolute path", record(config="/runs/config.json"), "run_integrity",
     "run-unreadable"),
    ("a named file is gone", record(eval_ids="gone.json"), "run_integrity", "run-unreadable"),
    ("the adapter is gone", record(adapter="gone/"), "run_integrity", "run-unreadable"),
    ("outputs names a folder", record(outputs="adapter/"), "run_integrity", "run-unreadable"),
    ("eval ids are objects, not ids", file("eval_ids.json", [{"id": "e0"}]), "run_integrity",
     "run-unreadable"),
    ("applied config differs, even in the seed", file("applied.json", {**CONFIG, "seed": 42}),
     "run_integrity", "config-not-applied"),
    ("defaults added at load time are not a difference",
     file("applied.json", {**CONFIG, "warmup": 0}), "run_integrity", Passed),
    ("a different model loaded", record(loaded_model="llama-3-8b"), "run_integrity",
     "wrong-model-loaded"),
    ("adapter declared, not loaded", record(adapter_loaded=False), "run_integrity",
     "adapter-not-loaded"),
    ("loading not recorded", record(loaded_model=None, adapter_loaded=None), "run_integrity",
     "load-not-recorded"),
    ("no seed", record(seed=None), "run_integrity", "seed-not-set"),
    ("no output items", file("outputs.jsonl", []), "run_outputs", "outputs-empty"),
    ("outputs empty", outputs(output=" "), "run_outputs", "outputs-empty"),
    ("outputs equal the base model's", file("outputs.jsonl", [{**o, "output": o["base_output"]}
                                                             for o in OUTPUTS]),
     "run_outputs", "outputs-same-as-base"),
    ("one output equal to the base model's is not enough",
     file("outputs.jsonl", [{"id": "e0", "output": "x", "base_output": "x"}]), "run_outputs",
     Passed),
    ("outputs collapsed", outputs(output="same"), "run_outputs", "outputs-collapsed"),
    ("null optional fields count as absent", outputs(base_output=None, truncated=None),
     "run_outputs", Passed),
    ("outputs truncated", outputs(truncated=True), "run_outputs", "outputs-truncated"),
    ("an output is not an object", file("outputs.jsonl", ["just text"]), "run_outputs", Skipped),
    ("no outputs named", record(outputs=None), "run_outputs", NotChecked),
    ("a config setting pinned to null is compared", lambda folder: (
        file("config.json", {**CONFIG, "scheduler": None})(folder),
        file("applied.json", {**CONFIG, "scheduler": "cosine"})(folder)),
     "run_integrity", "config-not-applied"),
    ("output_dir rewritten at load time is not a difference",
     file("applied.json", {**CONFIG, "output_dir": "runs/x-2026"}), "run_integrity", Passed),
    ("train and eval overlap", file("train_ids.json", ["t0", "e1"]), "run_data",
     "train-eval-overlap"),
    ("an item scored twice", file("outputs.jsonl", [*OUTPUTS, OUTPUTS[0]]), "run_data",
     "items-duplicated"),
    ("an eval item never scored", file("eval_ids.json", [*(o["id"] for o in OUTPUTS), "e99"]),
     "run_data", "items-not-scored"),
    ("a training item scored into the metric",
     file("outputs.jsonl", [*OUTPUTS, {"id": "t0", "output": "x"}]), "run_data",
     "items-outside-eval"),
    ("no training ids named", record(train_ids=None), "run_data", "ids-not-recorded"),
    ("duplicates found without eval ids", lambda folder: (
        record(eval_ids=None)(folder),
        file("outputs.jsonl", [*OUTPUTS, OUTPUTS[0]])(folder)), "run_data", "items-duplicated"),
    ("no eval ids or outputs named", record(eval_ids=None, outputs=None), "run_data",
     NotChecked),
]


def run_case(change: Callable[[str], None] | None) -> dict[str, CheckResult]:
    """Write a clean run, apply one change, and return every run check's result."""
    with tempfile.TemporaryDirectory() as folder:
        clean_run(folder)
        if change is not None:
            change(folder)
        return results(folder)


def main() -> int:
    """Run the clean case, the claim cases and every broken case, and report."""
    checked = []

    def expect(name: str, ok: bool, got: object) -> None:
        checked.append(ok)
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"        got {got}")

    for name, result in run_case(None).items():
        expect(f"clean run: {name} passes", isinstance(result, Passed), result)
    with tempfile.TemporaryDirectory() as folder:
        clean_run(folder)
        got = results(folder, {"id": "c", "value": 0.1})
        expect("a claim naming no run is not checked",
               all(isinstance(r, NotChecked) and r.missing == "run" for r in got.values()), got)
        try:
            parser = claim_register_parser.ClaimRegisterParser(
                (), BUILT_IN_CHECKS.claim_field_types())
            parser.parse_claim_entry({"id": "c", "value": 0.1, "run": "/runs/run.json"}, folder)
            expect("an absolute run path makes the claim unreadable", False, "no error")
        except inputs.ClaimRegisterError as e:
            expect("an absolute run path makes the claim unreadable", "absolute" in str(e), e)
    for name, change, check, want in CASES:
        got = run_case(change)[check]
        ok = (isinstance(got, want) if isinstance(want, type)
              else isinstance(got, Flagged) and want in {p.kind.code for p in got.problems})
        expect(f"{check}: {name}", ok, got)

    failed = checked.count(False)
    print(f"\n{'FAILED: ' + str(failed) + ' of ' if failed else 'All '}{len(checked)} cases "
          f"{'failed.' if failed else 'behaved as expected.'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
