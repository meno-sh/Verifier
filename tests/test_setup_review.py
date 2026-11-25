#!/usr/bin/env python3
"""Check the setup review (checks/setup_review), with a fake model.

Covers: on by default, but Off with no setup register (no call,
no key needed); a saved reading alone; an unchanged paper and register reuse the reading; a
model failure is Skipped; the prompt
holds the paper and the setup register, one line per fact,
or no register block when there is no register; the reply becomes one result with its kinds
(prefixed codes, soundness for setup faults); an unknown code is an unusable finding; a
frozen reading needs no model; the options refuse a call and a frozen reading together; the
register file is read from the state directory. Returns 1 if any case fails.
"""
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checks.setup_review import setup_review as S  # noqa: E402
from schema.primitives import Flagged, Off, Passed, Severity, Skipped  # noqa: E402
from utils import models, shared_paper_prefix  # noqa: E402

FAILED: list[str] = []


@contextlib.contextmanager
def environment_without_key():
    """No OpenRouter key while the block runs, so a key check would fail."""
    saved = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["OPENROUTER_API_KEY"] = saved


def expect(what: str, ok: bool, detail: str = "") -> None:
    """Print one case; remember a failure."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {what}" + ("" if ok else f"  ({detail[:300]})"))
    if not ok:
        FAILED.append(what)


REGISTER = {"paper/main.tex#s-aaa": {"text": "We train with Adam, learning rate 0.01.",
                                     "kind": "sentence", "section": "Method > Training",
                                     "keep": 92},
            "paper/main.tex#eq-bbb": {"text": "L = - log p(x)", "kind": "equation",
                                      "section": "Method", "keep": 85}}
REPLY = json.dumps({"findings": [
    {"code": "setup-inconsistency", "where": "Section 4", "quote": "learning rate 0.1",
     "why": "Section 3 says 0.01."},
    {"code": "term-drift", "where": "Section 2", "quote": "q", "why": "w"}]})


class Fake:
    """Stands in for models.ask_and_read; records the prompt and returns REPLY."""

    def __init__(self, reply: str = REPLY) -> None:
        self.calls: list[dict] = []
        self.reply = reply

    def __call__(self, prompt: str, model: str, read, timeout: int = 300, **kw: object):
        self.calls.append({"prompt": prompt, "model": model, **kw})
        return read(self.reply)


def test_prompt() -> None:
    """The prompt holds the register, or no register block without one; the paper is not in it."""
    with_reg = S.prompt_text(REGISTER)
    without = S.prompt_text(None)
    expect("the paper is not in the prompt, which comes after the shared paper prefix",
           "{paper}" not in S.PROMPT and "PAPER:" not in with_reg)
    expect("each fact is one line [id | kind | section] text, in order",
           "[s-aaa | sentence | Training] We train with Adam, learning rate 0.01.\n"
           "[eq-bbb | equation | Method] L = - log p(x)" in with_reg, with_reg[-300:])
    expect("with no register there is no register block, and no placeholder is left",
           S.REGISTER_HEAD not in without and "{register}" not in without
           and "{paper}" not in without and "{register}" not in with_reg)


def test_reply() -> None:
    """A reply becomes one result; codes get their prefixed kinds; unknown codes are unusable."""
    result = S.parse_reply(REPLY)
    kinds = [p.kind for p in result.problems] if isinstance(result, Flagged) else []
    expect("two findings give two problems with prefixed codes",
           [k.code for k in kinds] == ["setup-review-setup-inconsistency",
                                       "setup-review-term-drift"], repr(kinds))
    expect("a setup fault is soundness, term drift is presentation",
           kinds and kinds[0].severity == Severity.SOUNDNESS
           and kinds[1].severity == Severity.PRESENTATION)
    odd = S.parse_reply(json.dumps({"findings": [{"code": "made-up", "why": "x"}]}))
    expect("an unknown code is an unusable finding",
           isinstance(odd, Flagged) and odd.problems[0].kind == S.UNUSABLE_FINDING)
    expect("no findings is a pass", isinstance(S.parse_reply('{"findings": []}'), Passed))
    expect("a frozen reading that is not a reading is Skipped",
           isinstance(S.read_reply("not json"), Skipped))
    expect("no code is shared with model_review",
           not any(k.code in {"term-drift", "method-drift"} for k in S.REVIEW_KINDS))


class Context:
    """The parts of a PaperContext the check reads."""

    def __init__(self, options: dict, paper: str = "The paper.") -> None:
        self.options = options
        self.paper = type("P", (), {"text": paper})()


def test_run() -> None:
    """On by default but Off with no register; a call reads the register, asks, saves it."""
    expect("on by default, and Off with no setup register", S.WANT.default is True
           and isinstance(S.SetupReviewChecks().run(Context({})).checks["setup_review"], Off))
    with environment_without_key():
        S.options_fit({"want_setup_review": True})
    expect("with no setup register, the options need no model key", True)
    fake, real = Fake(), models.ask_and_read
    models.ask_and_read = fake
    try:
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "setup_register.json"
            reg.write_text(json.dumps(REGISTER))
            out = Path(tmp) / "readings" / "setup_review.txt"
            res = S.SetupReviewChecks().run(Context(
                {"want_setup_review": True, "setup_register_path": str(reg),
                 "setup_review_out": str(out)})).checks["setup_review"]
            saved = out.read_text() if out.exists() else ""
            missing = S.SetupReviewChecks().run(Context(
                {"want_setup_review": True, "setup_register_path": str(Path(tmp) / "none")}))
            empty_reg = Path(tmp) / "empty.json"
            empty_reg.write_text("{}")
            empty = S.SetupReviewChecks().run(Context(
                {"want_setup_review": True, "setup_register_path": str(empty_reg)}))
    finally:
        models.ask_and_read = real
    expect("a call gives the model's result, under the group's name", isinstance(res, Flagged))
    expect("the model is Opus with medium effort by default, and the register is in the prompt",
           fake.calls[0]["model"] == models.preset.OPUS
           and fake.calls[0].get("reasoning") == "medium"
           and "We train with Adam" in fake.calls[0]["prompt"], repr(fake.calls[0])[:300])
    expect("the paper goes first, as the shared paper prefix",
           fake.calls[0].get("shared_prefix_for_cache")
           == shared_paper_prefix.shared_paper_prefix_for_model_calls("The paper."),
           repr(fake.calls[0].get("shared_prefix_for_cache")))
    expect("the reading is saved", saved == REPLY)
    expect("a missing or empty register file is Off, with no model call",
           isinstance(missing.checks["setup_review"], Off)
           and isinstance(empty.checks["setup_review"], Off) and len(fake.calls) == 1,
           repr(len(fake.calls)))
    with tempfile.TemporaryDirectory() as tmp:
        frozen = Path(tmp) / "r.txt"
        frozen.write_text(REPLY)
        res = S.SetupReviewChecks().run(Context({"frozen_setup_review": str(frozen)}))
        try:
            S.options_fit({"frozen_setup_review": str(frozen), "want_setup_review": True})
            refused = False
        except ValueError:
            refused = True
    expect("a frozen reading needs no model", isinstance(res.checks["setup_review"], Flagged))
    expect("a call and a frozen reading together are refused", refused)
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "setup_register.json"
        bad.write_text("{not json")
        res = S.SetupReviewChecks().run(Context({"want_setup_review": True,
                                                 "setup_register_path": str(bad)}))
    expect("an unreadable register is a Skipped result, not a crash",
           isinstance(res.checks["setup_review"], Skipped), repr(res.checks))
    with tempfile.TemporaryDirectory() as tmp:
        frozen = Path(tmp) / "r.txt"
        frozen.write_text(REPLY)
        try:
            S.options_fit({"frozen_setup_review": str(frozen)})
            alone = ""
        except ValueError as e:
            alone = str(e)
    expect("a saved reading alone is accepted, though the review is on by default", alone == "",
           alone)
    fake = Fake()
    models.ask_and_read = fake
    try:
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "setup_register.json"
            reg.write_text(json.dumps(REGISTER))
            out = str(Path(tmp) / "setup_review.txt")
            options = {"setup_register_path": str(reg), "setup_review_out": out}
            S.SetupReviewChecks().run(Context(options))
            again = S.SetupReviewChecks().run(Context(options)).checks["setup_review"]
            calls_same = len(fake.calls)
            reg.write_text(json.dumps({**REGISTER, "n.md#s-new": {
                "text": "We use 3 seeds.", "kind": "sentence", "section": "Setup", "keep": 80}}))
            S.SetupReviewChecks().run(Context(options))
            calls_changed = len(fake.calls)
            for _ in range(2):                 # two documents of one project, two rounds
                for doc in ("The paper.", "The notes."):
                    S.SetupReviewChecks().run(Context(options, doc))
            calls_two_docs = len(fake.calls) - calls_changed
    finally:
        models.ask_and_read = real
    expect("an unchanged paper and register read the saved reading, with no new call",
           calls_same == 1 and isinstance(again, Flagged), repr(calls_same))
    expect("a changed register calls the model again", calls_changed == 2, repr(calls_changed))
    expect("two documents keep their own readings: a second round makes no call",
           calls_two_docs == 1, repr(calls_two_docs))

    def failing(*args: object, **kw: object) -> object:
        raise models.ProviderError("the provider timed out")
    models.ask_and_read = failing
    try:
        with tempfile.TemporaryDirectory() as tmp:
            reg = Path(tmp) / "setup_register.json"
            reg.write_text(json.dumps(REGISTER))
            res = S.SetupReviewChecks().run(Context({"setup_register_path": str(reg)}))
    finally:
        models.ask_and_read = real
    expect("a model failure is a Skipped result that blocks a pass, not a crash",
           isinstance(res.checks["setup_review"], Skipped), repr(res.checks))
    with tempfile.TemporaryDirectory() as tmp:
        reg = Path(tmp) / "setup_register.json"
        reg.write_text(json.dumps(REGISTER))
        long_paper = "word " * (shared_paper_prefix.MAX_PAPER_TOKENS
                                * shared_paper_prefix.CHARACTERS_PER_TOKEN // 4)
        res = S.SetupReviewChecks().run(Context({"setup_register_path": str(reg)}, long_paper))
    expect("a paper too long for the model is a Skipped result with the reason, not a crash",
           isinstance(res.checks["setup_review"], Skipped)
           and "tokens" in res.checks["setup_review"].because, repr(res.checks)[:300])
    try:
        S.options_fit({"want_setup_review": True, "setup_review_effort": "ultra"})
        refused_effort = False
    except ValueError:
        refused_effort = True
    expect("a bad effort is refused before any call", refused_effort)
    expect("the register option is watched in the state directory",
           S.REGISTER.watched == "setup_register.json" and S.REGISTER.in_state)


def main() -> int:
    """Run every case and report."""
    for test in (test_prompt, test_reply, test_run):
        print(test.__doc__.splitlines()[0])
        test()
    print("all passed" if not FAILED else f"{len(FAILED)} FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
