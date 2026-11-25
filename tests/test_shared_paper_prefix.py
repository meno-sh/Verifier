#!/usr/bin/env python3
"""Check the shared paper prefix, its cache marks, the first-call-alone rule and the cache counts.

  - the prefix of one paper is the same bytes every time, without the typesetting commands;
  - an empty paper or a paper above MAX_PAPER_TOKENS is refused, never cut;
  - a request puts the prefix first, with the only 1-hour cache mark; the system part after it
    has a 5-minute mark only when the caller asks for it; earlier turns come before the prompt;
  - the first call with a prefix and a model goes alone; the others wait until it finishes;
  - a failed first call lets the next call go alone; an old prefix goes alone again;
  - the spending log keeps the cached and the cache-write prompt tokens of a reply;
  - a project of Markdown notes only gives its notes as the paper, each after a line that
    names its file; one file is its text alone.

No model is called. Returns 1 if any case fails, so it can gate a commit.
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.project import watched_project  # noqa: E402
from utils import alerts, claim_files, models, shared_paper_prefix, spending, structure  # noqa: E402

FAILED: list[str] = []
PAPER = ("\\documentclass{article}\n\\usepackage{amsmath}\n"
         "Retrieval adds 4 points on QA.\n\n\n\nEnd.")


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


def test_prefix() -> None:
    """The prefix is fixed bytes, without typesetting commands; bad papers are refused."""
    first = shared_paper_prefix.shared_paper_prefix_for_model_calls(PAPER)
    expect("the same paper gives the same prefix",
           first == shared_paper_prefix.shared_paper_prefix_for_model_calls(PAPER))
    expect("the prefix starts with the fixed sentence",
           first.startswith(shared_paper_prefix.FIRST_SENTENCE + "\n\n"), first[:80])
    expect("the typesetting commands are removed",
           "\\usepackage" not in first and "\\documentclass" not in first, first)
    expect("3 or more blank lines become 1", "\n\n\n" not in first, repr(first))
    expect("an empty paper is refused",
           raises(ValueError,
                  lambda: shared_paper_prefix.shared_paper_prefix_for_model_calls(" \n")))
    too_long = "x" * ((shared_paper_prefix.MAX_PAPER_TOKENS + 1)
                      * shared_paper_prefix.CHARACTERS_PER_TOKEN)
    expect("a paper above the token limit is refused, not cut",
           raises(shared_paper_prefix.PaperTooLongForModelContext,
                  lambda: shared_paper_prefix.shared_paper_prefix_for_model_calls(too_long)))
    at_limit = "x" * (shared_paper_prefix.MAX_PAPER_TOKENS
                      * shared_paper_prefix.CHARACTERS_PER_TOKEN)
    expect("a paper at the token limit is kept whole",
           shared_paper_prefix.shared_paper_prefix_for_model_calls(at_limit).endswith(at_limit))


def test_request_body() -> None:
    """The prefix goes first with the only 1-hour mark; the system mark is asked for."""
    body = models.request_body("Q", "m", system="S", shared_prefix_for_cache="P")
    blocks = body["messages"][0]["content"]
    expect("the prefix is the first block, with the 1-hour mark",
           blocks[0] == {"type": "text", "text": "P",
                         "cache_control": {"type": "ephemeral", "ttl": "1h"}}, str(blocks))
    expect("the system part after the prefix has no mark by default",
           blocks[1] == {"type": "text", "text": "S"}, str(blocks))
    marked = models.request_body("Q", "m", system="S", shared_prefix_for_cache="P",
                                 cache_system_after_shared_prefix=True)["messages"][0]["content"]
    expect("the system part has a 5-minute mark when asked for",
           marked[1] == {"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}},
           str(marked))
    turns = models.request_body("Q2", "m", system="S", shared_prefix_for_cache="P",
                                earlier_turns=(("Q1", "A1"),))["messages"]
    expect("earlier turns come after the system part and before the prompt",
           [(m["role"], m["content"]) for m in turns[1:]]
           == [("user", "Q1"), ("assistant", "A1"), ("user", "Q2")], str(turns))
    plain = models.request_body("Q", "m", system="S")["messages"][0]["content"]
    expect("with no prefix the system part keeps its 5-minute mark",
           plain == [{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}],
           str(plain))
    expect("a system mark with no prefix is refused",
           raises(ValueError, lambda: models.request_body(
               "Q", "m", system="S", cache_system_after_shared_prefix=True)))


def test_first_call_alone() -> None:
    """The first call goes alone; a failed one lets the next go alone; an old prefix waits again."""
    gate = models.FirstCallAloneGate()
    running, most_at_once, lock = [0], [0], threading.Lock()
    order: list[str] = []

    def send(name: str, seconds: float) -> str:
        with lock:
            running[0] += 1
            most_at_once[0] = max(most_at_once[0], running[0])
            order.append(name)
        time.sleep(seconds)
        with lock:
            running[0] -= 1
        return name

    first = threading.Thread(target=lambda: gate.call("m", "P", lambda _: send("first", 0.3)))
    first.start()
    time.sleep(0.05)
    others = [threading.Thread(target=lambda n=n: gate.call("m", "P", lambda _: send(f"o{n}", 0.1)))
              for n in range(4)]
    for t in others:
        t.start()
    time.sleep(0.1)
    expect("the other calls wait while the first call runs", order == ["first"], str(order))
    for t in [first, *others]:
        t.join()
    expect("after the first call, the others run together", most_at_once[0] > 1,
           f"at most {most_at_once[0]} at once")
    expect("a call with no prefix never waits", gate.call("m", None, lambda _: "free") == "free")

    failing = models.FirstCallAloneGate()

    def fail(waited: float) -> str:
        raise models.ProviderError("down")

    expect("a failed first call raises its error",
           raises(models.ProviderError, lambda: failing.call("m", "P", fail)))
    expect("after a failed first call, the prefix is not taken as cached",
           ("m", models.hashlib.sha256(b"P").hexdigest()) not in failing.last_finished_call)

    now = [0.0]
    aging = models.FirstCallAloneGate(clock=lambda: now[0])
    aging.call("m", "P", lambda _: "x")
    pair = ("m", models.hashlib.sha256(b"P").hexdigest())
    expect("a recent prefix needs no first call", aging._wait_for_turn(pair) is None)
    now[0] = models.SHARED_PREFIX_SURELY_CACHED_SECONDS + 1
    expect("an old prefix goes alone again", aging._wait_for_turn(pair) is not None)

    slow, waited = models.FirstCallAloneGate(), []
    first = threading.Thread(target=lambda: slow.call("m", "P", lambda _: time.sleep(0.5)))
    first.start()
    time.sleep(0.05)
    slow.call("m", "P", waited.append)
    first.join()
    expect("a waiting call gets the seconds it waited, so its deadline moves by them",
           waited and 0.3 < waited[0] < 2.0, repr(waited))


def test_cache_counts(folder: Path) -> None:
    """The spending log keeps the cache counts of a reply, and refuses a bad count."""
    alerts.configure(folder / "alerts.jsonl", "test", folder)
    usage = {"prompt_tokens": 100, "completion_tokens": 5, "cost": 0.01,
             "prompt_tokens_details": {"cached_tokens": 90, "cache_write_tokens": 0}}
    entry = spending.record("m", {"usage": usage})
    expect("the record has the cached prompt tokens",
           entry is not None and entry["cached_prompt_tokens"] == 90
           and entry["cache_write_prompt_tokens"] == 0, str(entry))
    total = spending.totals(spending.read(spending.log_path(folder / "alerts.jsonl")))
    expect("the totals add the cached prompt tokens",
           total[spending.UNNAMED].cached_prompt_tokens == 90, str(total))
    bad = dict(usage, prompt_tokens_details={"cached_tokens": -1})
    expect("a negative cache count is refused",
           raises(spending.SpendingError, lambda: spending.record("m", {"usage": bad})))
    none = spending.record("m", {"usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    expect("a reply with no cache details has no cache keys",
           none is not None and "cached_prompt_tokens" not in none, str(none))


def test_notes_only_project(folder: Path) -> None:
    """A project of Markdown notes and no LaTeX document gives its notes as the paper."""
    root, state = folder / "notes-project", folder / "notes-project" / ".verifier"
    (root / "notes").mkdir(parents=True)
    state.mkdir()
    (root / ".gitignore").write_text(".verifier/\n")
    (root / "notes" / "a.md").write_text("# Results\n\nThe 7B model reaches 68.4%.\n")
    (root / "notes" / "b.md").write_text("# Setup\n\nWe use 3 seeds.\n")
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "notes"]):
        subprocess.run(["git", "-C", str(root), *args], check=True)
    project = watched_project.WatchedProject.load(root, state)
    expect("with no notes selected, there is no paper text", project.full_paper_text() is None)
    claim_files.save_scope(state, (structure.WHOLE,))
    text = project.full_paper_text()
    expect("each note comes after a line that names its file, in path order",
           text is not None and text.startswith("FILE: notes/a.md\n# Results")
           and "\n\nFILE: notes/b.md\n# Setup" in text, repr(text))
    claim_files.save_scope(state, ("notes/a.md",))
    expect("one note is its text alone", project.full_paper_text() == "# Results\n\nThe 7B "
           "model reaches 68.4%.\n", repr(project.full_paper_text()))
    prefix = shared_paper_prefix.shared_paper_prefix_for_model_calls(text or "x")
    expect("the prefix says the text is LaTeX or Markdown notes",
           prefix.startswith("The full text of the research paper (LaTeX or Markdown notes)"),
           prefix[:80])


def main() -> int:
    """Run every group of cases and report the failures."""
    with tempfile.TemporaryDirectory() as tmp:
        for group in (test_prefix, test_request_body, test_first_call_alone,
                      lambda: test_cache_counts(Path(tmp)),
                      lambda: test_notes_only_project(Path(tmp))):
            group()
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll shared paper prefix cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
