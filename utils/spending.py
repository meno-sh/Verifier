"""What the Verifier's own model calls cost, one record per reply.

utils/models.py records every reply that reports its usage. A caller names its purpose
with `purpose("method_writer")`, so that the costs of each check can be told apart. The
records go in `spending.jsonl`, beside the alert log of the process (utils/alerts.py),
so a watched project keeps them in `.verifier/runtime/`. OpenRouter gives the cost in
dollars in each reply; a host that gives no cost (a server of our own) has `cost` None.
The auditing agent's own calls go through OpenCode, not this module; the daemon reads
their cost from OpenCode's database (runtime/agent/opencode.py).
"""
from __future__ import annotations

import collections
import contextlib
import contextvars
import dataclasses
import os
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from utils import alerts, file_storage, text

FILE = "spending.jsonl"
UNNAMED = "unnamed"         # the purpose of a call made outside every `purpose` block

_PURPOSE: contextvars.ContextVar[str | None] = contextvars.ContextVar("purpose", default=None)


class SpendingError(ValueError):
    """A spending record or a reply's usage is not in the form this module writes or reads."""


@dataclasses.dataclass
class Total:
    """The calls of one purpose, and what they cost."""
    calls: int = 0
    cost: float = 0.0
    unknown: int = 0        # calls whose reply gave no cost
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0           # prompt tokens the provider read from its cache
    cache_write_prompt_tokens: int = 0      # prompt tokens the provider wrote to its cache


@contextlib.contextmanager
def purpose(name: str) -> Iterator[None]:
    """Record the model calls made inside the block under `name`, such as "method_writer"."""
    if not name or not name.replace("_", "").isalnum() or name != name.lower():
        raise ValueError(f"a purpose is a lower-case name, such as method_writer, got {name!r}")
    token = _PURPOSE.set(name)
    try:
        yield
    finally:
        _PURPOSE.reset(token)


def current_purpose() -> str | None:
    """The purpose of the `purpose` block this code runs in, or None outside every block."""
    return _PURPOSE.get()


def log_path(alert_log: Path) -> Path:
    """The spending log beside the alert log `alert_log`."""
    return alert_log.with_name(FILE)


def record(model: str, payload: dict) -> dict | None:
    """Append the usage of one reply to the spending log, and return the record.

    A reply with no `usage` (an error reply) is not recorded. A reply whose usage is not
    an object, or has a negative or non-numeric cost or token count, raises.

    Raises:
        SpendingError: the usage is not in the form OpenAI-style replies give it.
        AlertError: alerts.configure() was not called, so there is no log to write beside.
    """
    usage = payload.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise SpendingError(f"{model} gave usage that is not an object: {usage!r}")
    cost = usage.get("cost")
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, int | float)
                             or cost < 0):
        raise SpendingError(f"{model} gave a cost that is not a number of dollars: {cost!r}")
    tokens = {}
    for key in ("prompt_tokens", "completion_tokens"):
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SpendingError(f"{model} gave {key} {value!r}, not a count")
        tokens[key] = value
    tokens.update(_cache_tokens(model, usage))
    log = alerts.configured()
    entry = {"ts": int(time.time() * 1000), "process": log.process, "pid": os.getpid(),
             "purpose": _PURPOSE.get() or UNNAMED, "model": model,
             "cost": None if cost is None else float(cost), **tokens}
    if isinstance(usage.get("cost_basis"), str):    # such as list price, for a CLI call
        entry["cost_basis"] = usage["cost_basis"]
    file_storage.AppendOnlyJsonLinesFile(log_path(log.path)).append_row(entry)
    return entry


# The cache counts of a reply: the key in OpenRouter's `usage.prompt_tokens_details`, and the
# key of the spending record.
CACHE_TOKEN_KEYS = (("cached_tokens", "cached_prompt_tokens"),
                    ("cache_write_tokens", "cache_write_prompt_tokens"))


def _cache_tokens(model: str, usage: dict) -> dict[str, int]:
    """The prompt tokens read from and written to the provider's cache, as far as the reply says.

    A provider that gives no `prompt_tokens_details`, or no count in it, says nothing about
    its cache, so the record has no such key.

    Raises:
        SpendingError: the details are not an object, or a count is not a count.
    """
    details = usage.get("prompt_tokens_details")
    if details is None:
        return {}
    if not isinstance(details, dict):
        raise SpendingError(f"{model} gave prompt_tokens_details that is not an object: "
                            f"{details!r}")
    counts = {}
    for reply_key, record_key in CACHE_TOKEN_KEYS:
        value = details.get(reply_key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SpendingError(f"{model} gave {reply_key} {value!r}, not a count")
        counts[record_key] = value
    return counts


def read(path: Path) -> list[dict]:
    """Every record of the spending log, oldest first; none when there is no log.

    The last line can be partial while a writer appends it, so it is left out.

    Raises:
        SpendingError: a line other than the last is not a record.
    """
    try:
        rows = file_storage.AppendOnlyJsonLinesFile(path).read_rows_with_line_numbers()
    except file_storage.DamagedJsonLineError as e:
        raise SpendingError(str(e)) from None
    out = []
    for i, entry in rows:
        if not {"ts", "purpose", "model", "cost"} <= set(entry):
            raise SpendingError(f"{path}:{i} is not a spending record: "
                                f"{text.first_part(str(entry), 200)}")
        out.append(entry)
    return out


def totals(entries: Iterable[dict], since_ms: int = 0) -> dict[str, Total]:
    """The calls and cost of each purpose, from the records at or after `since_ms`."""
    out: dict[str, Total] = collections.defaultdict(Total)
    for e in entries:
        if e["ts"] < since_ms:
            continue
        t = out[e["purpose"]]
        t.calls += 1
        t.prompt_tokens += e.get("prompt_tokens", 0)
        t.completion_tokens += e.get("completion_tokens", 0)
        t.cached_prompt_tokens += e.get("cached_prompt_tokens", 0)
        t.cache_write_prompt_tokens += e.get("cache_write_prompt_tokens", 0)
        if e["cost"] is None:
            t.unknown += 1
        else:
            t.cost += e["cost"]
    return dict(out)
