"""Keep-or-drop labels for claim candidates from direct calls to a small model.

`vcheck label-candidates` asks for each candidate a keep score (0 to 100), a label (claim,
setup, background or junk) and the values it reports. A candidate at or above the cutoff
becomes one quote of its whole text for each value, each quote with its own value (or one
quote with no value; a whole table is always one quote with no value); one below becomes
a drop "auto:label <score>". A kept candidate labelled "setup" goes to the setup register
(claim_files.SETUP_REGISTER_FILE) in place of a quote. Answers are cached in the state
directory's cache/ folder, so a rerun gives the same files.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import re
import time
from collections.abc import Callable, Collection, Mapping
from pathlib import Path

from core.registry import run_options
from utils import alerts, candidate_rules, claim_files, extract, file_storage, models, spending

# The run options of `vcheck label-candidates`; core/project/watched_project.py adds them.
BATCH = run_options.RunOption(key="label_batch", kind=int, flag="--label-batch", default=1,
                           help="candidates in one call of vcheck label-candidates (1: one "
                                "candidate a call)")
CUTOFF = run_options.RunOption(key="label_cutoff", kind=int, flag="--label-cutoff", default=50,
                            help="vcheck label-candidates keeps a candidate whose keep score is at "
                                 "least this")
MODEL = run_options.RunOption(key="label_model", kind=str, flag="--label-model",
                           default=models.preset.HAIKU,
                           help="the model vcheck label-candidates asks (utils/models.py names)")
EFFORT = run_options.RunOption(key="label_effort", kind=str, flag="--label-effort", default="off",
                            help="the reasoning effort of vcheck label-candidates; off for none")
TIMEOUT = run_options.RunOption(key="label_timeout", kind=int, flag="--label-timeout",
                             default=120,
                             help="seconds vcheck label-candidates waits for one reply")
CACHE = run_options.RunOption(key="label_cache", kind=bool, flag="--label-cache", default=True,
                           help="read and write the answers of vcheck label-candidates in "
                                "the cache (off: ask again, to measure changed answers)")
SEED = run_options.RunOption(key="label_seed", kind=int, flag="--label-seed", default=0,
                          help="the seed of the order vcheck label-candidates sends candidates in")
EXAMPLES = run_options.RunOption(key="label_examples", kind=bool, flag="--label-examples",
                                 default=True,
                                 help="put the fixed labelled candidates "
                                      "(core/claim_steps/label_examples.json) in the prompt of "
                                      "vcheck label-candidates, with their answers")
RUN_OPTIONS = (BATCH, CUTOFF, MODEL, EFFORT, TIMEOUT, CACHE, SEED, EXAMPLES)

LABELS = ("claim", "setup", "background", "junk")
NO_EFFORT = "off"
PURPOSE = "label"               # the spending purpose of the calls (models.CLI_PURPOSE_ROLES)
PROMPT_VERSION = 10             # change it when the prompt changes, so the cache is not reused
CACHE_FILE = Path("cache") / "label_answers.json"     # in the state directory
# The labelled candidates of option label_examples, each with its label and its answer.
EXAMPLES_FILE = Path(__file__).with_name("label_examples.json")
WORKERS = 64                    # calls at once
# While the calls run, the cache is saved after this many new answers or seconds, so
# a run that is killed (the daemon's step timeout) keeps most of what it paid for.
SAVE_EVERY = 50
SAVE_SECONDS = 30.0
READ_ATTEMPTS = 3
SPACES = re.compile(r"\s+")

type AskAndRead = Callable[..., object]     # the signature of utils.models.ask_and_read
type Label = dict[str, object]              # {"keep", "label", "values"}


class LabelError(ValueError):
    """A setting of the labeller is not valid. The message says which."""


@dataclasses.dataclass(frozen=True)
class Settings:
    """How `vcheck label-candidates` asks: candidates per call, the cutoff, the model, and more."""
    batch: int = 1
    cutoff: int = 50
    model: str = models.preset.HAIKU
    effort: str = NO_EFFORT
    timeout: int = 120
    cache: bool = True
    seed: int = 0
    examples: bool = True

    def __post_init__(self) -> None:
        """Refuse a value outside its range or list.

        Raises:
            LabelError: the setting named in the message is not valid.
        """
        if self.batch < 1:
            raise LabelError(f"label_batch must be at least 1, got {self.batch}")
        if not 0 <= self.cutoff <= 100:
            raise LabelError(f"label_cutoff must be from 0 to 100, got {self.cutoff}")
        if problem := models.settings_problem(self.model, self.effort, self.timeout,
                                              (MODEL.key, EFFORT.key, TIMEOUT.key), NO_EFFORT):
            raise LabelError(problem)

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> Settings:
        """The settings from the run options, each at its default when it is not set."""
        return cls(batch=BATCH.value_in_options(options),
                   cutoff=CUTOFF.value_in_options(options),
                   model=MODEL.value_in_options(options), effort=EFFORT.value_in_options(options),
                   timeout=TIMEOUT.value_in_options(options), cache=CACHE.value_in_options(options),
                   seed=SEED.value_in_options(options),
                   examples=EXAMPLES.value_in_options(options))

    def mode(self) -> str:
        """The settings that change an answer, for the cache key.

        The examples are named only when they are on, with a hash of the examples file,
        so an edit to the file asks again.
        """
        return (f"batch={self.batch}|effort={self.effort}"
                + (f"|examples={examples_hash()}" if self.examples else ""))


def examples_hash() -> str:
    """The start of the SHA-256 of the examples file (option label_examples), for the cache key."""
    return hashlib.sha256(EXAMPLES_FILE.read_bytes()).hexdigest()[:16]


@dataclasses.dataclass
class LabelCount:
    """How many candidates one labelling had, the calls it made, and the cached answers."""
    pieces: int = 0
    calls: int = 0
    cached: int = 0


# ---------------------------------------------------------------- the prompt

# What the labeller keeps: the claims, and a second kept kind, a specific setup fact labelled
# "setup", for the setup register. Tuned on dev papers only (detail-register-test, test 5,
# rule v3).
KEEP_RULE = """\
You sort the pieces of the notes of a research project (a paper, a draft, lab notes or \
commit messages) into two registers. A reviewer reads both to find errors in the notes.

The claim register. A claim is a statement about this project's work that a reviewer could \
check and find false. Keep a piece, labelled "claim", when it states a claim of one of \
these kinds:

1. A result: a number that the authors measured or computed, with what it measures.
2. A comparison of two results of this project, or of a result of this project with a \
result that the authors cite.
3. A finding in words about the project's runs or data: a difference, a trend, or a \
qualitative observation. A hedge ("seems to", "about") does not change this.
4. A conclusion that the authors draw from the results, at any level.
5. A claim about this project's work that rests on cited work.

The setup register. Keep a piece, labelled "setup", when it states a specific fact about \
how this project's work is set up or defined, one that a reviewer could check against the \
rest of the notes and find false: which model, data, metric, test or setting is used and \
how; a hyperparameter or another number of the setup; a definition or notation; an \
equation; a step of the method or algorithm. Do not keep setup text that states no \
specific fact, such as motivation, or a general description of the approach that names \
nothing a reviewer could check.

Drop every other piece: a heading, a pointer to a table or figure, background about other \
work (with or without a citation), a plan or future work, an expectation, a question, an \
instruction, a recommendation, text of the venue's template or checklist, thanks or \
credits, a commit message with no finding, provenance, a description of a document, \
bookkeeping, a command or a log line, and a number alone. A table of results is kept.

When a piece describes the method and also claims something about it (what it can do, what \
it is better at, what it costs, or what the paper contributes), label it "claim".

A caption, or a sentence about a figure or table, is a pointer only when it states nothing \
else. When it names the data, models, metrics or settings used, label it "setup" (or \
"claim" when it states a result). A description of a task, dataset, benchmark or tool that \
this project uses is setup, even when it cites where it comes from.
"""

KEEP_SCORE = """\
For each piece, reply with:
- "keep": a whole number from 0 to 100: how sure you are that the piece should be kept \
(100: surely keep; 0: surely drop; 50: unsure);
"""

LABEL_LINE = """\
- "label": "claim" (a claim of this project), "setup" (setup or a definition), \
"background" (other papers' work) or "junk" (heading, pointer, template, thanks);
"""

REPLY_FORM = """
Reply with one JSON object keyed by the piece numbers, such as \
{"1": {"keep": 90, "label": "claim", "values": ["0.42", "0.31"]}, \
"2": {"keep": 5, "label": "junk", "values": []}}. \
Reply with the JSON object only: no reasoning, no other text."""


# The line of the reply that says which numbers are values. Code makes one quote for each
# value (`apply_labels`), so a value is one claim.
VALUES_RULE = ('- "values": each number that the piece reports as a claim or setup fact, copied '
               "as it is printed in the text, as a string. Each end of an interval or a range "
               "is its own value. These are not values: a number that names a condition (a model "
               "size, a sample size, a number of seeds, a confidence level), an error bar or a "
               "standard deviation, a number that the piece cites from other work, and a number "
               "inside a name, such as a model name. A dropped piece, a kept piece with no "
               "value, and a whole table (kind: table), which is one claim, get [].\n")


def reply_text() -> str:
    """What the reply holds for each piece (keep score, label, values), and its form."""
    return KEEP_SCORE + LABEL_LINE + VALUES_RULE + REPLY_FORM


def example_text(example: Mapping[str, object]) -> str:
    """The text of an example piece: what follows "text: " in its block."""
    return str(example["block"]).split("\ntext: ", 1)[1]


def examples_text() -> str:
    """The labelled candidates of option label_examples, each with its answer.

    Each answer has the example's label, as the reply must.
    """
    ex = json.loads(EXAMPLES_FILE.read_text())
    parts = ["Here are labelled pieces from other projects, each with the answer it should get "
             "(in a real reply, answers are keyed by piece number):"]
    for n, e in enumerate(ex["examples"], 1):
        given = e["answer"]
        answer = {"keep": given["keep"], "label": e["label"], "values": given["values"]}
        ans = json.dumps(answer, ensure_ascii=False)
        parts.append(f"Example {n} ({given['why']})\n{e['block']}\nAnswer: {ans}")
    return "\n\n".join(parts)


def system_prompt(examples: bool = False) -> str:
    """The system part: what to keep, the labelled examples if asked, and the reply's form."""
    middle = examples_text() + "\n\n" if examples else ""
    return KEEP_RULE + "\n" + middle + reply_text()


@dataclasses.dataclass(frozen=True)
class Item:
    """One piece the model labels: one candidate."""
    block: str                       # the piece as the prompt shows it, without its number
    cid: str                         # the candidate its label goes to


def piece_block(c: Mapping[str, object]) -> str:
    """One candidate as the prompt shows it: its kind, its context fields, then its text."""
    lines = [f"kind: {c.get('kind')}"]
    if c.get("section"):
        lines.append(f"section: {c['section']}")
    if c.get("previous"):
        lines.append(f"sentence before: {c['previous']}")
    lines.append(f"text: {c.get('text', '')}")
    return "\n".join(lines)


def items(cands: Mapping[str, Mapping[str, object]]) -> list[Item]:
    """The pieces to label: one per candidate. A table is one candidate (utils/extract.py)."""
    return [Item(piece_block(c), cid) for cid, c in cands.items()]


def calls(todo: list[Item], batch: int) -> list[list[Item]]:
    """The pieces of each call: `batch` to a call, in the order given."""
    return [todo[n:n + batch] for n in range(0, len(todo), batch)]


def call_text(pieces: list[Item]) -> str:
    """The user part of one call: the pieces, numbered from 1."""
    return "\n\n".join(f"Piece {n}\n{it.block}" for n, it in enumerate(pieces, 1))


def read_labels(text: str, count: int) -> list[dict]:
    """The answers of one reply, in piece order.

    Raises:
        models.BadReply: the reply is not one object with an answer of the right form
            for each piece number from 1 to `count`.
    """
    data = models.read_json(text)
    want = {str(n) for n in range(1, count + 1)}
    if not isinstance(data, dict) or set(data) != want:
        raise models.BadReply(f"the reply must be one JSON object with the keys "
                              f"{', '.join(sorted(want, key=int))}, one per piece")
    out = []
    for n in range(1, count + 1):
        a = data[str(n)]
        if not isinstance(a, dict):
            raise models.BadReply(f'piece {n}: the answer must be an object')
        keep, values = a.get("keep"), a.get("values")
        if isinstance(keep, bool) or not isinstance(keep, int) or not 0 <= keep <= 100:
            raise models.BadReply(f'piece {n}: "keep" must be a whole number from 0 to 100')
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise models.BadReply(f'piece {n}: "values" must be a list of numbers, each a '
                                  "string as printed in the text")
        label = a.get("label")
        if label not in LABELS:
            raise models.BadReply(f'piece {n}: "label" must be one of {", ".join(LABELS)}')
        out.append({"keep": keep, "label": label, "values": values})
    return out


# ---------------------------------------------------------------- labelling

def cache_key(it: Item, settings: Settings, mode: str | None = None) -> str:
    """The cache key of one piece: its text and context, the prompt version, model and mode.

    Through the claude CLI, the model asked for is mapped to a Claude model by the call's
    purpose and the CLI's model settings (models.cli_call), so the key also names the
    Claude model the call gets: an answer is not reused when that model changes. `mode`
    is settings.mode(), when the caller has it already.
    """
    mode = settings.mode() if mode is None else mode
    called = [models.label(settings.model, PURPOSE)] if models.uses_cli(settings.model) else []
    raw = json.dumps([it.block, PROMPT_VERSION, settings.model, mode, *called])
    return hashlib.sha256(raw.encode()).hexdigest()


def label_candidates(cands: Mapping[str, Mapping[str, object]], settings: Settings,
                     ask: AskAndRead | None = None, cache: dict | None = None,
                     label_count: LabelCount | None = None,
                     save: Callable[[Mapping[str, object]], None] | None = None
                     ) -> dict[str, Label]:
    """Ask the model for a keep score and the values for each candidate.

    Pieces are sent in an order shuffled with the settings' seed. A piece found in
    `cache` is not sent again; each new answer is added to it as its call returns. The
    first failed call stops the calls not yet started; the answers of those already
    running are still added, and then the error is raised.

    Args:
        cands: the candidates to label, by id.
        settings: how to ask (Settings).
        ask: models.ask_and_read, or a stand-in in tests.
        cache: answers by cache_key, read and updated in place; None for no cache.
        label_count: filled with the number of pieces, calls and cached answers.
        save: called with the cache every SAVE_EVERY new answers or SAVE_SECONDS while
            the calls run; None to save nothing here.

    Returns:
        {candidate id: {"keep": score, "label": one of LABELS, "values": [...]}}.

    Raises:
        models.ProviderError, models.BadReply: a call failed after its retries.
    """
    ask = models.ask_and_read if ask is None else ask
    cache = {} if cache is None else cache
    todo = items(cands)
    random.Random(settings.seed).shuffle(todo)
    plan = calls(todo, settings.batch)
    mode = settings.mode()
    keyed = [(pieces, [cache_key(it, settings, mode) for it in pieces]) for pieces in plan]
    answers: dict[str, dict] = {}
    to_ask = []
    for pieces, keys in keyed:
        if all(k in cache for k in keys):
            answers.update({k: cache[k] for k in keys})
        else:
            to_ask.append((pieces, keys))
    system = system_prompt(settings.examples)
    reasoning = None if settings.effort == NO_EFFORT else settings.effort

    def one(pieces: list[Item]) -> list[dict]:
        # each runs in a pool thread, which does not get the caller's purpose
        with spending.purpose(PURPOSE):
            return ask(call_text(pieces), settings.model,
                       lambda text: read_labels(text, len(pieces)),
                       settings.timeout, system=system, reasoning=reasoning,
                       read_attempts=READ_ATTEMPTS)

    unsaved, saved_at = 0, time.monotonic()

    def keep(k: int, result: list[dict]) -> None:
        nonlocal unsaved, saved_at
        for key, answer in zip(to_ask[k][1], result, strict=True):
            cache[key] = answers[key] = answer
        unsaved += len(result)
        if save is not None and (unsaved >= SAVE_EVERY
                                 or time.monotonic() - saved_at >= SAVE_SECONDS):
            save(cache)
            unsaved, saved_at = 0, time.monotonic()

    # The first failed call stops the calls not yet started, and is raised once the
    # calls already running have given their (paid) answers to `keep`.
    models.call_all_at_once([lambda pieces=pieces: one(pieces) for pieces, _ in to_ask],
                            WORKERS, keep, finish_running=True)
    if label_count is not None:
        label_count.pieces, label_count.calls = len(todo), len(to_ask)
        label_count.cached = len(plan) - len(to_ask)
    return {it.cid: {"keep": answers[key]["keep"], "label": answers[key]["label"],
                     "values": answers[key]["values"]}
            for pieces, keys in keyed for it, key in zip(pieces, keys, strict=True)}


# ---------------------------------------------------------------- quotes and drops

def quote_values(c: Mapping[str, object], label: Label) -> list[str]:
    """The values of a kept candidate, in order, each once; [] for a candidate with none.

    A whole table has none: it is one claim. Any other candidate has the values the model
    listed, each read as `extract.claim_numbers` reads the text; a listed value that the
    text does not print is left out, with a warning.
    """
    if c.get("kind") == extract.TABLE:
        return []
    text = str(c.get("text", ""))
    printed = set(extract.claim_numbers(text))
    values = []
    for listed in label["values"]:
        found = extract.claim_numbers(str(listed))
        if len(found) == 1 and found[0] in printed:
            values.append(found[0])
        else:
            alerts.notify("warn", "labeller.value_not_in_text",
                          f"the model listed the value {listed!r}, which the candidate "
                          "does not print; it is left out", text=text[:300])
    return list(dict.fromkeys(values))


def quote_id(cid: str, taken: Collection[str] = ()) -> str:
    """The id of the quote of a candidate: q-<candidate id>, with no spaces.

    Two candidate ids can give the same text ("a b" and "a_b"), and a quote of the agent's
    can have it already: an id in `taken` gets "-2", "-3", ... after it until it is free.
    """
    return extract.free_id(f"q-{SPACES.sub('_', cid)}", taken)


def apply_labels(cands: Mapping[str, Mapping[str, object]], labels: Mapping[str, Label],
                 cutoff: int, repeats: Mapping[str, str] | None = None,
                 quoted: Mapping[str, Collection[str]] | None = None,
                 taken: Collection[str] = ()) -> tuple[dict[str, dict], dict[str, str]]:
    """Turn labels into quotes and drops.

    A candidate whose keep score is at least `cutoff` gets one quote of its whole text that
    lists its values (`quote_values`), split into one quote for each value
    (`claim_files.split_values`); a whole table is one quote with no value. One below it
    gets a drop "auto:label <score>". A candidate that the rules dropped as a repeat
    (`repeats`: candidate id to its twin) keeps that drop while its twin's quotes answer for
    it (`candidate_rules.holds`), whether written before (`quoted`: quote words by candidate
    id) or kept here; otherwise its own label decides. No quote id is one in `taken` (the
    quotes there already) or one given twice here.

    A kept candidate labelled "setup" gets a drop "auto:setup <score>" in place of a quote:
    it goes to the setup register. A twin
    there has no quote, so its repeats are decided by their own labels.

    Returns:
        (quotes by quote id, as {"candidates": [id], "quote": words, "values": [...]};
         drop reasons by candidate id).
    """
    repeats = repeats or {}
    quoted = quoted or {}
    quotes: dict[str, dict] = {}
    drops: dict[str, str] = {}
    used = set(taken)

    def to_setup(a: Label) -> bool:
        return a.get("label") == "setup"

    kept = {cid: [str(cands[cid].get("text", ""))] for cid, a in labels.items()
            if a["keep"] >= cutoff and cid not in repeats and not to_setup(a)}
    for cid, a in labels.items():
        twin = repeats.get(cid)
        if twin is not None and candidate_rules.holds(cands[cid], [*quoted.get(twin, []),
                                                                   *kept.get(twin, [])]):
            continue
        if a["keep"] < cutoff:
            drops[cid] = f"auto:label {a['keep']}"
        elif to_setup(a):
            drops[cid] = f"{claim_files.AUTO_SETUP}{a['keep']}"
        else:
            qid = quote_id(cid, used)
            used.add(qid)
            quotes[qid] = {"candidates": [cid], "quote": str(cands[cid].get("text", "")),
                           claim_files.VALUES_KEY: quote_values(cands[cid], a)}
    return claim_files.split_values(quotes, used), drops


def read_cache(state_dir: Path) -> dict:
    """The cached answers in the state directory, or {} when there is no cache file yet.

    Raises:
        ValueError: the cache file is not a JSON object.
    """
    store = file_storage.JsonFile(state_dir / CACHE_FILE)
    if not store.exists():
        return {}
    answers = store.read()
    if not isinstance(answers, dict):
        raise ValueError(f"{state_dir / CACHE_FILE} is not a JSON object")
    return answers


def write_cache(state_dir: Path, cache: Mapping[str, object]) -> None:
    """Write the cached answers at once, sorted, so the file changes only where answers changed."""
    (state_dir / CACHE_FILE).parent.mkdir(parents=True, exist_ok=True)
    store = file_storage.JsonFile(state_dir / CACHE_FILE, indent=0, ensure_ascii=False)
    store.write(dict(sorted(cache.items())))
