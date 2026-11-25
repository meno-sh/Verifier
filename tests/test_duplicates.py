#!/usr/bin/env python3
"""Check the proposed equivalence classes of quotes: core/claim_steps/duplicates.py.

The cases cover the settings, the prompt text, the check of the items, and each part of
the search (identical texts, BM25 and rare-number short lists, blocks of 2, the block
reply, the both-sides vote, strict grouping) with its calls. The model call
is a fake that gives fixed replies; the retries of models.ask_and_read are tested in
test_alerts.py, and `vcheck propose-classes` in test_claim_rounds.py. No network is used.
Returns 1 if any case fails, so it can gate a commit.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.claim_steps import duplicates
from core.project import watched_project
from utils import models  # noqa: E402

FAILED: list[str] = []

WORDS = {"quote:a-brier": "On dataset A the calibrated forecaster's Brier score is 0.181.",
         "quote:a-brier-again": "The calibrated forecaster scores a Brier of 0.181 on A.",
         "quote:a-brier-table": "Calibrated, dataset A: Brier 0.181 (5 seeds).",
         "class:b-brier": "On dataset B the calibrated forecaster's Brier score is 0.205."}
IDS = sorted(WORDS)


def _refused(fn) -> bool:
    """Whether fn raises models.BadReply."""
    try:
        fn()
    except models.BadReply:
        return True
    return False


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def raises(error: type[BaseException], call, *args, **kwargs) -> str | None:
    """The message of the `error` that call(*args) raises, or None when it raises none."""
    try:
        call(*args, **kwargs)
    except error as e:
        return str(e)
    return None


def numbers(prompt: str) -> dict[str, int]:
    """The number of each statement in a prompt, by its words."""
    out = {}
    for line in prompt.split("\n\n")[0].splitlines()[1:]:
        n, words = line.split(". ", 1)
        out[words] = int(n)
    return out


class FakeModel:
    """models.ask_and_read with fixed answers, naming statements by their words.

    A call gets `by_order[order]` (order: the tuple of the statements' words in its prompt)
    or `propose`: a list of equivalence classes, each a list of words. With `together`,
    each call waits until that many run at once.
    """

    def __init__(self, propose: list | None = None, by_order: dict | None = None,
                 together: int = 0) -> None:
        self.propose, self.by_order, self.calls = propose or [], by_order or {}, []
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(together, timeout=10) if together else None

    def __call__(self, prompt: str, model: str, read, timeout: int = 300, *,
                 system: str | None = None, reasoning: str | None = None,
                 read_attempts: int = models.READ_ATTEMPTS) -> object:
        with self.lock:
            self.calls.append({"prompt": prompt, "model": model, "timeout": timeout,
                               "system": system, "reasoning": reasoning,
                               "read_attempts": read_attempts})
        if self.barrier is not None:
            self.barrier.wait()     # raises BrokenBarrierError if the calls run one by one
        number = numbers(prompt)
        classes = self.by_order.get(tuple(number), self.propose)
        return read(json.dumps({"classes": [[number[w] for w in p] for p in classes]}))


def test_settings() -> None:
    """The defaults, the run options that change them, and the values they refuse."""
    expect("the default is haiku 5.5 at effort medium, 180 s",
           duplicates.Settings.from_options({})
           == duplicates.Settings("https://openrouter.ai/api/v1/chat/completions"
                                  "#anthropic/claude-haiku-5.5", "medium", 180))
    options = {duplicates.MODEL.key: "m", duplicates.EFFORT.key: "high",
               duplicates.TIMEOUT.key: 30}
    expect("each run option changes its setting",
           duplicates.Settings.from_options(options) == duplicates.Settings("m", "high", 30))
    expect("a timeout that is not a whole number is refused",
           "must be int" in (raises(TypeError, lambda: duplicates.Settings.from_options(
               {duplicates.TIMEOUT.key: "soon"})) or ""))
    expect("the settings are watched run options, set in .verifier/options.json",
           {duplicates.MODEL.key, duplicates.EFFORT.key, duplicates.TIMEOUT.key}
           <= set(watched_project.WatchedProject.load(Path("/nonexistent"),
                                                    Path("/nonexistent/.verifier"))
                  .check_registry().run_option_catalog.options))
    for bad, field in ((("m", "huge", 30), "effort"), (("m", "low", 0), "timeout"),
                       ((" ", "low", 30), "model")):
        expect(f"an invalid {field} is refused",
               field in (raises(duplicates.DuplicatesError, duplicates.Settings, *bad) or ""))


def test_prompt() -> None:
    """The system message holds the rules and nothing of one project; bad items are refused."""
    system = duplicates.NEIGHBOURS_SYSTEM
    expect("the system message defines duplicates, and holds nothing of one project",
           duplicates.RULES in system and "0.181" not in system
           and all(w in system for w in ("There can be duplicates among the statements",
                                         "says all that the other statement says, and more",
                                         "narrower condition", "a conclusion that rests on")))
    for words, fragment in (({"": "words"}, "id"), ({"x": " "}, "no words")):
        expect(f"an item with an empty {fragment.split()[-1]} is refused",
               fragment in (raises(duplicates.DuplicatesError, duplicates.checked_ids, words)
                            or ""))
    unused = FakeModel()
    for words in ({}, {"x": "one item"}):
        expect(f"{len(words)} item(s): no call and no class",
               duplicates.propose(words, duplicates.Settings("m", "low", 45), unused) == []
               and not unused.calls)


SHORT = {"a": "On dataset A the calibrated forecaster's Brier score is 0.181.",
         "b": "The calibrated forecaster scores a Brier of 18.1% on dataset A.",
         "c": "On dataset A the calibrated forecaster's Brier score is 0.181.",
         "d": "On dataset B the calibrated forecaster's Brier score is 0.205.",
         "e": "The learning rate is 3e-4 for every run of the forecaster.",
         "f": "We train for 40 epochs on four GPUs with a batch of 256."}


def test_search_parts() -> None:
    """Each part of the search on small inputs, with no model call."""
    expect("identical texts after cleaning are a pair; other texts are not",
           duplicates.identical_pairs({**SHORT, "g": "- on dataset A the calibrated "
                                       "forecaster's *Brier* score is 0.181"})
           == {("a", "c"), ("a", "g"), ("c", "g")})
    expect("0.181 and 18.1% share a number key; 4, 40 and the year 2024 give none",
           duplicates.number_keys("0.181") & duplicates.number_keys("18.1%")
           and not duplicates.number_keys("4 runs in 2024") and duplicates.number_keys("40"))
    expect("items sharing a rare number are paired", ("a", "b") in duplicates.number_pairs(SHORT))
    scores = duplicates.bm25_scores(SHORT)
    lone = duplicates.bm25_scores({**SHORT, "g": "Zebras graze quietly."})
    expect("BM25 scores are symmetric, higher for the near-copy, and absent with no shared word",
           scores["a"]["d"] == scores["d"]["a"] and scores["a"]["c"] > scores["a"]["d"]
           and not lone["g"])
    lists = duplicates.neighbour_lists(SHORT)
    expect("a short list puts number-sharing items first (c, b), then by score",
           lists["a"][:2] == ["c", "b"] and len(lists["a"]) <= duplicates.CAP, repr(lists["a"]))
    blocks = duplicates.make_blocks(lists)
    expect("blocks hold 2 items, a quote and its nearest free candidate, each item once",
           all(len(b) <= duplicates.BLOCK for b in blocks) and blocks[0] == ["a", "c"]
           and sorted(x for b in blocks for x in b) == sorted(lists), repr(blocks))
    cands = duplicates.block_candidates(["a", "b"], lists)
    text = duplicates.block_text(SHORT, ["a", "b"], cands)
    expect("a block call names the Qs, then each candidate once, then the reply rule",
           text.startswith("Statements Q:\nQ1. On dataset A") and "Q2. The calibrated" in text
           and len(cands) == len(set(cands)) and text.endswith(duplicates.REPLY_ONLY))
    reply = json.dumps({"Q1": [cands.index("c") + 1], "Q2": [str(cands.index("a") + 1)]})
    answer = duplicates.read_block(reply, ["a", "b"], cands)
    expect("a block reply gives yes or no for every candidate of each Q, but not for itself",
           answer["a"]["c"] and not answer["a"]["b"] and answer["b"]["a"]
           and "a" not in answer["a"])
    for rule, reply in (('no list for "Q2"', '{"Q1": []}'),
                        ("is not a candidate number", '{"Q1": [99], "Q2": []}'),
                        ("is not a candidate number", '{"Q1": [true], "Q2": []}'),
                        ("one object", "[1, 2]")):
        message = raises(models.BadReply, duplicates.read_block, reply, ["a", "b"], cands)
        expect(f"a block reply that breaks a rule is refused: {rule}",
               message is not None and rule in message, repr(message))
    words = {"x": "alpha one", "y": "alpha two", "z": "alpha three"}
    both = duplicates.decide(words, {"x": {"y": True, "z": True}, "y": {"x": False, "z": True},
                                     "z": {"x": True, "y": True}})
    expect("a pair counts only if every call that judged it said yes, and a group never "
           "holds a pair that is not a yes",
           both == [frozenset({"y", "z"})] or both == [frozenset({"x", "z"})], repr(both))
    chain = duplicates.decide(words, {"x": {"y": True}, "y": {"z": True}})
    expect("strict grouping does not chain x=y and y=z into one group without x=z",
           len(chain) == 1 and len(next(iter(chain))) == 2, repr(chain))
    expect("identical texts are grouped with no answer at all",
           duplicates.decide({"p": "Same text.", "q": "same text"}, {}) == [frozenset({"p", "q"})])
    one_sided = {"x": {"y": True}}
    expect("a pair whose other vote is missing (its call failed) is not a duplicate",
           duplicates.decide(words, one_sided) and
           duplicates.decide(words, one_sided, unjudged={("x", "y")}) == [])
    expect("two settled items are never grouped, even with identical texts",
           duplicates.decide({"p": "Same text.", "q": "same text"}, {},
                             settled=frozenset({"p", "q"})) == [])
    expect("a settled item is never in a block",
           all("a" not in b for b in duplicates.make_blocks(lists, frozenset({"a"}))))
    big = {f"i{k}": f"common words here and item number {k} with extra {k % 7}" for k in range(200)}
    kept = duplicates.bm25_scores(big)
    expect("BM25 keeps at most KEEP scores per item in all (not every pair), symmetric",
           sum(map(len, kept.values())) <= 2 * duplicates.KEEP * len(big)
           and all(kept[j][i] == s for i, r in kept.items() for j, s in r.items()))
    expect("a reply number written as a superscript digit is refused, not a crash",
           _refused(lambda: duplicates.read_block('{"Q1": ["\u00b2"]}', ["a"], ["b", "c"])))


class FakeBlocks:
    """models.ask_and_read for block calls: a candidate is a duplicate of a Q when both name
    dataset A. With `fail`, the calls for blocks whose first Q holds that text raise."""

    def __init__(self, fail: str = "") -> None:
        self.calls, self.fail, self.lock = [], fail, threading.Lock()

    def __call__(self, prompt: str, model: str, read, timeout: int = 300, *,
                 system: str | None = None, reasoning: str | None = None,
                 read_attempts: int = models.READ_ATTEMPTS) -> object:
        with self.lock:
            self.calls.append({"prompt": prompt, "model": model, "system": system,
                               "reasoning": reasoning, "timeout": timeout})
        head, rest = prompt.split("\n\nCandidates:\n")
        qs = [line.split(". ", 1)[1] for line in head.splitlines()[1:]]
        cands = [line.split(". ", 1)[1] for line in rest.split("\n\n")[0].splitlines()]
        if self.fail and self.fail in qs[0]:
            raise models.ProviderError("the provider answered HTTP 500")
        reply = {f"Q{j}": [i for i, c in enumerate(cands, 1)
                           if c != q and "dataset A" in q and "dataset A" in c]
                 for j, q in enumerate(qs, 1)}
        return read(json.dumps(reply))


def test_search_calls() -> None:
    """The search end to end with a fake model: calls, settings and the grouping."""
    ask, count = FakeBlocks(), duplicates.CallCount()
    settings = duplicates.Settings("m", "low", 45)
    out = duplicates.propose(SHORT, settings, ask, call_count=count)
    expect("the dataset-A statements in one class, dataset B and the rest alone",
           out == [frozenset({"a", "b", "c"})], repr(out))
    expect("one call per block, each with the model, effort, timeout and the block system "
           "message, and the counts reported",
           len(ask.calls) == count.calls == len(duplicates.make_blocks(
               duplicates.neighbour_lists(SHORT))) and count.failed == 0
           and all((c["model"], c["reasoning"], c["timeout"], c["system"])
                   == ("m", "low", 45, duplicates.NEIGHBOURS_SYSTEM) for c in ask.calls))
    # 1 failed call of TEN's 10 is at the limit; of SHORT's 3 it would be refused
    failing, count = FakeBlocks(fail="scores a Brier"), duplicates.CallCount()
    out = duplicates.propose(TEN, settings, failing, call_count=count)
    expect("a failed call is left out and counted: it can miss a duplicate but adds none",
           count.failed == 1 and all(g <= {"a", "b", "c"} for g in out), repr(out))
    expect("a later round asks no call about two settled items only",
           all(not set(c["prompt"].split("Candidates:")[0]) & set() for c in ask.calls))
    settled = frozenset({"d", "e", "f"})
    ask2 = FakeBlocks()
    duplicates.propose(SHORT, settings, ask2, settled=settled)
    expect("settled items are never a Q: blocks hold only the new items",
           all(not any(SHORT[s] in c["prompt"].split("Candidates:")[0] for s in settled)
               for c in ask2.calls) and ask2.calls)
    try:
        duplicates.propose(SHORT, settings, FakeBlocks(fail=" "),
                           call_count=duplicates.CallCount())
        refused = False
    except duplicates.DuplicatesError:
        refused = True
    expect("every call failing refuses the proposal instead of writing a near-empty one",
           refused)
    unused = FakeBlocks()
    expect("one item: no call and no class",
           duplicates.propose({"x": "one item"}, settings, unused) == [] and not unused.calls)


# SHORT plus 14 unrelated items: 10 blocks, so one failed call is exactly 10% of them
TEN = {**SHORT, **{f"g{k:02d}": f"Ablation {k} removes module number {k} from the encoder."
                   for k in range(14)}}


class StagedBlocks(FakeBlocks):
    """FakeBlocks whose calls can wait or raise, keyed by the words of the block's first Q.

    `wait`: words whose call waits on `release` (up to `hold` seconds) before it answers.
    `error`: words whose call raises that error at once. `after`: words -> words whose
    call must finish first, so calls can be made to finish in any chosen order.
    """

    def __init__(self, wait: str = "", error: tuple[str, BaseException] | None = None,
                 after: dict | None = None, hold: float = 3.0) -> None:
        super().__init__()
        self.wait, self.error, self.after, self.hold = wait, error, after or {}, hold
        self.release, self.finished = threading.Event(), {}
        self.done, self.order = {}, []

    def __call__(self, prompt: str, model: str, read, timeout: int = 300, **kw) -> object:
        first = prompt.split("\n")[1].split(". ", 1)[1]
        if self.error and self.error[0] == first:
            raise self.error[1]
        if first == self.wait:
            self.release.wait(self.hold)
        if first in self.after:
            self.done.setdefault(self.after[first], threading.Event()).wait(5)
        out = super().__call__(prompt, model, read, timeout, **kw)
        with self.lock:
            self.finished[first] = True
            self.order.append(first)
        self.done.setdefault(first, threading.Event()).set()
        return out


def test_search_stops() -> None:
    """A fatal error in one block call stops the run at once, without waiting for others."""
    settings = duplicates.Settings("m", "low", 45)
    for error in (models.KeyMissing("OPENROUTER_API_KEY is not set"),
                  RuntimeError("a bug in the reader")):
        ask = StagedBlocks(wait=SHORT["a"], error=(SHORT["b"], error))
        start = time.monotonic()
        raised = raises(type(error), duplicates.propose, SHORT, settings, ask,
                        call_count=duplicates.CallCount())
        took = time.monotonic() - start
        slow_done = SHORT["a"] in ask.finished
        ask.release.set()
        expect(f"{type(error).__name__} in the 2nd block surfaces while the 1st block's "
               "call still runs",
               raised is not None and took < 1.0 and not slow_done,
               f"raised={raised!r}, took {took:.2f} s, first call finished={slow_done}")


def test_search_order() -> None:
    """The proposal does not depend on the order in which the block calls finish."""
    settings = duplicates.Settings("m", "low", 45)
    blocks = duplicates.make_blocks(duplicates.neighbour_lists(TEN))
    firsts = [TEN[b[0]] for b in blocks]
    seen = []    # what decide gets: the answers in their dict order, and the unjudged pairs
    decide = duplicates.decide

    def watched(statements, answers, unjudged=frozenset(), settled=frozenset(), values=None):
        seen.append(([(q, list(a.items())) for q, a in answers.items()], sorted(unjudged)))
        return decide(statements, answers, unjudged, settled, values)

    duplicates.decide = watched
    try:
        plain = duplicates.propose(TEN, settings, FakeBlocks())
        # each block waits for the one after it, so the calls finish last block first
        backwards = StagedBlocks(after=dict(zip(firsts, firsts[1:], strict=False)))
        out = duplicates.propose(TEN, settings, backwards)
    finally:
        duplicates.decide = decide
    expect("the grouping gets the same answers in the same order (block order) either way",
           len(seen) == 2 and seen[0] == seen[1])
    expect("calls that finish in reverse order give the same proposal, in the same order",
           backwards.order == firsts[::-1] and out == plain
           and [list(p) for p in out[0]] == [list(p) for p in plain[0]],
           f"{out!r} vs {plain!r}")


def test_search_fail_limit() -> None:
    """More than FAIL_MAX of the block calls failing refuses the proposal; FAIL_MAX does not."""
    settings = duplicates.Settings("m", "low", 45)
    message = raises(duplicates.DuplicatesError, duplicates.propose, SHORT, settings,
                     FakeBlocks(fail="scores a Brier"), call_count=duplicates.CallCount())
    expect("1 failed call of 3 (33%) is above 10%: refused, and the message says the rule",
           message is not None and "1 of 3 model calls failed" in message
           and "more than 10%" in message, repr(message))
    count = duplicates.CallCount()
    message = raises(duplicates.DuplicatesError, duplicates.propose, TEN, settings,
                     FakeBlocks(fail="scores a Brier"), call_count=count)
    expect("1 failed call of 10 (exactly 10%) is not above the limit: a proposal is made",
           message is None and (count.calls, count.failed) == (10, 1), f"{message!r} {count}")


def main() -> int:
    """Run every group of cases and report the failures."""
    for group in (test_settings, test_prompt, test_search_parts,
                  test_search_calls, test_search_stops, test_search_order,
                  test_search_fail_limit):
        print(group.__name__)
        group()
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll proposal cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
