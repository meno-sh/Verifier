#!/usr/bin/env python3
"""Check the labeller (core/claim_steps/labeller.py) and `vcheck label-candidates`, with a fake
model.

Covers: one call per piece or per batch; the keep cutoff; one quote of the whole text for
each kept candidate, and one quote with no value for a kept whole table; the label of each
piece; the cache and the option to ignore it; the fixed order; a failed call
stopping the run; a repeat drop kept only while its twin is kept; the settings refused;
`vcheck label-candidates` on a small project; the setup register; and the labelled
examples of the prompt. Returns 1 if any case fails.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import cli  # noqa: E402
from core.claim_steps import labeller  # noqa: E402
from utils import claim_files, extract, models, structure  # noqa: E402

FAILED: list[str] = []

def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def pieces(prompt: str) -> list[str]:
    """The text of each piece of a prompt, in order."""
    return [block.split("\ntext: ", 1)[1] for block in prompt.split("Piece ")[1:]]


class FakeModel:
    """models.ask_and_read: keep score 10 and label junk for a piece about thanks, else 90
    and claim.

    A kept piece's values are the first number of its text with a decimal point, if it has
    one, so a condition column such as "Seeds: 5" gives no value.

    `fail` makes calls raise ProviderError; `labels` False leaves the label out of the reply.
    """

    def __init__(self, fail: bool = False, labels: bool = True) -> None:
        self.calls: list[dict] = []
        self.fail, self.labels = fail, labels
        self.lock = threading.Lock()

    def __call__(self, prompt: str, model: str, read, timeout: int = 300, *,
                 system: str | None = None, reasoning: str | None = None,
                 read_attempts: int = models.READ_ATTEMPTS) -> object:
        with self.lock:
            self.calls.append({"prompt": prompt, "model": model, "system": system,
                               "reasoning": reasoning})
        if self.fail:
            raise models.ProviderError("the provider refused the call")
        reply = {}
        for n, text in enumerate(pieces(prompt), start=1):
            keep = 10 if "thank" in text.lower() else 90
            # a result, never a condition: the first number with a decimal point
            listed = [n for n in extract.claim_numbers(text) if "." in n][:1] if keep > 50 else []
            reply[str(n)] = {"keep": keep, "values": listed}
            if self.labels:
                reply[str(n)]["label"] = "claim" if keep > 50 else "junk"
        return read(json.dumps(reply))


def cand(kind: str, text: str, **fields: object) -> dict:
    """A candidate of this kind."""
    return {"kind": kind, "text": text, "section": "Results", **fields}


CANDS = {
    "s1": cand("sentence", "The 7B model answers 68.4% of the questions correctly.",
               previous="We ran the 7B model."),
    "s2": cand("sentence", "We thank Jane Doe for helpful discussions."),
    "s3": cand("sentence", "This sentence states a setup fact about seeds."),
    "t1": cand("table", "Table 1: Accuracy on the test set. Columns: Method | Seeds | Acc | F1. "
               "Rows: Method: Ours; Seeds: 5; Acc: 71.2; F1: 0.65 || Method: Base; Acc: 61.0; "
               "F1: ", table="Table 1"),
    "e1": cand("equation", "Eq. (1): L = - log p(x)"),
}


def test_labels() -> None:
    """Calls, cutoff, quotes, a whole table, labels and the settings."""
    fake = FakeModel()
    labels = labeller.label_candidates(CANDS, labeller.Settings(), ask=fake)
    expect("batch 1 makes one call per piece, on Haiku with no reasoning",
           len(fake.calls) == len(CANDS) and all(
               c["model"] == models.preset.HAIKU and c["reasoning"] is None
               for c in fake.calls), repr(len(fake.calls)))
    prompt = next(c["prompt"] for c in fake.calls if "68.4%" in c["prompt"])
    expect("a piece is sent with its kind, section and the sentence before it",
           "kind: sentence" in prompt and "section: Results" in prompt
           and "sentence before: We ran the 7B model." in prompt, prompt)
    table_prompt = next(c["prompt"] for c in fake.calls if "71.2" in c["prompt"])
    expect("a table is sent as one piece, with every row",
           "kind: table" in table_prompt and "61.0" in table_prompt, table_prompt)
    expect("the system part holds the keep rule and asks for a 0-100 score",
           fake.calls[0]["system"].startswith(labeller.KEEP_RULE)
           and '"keep"' in fake.calls[0]["system"])
    quotes, drops = labeller.apply_labels(CANDS, labels, 50)
    expect("a piece below the cutoff is dropped with its score", drops == {"s2": "auto:label 10"},
           repr(drops))
    expect("each value of a kept candidate gets one quote of the whole text, and a kept table "
           "is one quote with no value, whatever the model lists; id q-<candidate> for one "
           "value or none",
           {qid: (q["candidates"], q["value"]) for qid, q in quotes.items()}
           == {"q-s1": (["s1"], "68.4"), "q-s3": (["s3"], None), "q-t1": (["t1"], None),
               "q-e1": (["e1"], None)}
           and all(q["quote"] == CANDS[q["candidates"][0]]["text"] and set(q) == {
               "candidates", "quote", "value"} for q in quotes.values()), repr(quotes))
    _, drops = labeller.apply_labels(CANDS, labels, 95)
    expect("a higher cutoff drops more", set(drops) == set(CANDS), repr(drops))
    expect("a candidate id with spaces gives a quote id with none",
           labeller.quote_id("paper (v2)/main.tex#s-1") == "q-paper_(v2)/main.tex#s-1")

    fake = FakeModel()
    labeller.label_candidates(CANDS, labeller.Settings(batch=4), ask=fake)
    expect("batch 4 sends the 5 pieces in 2 calls", len(fake.calls) == 2, repr(len(fake.calls)))
    fake = FakeModel()
    labels = labeller.label_candidates(CANDS, labeller.Settings(), ask=fake)
    expect("each call asks for a label, and the label is kept",
           labels["s1"]["label"] == "claim" and labels["s2"]["label"] == "junk"
           and '"label"' in fake.calls[0]["system"], repr(labels["s1"]))
    try:
        labeller.label_candidates(CANDS, labeller.Settings(), ask=FakeModel(labels=False))
        refused = ""
    except models.BadReply as e:
        refused = str(e)
    expect("a reply with no label is refused", '"label" must be one of' in refused, refused)
    for bad in ({"batch": 0}, {"cutoff": 101}, {"effort": "huge"}):
        try:
            labeller.Settings(**bad)
            got = None
        except labeller.LabelError as e:
            got = e
        expect(f"the settings refuse {bad}", got is not None)
    settings = labeller.Settings.from_options({"label_batch": 25, "label_cutoff": 30,
                                               "label_cache": False})
    expect("the settings are read from the run options",
           (settings.batch, settings.cutoff, settings.cache) == (25, 30, False), repr(settings))


def test_cache_and_order() -> None:
    """The cache answers a rerun; the order is fixed by the seed; a failed call stops."""
    cache: dict = {}
    first = FakeModel()
    labeller.label_candidates(CANDS, labeller.Settings(batch=2), ask=first, cache=cache)
    again = FakeModel()
    count = labeller.LabelCount()
    labels = labeller.label_candidates(CANDS, labeller.Settings(batch=2), ask=again,
                                       cache=cache, label_count=count)
    expect("a rerun with the cache makes no call and gives the same labels",
           not again.calls and count.cached == 3 and labels["s2"]["keep"] == 10, repr(count))
    other = FakeModel()
    labeller.label_candidates(CANDS, labeller.Settings(batch=3), ask=other, cache=cache)
    expect("another batch size is not answered from the cache", len(other.calls) == 2)
    orders = []
    for _ in range(2):
        fake = FakeModel()
        labeller.label_candidates(CANDS, labeller.Settings(batch=6, seed=7), ask=fake)
        orders.append(fake.calls[0]["prompt"])
    expect("the same seed sends the pieces in the same order", orders[0] == orders[1])
    fake = FakeModel()
    labeller.label_candidates(CANDS, labeller.Settings(batch=6, seed=8), ask=fake)
    expect("another seed sends them in another order", fake.calls[0]["prompt"] != orders[0])
    try:
        labeller.label_candidates(CANDS, labeller.Settings(), ask=FakeModel(fail=True))
        raised = False
    except models.ProviderError:
        raised = True
    expect("a failed call stops the labelling with its error", raised)


def test_repeats() -> None:
    """A repeat drop of the rules stands while its twin is kept, else the repeat's label decides."""
    cands = {"a": cand("sentence", "We use five seeds."),
             "b": cand("sentence", "We use five seeds."),
             "c": cand("sentence", "We thank the reviewers."),
             "d": cand("sentence", "We thank the reviewers.")}
    labels = {"a": {"keep": 90, "label": None, "values": []},
              "b": {"keep": 90, "label": None, "values": []},
              "c": {"keep": 10, "label": None, "values": []},
              "d": {"keep": 80, "label": None, "values": []}}
    quotes, drops = labeller.apply_labels(cands, labels, 50, repeats={"b": "a", "d": "c"})
    expect("a repeat whose twin is kept keeps the rules' drop (nothing new for it)",
           not any(q["candidates"] == ["b"] for q in quotes.values()) and "b" not in drops,
           repr(quotes) + repr(drops))
    expect("a repeat whose twin is dropped is answered by its own label",
           any(q["candidates"] == ["d"] for q in quotes.values()) and drops.get("c")
           == "auto:label 10", repr(quotes) + repr(drops))
    _, drops = labeller.apply_labels(cands, {"b": labels["b"]}, 50, repeats={"b": "a"},
                                     quoted={"a": ["We use five seeds."]})
    expect("a twin quoted earlier keeps the repeat's drop", drops == {})
    eq = {"e": cand("equation", "Eq. (1): L = - log p(x | z)"),
          "s": cand("sentence", "We train with the loss L = - log p(x | z) on all runs.")}
    kept = {"e": {"keep": 90, "label": None, "values": []},
            "s": {"keep": 90, "label": None, "values": []}}
    quotes, drops = labeller.apply_labels(eq, kept, 50, repeats={"e": "s"})
    expect("an equation whose twin sentence is kept whole keeps the rules' drop",
           not any(q["candidates"] == ["e"] for q in quotes.values()) and "e" not in drops,
           repr(quotes) + repr(drops))
    quotes, _ = labeller.apply_labels(eq, {"e": kept["e"]}, 50, repeats={"e": "s"},
                                      quoted={"s": ["We train with the loss"]})
    expect("an equation whose twin was quoted earlier, without it, gets its own quote",
           any(q["candidates"] == ["e"] for q in quotes.values()), repr(quotes))


def git(root: Path, *args: str) -> str:
    """Run git in the test repository."""
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, "GIT_AUTHOR_NAME": "t",
                                          "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                                          "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


def vcheck(root: Path, *args: str) -> tuple[int, str]:
    """Run a vcheck command quietly; return (exit code, output)."""
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.main(["--state", str(root / ".verifier"), "--root", str(root), *args])
    return rc, out.getvalue()


NOTE = """# Results

With retrieval, the 7B model answers 68.4% of the questions correctly.
Thanks go to the people who helped.
Thanks to Jane, the answers were checked by hand.
This bad span sentence states a setup fact about seeds.
We ran three seeds for each condition.
We ran three seeds for each condition.

| Method | Acc |
|---|---|
| Ours | 71.2 |
"""


def test_command() -> None:
    """`vcheck label-candidates` answers each open candidate; check-quotes passes; cache holds."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "notes").mkdir()
        git(root, "init", "-q")
        (root / ".gitignore").write_text(".verifier/\n")
        (root / "notes" / "results.md").write_text(NOTE)
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "notes")
        state = root / ".verifier"
        state.mkdir()
        claim_files.save_scope(state, (structure.WHOLE,))
        claim_files.begin_round(root, state, git(root, "rev-parse", "HEAD"))
        assert vcheck(root, "drop-non-claims")[0] == 0
        fake, real = FakeModel(), models.ask_and_read
        models.ask_and_read = fake
        try:
            rc, out = vcheck(root, "label-candidates")
            rc2, out2 = vcheck(root, "label-candidates")
        finally:
            models.ask_and_read = real
        dropped = json.loads((state / claim_files.DROPPED).read_text())
        quotes = json.loads((state / claim_files.QUOTES).read_text())
        expect("label answers the open candidates and says how",
               rc == 0 and "labelled" in out and any(d["why"].startswith("auto:label")
                                                     for d in dropped.values()), out)
        expect("label leaves the rules' repeat drop, whose twin it kept",
               any(d["why"].startswith("auto:R14") for d in dropped.values()), json.dumps(dropped))
        expect("the quotes and drops have empty stamps",
               all(e["created"] == "" for e in [*dropped.values(), *quotes.values()]))
        rc, out = vcheck(root, "check-quotes")
        expect("after label, check-quotes passes", rc == 0, out)
        expect("the answers are cached in the state directory",
               (state / labeller.CACHE_FILE).exists()
               and len(json.loads((state / labeller.CACHE_FILE).read_text())) > 0)
        expect("a second label finds nothing open and makes no call",
               rc2 == 0 and "0 candidates" in out2 and len(fake.calls) < 10, out2)


def test_split_setup() -> None:
    """A kept setup fact goes to the setup register, with its repeats."""
    lab = lambda keep, label: {"keep": keep, "label": label, "values": []}  # noqa: E731
    cands = {cid: {"kind": "sentence", "text": f"text of {cid}"}
             for cid in ("c", "s", "low", "r", "r2", "old")}
    # an equation is held only by a quote with its whole text, which "c" does not have
    cands["rc"] = {"kind": extract.EQUATION, "text": "L = - log p(x)"}
    labels = {"c": lab(90, "claim"), "s": lab(85, "setup"), "low": lab(30, "setup"),
              "r": lab(80, "setup"), "r2": lab(80, "claim"), "rc": lab(80, "setup")}
    quotes, drops = labeller.apply_labels(cands, labels, 50, {"r": "s", "r2": "s", "rc": "c"})
    quoted = {q["candidates"][0] for q in quotes.values()}
    setup = {cid for cid, why in drops.items() if why.startswith(claim_files.AUTO_SETUP)}
    expect("a setup fact at or above the cutoff goes to the setup register; one below is a "
           "labeller drop",
           "s" in setup and drops.get("low") == "auto:label 30" and "c" in quoted,
           repr((setup, drops)))
    expect("a repeat of a setup fact is decided by its own label: setup goes with it, a claim "
           "is quoted", "r" in setup and "r2" in quoted, repr((setup, quoted)))
    expect("a setup-labelled repeat of a claim the claim's quote does not hold goes to the "
           "register, not to a quote", "rc" in setup and "rc" not in quoted, repr(quoted))
    _, drops = labeller.apply_labels(cands, {"r": lab(80, "setup")}, 50, {"r": "old"})
    expect("a repeat of a fact sent to the register in an earlier round goes there too",
           drops == {"r": f"{claim_files.AUTO_SETUP}80"}, repr(drops))


class SetupFake(FakeModel):
    """FakeModel that labels any piece about seeds "setup"."""

    def __call__(self, prompt: str, model: str, read, timeout: int = 300, **kw: object) -> object:
        def relabel(text: str) -> object:
            reply = json.loads(text)
            for n, piece in enumerate(pieces(prompt), start=1):
                if "seeds" in piece:
                    reply[str(n)]["label"] = "setup"
            return read(json.dumps(reply))
        return super().__call__(prompt, model, relabel, timeout, **kw)


def test_command_setup() -> None:
    """`vcheck label-candidates` writes the setup register; check-quotes passes."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "notes").mkdir()
        git(root, "init", "-q")
        (root / ".gitignore").write_text(".verifier/\n")
        (root / "notes" / "results.md").write_text(NOTE)
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "notes")
        state = root / ".verifier"
        state.mkdir()
        claim_files.save_scope(state, (structure.WHOLE,))
        claim_files.begin_round(root, state, git(root, "rev-parse", "HEAD"))
        assert vcheck(root, "drop-non-claims")[0] == 0
        fake, real = SetupFake(), models.ask_and_read
        models.ask_and_read = fake
        try:
            rc, out = vcheck(root, "label-candidates")
        finally:
            models.ask_and_read = real
        dropped = json.loads((state / claim_files.DROPPED).read_text())
        quotes = json.loads((state / claim_files.QUOTES).read_text())
        register = claim_files.load_setup_register(state)
        cands = json.loads((state / claim_files.CANDIDATES).read_text())
        seeds = {cid for cid, c in cands.items() if "seeds" in str(c.get("text", ""))}
        expect("the label is asked for",
               rc == 0 and '"label"' in fake.calls[0]["system"], out)
        expect("every piece about seeds is in the setup register, with an auto:setup drop, and "
               "in no quote",
               seeds and set(register) == seeds
               and all(dropped[cid]["why"].startswith(claim_files.AUTO_SETUP) for cid in seeds)
               and not any(set(q["candidates"]) & seeds for q in quotes.values())
               and "setup register" in out, json.dumps(register) + out)
        expect("a setup register entry has the text, kind, section and keep score, in paper order",
               all(set(e) == {"text", "kind", "section", "keep"} for e in register.values())
               and list(register) == [cid for cid in cands if cid in register],
               json.dumps(register))
        expect("the claims are still quoted", any("68.4" in q["quote"] for q in quotes.values()))
        rc, out = vcheck(root, "check-quotes")
        expect("after label, check-quotes passes with the auto:setup drops", rc == 0, out)
        some = next(iter(register))
        claim_files.save_setup_register(state, {k: v for k, v in register.items() if k != some})
        rc, out = vcheck(root, "check-quotes")
        expect("an auto:setup drop of a fact not in the setup register is refused",
               rc != 0 and "setup register" in out, out[-300:])
        claim_files.save_setup_register(state, register)
        dropped_path = state / claim_files.DROPPED
        dropped = json.loads(dropped_path.read_text())
        dropped[some]["why"] = dropped[some]["why"].replace("auto:setup", "auto:label")
        dropped_path.write_text(json.dumps(dropped))
        rc, out = vcheck(root, "check-quotes")
        expect("an auto:label drop of a fact in the setup register is refused, so a setup fact "
               "cannot be passed off as a labeller drop", rc != 0 and "labeller" in out,
               out[-300:])


def test_quote_ids() -> None:
    """Quote ids never collide: two candidate ids alike but for spaces, or an earlier quote."""
    two = {"a b": cand("sentence", "The model reaches 71.2% accuracy."),
           "a_b": cand("sentence", "The model uses 3 seeds.")}
    labels = {cid: {"keep": 90, "label": None, "values": []} for cid in two}
    quotes, _ = labeller.apply_labels(two, labels, 50)
    expect("candidates 'a b' and 'a_b' each keep their own quote",
           sorted(q["candidates"][0] for q in quotes.values()) == ["a b", "a_b"],
           repr(quotes))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "notes").mkdir()
        git(root, "init", "-q")
        (root / ".gitignore").write_text(".verifier/\n")
        (root / "notes" / "results.md").write_text(NOTE)
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "notes")
        state = root / ".verifier"
        state.mkdir()
        claim_files.save_scope(state, (structure.WHOLE,))
        claim_files.begin_round(root, state, git(root, "rev-parse", "HEAD"))
        cands = json.loads((state / claim_files.CANDIDATES).read_text())
        mine, other = list(cands)[0], list(cands)[1]
        clash = labeller.quote_id(other)
        earlier = {"created": "", "last_updated": "", "candidates": [mine],
                   "quote": cands[mine]["text"], "value": None}
        (state / claim_files.QUOTES).write_text(json.dumps({clash: earlier}))
        fake, real = FakeModel(), models.ask_and_read
        models.ask_and_read = fake
        try:
            rc, out = vcheck(root, "label-candidates")
        finally:
            models.ask_and_read = real
        quotes = json.loads((state / claim_files.QUOTES).read_text())
        expect("label works when a quote already has the id it would give",
               rc == 0 and quotes[clash] == earlier
               and any(q["candidates"] == [other] for k, q in quotes.items() if k != clash),
               out + json.dumps(quotes))
        expect("the paid answers are in the cache file",
               len(json.loads((state / labeller.CACHE_FILE).read_text())) > 0)
        rc, out = vcheck(root, "check-quotes")
        expect("after label, check-quotes passes, the label drops included", rc == 0, out)


class SlowModel(FakeModel):
    """A FakeModel whose call fails at once for a piece about "boom", and waits otherwise.

    `answered` counts the calls that returned an answer.
    """

    def __init__(self) -> None:
        super().__init__()
        self.answered = 0

    def __call__(self, prompt: str, *args: object, **kwargs: object) -> object:
        if "boom" in prompt:
            raise models.ProviderError("the provider refused the call")
        time.sleep(0.3)
        out = super().__call__(prompt, *args, **kwargs)
        with self.lock:
            self.answered += 1
        return out


class WatchingModel(FakeModel):
    """A FakeModel that, at its `at`-th call, waits for the cache file to hold `want` answers."""

    def __init__(self, path: Path, at: int, want: int) -> None:
        super().__init__()
        self.path, self.at, self.want = path, at, want
        self.saw: int | None = None

    def __call__(self, prompt: str, *args: object, **kwargs: object) -> object:
        with self.lock:
            n = len(self.calls) + 1
        if n == self.at:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    self.saw = len(json.loads(self.path.read_text()))
                except (OSError, ValueError):
                    self.saw = 0
                if self.saw >= self.want:
                    break
                time.sleep(0.05)
        return super().__call__(prompt, *args, **kwargs)


def test_cache_saved() -> None:
    """The cache is written whole, saved while the calls run, and keeps answers after a failure."""
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp)
        labeller.write_cache(state, {"a": {"keep": 1}})
        real = os.replace

        def broken(*args: object, **kwargs: object) -> None:
            raise OSError("the disk is full")
        os.replace = broken
        try:
            labeller.write_cache(state, {"a": {"keep": 1}, "b": {"keep": 2}})
        except OSError:
            pass
        finally:
            os.replace = real
        try:
            kept = labeller.read_cache(state) == {"a": {"keep": 1}}
        except ValueError:
            kept = False
        expect("a cache write that fails half way leaves the cache file as it was", kept,
               (state / labeller.CACHE_FILE).read_text())
    cands = {f"s{n}": cand("sentence", f"Result {n} reaches {n}.5% accuracy.") for n in range(4)}
    cands["s9"] = cand("sentence", "This boom sentence makes the call fail.")
    cache: dict = {}
    slow = SlowModel()
    try:
        labeller.label_candidates(cands, labeller.Settings(), ask=slow, cache=cache)
        raised = False
    except models.ProviderError:
        raised = True
    time.sleep(1)               # any call still running after the error has ended by now
    expect("after a failed call, every call that returns an answer has it cached",
           raised and slow.answered >= 1 and len(cache) == slow.answered,
           f"raised {raised}, {slow.answered} answered, {len(cache)} cached")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "notes").mkdir()
        git(root, "init", "-q")
        (root / ".gitignore").write_text(".verifier/\n")
        (root / "notes" / "many.md").write_text("# Results\n\n" + "\n\n".join(
            f"Setting {n} reaches {n}.25% accuracy on the test set." for n in range(90)) + "\n")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "notes")
        state = root / ".verifier"
        state.mkdir()
        claim_files.save_scope(state, (structure.WHOLE,))
        claim_files.begin_round(root, state, git(root, "rev-parse", "HEAD"))
        fake, real_ask = WatchingModel(state / labeller.CACHE_FILE, 75, 50), models.ask_and_read
        models.ask_and_read = fake
        try:
            rc, out = vcheck(root, "label-candidates")
        finally:
            models.ask_and_read = real_ask
        expect("label-candidates saves the cache every 50 answers while the calls run, so a "
               "killed run keeps what it paid for",
               rc == 0 and fake.saw is not None and fake.saw >= 50,
               f"rc {rc}, the 75th call saw {fake.saw} cached answers; {out}")


def example_text(example: dict) -> str:
    """The text of an example piece: what follows "text: " in its block."""
    return example["block"].split("\ntext: ", 1)[1]


def test_examples() -> None:
    """The labelled examples of option label_examples, and the system part that holds them."""
    examples = json.loads(labeller.EXAMPLES_FILE.read_text())["examples"]
    expect("each example has an answer with a keep score, a reason and values",
           examples and all(set(e["answer"]) == {"keep", "why", "values"}
                            and 0 <= e["answer"]["keep"] <= 100 and e["answer"]["why"]
                            for e in examples), repr(len(examples)))
    expect("an example is kept (score 50 or more) only when its label is claim or setup, and "
           "only a kept example has values",
           all((e["answer"]["keep"] < 50 or e["label"] in ("claim", "setup"))
               and (e["answer"]["keep"] >= 50 or not e["answer"]["values"]) for e in examples),
           repr([(e["label"], e["answer"]) for e in examples]))
    expect("each value is a number that the example prints, and listed once",
           all(v in extract.claim_numbers(example_text(e)) for e in examples
               for v in e["answer"]["values"])
           and all(len(set(e["answer"]["values"])) == len(e["answer"]["values"])
                   for e in examples))
    off, on = FakeModel(), FakeModel()
    labeller.label_candidates(CANDS, labeller.Settings(examples=False), ask=off)
    labeller.label_candidates(CANDS, labeller.Settings.from_options({"label_examples": True}),
                              ask=on)
    system = on.calls[0]["system"]
    expect("off, the system part is the keep rule and the reply's form only",
           all(c["system"] == labeller.KEEP_RULE + "\n" + labeller.reply_text()
               for c in off.calls)
           and "Example 1" not in off.calls[0]["system"])
    expect("on, the system part holds every example in order, with its answer, between the "
           "rule and the reply",
           all(f"Example {n} ({e['answer']['why']})\n{e['block']}\nAnswer: "
               + json.dumps({"keep": e["answer"]["keep"], "label": e["label"],
                             "values": e["answer"]["values"]}, ensure_ascii=False) in system
               for n, e in enumerate(examples, 1))
           and all(system.index(f"Example {n} (") < system.index(f"Example {n + 1} (")
                   for n in range(1, len(examples)))
           and system.startswith(labeller.KEEP_RULE + "\nHere are labelled pieces")
           and system.endswith("\n\n" + labeller.reply_text()), system[:400])
    expect("on, the pieces sent are the same as off",
           sorted(c["prompt"] for c in on.calls) == sorted(c["prompt"] for c in off.calls))
    expect("the option is on by default and its key is label_examples",
           labeller.EXAMPLES.default is True and labeller.Settings().examples is True
           and labeller.EXAMPLES in labeller.RUN_OPTIONS
           and labeller.EXAMPLES.key == "label_examples"
           and not labeller.Settings.from_options({"label_examples": False}).examples)
    mine = " ".join(labeller.system_prompt().split())
    expect("the keep rule keeps the setup and definitions, and asks for a keep score and a "
           "label",
           "The setup register. Keep a piece, labelled \"setup\"" in mine
           and "a definition or notation" in mine and '"label"' in mine, mine[:300])
    item = labeller.items(CANDS)[0]
    settings = labeller.Settings(examples=True)
    expect("the examples are in the cache key, and off keeps the key with no examples",
           labeller.cache_key(item, settings) != labeller.cache_key(
               item, labeller.Settings(examples=False))
           and labeller.Settings(examples=False).mode() == "batch=1|effort=off")
    cache: dict = {}
    labeller.label_candidates(CANDS, labeller.Settings(examples=False),
                              ask=FakeModel(), cache=cache)
    again = FakeModel()
    labeller.label_candidates(CANDS, settings, ask=again, cache=cache)
    expect("answers cached without the examples are not reused with them",
           len(again.calls) == len(CANDS), repr(len(again.calls)))


def test_examples_labels() -> None:
    """Each example's answer carries its label, as the reply must."""
    examples = json.loads(labeller.EXAMPLES_FILE.read_text())["examples"]
    expect("each example has a label",
           all(e.get("label") in labeller.LABELS for e in examples),
           repr([e.get("label") for e in examples]))
    fake = FakeModel()
    labeller.label_candidates(CANDS, labeller.Settings(examples=True), ask=fake)
    answers = [json.loads(line.removeprefix("Answer: "))
               for line in fake.calls[0]["system"].splitlines()
               if line.startswith("Answer: ")]
    expect("each example's answer has keep, label and values, as the reply's form asks",
           len(answers) == len(examples)
           and all(list(a) == ["keep", "label", "values"] and a["label"] == e["label"]
                   for a, e in zip(answers, examples, strict=True)), repr(answers))
    try:
        read = labeller.read_labels(
            json.dumps({str(n): a for n, a in enumerate(answers, 1)}), len(answers))
    except models.BadReply as e:
        read = str(e)
    expect("a reply written like the examples' answers is read",
           isinstance(read, list)
           and [a["label"] for a in read] == [e["label"] for e in examples], repr(read))
    tables = [e for e in examples if e["block"].startswith("kind: table\n")]
    expect("two examples are whole tables: a results table labelled claim and a setup table "
           "labelled setup, each with no value",
           sorted(e["label"] for e in tables) == ["claim", "setup"]
           and all(not e["answer"]["values"] for e in tables)
           and not any(e["block"].startswith("kind: table row") for e in examples),
           repr([e["block"][:60] for e in tables]))


def test_examples_cache_key() -> None:
    """The cache key changes with the content of the examples file."""
    item = labeller.items(CANDS)[0]
    on = labeller.Settings(examples=True)
    before = labeller.cache_key(item, on)
    real = labeller.EXAMPLES_FILE
    with tempfile.TemporaryDirectory() as tmp:
        edited = json.loads(real.read_text())
        edited["examples"][0]["answer"]["keep"] = 60
        path = Path(tmp) / "label_examples.json"
        path.write_text(json.dumps(edited))
        labeller.EXAMPLES_FILE = path
        try:
            after = labeller.cache_key(item, on)
            off = labeller.cache_key(item, labeller.Settings(examples=False))
        finally:
            labeller.EXAMPLES_FILE = real
    expect("an edited examples file gives other cache keys, so old answers are not reused",
           before != after, before)
    expect("with the examples off, the key does not depend on the file",
           off == labeller.cache_key(item, labeller.Settings(examples=False)))


def test_table_claims() -> None:
    """A kept whole table is one quote with no value; the others are answered as before."""
    lab = lambda keep, label="claim": {"keep": keep, "label": label, "values": ["1.5"]}  # noqa: E731
    cands = {
        "t": {"kind": extract.TABLE, "table": "Table 1", "origin": "p.tex",
              "text": "Table 1: Results. Columns: Model | Acc. Rows: Model: A; Acc: 1.5"},
        "x": {"kind": extract.TABLE, "table": "Table 2", "origin": "p.tex",
              "text": "Table 2: Other. Columns: Model | Acc. Rows: Model: C; Acc: 1.5"},
        "h": {"kind": extract.TABLE, "table": "Table 3", "origin": "p.tex",
              "text": "Table 3: Hyperparameters. Columns: Name | Value. Rows: Name: LR; "
                      "Value: 1.5"},
        "s": {"kind": extract.SENTENCE, "text": "A reaches 1.5."},
    }
    labels = {"t": lab(80), "x": lab(10), "h": lab(90, "setup"), "s": lab(90)}
    quotes, drops = labeller.apply_labels(cands, labels, 50)
    by_first = {q["candidates"][0]: q for q in quotes.values()}
    expect("a kept table is one quote of its whole text, with no value, though the model "
           "listed one",
           by_first.get("t") == {"candidates": ["t"], "quote": cands["t"]["text"], "value": None},
           repr(quotes))
    expect("a table below the cutoff is dropped, a setup table goes to the setup register, and "
           "a sentence keeps its value",
           drops.get("x") == "auto:label 10" and drops.get("h") == f"{claim_files.AUTO_SETUP}90"
           and by_first["s"]["value"] == "1.5", repr(drops))
    found = extract.coverage(cands, quotes, {k: {"why": w} for k, w in drops.items()})
    expect("every candidate is answered and each quote occurs in its candidate",
           found == ([], []), repr(found))


def main() -> int:
    """Run every case and report."""
    for test in (test_labels, test_cache_and_order, test_repeats, test_command,
                 test_split_setup, test_command_setup, test_quote_ids, test_cache_saved,
                 test_examples,
                 test_examples_labels, test_examples_cache_key, test_table_claims):
        print(test.__doc__.splitlines()[0])
        test()
    print("all passed" if not FAILED else f"{len(FAILED)} FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
