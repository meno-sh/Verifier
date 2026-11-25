#!/usr/bin/env python3
"""Check the one rule for model names, and the comparison of two run configs.

The cases check that:
  - case, the separators "-", "_" and " ", and an organisation prefix do not count;
  - a different model, or a different size, still counts;
  - two run configs that differ only in the form of a model name do not differ;
  - the keys that name a run are ignored, and other differences are found.
Returns 1 if any case fails.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import model_names, run_configs  # noqa: E402

FAILED: list[str] = []


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def main() -> int:
    """Run every case."""
    same = [("Qwen/Qwen3-8B", "qwen3-8b"), ("Qwen3_8B", "qwen3-8b"),
            ("Llama 3.1 8B", "llama-3.1-8b"), (" Qwen3-8B ", "QWEN3-8B")]
    for a, b in same:
        expect(f"same model: {a!r} and {b!r}", model_names.same_model(a, b),
               f"{model_names.model_name_key(a)} != {model_names.model_name_key(b)}")
    different = [("Qwen3-8B", "Qwen3-32B"), ("Qwen3-8B", "Llama-3-8B")]
    for a, b in different:
        expect(f"different models: {a!r} and {b!r}", not model_names.same_model(a, b))
    expect("a known name is found by its key",
           model_names.KNOWN_NAMES.get(model_names.model_name_key("qwen3_8b")) is not None)
    base = {"model": "Qwen/Qwen3-8B", "lr": 1, "run_id": "a", "data": {"path": "x.json"}}
    other = {"model": "qwen3_8b", "lr": 1.0, "run_id": "b", "data": {"path": "x.json"}}
    expect("run configs: the form of a model name, and 1 against 1.0, are no difference",
           run_configs.diff(base, other, ignore=run_configs.DEFAULT_IGNORE) == [],
           repr(run_configs.diff(base, other, ignore=run_configs.DEFAULT_IGNORE)))
    changed = {**other, "data": {"path": "y.json"}}
    expect("run configs: a different data path is a difference",
           run_configs.diff(base, changed, ignore=run_configs.DEFAULT_IGNORE)
           == [("data.path", "x.json", "y.json")])
    if FAILED:
        print(f"\n{len(FAILED)} case(s) failed: {FAILED}")
        return 1
    print("\nAll model name cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
