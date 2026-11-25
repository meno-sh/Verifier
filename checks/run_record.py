"""Reads a run record, the JSON file that a run writes about itself.

A claim names its run record with the `run` key. Every key in a record is optional, and a
check reports a key that is missing. Paths are relative to the folder of the record. Id
files are JSON lists. The outputs file is JSON Lines: `id`, `output`, and optionally
`base_output` and `truncated`.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping

from schema import inputs, primitives
from schema.primitives import record

# The claim key every run check reads: the path to the run's record.
FIELDS = {"run": inputs.FieldType.PATH}


class RunRecordError(ValueError):
    """A run record, or a file it names, that cannot be used as written."""


@record
class RunRecord:
    """One run's record, with every path resolved against the record's folder."""
    config: str | None
    applied_config: str | None         # the config that the code applied
    model: str | None
    loaded_model: str | None
    adapter: str | None
    adapter_loaded: bool | None
    seed: int | None
    train_ids: str | None
    eval_ids: str | None
    outputs: str | None


@record
class Output:
    """One item's output, as the run recorded it."""
    id: str
    output: str
    base_output: str | None            # the output of the untrained model
    truncated: bool


# Each key of the record and the type of its value; a path is resolved on reading.
PATH_KEYS = ("config", "applied_config", "adapter", "train_ids", "eval_ids", "outputs")
TYPES = {"model": str, "loaded_model": str, "adapter_loaded": bool, "seed": int,
         **{key: str for key in PATH_KEYS}}


def load(path: str) -> RunRecord:
    """Read and parse a run record.

    Raises:
        RunRecordError: when it is unreadable, not a JSON object, has an unknown key,
            a value of the wrong type, or an absolute path.
    """
    raw = load_json(path, dict)
    unknown = sorted(set(raw) - TYPES.keys())
    if unknown:
        raise RunRecordError(f"the run record has unknown key(s) {', '.join(unknown)}; "
                             f"allowed are {', '.join(sorted(TYPES))}")
    base = os.path.dirname(os.path.abspath(path))
    values = {key: _typed(raw.get(key), kind, f"`{key}` in the run record")
              for key, kind in TYPES.items()}
    for key in PATH_KEYS:
        if values[key] is not None:
            values[key] = _resolve(values[key], key, base)
    return RunRecord(**values)


def files_named_in_run_record(claim: inputs.Claim,
                               options: Mapping[str, object]) -> tuple[str, ...]:
    """The files a run check reads for a claim: its run record, and the paths in it.

    A record that cannot be read names no other file; the check reports it.
    """
    path = claim.fields.get("run")
    if not isinstance(path, str):
        return ()
    try:
        run = load(path)
    except RunRecordError:
        return (path,)
    return (path, *(p for key in PATH_KEYS if (p := getattr(run, key)) is not None))


def load_json[T](path: str, kind: type[T]) -> T:
    """A JSON file whose content must be of the given type, such as a config's dict.

    Raises:
        RunRecordError: naming the file, when it is unreadable, not JSON, or of another type.
    """
    try:
        content = json.loads(_read(path))
    except (ValueError, RecursionError) as e:
        raise RunRecordError(f"{os.path.basename(path)} is not valid JSON: {e}") from None
    if not isinstance(content, kind):
        raise RunRecordError(f"{os.path.basename(path)} must be a JSON {kind.__name__}")
    return content


def load_ids(path: str) -> list[str]:
    """The item ids in a JSON list of strings or whole numbers.

    Raises:
        RunRecordError: when the file is unreadable or holds anything else.
    """
    ids = load_json(path, list)
    for item in ids:
        _typed(item, str | int, f"every id in {os.path.basename(path)}")
    return [str(x) for x in ids]


def load_outputs(path: str) -> list[Output]:
    """Every item in a JSON Lines outputs file. A null optional field counts as absent.

    Raises:
        RunRecordError: when the file is unreadable or a line is not an output object.
    """
    name, out = os.path.basename(path), []
    for n, line in enumerate(_read(path).splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            raise RunRecordError(f"{name} line {n} is not JSON") from None
        if not isinstance(item, dict):
            raise RunRecordError(f"{name} line {n} must be an object")
        at = f"{name} line {n}"
        out.append(Output(
            id=str(_required(item.get("id"), str | int, f"`id` on {at}")),
            output=_required(item.get("output"), str, f"`output` on {at}"),
            base_output=_typed(item.get("base_output"), str, f"`base_output` on {at}"),
            truncated=bool(_typed(item.get("truncated"), bool, f"`truncated` on {at}"))))
    return out


def _read(path: str) -> str:
    """A file's text, or a RunRecordError naming it: gone, a folder, unreadable, not UTF-8."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        raise RunRecordError(f"cannot read {os.path.basename(path)}: {e.strerror}") from None
    except UnicodeDecodeError:
        raise RunRecordError(f"{os.path.basename(path)} is not UTF-8 text") from None


def _typed(value: object, kind: type | object, what: str) -> object:
    """The value if absent or of the given type; a boolean is never a number or an id."""
    if value is None or (isinstance(value, kind) and (kind is bool or not isinstance(value, bool))):
        return value
    raise RunRecordError(f"{what} must be {getattr(kind, '__name__', kind)}, "
                         f"got {type(value).__name__}")


def _required(value: object, kind: type | object, what: str) -> object:
    """The value, which must be present and of the given type."""
    if value is None:
        raise RunRecordError(f"{what} is required")
    return _typed(value, kind, what)


def _resolve(value: str, key: str, base: str) -> str:
    """A path relative to the record's folder, resolved; an absolute path is refused."""
    resolved = primitives.path_in_folder(base, value)
    if resolved is None:
        raise RunRecordError(f"`{key}` {value!r} is an absolute path; write it relative to the "
                             "run record's folder")
    return resolved
