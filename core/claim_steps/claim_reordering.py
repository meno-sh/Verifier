"""The reordering stage: a model sorts the claims of the register, most valuable to a reader first.

`vcheck reorder-claims` runs it after the agent's round, before `vcheck link-claims`. The
sort is a quicksort. Each step shows a model the claims as a JSON list, and the model marks
the claims that rank in the first half. A claim that is high-level, general and of direct,
significant value ranks first. No claim of the second half may rest on evidence from a claim
of the first half, so the linker never links a claim to a claim before it
(core/claim_steps/full_paper_linker.py). The two halves are sorted in the same way, in one
thread pool, and no step waits for the steps under it: each step writes its claims into its
own part of the result list (WritableListSlice).
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from core.claim_steps import full_paper_linker
from core.registry import run_options
from utils import alerts, checksums, claim_files, file_storage, models, spending

PURPOSE = "claim_reordering"            # the spending purpose of the sort's calls
ORDER_FILE = "claim_order.json"         # in the state folder: the checksum of the last sort
FAILURES_BEFORE_STOP = 5                # refused replies in a row that stop the sort
# One entry of the claim register, as claim_files reads it.
ClaimEntry = dict[str, Any]
ID_PREFIX = "c"                         # the short id of each claim in a prompt: c1, c2, ...

MODEL = run_options.RunOption(
    key="claim_reordering_model", kind=str, flag="--claim-reordering-model",
    default=models.preset.HAIKU,
    help="the model `vcheck reorder-claims` asks (utils/models.py names)")
EFFORT = run_options.RunOption(
    key="claim_reordering_effort", kind=str, flag="--claim-reordering-effort", default="high",
    choices=models.EFFORTS, help="the reasoning effort of each call of `vcheck reorder-claims`")
MAX_OUTPUT_TOKENS = run_options.RunOption(
    key="claim_reordering_max_output_tokens", kind=int,
    flag="--claim-reordering-max-output-tokens", default=64000,
    help="the most tokens of one reply of `vcheck reorder-claims`, with its reasoning")
POOL_SIZE = run_options.RunOption(
    key="claim_reordering_pool_size", kind=int, flag="--claim-reordering-pool-size", default=64,
    help="the threads of the one pool in which `vcheck reorder-claims` sorts the parts")
TIMEOUT = run_options.RunOption(
    key="claim_reordering_timeout", kind=int, flag="--claim-reordering-timeout", default=900,
    help="seconds `vcheck reorder-claims` waits for one reply")
RUN_OPTIONS = (MODEL, EFFORT, MAX_OUTPUT_TOKENS, POOL_SIZE, TIMEOUT)

# The JSON schema of the reply, as the prompt shows it.
REPLY_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "ranks_in_first_half": {"type": "boolean"}},
        "required": ["id", "ranks_in_first_half"],
        "additionalProperties": False,
    },
}

SPLIT_PROMPT = """You help to sort the claims of a research paper. The sorted list puts first \
the claims that are high-level, general and of direct, significant value to a reader. It puts \
last the claims that are low-level, specific, or of value only as support for other claims.

Below is a JSON list of {count} claims. Each claim has a short id, its statement, and the field \
"ranks_in_first_half", which is null now. Decide for each claim if it ranks in the first half of \
the sorted list. Set the field to true for a claim in the first half, and to false for a claim \
in the second half.

Obey these rules:
1. Put first the claims that are high-level, general and of direct, significant value.
2. No claim in the second half may rely on evidential support from any claim in the first \
half. When claim A gives evidence for claim B, A must not rank before B: put B in the first half, \
or put both in the same half.
3. The claims in the first half must be of more interest to a casual reader than the claims in \
the second half.
4. Mark approximately 50% of the claims as true; large deviations from this count will be \
rejected.

Reply with one JSON list and nothing else. The list has one element for each claim, in the same \
order as the input, with the same ids. Each element has only the fields "id" and \
"ranks_in_first_half". Do not give the statements. The JSON schema of the reply:
{schema}

The claims:
{claims}"""


class ClaimReorderingError(ValueError):
    """The register changed while the model sorted it, so the order is not written."""


@dataclasses.dataclass(frozen=True)
class ClaimReorderingSettings:
    """The settings of one `vcheck reorder-claims`, from the run options."""
    model: str
    effort: str
    max_output_tokens: int
    pool_size: int
    timeout: int

    def __post_init__(self) -> None:
        """Refuse a bad model, effort or timeout, or a limit or pool size below 1."""
        problem = models.settings_problem(self.model, self.effort, self.timeout,
                                          ("claim_reordering_model", "claim_reordering_effort",
                                           "claim_reordering_timeout"))
        if problem is not None:
            raise ValueError(problem)
        if self.max_output_tokens < 1 or self.pool_size < 1:
            raise ValueError("claim_reordering_max_output_tokens and claim_reordering_pool_size "
                             f"must be at least 1, got {self.max_output_tokens} and "
                             f"{self.pool_size}")

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> ClaimReorderingSettings:
        """The settings that the run options give."""
        model, effort = MODEL.value_in_options(options), EFFORT.value_in_options(options)
        tokens = MAX_OUTPUT_TOKENS.value_in_options(options)
        pool_size, timeout = POOL_SIZE.value_in_options(options), TIMEOUT.value_in_options(options)
        assert isinstance(model, str) and isinstance(effort, str), (model, effort)
        assert isinstance(tokens, int) and isinstance(pool_size, int), (tokens, pool_size)
        assert isinstance(timeout, int), timeout
        return cls(model=model, effort=effort, max_output_tokens=tokens, pool_size=pool_size,
                   timeout=timeout)


class WritableListSlice:
    """A part of a list that a writer fills in place: item k of the part is item start + k.

    A Python slice of a list is a copy, so a write to it does not reach the list. Each step of
    the sort gets one of these, and the slice of one, `writes_to[:k]`, is again a part of the
    same list. Each item is written once.
    """

    def __init__(self, backing_list: list[ClaimEntry | None], start: int = 0,
                 length: int | None = None) -> None:
        """The part of `backing_list` from `start`, `length` items long (to its end when None).

        Raises:
            ValueError: the part is not inside the list.
        """
        length = len(backing_list) - start if length is None else length
        if start < 0 or length < 0 or start + length > len(backing_list):
            raise ValueError(f"the part {start}..{start + length} is not inside a list of "
                             f"{len(backing_list)} items")
        self.backing_list, self.start, self.length = backing_list, start, length

    def __len__(self) -> int:
        """The number of items of the part."""
        return self.length

    def __getitem__(self, part: slice) -> WritableListSlice:
        """The part of this part that `part` names, which writes into the same list.

        Raises:
            ValueError: `part` has a step other than 1.
        """
        first, stop, step = part.indices(self.length)
        if step != 1:
            raise ValueError(f"a part of the result list has a step of 1, not {step}")
        return WritableListSlice(self.backing_list, self.start + first, max(0, stop - first))

    def __setitem__(self, index: int, claim: ClaimEntry) -> None:
        """Write `claim` into item `index` of the part.

        Raises:
            IndexError: `index` is not an item of the part.
            ValueError: the item was written before.
        """
        if not 0 <= index < self.length:
            raise IndexError(f"item {index} is not in a part of {self.length} items")
        if self.backing_list[self.start + index] is not None:
            raise ValueError(f"item {self.start + index} of the result list is written twice")
        self.backing_list[self.start + index] = claim


def first_half_count_limits(count: int) -> tuple[int, int]:
    """The least and the most claims of `count` that a reply may mark as in the first half.

    The limits are one third and two thirds, both included and rounded to the side that
    allows more. Each half must keep at least one claim, or the sort cannot end, so the
    limits of 2 claims are 1 and 1.

    Raises:
        ValueError: `count` is below 2.
    """
    if count < 2:
        raise ValueError(f"a list of {count} claims is not split")
    return max(1, count // 3), min(count - 1, math.ceil(2 * count / 3))


def claim_text(claim: ClaimEntry) -> str:
    """The statement of a claim as the prompt gives it: a table claim is cut at the cap.

    A whole table is cut at claim_files.TABLE_STATEMENT_CHARS characters, as in the linker.
    """
    statement = claim["statement"]
    assert isinstance(statement, str) and statement, claim
    return (statement[:claim_files.TABLE_STATEMENT_CHARS]
            if full_paper_linker.is_table(claim["id"]) else statement)


def split_prompt(claims: Sequence[ClaimEntry]) -> str:
    """The prompt that asks which claims of `claims` rank in the first half.

    The prompt asks for about half; read_split accepts first_half_count_limits.
    """
    first_half_count_limits(len(claims))         # refuses a list that is not split
    shown = [{"id": f"{ID_PREFIX}{n}", "statement": claim_text(c), "ranks_in_first_half": None}
             for n, c in enumerate(claims, 1)]
    return SPLIT_PROMPT.format(count=len(claims),
                               schema=json.dumps(REPLY_SCHEMA, indent=2),
                               claims=json.dumps(shown, indent=2, ensure_ascii=False))


def read_split(reply: str, count: int) -> list[bool]:
    """For each of `count` claims, in order, True when the reply ranks it in the first half.

    Raises:
        models.BadReply: the reply is not one JSON list, its length is not `count`, an
            element has other fields than id and ranks_in_first_half, the ids are not c1 to
            c<count> in order, a mark is not true or false, or the number of claims marked
            true is outside first_half_count_limits.
    """
    value = models.read_json(reply)
    if not isinstance(value, list):
        raise models.BadReply(f"the reply is a {type(value).__name__}, not a JSON list")
    if len(value) != count:
        raise models.BadReply(f"the reply has {len(value)} elements, not {count}")
    marks = []
    for n, element in enumerate(value, 1):
        if not isinstance(element, dict) or set(element) != {"id", "ranks_in_first_half"}:
            raise models.BadReply(f"element {n} must have only the fields id and "
                                  f"ranks_in_first_half: {element!r}")
        if element["id"] != f"{ID_PREFIX}{n}":
            raise models.BadReply(f"element {n} has the id {element['id']!r}, not "
                                  f"{ID_PREFIX}{n}: give every id once, in the input order")
        if not isinstance(element["ranks_in_first_half"], bool):
            raise models.BadReply(f"element {n}: ranks_in_first_half must be true or false, "
                                  f"not {element['ranks_in_first_half']!r}")
        marks.append(element["ranks_in_first_half"])
    least, most = first_half_count_limits(count)
    if not least <= sum(marks) <= most:
        raise models.BadReply(f"the reply marks {sum(marks)} of {count} claims as in the first "
                              f"half; mark at least {least} and at most {most}")
    return marks


class ClaimQuicksort:
    """Sorts claims with a model, most valuable to a reader first, in one pool of threads.

    Each call of quicksort_claims runs in the pool. It asks the model for the split, puts
    the calls of the two halves into the pool, and returns at once. The first error of any
    call stops the sort: no call starts a new half, and sort_claims raises it.
    """

    def __init__(self, settings: ClaimReorderingSettings,
                 ask: Callable[..., str] | None = None) -> None:
        """A sort with `settings`; `ask` is models.ask unless a test gives another."""
        self.settings = settings
        self.ask = models.ask if ask is None else ask
        self.lock = threading.Lock()
        self.pending_calls = 0
        self.first_error: BaseException | None = None
        self.all_calls_done = threading.Event()
        self.pool: concurrent.futures.ThreadPoolExecutor | None = None

    def sort_claims(self, claims: Sequence[ClaimEntry]) -> list[ClaimEntry]:
        """`claims` in the order of the model, most valuable to a reader first.

        Each ClaimQuicksort sorts once, because the state of the sort is its own.

        Raises:
            RuntimeError: this ClaimQuicksort sorted before.
            alerts.Fatal: FAILURES_BEFORE_STOP replies in a row for one split were refused.
            models.ProviderError: a model call failed after its retries.
            ValueError: two claims have the same id.
        """
        if self.pool is not None:
            raise RuntimeError("a ClaimQuicksort sorts once; make a new one for each sort")
        ids = [c["id"] for c in claims]
        if len(set(ids)) != len(ids):
            raise ValueError(f"the claims to sort repeat an id: {ids}")
        if not claims:
            return []
        result: list[ClaimEntry | None] = [None] * len(claims)
        self.pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.settings.pool_size, thread_name_prefix="claim-quicksort")
        try:
            self.put_call_in_pool(list(claims), WritableListSlice(result))
            self.all_calls_done.wait()
        finally:
            self.pool.shutdown(wait=True, cancel_futures=True)
        if self.first_error is not None:
            raise self.first_error
        written = [c for c in result if c is not None]
        if sorted(c["id"] for c in written) != sorted(ids):
            raise AssertionError(f"the sort wrote {len(written)} of {len(claims)} claims, or "
                                 "not the same claims")
        return written

    def put_call_in_pool(self, claims: list[ClaimEntry], writes_to: WritableListSlice) -> None:
        """Put one call of quicksort_claims into the pool, unless an error stopped the sort."""
        assert self.pool is not None, "sort_claims makes the pool"
        with self.lock:
            if self.first_error is not None:
                return
            self.pending_calls += 1
            future = self.pool.submit(self.quicksort_claims, claims, writes_to)
        future.add_done_callback(self.note_call_done)      # outside the lock: it can run here

    def note_call_done(self, future: concurrent.futures.Future[None]) -> None:
        """Count one call as done; keep its error, if it is the first, and stop the sort."""
        error = None if future.cancelled() else future.exception()
        with self.lock:
            self.pending_calls -= 1
            if error is not None and self.first_error is None:
                self.first_error = error
            if self.pending_calls == 0 or self.first_error is not None:
                self.all_calls_done.set()

    def quicksort_claims(self, claims: list[ClaimEntry], writes_to: WritableListSlice) -> None:
        """Sort `claims` into `writes_to`: one claim is written; more are split in two halves.

        The halves keep the order of `claims`, and each goes back into the pool with its
        own part of `writes_to`. This call does not wait for them.
        """
        if len(claims) != len(writes_to) or not claims:
            raise ValueError(f"{len(claims)} claims cannot be written into {len(writes_to)} items")
        if len(claims) == 1:
            writes_to[0] = claims[0]
            return
        marks = self.split_in_halves(claims)
        first_half = [c for c, first in zip(claims, marks, strict=True) if first]
        second_half = [c for c, first in zip(claims, marks, strict=True) if not first]
        self.put_call_in_pool(first_half, writes_to[:len(first_half)])
        self.put_call_in_pool(second_half, writes_to[len(first_half):])

    def split_in_halves(self, claims: Sequence[ClaimEntry]) -> list[bool]:
        """Ask the model which claims rank in the first half, until a reply passes read_split.

        Each refused reply is a `warn` in the alert log, and the prompt is sent again with the
        error. FAILURES_BEFORE_STOP refused replies in a row are a `fatal` record.

        Raises:
            alerts.Fatal: FAILURES_BEFORE_STOP replies in a row were refused.
            models.ProviderError: a model call failed after its retries.
        """
        prompt = split_prompt(claims)
        sent = prompt
        for attempt in range(1, FAILURES_BEFORE_STOP + 1):
            with spending.purpose(PURPOSE):
                reply = self.ask(sent, self.settings.model, self.settings.timeout,
                                 reasoning=self.settings.effort,
                                 max_output_tokens=self.settings.max_output_tokens)
            try:
                return read_split(reply, len(claims))
            except models.BadReply as e:
                if attempt == FAILURES_BEFORE_STOP:
                    raise alerts.fail(
                        "claim_reordering.split_failed",
                        f"{FAILURES_BEFORE_STOP} replies in a row for a split of "
                        f"{len(claims)} claims were refused; the last: {e}", error=e,
                        claims=[c["id"] for c in claims], reply=reply) from e
                alerts.notify("warn", "claim_reordering.bad_split",
                              f"{self.settings.model}: {e}", error=e, attempt=attempt,
                              of=FAILURES_BEFORE_STOP, claims=len(claims), reply=reply)
                sent = prompt + models.RETRY_NOTE.format(error=e)
        raise AssertionError("unreachable: the loop returns or raises")


def sortable_claims(register: Sequence[object]) -> list[ClaimEntry]:
    """The claims of the register that have a statement, in register order.

    A claim with no statement, such as an artifact claim with only a value, cannot be shown
    to the model, so it is not sorted.
    """
    return [e for e in register
            if isinstance(e, dict) and "id" in e and isinstance(e.get("statement"), str)
            and e["statement"]]


def order_checksum(claims: Sequence[ClaimEntry]) -> str:
    """The checksum of the ids and statements of `claims`, in order."""
    return checksums.sha256_of_json([[c["id"], c["statement"]] for c in claims])


def is_sorted_already(state_dir: Path, claims: Sequence[ClaimEntry]) -> bool:
    """True when the last sort wrote these claims, with these statements, in this order."""
    saved = file_storage.JsonFile(state_dir / ORDER_FILE)
    if not saved.exists():
        return False
    value = saved.read()
    assert isinstance(value, dict) and isinstance(value.get("sorted_sha256"), str), value
    return bool(value["sorted_sha256"] == order_checksum(claims))


def save_sorted_checksum(state_dir: Path, claims: Sequence[ClaimEntry]) -> None:
    """Note that the register holds `claims` in the order of the last sort."""
    file_storage.JsonFile(state_dir / ORDER_FILE, indent=2).write(
        {"sorted_sha256": order_checksum(claims)})


def apply_order(register: list[ClaimEntry], sorted_claims: Sequence[ClaimEntry]) -> None:
    """Put the claims of `sorted_claims` first in `register`, in that order, then the others.

    The other entries (claims with no statement) keep their order, after the sorted claims.

    Raises:
        ClaimReorderingError: the register's sortable claims are not those of `sorted_claims`
            with the same statements, because the register changed during the sort.
    """
    now = sortable_claims(register)
    if sorted(order_checksum([c]) for c in now) != sorted(
            order_checksum([c]) for c in sorted_claims):
        raise ClaimReorderingError("the register changed while the model sorted its claims, "
                                   "so the order is not written; the next round sorts again")
    by_id = {c["id"]: c for c in now}
    sorted_ids = {c["id"] for c in sorted_claims}
    others = [e for e in register if not (isinstance(e, dict) and e.get("id") in sorted_ids)]
    register[:] = [by_id[c["id"]] for c in sorted_claims] + others
