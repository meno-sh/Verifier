#!/usr/bin/env python3
"""Check the reordering stage (core/claim_steps/claim_reordering.py).

  - the limits of the first half are one third and two thirds, rounded to allow more, and
    each half keeps at least one claim;
  - a reply is read only when it has every id once, in order, with only the two fields, a
    true or false mark, and a first half inside the limits;
  - a part of the result list writes into the list, and each item is written once;
  - the sort asks the model with the schema, the short ids and null marks, at effort high
    with the output limit, and gives the claims in the model's order;
  - no call waits for its halves, so a pool of one thread sorts too;
  - a refused reply is a `warn` and is asked again; 5 refused replies in a row are `fatal`;
  - the order goes into the register, with the claims with no statement last, and a register
    that changed during the sort is refused;
  - the checksum of the last sort skips a sort of the same claims in the same order.

No model is called. Returns 1 if any case fails, so it can gate a commit.
"""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.claim_steps import claim_reordering as R  # noqa: E402
from utils import alerts, models  # noqa: E402

FAILED: list[str] = []
SETTINGS = R.ClaimReorderingSettings(model="m", effort="high", max_output_tokens=64000,
                                     pool_size=4, timeout=9)


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def raises(error: type[BaseException], call) -> bool:
    """True when `call()` raises `error`."""
    try:
        call()
    except error:
        return True
    return False


def claims_of(values: list[int]) -> list[dict]:
    """One claim for each value; a higher value is a more valuable claim."""
    return [{"id": f"q{v}", "statement": f"Claim of value {v}."} for v in values]


def reply_of(marks: list[object], ids: list[str] | None = None) -> str:
    """A reply that gives `marks`, with the ids c1, c2, ... unless `ids` are given."""
    ids = ids or [f"c{n}" for n in range(1, len(marks) + 1)]
    return json.dumps([{"id": i, "ranks_in_first_half": m}
                       for i, m in zip(ids, marks, strict=True)])


class FakeModel:
    """models.ask for the sort: the first half is the half of the claims with the highest values.

    `bad_replies` refused replies come first. Each call is recorded.
    """

    def __init__(self, bad_replies: int = 0) -> None:
        self.calls: list[dict] = []
        self.bad_replies = bad_replies
        self.lock = threading.Lock()

    def ask(self, prompt: str, model: str, timeout: int = 300, **kw: object) -> str:
        with self.lock:
            self.calls.append({"prompt": prompt, "model": model, **kw})
            if self.bad_replies:
                self.bad_replies -= 1
                return "not JSON"
        shown = json.loads(prompt.split("The claims:\n", 1)[1].split("\n\nYOUR LAST REPLY")[0])
        values = [int(c["statement"].split()[-1].rstrip(".")) for c in shown]
        top = sorted(values, reverse=True)[:(len(values) + 1) // 2]
        return reply_of([v in top for v in values])


def test_limits_and_reading() -> None:
    """The limits of the first half, and which replies are read."""
    limits = {n: R.first_half_count_limits(n) for n in range(2, 8)}
    expect("the limits are a third and two thirds, rounded to allow more, never 0 or all",
           limits == {2: (1, 1), 3: (1, 2), 4: (1, 3), 5: (1, 4), 6: (2, 4), 7: (2, 5)},
           repr(limits))
    expect("a list of one claim is not split", raises(ValueError,
                                                      lambda: R.first_half_count_limits(1)))
    expect("a good reply is read", R.read_split(reply_of([True, False, False]), 3)
           == [True, False, False])
    expect("a good reply in a json fence is read",
           R.read_split("```json\n" + reply_of([False, True]) + "\n```", 2) == [False, True])
    bad = {"too short": reply_of([True, False]),
           "an id out of order": reply_of([True, False, False], ["c2", "c1", "c3"]),
           "an id twice": reply_of([True, False, False], ["c1", "c1", "c3"]),
           "a mark that is not true or false": reply_of([True, None, False]),
           "a mark as text": reply_of([True, "false", False]),
           "no claim in the first half": reply_of([False, False, False]),
           "every claim in the first half": reply_of([True, True, True]),
           "the statement given back": json.dumps(
               [{"id": f"c{n}", "ranks_in_first_half": n == 1, "statement": "s"}
                for n in (1, 2, 3)]),
           "not a list": json.dumps({"c1": True}),
           "text around the JSON": "Here: " + reply_of([True, False, False])}
    for name, reply in bad.items():
        expect(f"a reply with {name} is refused",
               raises(models.BadReply, lambda reply=reply: R.read_split(reply, 3)))


def test_writable_list_slice() -> None:
    """A part of the result list writes into the list, once for each item."""
    result: list[dict | None] = [None] * 5
    whole = R.WritableListSlice(result)
    first, second = whole[:2], whole[2:]
    second[:1][0] = {"id": "x"}
    second[1:][1] = {"id": "z"}
    first[1] = {"id": "b"}
    expect("each part writes at its place in the list",
           [r and r["id"] for r in result] == [None, "b", "x", None, "z"], repr(result))
    expect("an item is written once", raises(ValueError, lambda: first.__setitem__(1, {})))
    expect("an index outside the part is refused",
           raises(IndexError, lambda: first.__setitem__(2, {})))
    expect("a part with a step is refused", raises(ValueError, lambda: whole[::2]))


def test_sort(folder: Path) -> None:
    """The sort asks as it must, gives the model's order, and works with one thread."""
    alerts.configure(folder / "alerts.jsonl", "test", folder)
    values = [3, 9, 1, 7, 5, 8, 2, 6, 4, 10, 0]
    fake = FakeModel()
    out = R.ClaimQuicksort(SETTINGS, fake.ask).sort_claims(claims_of(values))
    expect("the claims come in the model's order, the most valuable first",
           [c["id"] for c in out] == [f"q{v}" for v in sorted(values, reverse=True)],
           repr([c["id"] for c in out]))
    first = fake.calls[0]
    expect("each call asks at effort high, with the output limit",
           all(c["reasoning"] == "high" and c["max_output_tokens"] == 64000
               and c["model"] == "m" for c in fake.calls), repr(first))
    prompt = first["prompt"]
    shown = json.loads(prompt.split("The claims:\n", 1)[1])
    expect("the prompt has the schema, the limits and each claim with a short id and a null mark",
           '"ranks_in_first_half"' in prompt and '"additionalProperties": false' in prompt
           and "Mark approximately 50% of the claims as true; large deviations from this "
               "count will be rejected." in prompt
           and shown[0] == {"id": "c1", "statement": "Claim of value 3.",
                            "ranks_in_first_half": None}
           and len(shown) == len(values), prompt[-600:])
    expect("a list of one claim, or of none, calls no model",
           [c["id"] for c in R.ClaimQuicksort(SETTINGS, fake.ask).sort_claims(claims_of([4]))]
           == ["q4"] and R.ClaimQuicksort(SETTINGS, fake.ask).sort_claims([]) == [])
    one_thread = R.ClaimReorderingSettings(model="m", effort="high", max_output_tokens=64000,
                                           pool_size=1, timeout=9)
    out = R.ClaimQuicksort(one_thread, FakeModel().ask).sort_claims(claims_of(values))
    expect("a pool of one thread sorts too, because no call waits for its halves",
           [c["id"] for c in out] == [f"q{v}" for v in sorted(values, reverse=True)])
    expect("two claims with one id are refused",
           raises(ValueError, lambda: R.ClaimQuicksort(SETTINGS, fake.ask).sort_claims(
               claims_of([1, 1]))))


def test_refused_replies(folder: Path) -> None:
    """A refused reply is a warn and is asked again; 5 in a row are fatal."""
    log = folder / "refused.jsonl"
    alerts.configure(log, "test", folder)
    fake = FakeModel(bad_replies=2)
    out = R.ClaimQuicksort(SETTINGS, fake.ask).sort_claims(claims_of([1, 2]))
    records = alerts.read(log)
    warns = [r for r in records if r["key"] == "claim_reordering.bad_split"]
    expect("2 refused replies are 2 warns, and the third reply sorts",
           [c["id"] for c in out] == ["q2", "q1"] and len(warns) == 2
           and all(r["level"] == "warn" for r in warns) and len(fake.calls) == 3,
           repr((out, warns)))
    expect("the prompt after a refused reply gives the error",
           "YOUR LAST REPLY BROKE A RULE" in fake.calls[1]["prompt"])
    failing = FakeModel(bad_replies=5)
    stopped = raises(alerts.Fatal, lambda: R.ClaimQuicksort(SETTINGS, failing.ask)
                     .sort_claims(claims_of([1, 2, 3])))
    records = alerts.read(log)
    fatal = [r for r in records if r["key"] == "claim_reordering.split_failed"]
    expect("5 refused replies in a row stop the sort with one fatal record, after 4 warns",
           stopped and len(failing.calls) == 5 and len(fatal) == 1
           and fatal[0]["level"] == "fatal"
           and len([r for r in records if r["key"] == "claim_reordering.bad_split"]) == 2 + 4,
           repr(fatal))


def test_apply_order_and_checksum(folder: Path) -> None:
    """The order goes into the register; the checksum skips a second sort of the same order."""
    register = [{"id": "a", "statement": "A."}, {"id": "art", "value": 3.0},
                {"id": "b", "statement": "B."}, {"id": "c", "statement": "C."}]
    sorted_claims = [register[3], register[0], register[2]]
    R.apply_order(register, sorted_claims)
    expect("the sorted claims come first, and a claim with no statement comes last",
           [e["id"] for e in register] == ["c", "a", "b", "art"], repr(register))
    changed = [{"id": "a", "statement": "A, changed."}, {"id": "c", "statement": "C."},
               {"id": "b", "statement": "B."}]
    expect("a register whose claims changed during the sort is refused",
           raises(R.ClaimReorderingError, lambda: R.apply_order(changed, sorted_claims)))
    claims = R.sortable_claims(register)
    expect("before a sort, the claims are not sorted", not R.is_sorted_already(folder, claims))
    R.save_sorted_checksum(folder, claims)
    expect("after a sort, the same claims in the same order are sorted",
           R.is_sorted_already(folder, claims))
    expect("a new claim, or a new order, needs a new sort",
           not R.is_sorted_already(folder, [*claims, {"id": "d", "statement": "D."}])
           and not R.is_sorted_already(folder, list(reversed(claims))))


def test_settings() -> None:
    """The settings refuse values that cannot work."""
    expect("an effort that is not an effort is refused",
           raises(ValueError, lambda: R.ClaimReorderingSettings(
               model="m", effort="huge", max_output_tokens=1, pool_size=1, timeout=9)))
    expect("a pool of 0 threads is refused",
           raises(ValueError, lambda: R.ClaimReorderingSettings(
               model="m", effort="high", max_output_tokens=1, pool_size=0, timeout=9)))
    defaults = R.ClaimReorderingSettings.from_options({})
    expect("the defaults are Haiku at effort high, a pool of 64 and a high output limit",
           defaults.model == models.preset.HAIKU and defaults.effort == "high"
           and defaults.pool_size == 64 and defaults.max_output_tokens == 64000,
           repr(defaults))
    body = models.request_body("q", "m", reasoning="high", max_output_tokens=64000)
    expect("the request has the output limit as max_tokens",
           body["max_tokens"] == 64000 and body["reasoning"] == {"effort": "high"}, repr(body))


def main() -> int:
    """Run every group of cases and report the failures."""
    with tempfile.TemporaryDirectory() as tmp:
        for group in (test_limits_and_reading, test_writable_list_slice,
                      lambda: test_sort(Path(tmp)), lambda: test_refused_replies(Path(tmp)),
                      lambda: test_apply_order_and_checksum(Path(tmp)), test_settings):
            group()
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll claim reordering cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
