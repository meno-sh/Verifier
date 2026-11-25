#!/usr/bin/env python3
"""Check the full-paper linker of stage 5 (core/claim_steps/full_paper_linker.py).

  - the linker shows all 37 labelled examples of the 4 pilot papers;
  - the linker links each claim with no source, not a table claim, not yet asked with this register;
  - each claim gets 2 calls in one conversation, with the paper as the shared prefix and the
    examples and the register after it, with their own cache mark;
  - a reply with a register number out of range is refused;
  - a failed claim is a `warn` and is not saved;
  - the links go into the register, with an empty `last_updated`; a link that closes a cycle
    stays;
  - a link to a claim before it in the register order is dropped, with at most 5 `warn`
    records in one run.

No model is called. Returns 1 if any case fails, so it can gate a commit.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.claim_steps import full_paper_linker as L  # noqa: E402
from utils import alerts, claim_files, models, shared_paper_prefix  # noqa: E402

FAILED: list[str] = []
PAPER = "Retrieval adds 4 points on QA. Table 1 shows the 7B model at 68.4%."
CLAIMS = [{"id": "helps", "statement": "Retrieval helps."},
          {"id": "gain", "statement": "Retrieval adds 4 points on QA."},
          {"id": "t#tab-1", "statement": "Table 1: Accuracy. Columns: Model | Acc."},
          {"id": "acc", "statement": "The 7B model reaches 68.4%.", "source": {
              "kind": "artifact", "path": "runs/a.json"}}]
SETTINGS = L.FullPaperLinkerSettings(model="m", tables=False,
                                     workers=2, timeout=9)


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def raises(error: type[Exception], call) -> bool:
    """True when `call()` raises `error`."""
    try:
        call()
    except error:
        return True
    return False


def test_examples() -> None:
    """The linker shows all 37 labelled examples of the 4 pilot papers."""
    examples = L.labelled_examples()
    expect("the file holds 37 examples of the 4 pilot papers, each with a label and a reason",
           len(examples) == 37
           and {e["paper"] for e in examples} == {"2ySt3cdGfJ", "1R5BcYS8EC", "AjXkRZIvjB",
                                                  "9WbNpRuFuS"}
           and all(e["label"] in ("link", "do not link") and e["reason"] for e in examples))
    text = L.render(examples)
    expect("the examples are rendered with their label and reason",
           text.startswith("Example 1\nClaim: ") and "Example 37\n" in text
           and "Correct label: " in text, text[:200])
    expect("there is no option to pick an example set",
           all("example_set" not in o.key for o in L.RUN_OPTIONS))


def test_choice_and_parse() -> None:
    """Which claims are linked, and which replies are read."""
    checksum = L.register_checksum(CLAIMS)
    todo = L.claims_to_link(CLAIMS, False, frozenset(), checksum)
    expect("only claims with no source and not of a table are linked",
           [c["id"] for c in todo] == ["helps", "gain"], repr(todo))
    expect("a claim asked with this register is not asked again",
           [c["id"] for c in L.claims_to_link(CLAIMS, False, frozenset({("helps", checksum)}),
                                              checksum)] == ["gain"])
    expect("a claim asked with another register is asked again",
           len(L.claims_to_link(CLAIMS, False, frozenset({("helps", "old")}), checksum)) == 2)
    expect("with tables on, a table claim is linked too",
           "t#tab-1" in [c["id"] for c in L.claims_to_link(CLAIMS, True, frozenset(), checksum)])
    good = 'Points: {"points": [{"point": "p", "quote": "q", "place": "s", "items": [1]}]}'
    expect("a reply with one JSON object is read", L.parse(good, 4)[0]["items"] == [1])
    expect("a register number out of range is refused",
           raises(models.BadReply, lambda: L.parse(good.replace("[1]", "[9]"), 4)))
    expect("a reply with no JSON is refused", raises(models.BadReply, lambda: L.parse("no", 4)))
    for reply in ('{"points":[{"point":"x"}]}', '{"points":"ab"}', '{"points":[1]}'):
        expect(f"a reply of the wrong form is refused, so the model is asked again: {reply}",
               raises(models.BadReply, lambda reply=reply: L.parse(reply, 4)))


class FakeModel:
    """models.ask and models.ask_and_read: records each call; step 2 links to `items`."""

    def __init__(self, items: dict[str, list[int]], fail: str | None = None) -> None:
        self.calls: list[dict] = []
        self.items, self.fail = items, fail

    def ask(self, prompt: str, model: str, timeout: int = 300, **kw: object) -> str:
        self.calls.append({"prompt": prompt, **kw})
        claim = prompt.split("\n")[0].removeprefix("Claim: ")
        if claim == self.fail:
            raise models.ProviderError("down")
        return f"pieces of {claim}"

    def ask_and_read(self, prompt: str, model: str, read, timeout: int = 300, **kw: object):
        self.calls.append({"prompt": prompt, **kw})
        claim = kw["earlier_turns"][0][1].removeprefix("pieces of ")
        own = next(c["id"] for c in CLAIMS if c["statement"] == claim)
        return read(json.dumps({"points": [{"point": "p", "quote": "q", "place": "s",
                                             "items": self.items[own]}]}))


def test_link_claims(folder: Path) -> None:
    """The 2 calls of each claim, the saved answers, and a failed claim."""
    alerts.configure(folder / "alerts.jsonl", "test", folder)
    fake = FakeModel({"helps": [1, 0], "gain": []}, fail=None)
    real = models.ask, models.ask_and_read
    models.ask, models.ask_and_read = fake.ask, fake.ask_and_read
    saved: list[dict] = []
    try:
        todo = L.claims_to_link(CLAIMS, False, frozenset(), L.register_checksum(CLAIMS))
        first_run = L.link_claims(PAPER, CLAIMS, todo, SETTINGS, saved.append)
        failing = FakeModel({"helps": [1], "gain": []}, fail="Retrieval adds 4 points on QA.")
        models.ask, models.ask_and_read = failing.ask, failing.ask_and_read
        saved_after_failure: list[dict] = []
        counts = L.link_claims(PAPER, CLAIMS, todo, SETTINGS, saved_after_failure.append)
    finally:
        models.ask, models.ask_and_read = real
    expect("each claim is linked, and the claim itself is never its own link",
           (first_run.linked, first_run.failed, first_run.links_against_order) == (2, 0, 0)
           and {r["claim"]: r["links"] for r in saved} == {"helps": ["gain"], "gain": []},
           repr(saved))
    expect("each answer is saved with the checksum of the register",
           all(r["register_sha256"] == L.register_checksum(CLAIMS) for r in saved))
    prefix = shared_paper_prefix.shared_paper_prefix_for_model_calls(PAPER)
    expect("every call sends the paper as the shared prefix, and marks the system for the cache",
           all(c["shared_prefix_for_cache"] == prefix and c["cache_system_after_shared_prefix"]
               for c in fake.calls), repr(fake.calls[0])[:300])
    system = fake.calls[0]["system"]
    expect("the system part has the examples, then the numbered register, and not the paper",
           system.index("LABELLED EXAMPLES") < system.index("CLAIM REGISTER:\n[0] Retrieval")
           and PAPER not in system, system[:200])
    step2 = [c for c in fake.calls if "earlier_turns" in c]
    expect("step 2 asks only for items with a number above the claim's own number",
           all("Give only items with a number above the claim's own number" in c["prompt"]
               for c in step2), repr(step2)[:300])
    expect("step 2 sends step 1 and its reply as an earlier turn",
           len(step2) == 2 and all(c["earlier_turns"][0][0].startswith("Claim: ")
                                   for c in step2), repr(step2)[:300])
    log = [r for r in alerts.read(folder / "alerts.jsonl")
           if r["key"] == "full_paper_linker.failed"]
    expect("a failed claim is a warn and is not saved; the other claim is saved",
           (counts.linked, counts.failed) == (1, 1)
           and [r["claim"] for r in saved_after_failure] == ["helps"]
           and len(log) == 1 and log[0]["level"] == "warn", repr((counts, log)))


def test_links_against_order(folder: Path) -> None:
    """A link to a claim before it in the register is dropped; at most 5 warns in one run."""
    alerts.configure(folder / "order-alerts.jsonl", "test", folder)
    row, dropped = L.drop_links_against_order({"claim": "gain", "links": ["helps", "acc"]},
                                              CLAIMS)
    expect("the link to the claim before is dropped, and the link to the claim after stays",
           row["links"] == ["acc"] and dropped == ["helps"]
           and row["links_against_order"] == ["helps"], repr(row))
    claims = [{"id": f"k{n}", "statement": f"Claim {n}."} for n in range(8)]
    fake = FakeModel({}, fail=None)

    def ask_and_read(prompt: str, model: str, read, timeout: int = 300, **kw: object):
        own = int(kw["earlier_turns"][0][1].removeprefix("pieces of Claim ").rstrip("."))
        return read(json.dumps({"points": [{"point": "p", "quote": "q", "place": "s",
                                             "items": list(range(own))}]}))
    real = models.ask, models.ask_and_read
    models.ask, models.ask_and_read = fake.ask, ask_and_read
    saved: list[dict] = []
    try:
        counts = L.link_claims(PAPER, claims, claims, SETTINGS, saved.append)
    finally:
        models.ask, models.ask_and_read = real
    warns = [r for r in alerts.read(folder / "order-alerts.jsonl")
             if r["key"] == "full_paper_linker.link_against_order"]
    expect("every link goes back in the order, so all 28 are dropped, with 5 warns",
           counts.links_against_order == 28 and len(warns) == 5
           and all(r["level"] == "warn" for r in warns)
           and all(r["links"] == [] for r in saved), repr((counts, len(warns))))


def test_apply_links() -> None:
    """The links go into the register; every link stays, also one that closes a cycle."""
    register = [{"id": "a", "statement": "A.", "last_updated": "x"},
                {"id": "b", "statement": "B.", "last_updated": "x",
                 "source": {"kind": "claims", "claims": ["c"]}},
                {"id": "c", "statement": "C.", "last_updated": "x"},
                {"id": "d", "statement": "D.", "last_updated": "x",
                 "source": {"kind": "artifact", "path": "x.json"}}]
    rows = [{"claim": "a", "links": ["b"]}, {"claim": "c", "links": ["a", "d"]},
            {"claim": "d", "links": ["a"]}]
    changed = L.apply_links(register, rows)
    by_id = {c["id"]: c for c in register}
    expect("a claim with no source gets its links and an empty last_updated",
           by_id["a"]["source"] == {"kind": "claims", "claims": ["b"]}
           and by_id["a"]["last_updated"] == claim_files.NEW, repr(by_id["a"]))
    expect("a link that closes a cycle (c -> a -> b -> c) stays",
           by_id["c"]["source"] == {"kind": "claims", "claims": ["a", "d"]}, repr(by_id["c"]))
    expect("a claim with a source keeps it", by_id["d"]["source"]["kind"] == "artifact")
    expect("2 claims changed", changed == 2, repr(changed))
    expect("a link to a claim that is not in the register is refused",
           raises(ValueError, lambda: L.apply_links(
               [{"id": "e", "statement": "E."}], [{"claim": "e", "links": ["gone"]}])))


def test_register_text_table_cap() -> None:
    """A table claim shows at most TABLE_STATEMENT_CHARS characters in the linker's register."""
    claims = [{"id": "q-p.tex#tab-1", "statement": "T" * 5000},
              {"id": "q-p.tex#s-1", "statement": "S" * 5000}]
    lines = L.register_text(claims).splitlines()
    expect("a table claim is cut at the cap and a sentence claim is not",
           len(lines[0]) == len("[0] ") + claim_files.TABLE_STATEMENT_CHARS
           and len(lines[1]) == len("[1] ") + 5000, repr([len(x) for x in lines]))


def main() -> int:
    """Run every group of cases and report the failures."""
    with tempfile.TemporaryDirectory() as tmp:
        for group in (test_examples, test_choice_and_parse,
                      lambda: test_link_claims(Path(tmp)),
                      lambda: test_links_against_order(Path(tmp)), test_apply_links,
                      test_register_text_table_cap):
            group()
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll full-paper linker cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
