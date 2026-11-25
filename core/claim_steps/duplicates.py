"""Proposed equivalence classes of quotes, for the CLASSES stage that code does.

Two quotes are in one equivalence class when they state the same claim; one class is
one claim in the register. `vcheck propose-classes` writes a proposal, and `vcheck
accept-classes` takes it. The search: each item's nearest items by word overlap
and rare numbers, one small-model call per block of 2 items, and a pair counts only if
every call that judged it said yes (`propose`). It scales to thousands of items.
"""
from __future__ import annotations

import dataclasses
import heapq
import itertools
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping

from core.registry import run_options
from utils import claim_files, models, spending, text, text_search

# Replies in all for one call. A result for about 100 claims sometimes puts a claim in
# two equivalence classes, and each refused reply is asked for again with the rule named.
READ_ATTEMPTS = 3

# The search (see the module docstring). The numbers are the ones measured in
# the grouping benchmark of Nov 10-11.
# model, effort and timeout, when the run options do not set them
DEFAULT_MODEL, DEFAULT_EFFORT, DEFAULT_TIMEOUT = models.preset.HAIKU, "medium", 180
NEIGHBOURS = 10        # BM25 nearest items per item
CAP = 15               # candidates per item, number-sharing ones first
RARE_MAX = 30          # a number shared by more items than this is too common to link them
BLOCK = 2              # items per call
WORKERS = 64           # calls at once
KEEP = 4 * CAP         # BM25 scores kept per item (the rest are never used)
FAIL_MAX = 0.1         # above this share of failed calls, the proposal is refused
TOKEN = re.compile(r"[a-z]+|\d+(?:\.\d+)?")
# BM25 constants of the grouping benchmark; the source search (utils/text_search.py) uses 1.5
BM25_K1, BM25_B = 1.2, 0.75

# The run options of `vcheck propose-classes`; WatchedProject.check_registry adds them.
MODEL = run_options.RunOption(key="duplicates_model", kind=str, flag="--duplicates-model",
                           help="the model `vcheck propose-classes` asks, as a model "
                                f"name (utils/models.py); by default {DEFAULT_MODEL}")
EFFORT = run_options.RunOption(key="duplicates_effort", kind=str, flag="--duplicates-effort",
                            help="the reasoning effort of `vcheck propose-classes`; by "
                                 f"default {DEFAULT_EFFORT}")
TIMEOUT = run_options.RunOption(key="duplicates_timeout", kind=int, flag="--duplicates-timeout",
                             help="seconds `vcheck propose-classes` waits for one reply; by "
                                  f"default {DEFAULT_TIMEOUT}")
RUN_OPTIONS = (MODEL, EFFORT, TIMEOUT)

type AskAndRead = Callable[..., object]     # the signature of utils.models.ask_and_read
# The equivalence classes of one call; an item in none has no duplicate.


class DuplicatesError(ValueError):
    """A setting or an item is not valid. The message says which."""


@dataclasses.dataclass(frozen=True)
class Settings:
    """The model calls: which model, how much reasoning, and how long to wait.

    A model, effort or timeout left as None takes its default (DEFAULT_MODEL,
    DEFAULT_EFFORT, DEFAULT_TIMEOUT).
    """
    model: str | None = None
    effort: str | None = None
    timeout: int | None = None

    def __post_init__(self) -> None:
        """Fill the defaults, then refuse bad values.

        Refused: an empty model, an unknown effort, a timeout below 1 s.
        """
        model, effort, timeout = DEFAULT_MODEL, DEFAULT_EFFORT, DEFAULT_TIMEOUT
        for name, value in (("model", model), ("effort", effort), ("timeout", timeout)):
            if getattr(self, name) is None:
                object.__setattr__(self, name, value)
        if problem := models.settings_problem(self.model, self.effort, self.timeout):
            raise DuplicatesError(problem)

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> Settings:
        """The settings from the run options, each at its default when it is not set.

        Raises:
            DuplicatesError: an option is set to a value the settings refuse.
        """
        return cls(model=MODEL.value_in_options(options), effort=EFFORT.value_in_options(options),
                   timeout=TIMEOUT.value_in_options(options))


@dataclasses.dataclass
class CallCount:
    """The model calls that one proposal made, and how many of them failed."""
    calls: int = 0
    failed: int = 0


# The last line of every user message: with little reasoning, the model reasons in its reply.
REPLY_ONLY = "Reply with the JSON object only: no reasoning, no other text."

# The duplicate rules of the prompt.
RULES = """\
There can be duplicates among the statements. Two statements are duplicates when they \
say the same thing about the same quantity or the same result. Their words can be the \
same or different. Two statements are also duplicates when one statement says all that \
the other statement says, and more, about the same quantity or result. For example, one \
statement gives a result, and another statement gives the same result with its error bar.

Two statements are not duplicates when:
- they give different numbers, or they give the same number for different quantities;
- they are about a different model, dataset, method, metric, run, seed or condition;
- one statement limits the other statement to a narrower condition, subset or setting;
- one statement is a conclusion that rests on the other statement;
- they have the same words but a different number in double braces.

A statement can mark one of its numbers in double braces, as in "accuracy rises from 61.0 \
to {{68.4}}". The statement is then only about that number: a sentence with several \
numbers gives one statement for each, with the same words. Two such statements are about \
different results.
"""


def checked_ids(statements: Mapping[str, str]) -> list[str]:
    """The item ids, sorted, after a check that each id and its words is non-empty text.

    The model gets numbers, not ids: an id is a file key, and two ids that look alike
    say nothing about the statements.

    Raises:
        DuplicatesError: an id or the words of an item is not non-empty text.
    """
    for cid, words in statements.items():
        if not isinstance(cid, str) or not cid.strip():
            raise DuplicatesError(f"an item id must be non-empty text, got {cid!r}")
        if not isinstance(words, str) or not words.strip():
            raise DuplicatesError(f"item {cid!r} has no words to compare: {words!r}")
    return sorted(statements)


def number_in(k: object, size: int) -> bool:
    """Whether k is a whole number from 1 to size (true and false are not numbers)."""
    return isinstance(k, int) and not isinstance(k, bool) and 1 <= k <= size


# ---------------------------------------------------------------- the search

NEIGHBOURS_SYSTEM = """\
You help a program that checks the claims of a research project. The user message \
gives statements from the project's notes.

""" + RULES + """
The user message gives several statements Q1, Q2, ... and numbered candidate statements. For \
each Q, name every candidate that is a duplicate of that Q, by the rules above. A candidate \
can be the same statement as a Q; never name a Q as a duplicate of itself.

Reply with one JSON object and nothing else: {"Q1": [<candidate numbers>], "Q2": [...], ...}, \
with a key for every Q. Use [] when a Q has no duplicate among the candidates.
"""

type Pair = tuple[str, str]


def pair(a: str, b: str) -> Pair:
    """The two ids in a fixed order, so a pair is the same either way round."""
    return (a, b) if a <= b else (b, a)


def identical_pairs(statements: Mapping[str, str]) -> set[Pair]:
    """Every pair of items whose cleaned texts are the same."""
    by: dict[str, list[str]] = defaultdict(list)
    for cid, words in statements.items():
        by[text.comparable(words)].append(cid)
    return {pair(a, b) for ids in by.values() for a, b in itertools.combinations(sorted(ids), 2)}


def bm25_scores(statements: Mapping[str, str],
                also: set[Pair] = frozenset()) -> dict[str, dict[str, float]]:
    """For each item, its BM25 scores with the items nearest to it.

    A score is the higher of the two directions (each item as the query), so the scores
    are symmetric; an item has no score with itself. The query side counts each word
    once; the document side is standard BM25 (BM25_K1, BM25_B). Each item keeps its KEEP
    best scores (and the scores of items that kept it), plus the pairs in `also`, so
    memory grows with the items, not their square.
    """
    extra: dict[str, set[str]] = defaultdict(set)
    for a, b in also:
        extra[a].add(b)
    ids = sorted(statements)
    toks = {i: TOKEN.findall(statements[i].lower()) for i in ids}
    n = len(ids)
    df: Counter[str] = Counter()
    for i in ids:
        df.update(set(toks[i]))
    avg = sum(len(toks[i]) for i in ids) / max(1, n)
    weights: dict[str, dict[str, float]] = {}
    postings: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for i in ids:
        weights[i] = {w: text_search.bm25_weight(tf, df[w], n, len(toks[i]), avg,
                                                 BM25_K1, BM25_B)
                      for w, tf in Counter(toks[i]).items()}
        for w, s in weights[i].items():
            postings[w].append((i, s))
    out: dict[str, dict[str, float]] = {i: {} for i in ids}
    for i in ids:
        # i as the query scores j by j's weights; j as the query scores i by i's weights,
        # over the same shared words
        fwd: dict[str, float] = defaultdict(float)
        back: dict[str, float] = defaultdict(float)
        for w, own in weights[i].items():
            for j, s in postings[w]:
                if j != i:
                    fwd[j] += s
                    back[j] += own
        both = ((j, max(s, back[j])) for j, s in fwd.items())
        for j, s in heapq.nlargest(KEEP, both, key=lambda kv: (kv[1], kv[0])):
            out[i][j] = out[j][i] = s
        for j in extra[i]:
            if j in fwd:
                out[i][j] = out[j][i] = max(fwd[j], back[j])
    return out


def _sig(v: float, n: int) -> str:
    return f"{v:.{n}g}" if v else "0"


NUMBER = re.compile(r"(?<![\w.])([-+−]?)(\d+(?:,\d{3})*(?:\.\d+)?)(\s*\\?%)?")


def number_keys(text: str) -> set[str]:
    """The forms of each informative number in the text, for linking items by number.

    Small whole numbers (counts, list items) and years are left out. A percentage and its
    fraction get the same key (71.3% and 0.713), at 2 and 3 significant figures, so a
    rounded copy still links.
    """
    keys = set()
    for _sign, body, pct in NUMBER.findall(text):
        s = body.replace(",", "")
        a = abs(float(s))
        whole = "." not in s
        if whole and (a <= 10 or 1900 <= a <= 2100):
            continue
        forms = {a, a / 100} if pct else ({a, a * 100} if a < 1 else {a, a / 100})
        for f in forms:
            keys.add(_sig(f, 3))
            keys.add(_sig(f, 2))
    return keys


def number_pairs(statements: Mapping[str, str]) -> set[Pair]:
    """Pairs of items that share a number key used by at most RARE_MAX items."""
    by: dict[str, set[str]] = defaultdict(set)
    for cid, words in statements.items():
        for key in number_keys(words):
            by[key].add(cid)
    return {pair(a, b) for members in by.values() if 2 <= len(members) <= RARE_MAX
            for a, b in itertools.combinations(sorted(members), 2)}


def neighbour_lists(statements: Mapping[str, str]) -> dict[str, list[str]]:
    """Each item's candidates, at most CAP; an item with no candidate is left out.

    The candidates are its NEIGHBOURS best BM25 items plus every item it shares a rare
    number with, number-sharing first, then by BM25 score, then by id.
    """
    nums = number_pairs(statements)
    scores = bm25_scores(statements, nums)
    cands: dict[str, set[str]] = defaultdict(set)
    for i, row in scores.items():
        for j in heapq.nlargest(NEIGHBOURS, row, key=lambda j, row=row: (row[j], j)):
            cands[i].add(j)
            cands[j].add(i)
    for a, b in nums:
        cands[a].add(b)
        cands[b].add(a)
    return {q: sorted(cs, key=lambda o, q=q: (pair(q, o) not in nums,
                                              -scores[q].get(o, 0.0), o))[:CAP]
            for q, cs in sorted(cands.items())}


def make_blocks(lists: Mapping[str, list[str]],
                settled: frozenset[str] = frozenset()) -> list[list[str]]:
    """Blocks of BLOCK items: an item, then its nearest candidates not yet in a block.

    A settled item is never in a block: it is only a candidate of the others.
    """
    left, out = set(lists) - settled, []
    for q in sorted(lists):
        if q not in left:
            continue
        block = [q]
        left.discard(q)
        for c in lists[q]:
            if len(block) == BLOCK:
                break
            if c in left:
                block.append(c)
                left.discard(c)
        out.append(block)
    return out


def block_text(statements: Mapping[str, str], qs: list[str], cands: list[str]) -> str:
    """The user message of one block call: the Qs, then the numbered candidates."""
    message = "Statements Q:\n" + "\n".join(f"Q{j}. {text.on_one_line(statements[q])}"
                                             for j, q in enumerate(qs, 1))
    message += "\n\nCandidates:\n" + "\n".join(f"{i}. {text.on_one_line(statements[c])}"
                                                for i, c in enumerate(cands, 1))
    return message + "\n\n" + REPLY_ONLY


def block_candidates(qs: list[str], lists: Mapping[str, list[str]]) -> list[str]:
    """The union of the block's short lists, each item once, then the block's own items."""
    cands: list[str] = []
    for q in qs:
        for c in lists.get(q, []):
            if c not in cands:
                cands.append(c)
    for q in qs:
        if q not in cands:
            cands.append(q)
    return cands


def read_block(text: str, qs: list[str], cands: list[str]) -> dict[str, dict[str, bool]]:
    """For each Q, whether each candidate (other than Q) is its duplicate.

    The reply names them by number, such as {"Q1": [2, 5], "Q2": []}.

    Raises:
        models.BadReply: no list for a Q, or a number that is not a candidate's.
    """
    raw = models.read_json(text)
    if not isinstance(raw, dict):
        raise models.BadReply('reply with one object: {"Q1": [numbers], ...}')
    out = {}
    for j, q in enumerate(qs, 1):
        v = raw.get(f"Q{j}")
        if not isinstance(v, list):
            raise models.BadReply(f'the reply has no list for "Q{j}"')
        yes = set()
        for n in v:
            if isinstance(n, str) and n.strip().isascii() and n.strip().isdecimal():
                n = int(n)
            if not number_in(n, len(cands)):
                raise models.BadReply(f'"Q{j}": {n!r} is not a candidate number, 1 to '
                                      f"{len(cands)}")
            yes.add(cands[n - 1])
        out[q] = {c: c in yes for c in cands if c != q}
    return out


def group_strict(scored: Mapping[Pair, float], ok: set[Pair],
                 tie: Callable[[str, str], float]) -> claim_files.Partition:
    """Groups from yes-pairs, best first; only groups of 2 or more items are returned.

    Two groups merge only if every pair across them is in `ok`. Ties go to the pair with
    more shared words (`tie`), then to the ids, so the result does not depend on set order.
    """
    group: dict[str, set[str]] = {}
    for (a, b), _ in sorted(scored.items(), key=lambda kv: (-kv[1], -tie(*kv[0]), kv[0])):
        ga, gb = group.get(a, {a}), group.get(b, {b})
        if ga is gb:
            continue
        if all(pair(x, y) in ok for x in ga for y in gb):
            g = ga | gb
            for x in g:
                group[x] = g
    seen, out = set(), []
    for g in group.values():
        if id(g) not in seen and len(g) > 1:
            seen.add(id(g))
            out.append(frozenset(g))
    return sorted(out, key=lambda g: sorted(g))


def decide(statements: Mapping[str, str], answers: Mapping[str, Mapping[str, bool]],
           unjudged: set[Pair] = frozenset(),
           settled: frozenset[str] = frozenset(),
           values: Mapping[str, str | None] | None = None) -> claim_files.Partition:
    """The equivalence classes from the calls' answers.

    A pair is a duplicate only if every call that judged it said yes (a pair is usually
    judged twice, once from each side). Identical texts are duplicates with no call, and
    are grouped first. A pair no call judged is not a duplicate, nor is a pair in
    `unjudged` (a failed call would have judged it, so its vote is missing), nor a pair
    of two settled items, nor a pair whose `values` differ (`claim_files.same_value`).
    """
    said: dict[Pair, list[bool]] = defaultdict(list)
    for q, row in answers.items():
        for c, yes in row.items():
            said[pair(q, c)].append(yes)
    ok = {p for p, votes in said.items() if all(votes) and p not in unjudged}
    same = identical_pairs(statements)
    ok |= same
    values = values or {}
    ok = {(a, b) for a, b in ok if not (a in settled and b in settled)
          and claim_files.same_value(values.get(a), values.get(b))}
    scored = {p: (100.0 if p in same else 1.0) for p in ok}
    words = {cid: set(re.findall(r"\w+", t.lower())) for cid, t in statements.items()}
    jaccard = lambda a, b: len(words[a] & words[b]) / max(1, len(words[a] | words[b]))  # noqa: E731
    return group_strict(scored, ok, jaccard)


def propose(statements: Mapping[str, str], settings: Settings,
            ask_and_read: AskAndRead | None = None,
            call_count: CallCount | None = None,
            settled: frozenset[str] = frozenset(),
            values: Mapping[str, str | None] | None = None) -> claim_files.Partition:
    """The proposed partition of the items into equivalence classes (see the module docstring).

    Settled items (earlier classes) are only candidates: no block is built for them, and
    two of them are never grouped. A block whose call fails after its retries is left
    out, and every pair it would have judged counts as not a duplicate, so a failure can
    miss a duplicate but never adds one. `call_count`, when given, gets the calls made
    and failed.

    Returns:
        The equivalence classes; with fewer than two items, none.

    Raises:
        DuplicatesError: an id or the words of an item is empty, or more than FAIL_MAX
            of the calls failed (no proposal is written then).
        models.KeyMissing: no API key; no further call is made.
    """
    ids = checked_ids(statements)
    if len(ids) < 2:
        return []
    ask = models.ask_and_read if ask_and_read is None else ask_and_read
    lists = neighbour_lists(statements)
    blocks = make_blocks(lists, settled)

    def one(qs: list[str]) -> dict[str, dict[str, bool]]:
        cands = block_candidates(qs, lists)
        # each runs in a pool thread, which does not get the caller's purpose
        with spending.purpose("duplicates"):
            return ask(block_text(statements, qs, cands), settings.model,
                       lambda text: read_block(text, qs, cands), settings.timeout,
                       system=NEIGHBOURS_SYSTEM, reasoning=settings.effort,
                       read_attempts=READ_ATTEMPTS)

    # each block's answer, or None when its call failed, kept in block order so the
    # proposal does not depend on which call finishes first
    results: list[dict[str, dict[str, bool]] | None] = [None] * len(blocks)

    def keep(k: int, answer: dict[str, dict[str, bool]]) -> None:
        results[k] = answer

    # a failed block stays None in results, and counts as failed below
    models.call_all_at_once([lambda b=b: one(b) for b in blocks], WORKERS, keep,
                            tolerated=(models.ProviderError, models.BadReply))
    answers: dict[str, dict[str, bool]] = {}
    unjudged: set[Pair] = set()
    failed = 0
    for qs, result in zip(blocks, results, strict=True):
        if result is None:
            failed += 1
            unjudged |= {pair(q, c) for q in qs for c in block_candidates(qs, lists) if c != q}
        else:
            answers.update(result)
    if call_count is not None:
        call_count.calls, call_count.failed = len(blocks), failed
    if blocks and failed > FAIL_MAX * len(blocks):
        raise DuplicatesError(f"{failed} of {len(blocks)} model calls failed, more than "
                              f"{FAIL_MAX:.0%}; no proposal was written. Check the model "
                              f"({settings.model}) and the API key, then run the command again")
    return decide(statements, answers, unjudged, settled, values)
