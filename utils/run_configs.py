"""Compare the configs of two runs: which keys differ, once cosmetic differences are removed.

A config is a nested mapping. Its keys are compared as dotted keys, such as "judge.model".
Strings compare without case, numbers as floats, and model names by
model_names.model_name_key.
"""
from collections.abc import Collection, Iterable, Mapping

from utils import model_names

# Keys that differ between any two runs without making them unlike: run identity,
# output locations, timing, and the seed list (seed count is checked elsewhere).
# Deliberately narrow. A generic key such as "name" or "path" is not ignored, because
# under "judge" or "data" it is exactly the kind of difference a comparison exists for.
DEFAULT_IGNORE = ("seed", "seeds", "run_id", "run_name", "output_dir", "out_dir", "log_dir",
                  "wandb", "timestamp", "started_at", "finished_at")


def flatten(obj: Mapping[str, object], prefix: str = "") -> dict[str, object]:
    """Flatten nested dicts into dotted keys, e.g. {"judge": {"model": m}} -> {"judge.model": m}."""
    out = {}
    for k, v in obj.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, Mapping):
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out


def normalized_value(value: object, is_model: bool = False) -> object:
    """A config value in the form values are compared in, so cosmetic differences do not count.

    Strings are lower-cased and trimmed. Under a key naming a model, a string is compared
    by its model name key; other strings keep their slashes, since there they are usually
    paths. Numbers compare as floats, so 1 and 1.0 are the same. Lists compare element by
    element.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return model_names.model_name_key(value) if is_model else value.strip().lower()
    if isinstance(value, (list, tuple)):
        return [normalized_value(v, is_model) for v in value]
    return value


def is_ignored(key: str, ignore: Collection[str]) -> bool:
    """Return True if the ignore list holds the dotted key, its last part or a parent section."""
    last = key.rsplit(".", 1)[-1]
    return key in ignore or last in ignore or any(key.startswith(i + ".") for i in ignore)


def diff(cfg_a: Mapping[str, object], cfg_b: Mapping[str, object],
         ignore: Iterable[str] = ()) -> list[tuple[str, object, object]]:
    """Return the sorted list of (key, value_a, value_b) where two configs differ.

    A key present on only one side counts as a difference, with None for the
    missing side. Keys in `ignore` are skipped; comparing two runs passes
    DEFAULT_IGNORE, so the keys naming each run do not count.
    """
    ignore = set(ignore)
    a, b = flatten(cfg_a), flatten(cfg_b)
    out = []
    for key in sorted(set(a) | set(b)):
        if is_ignored(key, ignore):
            continue
        is_model = "model" in key.lower()
        if normalized_value(a.get(key), is_model) != normalized_value(b.get(key), is_model):
            out.append((key, a.get(key), b.get(key)))
    return out
