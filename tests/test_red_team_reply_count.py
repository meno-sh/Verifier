#!/usr/bin/env python3
"""Check that the prompt red team reads a reply with the wrong number of proposals.

The prompt asks for exactly N proposals. A reply with fewer gives all of them, a reply
with more gives its first N, and a reply with none is refused. The model call is a fake
that gives fixed replies, with no network. Returns 1 if any case fails, so it can gate a
commit.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checks.uncertainty import red_team, uncertainty  # noqa: E402
from checks.uncertainty.red_team import reply  # noqa: E402
from utils import (
    alerts,  # noqa: E402
    models,  # noqa: E402
)

# reply.read() writes a warn when it leaves out a broken proposal
_ALERTS = tempfile.mkdtemp()
alerts.configure(Path(_ALERTS) / "alerts.jsonl", "test", Path(_ALERTS))

FAILED: list[str] = []
M = "https://openrouter.ai/api/v1/chat/completions#m"     # a model name; no call is made
ASKED = 8       # the default red_team_proposals
RESULTS = 2
CLAIM = uncertainty.Premise(id="h-retrieval", statement="Retrieval helps on every task.",
                            value=None)
# the full paper, the one premise the check gives the red team (uncertainty.paper_premise)
PREMISES = (uncertainty.paper_premise("Retrieval adds 4 points on QA."),)


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def proposal(k: int) -> dict:
    """Proposal number k of a reply, as JSON values, with one result."""
    return {"hypotheticals": [{"design": f"Design {k}.", "result": f"Result {k}."}]}


def reply_with(n: int) -> str:
    """A reply that keeps every rule except the count: n proposals."""
    return json.dumps({"proposals": [proposal(k) for k in range(1, n + 1)]})


def designs(attacks: tuple) -> list[str]:
    """The design of the first result of each proposal, in order."""
    return [a.hypotheticals[0].design for a in attacks]


class FakeAskAndRead:
    """models.ask_and_read with fixed replies: records each call and reads the next reply."""

    def __init__(self, *replies: str) -> None:
        self.replies, self.calls = list(replies), 0

    def __call__(self, prompt: str, model: str, read, timeout: int = 300, *,
                 system: str | None = None, reasoning: str | None = None, **kw) -> object:
        self.calls += 1
        return read(self.replies.pop(0))


def test_counts() -> None:
    """Replies with one fewer, exactly N, and one more proposal are read."""
    for n, want in [(ASKED - 1, ASKED - 1), (ASKED, ASKED), (ASKED + 1, ASKED)]:
        got = reply.read(reply_with(n), ASKED, RESULTS)
        expect(f"a reply with {n} proposals gives {want}, in order",
               designs(got) == [f"Design {k}." for k in range(1, want + 1)], repr(designs(got)))
    one = reply.read(reply_with(1), ASKED, RESULTS)
    expect("a reply with 1 proposal gives 1", len(one) == 1, repr(one))


def test_empty() -> None:
    """A reply with no proposals is refused, naming the rule."""
    try:
        reply.read(reply_with(0), ASKED, RESULTS)
        expect("a reply with 0 proposals is refused", False, "no error raised")
    except models.BadReply as e:
        expect("a reply with 0 proposals is refused, naming the rule",
               "at least 1 item, not 0" in str(e), str(e))


def test_other_rules() -> None:
    """The other rules still hold: a broken proposal is left out, and all broken is refused."""
    items = [proposal(k) for k in range(1, ASKED + 1)]
    items[2] = {"hypotheticals": []}
    got = reply.read(json.dumps({"proposals": items}), ASKED, RESULTS)
    expect("a broken proposal among the first N is left out", len(got) == ASKED - 1, repr(got))
    try:
        reply.read(json.dumps({"proposals": [{"hypotheticals": []}] * ASKED}), ASKED, RESULTS)
        expect("a reply with every proposal broken is refused", False, "no error raised")
    except models.BadReply as e:
        expect("a reply with every proposal broken is refused, naming the first",
               "proposal 1" in str(e), str(e))
    extra = [proposal(k) for k in range(1, ASKED + 1)] + [{"hypotheticals": []}]
    got = reply.read(json.dumps({"proposals": extra}), ASKED, RESULTS)
    expect("a proposal after the first N is not read, so a broken one there is no error",
           len(got) == ASKED, repr(got))


def test_attack() -> None:
    """The red team returns a short reply's proposals after one request, with no retry."""
    ask = FakeAskAndRead(reply_with(ASKED - 1))
    team = red_team.PromptRedTeam(model=M, proposals=ASKED, results=RESULTS, iterations=1,
                                  ask_and_read=ask)
    got = team.attack(CLAIM, PREMISES, lambda attack: None)
    expect("one round with a 7-proposal reply gives 7 proposals from one request",
           len(got) == ASKED - 1 and ask.calls == 1, f"{len(got)} proposals, {ask.calls} calls")


def main() -> int:
    """Run every group of cases and report the failures."""
    for group in (test_counts, test_empty, test_other_rules, test_attack):
        print(group.__name__)
        group()
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll red team reply count cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
