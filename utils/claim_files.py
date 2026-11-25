"""The five claim files of a state directory, kept across rounds, and the checks on them.

Before each round, `begin_round` brings the files in step with the notes a person
selected (`claim_scope.json`). Code then does three stages (QUOTES, CLASSES, REGISTER),
each with its check (CLAIM_STEPS in runtime/daemon.py), and the agent gives sources to the
claims. An entry of an earlier round cannot change, except as `register_problems` allows.
`stamp` writes the checked commit in the empty stamps when the three checks pass.
"""
from __future__ import annotations

import copy
import dataclasses
import decimal
import glob
import json
import os
import re
from collections.abc import Collection, Mapping
from pathlib import Path

from schema import inputs
from schema.claim import claim_register_parser
from utils import candidate_rules, checksums, extract, file_storage, structure

CANDIDATES = "claim_candidates.json"
QUOTES = "claim_quotes.json"
DROPPED = "dropped.json"
CLASSES = "claim_equiv_classes.json"
REGISTER = "claim_register.json"
FILES = (CANDIDATES, QUOTES, DROPPED, CLASSES, REGISTER)
ROUND = "claim_round.json"
SCOPE = "claim_scope.json"                   # the notes a person selected for claims
PROPOSALS_DIR = "tmp"
# Held by every process that reads claim files and then writes them: the vcheck commands,
# the daemon and the pin editor of a review (core/gate/artifact_pins.py). It is in
# runtime/, which the state's git history leaves out.
LOCK_FILE = Path("runtime") / "claim_files.lock"

NEW = ""                                     # the stamp of an entry of the round in progress
STAMPS = ("created", "last_updated")
# A quote with N values is N quotes with the same words, each with one value ("value", a
# number as `extract.claim_numbers` gives it, or null for a quote with no value). A new
# quote can list its values instead ("values"); `split_values` makes the N quotes.
QUOTE_KEYS = frozenset({*STAMPS, "candidates", "quote", "value"})
VALUES_KEY = "values"
DROP_KEYS = frozenset({*STAMPS, "why"})
# The setup register, in the state directory: {candidate id: {"text", "kind", "section",
# "keep"}} in paper order. The labeller sends each specific, checkable setup fact (labelled
# "setup") to it, with a drop "auto:setup <score>", so only claims reach the classes. It is
# not one of the claim FILES. Claims plus setup kept 33 of 37 known errors on the tuning
# papers, against 16 with only the claims.
SETUP_REGISTER_FILE = "setup_register.json"
# A kept table is one claim: one quote of the whole table, with no value, whose claim code
# writes. The duplicates models see no quote of a whole table (`proposal_items`).
# The most characters of a table claim's statement that code writes, or that a prompt
# shows for any table claim (the linker's register): the paper is in the same prompt.
TABLE_STATEMENT_CHARS = 5000


# A reason "repeat of <quote id>" names a quote that exists. The core's drops start "auto:",
# and each must be what the core gives that candidate now: the rules' reason
# (utils/candidate_rules.py), or the labeller's score on record (ClaimRound.keep_scores).
REPEAT_OF = re.compile(r"repeat of (?:quote )?(\S+?)[.,;:]?(?:\s|$)", re.IGNORECASE)
# The core's drops start "auto:". A reason that starts so in any case ("Auto:", "AUTO:")
# counts as one, and must be what the core gives the candidate (_auto_problems).
AUTO = "auto:"
AUTO_RULE = re.compile(r"auto:R\d+\b", re.IGNORECASE)
AUTO_LABEL = "auto:label "
AUTO_SETUP = "auto:setup "
CLASS_KEYS = frozenset({*STAMPS, "quotes"})
# Register keys the core writes; the agent's view of an entry leaves them out.
CORE_KEYS = ("last_updated", "quotes", "origin")
PIN_KEYS = ("sha256", "proposed_sha256")
# Source keys the core writes: the pins, and the method `vcheck write-methods` gives.
CORE_SOURCE_KEYS = (*PIN_KEYS, "method")
# The keys of a quote entry in the old format, where a quote also held the register keys.
OLD_QUOTE_KEYS = frozenset({"statement", "source", "keys", "superseded_by"})
# The method an artifact claim names until `vcheck write-methods` gives it one.
NO_METHOD_YET = "(written later by vcheck write-methods)"
ID = re.compile(r"\S+")
LISTED = 40                                  # problems or new entries printed in full
# The candidate kinds whose quotes read as sentences, which a statement is taken from first.
SENTENCE_KINDS = frozenset({extract.SENTENCE, extract.COMMIT})
# Two values are one value when they differ by at most the larger of: this share of the
# larger value, and VALUE_RESOLUTIONS times the weight of the lowest printed digit of the
# cruder value (`same_value`). The quotes of one class must have one value
# (`class_problems`), and grouping never joins two quotes whose values differ.
VALUE_RELATIVE_TOLERANCE = 0.01
VALUE_RESOLUTIONS = 2

# One proposal of `vcheck propose-classes`: the classes, each a set of item ids.
type Partition = list[frozenset[str]]
type Entries = dict[str, dict]


class ClaimFilesError(ValueError):
    """The claim files cannot be used: some are missing, or one is in an old format."""


class ClaimFilesBroken(ClaimFilesError):
    """A claim file is not valid JSON, or not the right shape. The agent can fix it."""


@dataclasses.dataclass
class ClaimFiles:
    """The five claim files as read: four maps from id to entry, and the register's claims."""
    candidates: Entries
    quotes: Entries
    dropped: Entries
    classes: Entries
    register: list[dict]
    # the candidates in the setup register (keep rule claims_plus_setup), for the check of
    # their "auto:setup <score>" drops
    setup_ids: frozenset[str] = frozenset()

    @classmethod
    def empty(cls) -> ClaimFiles:
        """The files of a working directory with no round yet."""
        return cls({}, {}, {}, {}, [])

    def claim(self, claim_id: str) -> dict | None:
        """The register entry with this id, or None."""
        return next((e for e in self.register if isinstance(e, dict) and e.get("id") == claim_id),
                    None)


@dataclasses.dataclass
class ClaimRound:
    """What the checks of one round need to know beyond the five claim files.

    claim_round.json holds it. It is not runtime/rounds.Round, which records the batch of
    events that the daemon sends to the agent; the daemon begins and ends both.
    """
    commit: str                              # the commit the round checks
    first: bool                              # no entry was stamped when the round began
    # the entries of earlier rounds, whole, as they were when the round began; only a pin
    # change that core/gate/artifact_pins.py makes (`record_pins`) changes them later
    existing: dict[str, Entries]
    # what the core wrote in this round: the pin of each claim it pinned, and the method
    # of each claim `vcheck write-methods` gave one
    pinned: dict[str, object] = dataclasses.field(default_factory=dict)
    # the checksum that a person was asked to confirm (`record_pins`), of each claim pinned
    # in this round that has one
    proposed: dict[str, object] = dataclasses.field(default_factory=dict)
    methods: dict[str, str] = dataclasses.field(default_factory=dict)
    stamped: bool = False                    # the round ended with its checks passed
    # the keep score of each candidate `vcheck label-candidates` dropped in this round: a new
    # drop "auto:label <score>" stands only if it is here with that score
    keep_scores: dict[str, int] = dataclasses.field(default_factory=dict)
    # the statement `vcheck fill-register` wrote for each of its claims in this round, which
    # the agent must not rewrite; kept for the whole round
    written: dict[str, str] = dataclasses.field(default_factory=dict)


# ---------------------------------------------------------------- reading and writing

def load_setup_register(state_dir: Path) -> dict[str, dict]:
    """The setup register (SETUP_REGISTER_FILE) in the state directory, or {} when there is none.

    Raises:
        ClaimFilesError: the file is not a JSON object of objects.
    """
    return read_setup_register(state_dir / SETUP_REGISTER_FILE)


def read_setup_register(path: Path) -> dict[str, dict]:
    """The setup register at `path`, or {} when there is no such file.

    Raises:
        ClaimFilesError: the file is not a JSON object of objects.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise ClaimFilesError(f"{path}: not valid JSON: {e}") from e
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise ClaimFilesError(f"{path}: must be a JSON object of objects")
    return data


def save_setup_register(state_dir: Path, register: Mapping[str, Mapping[str, object]]) -> None:
    """Write the setup register to the state directory, whole (never half written)."""
    file_storage.JsonFile(state_dir / SETUP_REGISTER_FILE, indent=1,
                          ensure_ascii=False).write(dict(register))


def lock(state_dir: Path) -> file_storage.FileLock:
    """The lock of the claim files of `state_dir`. It is not reentrant, so hold it once."""
    return file_storage.FileLock(state_dir / LOCK_FILE)


def load(state_dir: Path) -> ClaimFiles | None:
    """The claim files, or None when none of them exists.

    Raises:
        ClaimFilesError: some but not all of the five files exist, or a file is in the
            old format.
        ClaimFilesBroken: a file is not valid JSON, or not the right shape.
    """
    present = [name for name in FILES if (state_dir / name).exists()]
    if not present:
        return None
    if missing := [name for name in FILES if name not in present]:
        raise ClaimFilesError(
            f"these claim files are missing: {', '.join(missing)}. The state directory "
            f"has {', '.join(present)}. The five files exist together or not at all. Claim "
            "files of an older version are not read: delete them, and the next round "
            "starts again as a first round.")
    data = {}
    for name in FILES:
        try:
            data[name] = json.loads((state_dir / name).read_text(),
                                    parse_float=inputs.PrintedFloat)
        except json.JSONDecodeError as e:
            raise ClaimFilesBroken(f"{name} is not valid JSON: {e}") from None
    for name in (CANDIDATES, QUOTES, DROPPED, CLASSES):
        if name == CANDIDATES and isinstance(data[name], list):
            raise ClaimFilesError(f"{name} holds a list. This is the old format.")
        if not isinstance(data[name], dict):
            raise ClaimFilesBroken(f"{name} must be a JSON object keyed by id.")
    register = data[REGISTER]
    if not isinstance(register, dict) or set(register) != {"claims"} \
            or not isinstance(register["claims"], list):
        raise ClaimFilesBroken(f'{REGISTER} must be an object with only a "claims" list.')
    for cid, c in data[CANDIDATES].items():
        if not isinstance(c, dict) or not all(isinstance(c.get(k), str) for k in STAMPS):
            raise ClaimFilesError(f"{CANDIDATES}: candidate {cid} has no created and "
                                  "last_updated stamps. This is the old format.")
    for qid, q in data[QUOTES].items():
        if isinstance(q, dict) and OLD_QUOTE_KEYS & set(q):
            raise ClaimFilesError(f"{QUOTES}: entry {qid} has register keys "
                                  f"({', '.join(sorted(OLD_QUOTE_KEYS & set(q)))}). This is "
                                  f"the old format. Register keys belong in {REGISTER}.")
        if isinstance(q, dict) and "value" not in q and VALUES_KEY not in q:
            raise ClaimFilesError(f'{QUOTES}: entry {qid} has no "value". This is the old '
                                  "format, from before a quote had one value. Claim files of "
                                  "an older version are not read: delete them, and the next "
                                  "round starts again as a first round.")
    return ClaimFiles(data[CANDIDATES], data[QUOTES], data[DROPPED], data[CLASSES],
                      register["claims"], frozenset(load_setup_register(state_dir)))


def save(files: ClaimFiles, state_dir: Path) -> None:
    """Write the five files, each decimal as it was printed (0.120 stays 0.120)."""
    for name, data in ((CANDIDATES, files.candidates), (QUOTES, files.quotes),
                       (DROPPED, files.dropped), (CLASSES, files.classes),
                       (REGISTER, {"claims": files.register})):
        file_storage.AtomicallyWrittenFile(state_dir / name).write_text(dump(data))


# A number as JSON writes it: no "+", no leading zero, and a digit on each side of the point.
JSON_NUMBER = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")


def json_number_text(printed: inputs.PrintedFloat) -> str:
    """The printed text of a decimal as a JSON number, with each printed decimal kept.

    A paper can print ".074", "-.90", "+4" or "1,000". Python reads these as numbers, and
    JSON does not, so the claim files could not be read again. The text loses only its
    "+", its separators and its extra leading zeros, and gains a "0" before a point.

    Raises:
        ValueError: the text is not a number that JSON can write with the same value.
    """
    text = inputs.PrintedFloat.plain(printed.text).strip().removeprefix("+")
    sign, digits = ("-", text[1:]) if text.startswith("-") else ("", text)
    digits = digits.lstrip("0") or "0"
    if digits.startswith((".", "e", "E")):
        digits = "0" + digits
    number = sign + digits.removesuffix(".")
    if not JSON_NUMBER.fullmatch(number) or float(number) != float(printed):
        raise ValueError(f"the printed value {printed.text!r} cannot be written as a JSON "
                         f"number with the same value; got {number!r}")
    return number


def dump(data: object) -> str:
    """JSON text with each printed decimal written as it was printed, as a JSON number."""
    def mark(v: object) -> object:
        """Wrap printed decimals in nested records, so that their text survives json.dumps."""
        if isinstance(v, inputs.PrintedFloat):
            return f"@@{json_number_text(v)}@@"
        if isinstance(v, dict):
            return {k: mark(x) for k, x in v.items()}
        if isinstance(v, list):
            return [mark(x) for x in v]
        return v
    text = json.dumps(mark(data), indent=1, ensure_ascii=False)
    return re.sub(r'"@@([^@"]+)@@"', r"\1", text) + "\n"


def read_round(state_dir: Path) -> ClaimRound:
    """The round in progress, or the last one.

    Raises:
        ClaimFilesError: no round has begun in this state directory.
    """
    path = state_dir / ROUND
    if not path.exists():
        raise ClaimFilesError(f"no round has begun: {ROUND} does not exist. The daemon "
                              "begins a round before it wakes the agent.")
    return ClaimRound(**json.loads(path.read_text()))


def save_round(r: ClaimRound, state_dir: Path) -> None:
    """Write the round record."""
    file_storage.JsonFile(state_dir / ROUND, indent=1, ensure_ascii=False).write(
        dataclasses.asdict(r))


def loaded(state_dir: Path, root: Path) -> tuple[ClaimFiles, ClaimRound]:
    """The claim files and the round, for a command the agent runs.

    If the round on record is not for the commit the daemon checks (the daemon could not
    begin it, because the agent had broken a claim file), it begins now. The commit is
    the daemon's record (structure.checked_commit), not the HEAD of `root`, because the
    agent works in the checkout and can move its HEAD.

    Raises:
        ClaimFilesError: there are no claim files, some are missing, or no round has begun.
    """
    checked = structure.checked_commit(state_dir)
    if checked and (not (state_dir / ROUND).exists() or read_round(state_dir).commit != checked) \
            and load(state_dir) is not None:
        begin_round(root, state_dir, checked)
    files = load(state_dir)
    if files is None:
        raise ClaimFilesError("there are no claim files yet. The daemon makes them before "
                              "it wakes the agent.")
    return files, read_round(state_dir)


# ---------------------------------------------------------------- the selected notes

def scope_paths(root: Path, given: Collection[str]) -> tuple[str, ...]:
    """The files and folders `given`, as scope entries: relative to `root`, with `/`.

    A path may be relative to `root` or absolute. `root` itself is `structure.WHOLE`.

    Raises:
        ValueError: `given` is empty, or a path is outside `root`.
    """
    if not given:
        raise ValueError("select at least one file or folder")
    out = []
    for path in given:
        if not path.strip():
            raise ValueError("a selected path is empty")
        full = (root / path).resolve()
        if not full.is_relative_to(root.resolve()):
            raise ValueError(f"{path} is outside the working directory {root}")
        rel = full.relative_to(root.resolve()).as_posix()
        out.append(structure.WHOLE if rel == "." else rel)
    return tuple(sorted(set(out)))


def read_scope(state_dir: Path) -> tuple[str, ...]:
    """The files and folders a person selected, from which notes give candidates.

    Raises:
        ClaimFilesError: no selection is saved, or the file is not as `save_scope` writes it.
    """
    path = state_dir / SCOPE
    if not path.exists():
        raise ClaimFilesError(f"no notes are selected: {SCOPE} does not exist. Run "
                              "`verifier start` to select the files and folders to read "
                              "claims from.")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ClaimFilesError(f"{SCOPE} is not valid JSON: {e}") from None
    paths = data.get("paths") if isinstance(data, dict) and set(data) == {"paths"} else None
    if not isinstance(paths, list) or not paths \
            or not all(isinstance(p, str) and p.strip() for p in paths):
        raise ClaimFilesError(f'{SCOPE} must be {{"paths": [...]}} with at least one path')
    return tuple(paths)


def save_scope(state_dir: Path, paths: tuple[str, ...]) -> None:
    """Write the selected files and folders (entries made by `scope_paths`)."""
    if not paths:
        raise ValueError("select at least one file or folder")
    file_storage.JsonFile(state_dir / SCOPE, indent=1).write({"paths": list(paths)})


# ---------------------------------------------------------------- the start of a round

def begin_round(root: Path, state_dir: Path, commit: str) -> str:
    """Bring the claim files in step with the notes at `commit`, before the agent is woken.

    Writes every current candidate, removes what the notes no longer say, and records
    the round. When the last round did not pass its checks, its entries of earlier
    rounds are first put back as they were when it began, so a change the checks
    refused never becomes the new start. Its new entries, links removed, pins and
    methods are kept.

    Args:
        root: the checkout of the working directory at `commit`.
        state_dir: the state directory that holds the claim files.
        commit: the commit the round checks.

    Returns:
        One line that says what changed.

    Raises:
        ClaimFilesError: the claim files cannot be read (see `load`), or no notes are
            selected (see `read_scope`).
    """
    files = load(state_dir)
    files = ClaimFiles.empty() if files is None else files
    previous = read_round(state_dir) if (state_dir / ROUND).exists() else None
    carried = previous is not None and not previous.stamped
    if carried:
        _restore(files, previous.existing)
    first = not any(_stamped(e) for e in _all_entries(files))
    found = {c["id"]: c for c in structure.folder_candidates(str(root),
                                                             within=read_scope(state_dir))}
    summary = _purge(files, found)
    new = {e["id"] for e in files.register if _has_id(e) and not _stamped(e)}
    r = ClaimRound(commit=commit, first=first, existing=_existing(files),
              pinned={c: v for c, v in previous.pinned.items() if c in new} if carried else {},
              proposed={c: v for c, v in previous.proposed.items() if c in new} if carried
              else {},
              methods={c: v for c, v in previous.methods.items() if c in new} if carried
              else {},
              keep_scores={c: v for c, v in previous.keep_scores.items()
                           if c in files.candidates} if carried else {},
              written={c: v for c, v in previous.written.items() if c in new} if carried
              else {})
    save(files, state_dir)
    save_round(r, state_dir)
    return summary


def _restore(files: ClaimFiles, existing: Mapping[str, Entries]) -> None:
    """Put back each entry of earlier rounds as `existing` holds it."""
    for name in ("quotes", "dropped", "classes"):
        getattr(files, name).update(copy.deepcopy(existing[name]))
    at = {e["id"]: n for n, e in enumerate(files.register) if _has_id(e)}
    for cid, entry in existing["register"].items():
        if cid in at:
            files.register[at[cid]] = copy.deepcopy(entry)
        else:
            files.register.append(copy.deepcopy(entry))


def new_entry(**fields: object) -> dict:
    """An entry of the round in progress: empty stamps, then the fields."""
    return {"created": NEW, "last_updated": NEW, **fields}


def quoted_candidates(files: ClaimFiles) -> set[str]:
    """The candidates a quote names, from each quote whose candidates are a list of ids."""
    return {n for q in files.quotes.values() if isinstance(q, dict)
            for n in (_ids(q.get("candidates")) or [])}


def quote_words(files: ClaimFiles) -> dict[str, list[str]]:
    """The words of each quote that names a candidate, by candidate id.

    Every candidate a quote names is a key, so it counts as answered (the rules give it
    no drop), also when the quote's "quote" is not text yet; such a quote adds no words.
    """
    out: dict[str, list[str]] = {}
    for q in files.quotes.values():
        if isinstance(q, dict):
            for n in _ids(q.get("candidates")) or []:
                words = out.setdefault(n, [])
                if isinstance(q.get("quote"), str):
                    words.append(q["quote"])
    return out


def _has_id(entry: object) -> bool:
    """True for a register entry with a text id."""
    return isinstance(entry, dict) and isinstance(entry.get("id"), str)


def _ids(value: object) -> list[str] | None:
    """A list of text ids, or None when `value` is not one (the agent wrote something else)."""
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    return None


def _purge(files: ClaimFiles, found: Mapping[str, dict]) -> str:
    """Make `files` match the current candidates, and remove links to removed claims.

    Returns a summary. A claim that loses every link loses its source, so `vcheck
    link-claims` links it again (core/claim_steps/full_paper_linker.py).
    """
    old = files.candidates
    gone = [cid for cid in old if cid not in found]
    files.candidates = {}
    for cid in [*(c for c in old if c in found), *(c for c in found if c not in old)]:
        fields = {k: v for k, v in found[cid].items() if k != "id"}
        before = old.get(cid)
        if before is None:
            files.candidates[cid] = new_entry(**fields)
        elif {k: v for k, v in before.items() if k not in STAMPS} != fields:
            files.candidates[cid] = {"created": before["created"], "last_updated": NEW,
                                     **fields}
        else:
            files.candidates[cid] = before
    for cid in gone:
        files.dropped.pop(cid, None)
    moved, removed_quotes = 0, set()
    for qid, q in list(files.quotes.items()):
        names = _ids(q.get("candidates")) if isinstance(q, dict) else None
        if names is None or all(n in found for n in names):
            continue
        now = _relocate(q.get("quote"), names, old, found)
        if now is None:
            del files.quotes[qid]
            removed_quotes.add(qid)
        else:
            q["candidates"], q["last_updated"] = now, NEW
            for cid in now:                  # the quote now answers a candidate that was dropped
                files.dropped.pop(cid, None)
            moved += 1
    removed_claims = set()
    for cid, c in list(files.classes.items()):
        quotes = _ids(c.get("quotes")) if isinstance(c, dict) else None
        if quotes is None or not removed_quotes & set(quotes):
            continue
        left = [q for q in quotes if q not in removed_quotes]
        if left:
            c["quotes"], c["last_updated"] = left, NEW
        else:
            del files.classes[cid]
            removed_claims.add(cid)
    files.register = [e for e in files.register
                      if not (_has_id(e) and e["id"] in removed_claims)]
    lost = 0
    for e in files.register:
        source = e.get("source") if _has_id(e) else None
        if not (isinstance(source, dict) and source.get("kind") == "claims"
                and _ids(source.get("claims")) is not None):
            continue
        if any(c in removed_claims for c in source["claims"]):
            source["claims"] = [c for c in source["claims"] if c not in removed_claims]
            if not source["claims"]:
                del e["source"]
            e["last_updated"] = NEW
            lost += 1
    for e in files.register:
        if _has_id(e) and e["id"] in files.classes:
            _set_core_keys(e, files)
    new = sum(1 for cid in found if cid not in old)
    summary = (f"{new} new candidates, {len(gone)} removed; {len(removed_quotes)} quotes "
               f"removed, {moved} moved; {len(removed_claims)} claims removed, "
               f"{lost} lost a link")
    return summary


def _relocate(quote: object, names: list[str], old: Mapping[str, dict],
              found: Mapping[str, dict]) -> list[str] | None:
    """The current candidates that hold a quote whose candidates changed, or None.

    The quote keeps its candidates that are still there, if their text holds it.
    Otherwise it moves to a current candidate with the same text as a gone candidate
    (the note was renamed, or an earlier copy of the sentence was removed), first one of
    the same file. A candidate whose text changed is a new candidate: its quotes are
    removed, and the labeller answers it again, so a number it adds is not missed.
    """
    if not isinstance(quote, str):
        return None
    still = [n for n in names if n in found]
    if still and extract.quote_in(quote, (found[n]["text"] for n in still)):
        return still
    texts = {" ".join(str(old[n].get("text")).split()) for n in names if n in old}
    same = [cid for cid, c in found.items() if " ".join(str(c["text"]).split()) in texts
            and extract.quote_in(quote, [c["text"]])]
    where = {file_of(n) for n in names}
    return next(([cid] for cid in same if file_of(cid) in where), same[:1] or None)


def file_of(candidate_id: str) -> str:
    """The note path, or `commit:<sha10>`, that a candidate id starts with."""
    return candidate_id.rsplit("#", 1)[0]


def _existing(files: ClaimFiles) -> dict[str, Entries]:
    """The entries of earlier rounds, whole."""
    return {
        "quotes": {k: copy.deepcopy(v) for k, v in files.quotes.items() if _stamped(v)},
        "dropped": {k: copy.deepcopy(v) for k, v in files.dropped.items() if _stamped(v)},
        "classes": {k: copy.deepcopy(v) for k, v in files.classes.items() if _stamped(v)},
        "register": {e["id"]: copy.deepcopy(e) for e in files.register
                     if _has_id(e) and _stamped(e)},
    }


def _stamped(entry: object) -> bool:
    """True for an entry of an earlier round: its `created` holds a commit id."""
    return isinstance(entry, dict) and isinstance(entry.get("created"), str) \
        and entry["created"] != NEW


def _all_entries(files: ClaimFiles) -> list[object]:
    """Every entry of the five files."""
    return [*files.candidates.values(), *files.quotes.values(), *files.dropped.values(),
            *files.classes.values(), *files.register]


def _pin(entry: dict) -> bool:
    """True if the entry's source holds a pin or a proposed pin."""
    return bool(_pins_of(entry))


def _pins_of(entry: dict) -> dict:
    """The pin and the proposed pin of the entry's source, those it has."""
    source = entry.get("source")
    if not isinstance(source, dict):
        return {}
    return {k: source[k] for k in PIN_KEYS if source.get(k)}


def _method_of(entry: dict) -> object:
    """The method the entry's source names, or None."""
    source = entry.get("source")
    return source.get("method") if isinstance(source, dict) else None


def agent_view(entry: dict) -> dict:
    """A register entry without the keys only the core writes."""
    out = {k: copy.deepcopy(v) for k, v in entry.items() if k not in CORE_KEYS}
    if isinstance(out.get("source"), dict):
        out["source"] = {k: v for k, v in out["source"].items() if k not in CORE_SOURCE_KEYS}
    return out


def _set_core_keys(entry: dict, files: ClaimFiles) -> None:
    """Write the quotes and the origin of a claim from its class; stamp it if they changed.

    `quotes` holds one [quote id, words] pair for each quote of the class, in class order.
    """
    c = files.classes[entry["id"]]
    if not isinstance(c, dict) or _ids(c.get("quotes")) is None:
        return                              # a class the agent is still writing
    words = {q: (files.quotes.get(q) or {}).get("quote") for q in c["quotes"]}
    if not all(isinstance(w, str) for w in words.values()):
        return                              # a quote the agent is still writing; a check names it
    quotes = [[q, w] for q, w in words.items()]
    origin = _origin(list(c["quotes"]), files)
    before = (entry.get("quotes"), entry.get("origin"))
    entry["quotes"] = quotes
    if origin is None:
        entry.pop("origin", None)
    else:
        entry["origin"] = origin
    if before != (quotes, origin):
        entry["last_updated"] = NEW


def _origin(quote_ids: list[str], files: ClaimFiles) -> str | None:
    """The note the quotes come from, if all their candidates come from one note.

    A claim from a commit message has no origin.
    """
    found = {(files.candidates.get(n) or {}).get("origin")
             for q in quote_ids for n in _ids((files.quotes.get(q) or {}).get("candidates")) or []}
    return found.pop() if len(found) == 1 else None


# ---------------------------------------------------------------- the three checks

def quote_problems(files: ClaimFiles, r: ClaimRound) -> list[str]:
    """What stops the QUOTES stage: bad entries, changed old entries, unanswered candidates."""
    problems = []
    quoted = quoted_candidates(files)
    words = quote_words(files)
    ruled = _rule_reasons(files, words)
    for name, entries, keys in ((QUOTES, files.quotes, QUOTE_KEYS),
                                (DROPPED, files.dropped, DROP_KEYS)):
        for key, e in entries.items():
            if not isinstance(e, dict) or set(e) != keys:
                problems.append(f'{name}: entry "{key}" must be an object with exactly the keys '
                                f'{", ".join(sorted(keys))}.')
                continue
            problems += _stamp_problems(name, key, e, r.existing[_kind(name)])
            if name == DROPPED and (not isinstance(e["why"], str) or not e["why"].strip()):
                problems.append(f'{name}: entry "{key}" needs a reason in "why".')
            elif name == DROPPED and e["created"] == NEW:
                problems += (_reason_problems(key, e["why"], files.quotes, quoted, words,
                                              files.candidates.get(key))
                             or _auto_problems(key, e["why"], ruled, r.keep_scores,
                                               files.setup_ids))
        problems += _old_entry_problems(name, entries, r.existing[_kind(name)])
    quotes = {k: v for k, v in files.quotes.items() if isinstance(v, dict)}
    dropped = {k: v for k, v in files.dropped.items() if isinstance(v, dict)}
    unanswered, found = extract.coverage(files.candidates, quotes, dropped)
    problems += [f"{QUOTES}: {p}" for p in found]
    problems += [f"Candidate {c} has no answer. Add a quote entry or a drop entry for it."
                 for c in unanswered]
    return problems + _value_problems(quotes)


def split_values(quotes: Entries, taken: Collection[str] = ()) -> Entries:
    """The quotes with each quote that lists its "values" split into one quote for each value.

    The quotes of one split have the words, candidates and stamps of the quote they come
    from. A quote with one value, or none, keeps its id; with several, the n-th gets
    "<id>-<n>", or "-2", "-3", ... after that if the id is in `taken` or used here. A listed
    value is read as `extract.claim_numbers` reads the text ("68.4%" is "68.4"), so it can
    be compared with the numbers of the quote; a value it cannot read stays as written, for
    `vcheck check-quotes` to name. A quote whose "values" is not a list is left as it is,
    for the same check to name.
    """
    used = set(taken) | set(quotes)
    out: dict[str, object] = {}
    for qid, q in quotes.items():
        if not isinstance(q, dict) or not isinstance(q.get(VALUES_KEY), list):
            out[qid] = q
            continue
        base = {k: v for k, v in q.items() if k != VALUES_KEY}
        values = list(dict.fromkeys(
            found[0] if isinstance(v, str) and len(found := extract.claim_numbers(v)) == 1
            else v for v in q[VALUES_KEY])) or [None]
        if len(values) == 1:
            out[qid] = {**base, "value": values[0]}
            continue
        for n, value in enumerate(values, start=1):
            sister = extract.free_id(f"{qid}-{n}", used)
            used.add(sister)
            out[sister] = {**base, "value": value}
    return out


def _value_problems(quotes: Entries) -> list[str]:
    """A value that its quote does not print, or two sister quotes with the same value."""
    problems, seen = [], {}
    for qid, q in quotes.items():
        if set(q) != QUOTE_KEYS or not isinstance(q["quote"], str):
            continue                        # the key check names it
        value = q["value"]
        if value is not None and (not isinstance(value, str)
                                  or value not in extract.claim_numbers(q["quote"])):
            problems.append(f'{QUOTES}: entry "{qid}" has the value {json.dumps(value)}, which '
                            'its quote does not print. A value is a number of the quote, '
                            'written as a string, or null.')
            continue
        key = (_sister_key(q), value)
        if key in seen:
            problems.append(f'{QUOTES}: entries "{seen[key]}" and "{qid}" have the same words, '
                            "candidates and value. Give each value of a quote one entry.")
        seen.setdefault(key, qid)
    return problems


def _sister_key(quote: Mapping[str, object]) -> tuple[object, ...]:
    """What sister quotes share: their candidates and their words."""
    return (tuple(_ids(quote.get("candidates")) or ()), quote.get("quote"))


def sister_values(files: ClaimFiles, quote_id: str) -> list[str]:
    """The values of the other quotes with the same candidates and words as this quote."""
    key = _sister_key(files.quotes[quote_id])
    return [q["value"] for qid, q in files.quotes.items()
            if qid != quote_id and isinstance(q, dict) and _sister_key(q) == key
            and isinstance(q.get("value"), str)]


def quote_text(files: ClaimFiles, quote_id: str, anonymized: bool = False) -> str:
    """The text of a quote for a reader: its words, its own value marked (value_marked_text)."""
    q = files.quotes[quote_id]
    return extract.value_marked_text(q["quote"], q["value"], sister_values(files, quote_id),
                                     anonymized)


def _class_quotes(files: ClaimFiles, claim_id: str) -> list[str]:
    """The quote ids of a claim's class.

    Raises:
        ClaimFilesError: the claim has no class, so the REGISTER stage has not passed.
    """
    c = files.classes.get(claim_id)
    if not isinstance(c, dict) or _ids(c.get("quotes")) is None:
        raise ClaimFilesError(f'claim "{claim_id}" has no class in {CLASSES}. Run vcheck '
                              "check-register first.")
    return c["quotes"]


def claim_value(files: ClaimFiles, claim_id: str) -> str | None:
    """The value of a claim's class: the first value of its quotes, or None."""
    return next((q["value"] for q in (files.quotes.get(qid) or {} for qid in
                                       _class_quotes(files, claim_id))
                 if isinstance(q.get("value"), str)), None)


def claim_sister_values(files: ClaimFiles, claim_id: str) -> list[str]:
    """The values of the sister quotes of the quotes of a claim's class."""
    return list(dict.fromkeys(v for qid in _class_quotes(files, claim_id)
                              if qid in files.quotes for v in sister_values(files, qid)))


def claim_text(files: ClaimFiles, claim_id: str, anonymized: bool = False) -> str:
    """The text of a claim for a reader: its statement, with the value of its class marked."""
    entry = files.claim(claim_id) or {}
    return extract.value_marked_text(str(entry.get("statement", "")),
                                     claim_value(files, claim_id),
                                     claim_sister_values(files, claim_id), anonymized)


def same_value(a: str | None, b: str | None) -> bool:
    """True if two values are one value, or either is None.

    They are one value when they differ by at most the larger of VALUE_RELATIVE_TOLERANCE
    of the larger value and VALUE_RESOLUTIONS times the cruder resolution: the weight of
    the lowest printed digit (1 for "85", 0.1 for "85.2"). So 85 and 86 are one value, and
    85.2 and 86.1 are two.
    """
    if a is None or b is None:
        return True
    # exact decimals, so a difference of exactly 2 steps is not lost to float rounding
    x, y = (decimal.Decimal(inputs.PrintedFloat.plain(v)) for v in (a, b))
    resolution = max(decimal.Decimal(1).scaleb(v.as_tuple().exponent) for v in (x, y))
    return abs(x - y) <= max(decimal.Decimal(str(VALUE_RELATIVE_TOLERANCE)) * max(abs(x), abs(y)),
                             VALUE_RESOLUTIONS * resolution)


def _reason_problems(key: str, why: str, quotes: Entries, quoted: set[str],
                     words: Mapping[str, list[str]], cand: object) -> list[str]:
    """What is wrong with the reason of a new drop: a repeat of no quote.

    `quoted` holds the candidates a quote names, and `words` the words of those quotes. A
    repeat that the rules dropped (utils/candidate_rules.py) names a candidate, which must
    be one of them; for an equation, one of its quotes must also hold the equation
    (`candidate_rules.holds`). `cand` is the dropped candidate.
    """
    text = why.strip()
    if (twin := candidate_rules.twin(text)) is not None and twin not in quoted:
        return [f'{DROPPED}: drop "{key}" says it repeats candidate {twin}, which has no quote. '
                f'Quote {twin}, or replace this drop with your own answer for "{key}".']
    if twin is not None and isinstance(cand, dict) \
            and not candidate_rules.holds(cand, words.get(twin, [])):
        return [f'{DROPPED}: drop "{key}" says it repeats candidate {twin}, but no quote of '
                f'{twin} holds the text of "{key}", so it would be in no quote: this drop does '
                f'not hold. Quote "{key}" itself, or quote its whole text in {twin}.']
    if text.lower().startswith("repeat of"):
        found = REPEAT_OF.match(text)
        qid = found.group(1) if found else ""
        if qid not in quotes:
            return [f'{DROPPED}: drop "{key}" says it repeats "{qid}", but no quote has this '
                    'id. Write "repeat of <quote id>" with the quote it repeats.']
    return []


def _rule_reasons(files: ClaimFiles, quoted: Mapping[str, list[str]]) -> dict[str, str]:
    """What the rules give each candidate now, as `vcheck drop-non-claims` would.

    A new drop with a rule's reason is left open, so the rules judge it again; every
    other drop counts as an answer, as when the rules ran.
    """
    if not any(isinstance(d, dict) and d.get("created") == NEW
               and is_auto(str(d.get("why", ""))) for d in files.dropped.values()):
        return {}
    answered = {cid for cid, d in files.dropped.items()
                if not (isinstance(d, dict) and d.get("created") == NEW
                        and AUTO_RULE.match(str(d.get("why", "")).strip()))}
    return candidate_rules.non_claim_reasons(files.candidates, quoted, answered)


def is_auto(why: str) -> bool:
    """True when a drop's reason says the core made it: it starts "auto:", in any case."""
    return why.strip().lower().startswith(AUTO)


def _auto_problems(key: str, why: str, ruled: Mapping[str, str],
                   keep_scores: Mapping[str, int],
                   setup_ids: Collection[str] = ()) -> list[str]:
    """What is wrong with a new drop whose reason says the core made it.

    The drop must be one the core makes, so the "auto:" start cannot pass a drop off as
    the core's: a rule's reason must be what the rules give the candidate now, and
    "auto:label <score>" must be the labeller's answer on record in this round, and
    "auto:setup <score>" also a fact in the setup register (`setup_ids`).
    """
    text = why.strip() if isinstance(why, str) else ""
    if not is_auto(text):
        return []
    if text.lower().startswith(AUTO_LABEL):
        score = keep_scores.get(key)
        if score is None or text.lower() != f"{AUTO_LABEL}{score}" or key in setup_ids:
            return [f'{DROPPED}: drop "{key}" says "{_short(text)}", but the labeller did not '
                    'drop this candidate with this score.']
        return []
    if text.lower().startswith(AUTO_SETUP):
        score = keep_scores.get(key)
        if score is None or text.lower() != f"{AUTO_SETUP}{score}" or key not in setup_ids:
            return [f'{DROPPED}: drop "{key}" says "{_short(text)}", but the labeller did not '
                    'send this candidate to the setup register with this score.']
        return []
    if ruled.get(key, "").lower() != text.lower():
        return [f'{DROPPED}: drop "{key}" says "{_short(text)}", which is not what the rules '
                "give this candidate."]
    return []


def class_problems(files: ClaimFiles, r: ClaimRound) -> list[str]:
    """What stops the CLASSES stage: bad, missing, doubled or changed classes."""
    problems, where = [], {}
    for cid, c in files.classes.items():
        if not ID.fullmatch(cid):
            problems.append(f'{CLASSES}: the class id "{cid}" must be text with no spaces.')
        if not isinstance(c, dict) or set(c) != CLASS_KEYS or not _ids(c["quotes"]):
            problems.append(f'{CLASSES}: class "{cid}" must be an object with exactly the keys '
                            f'{", ".join(sorted(CLASS_KEYS))}, and "quotes" must list one or '
                            "more quote ids.")
            continue
        problems += _stamp_problems(CLASSES, cid, c, r.existing["classes"])
        for q in c["quotes"]:
            if q not in files.quotes:
                problems.append(f'{CLASSES}: class "{cid}" names "{q}", but no quote has this id.')
            elif q in where:
                problems.append(f'Quote "{q}" is in class "{where[q]}" and in class "{cid}". '
                                "Put each quote in exactly one class.")
            else:
                where[q] = cid
    problems += [f'Quote "{q}" is not in a class.' for q in files.quotes if q not in where]
    for cid, c in files.classes.items():
        quote_ids = (_ids(c.get("quotes")) if isinstance(c, dict) else None) or []
        quotes = [q for q in (files.quotes.get(qid) for qid in quote_ids) if isinstance(q, dict)]
        # only a value that its quote prints; the quote check names any other value
        values = sorted({q["value"] for q in quotes
                         if isinstance(q.get("value"), str) and isinstance(q.get("quote"), str)
                         and q["value"] in extract.claim_numbers(q["quote"])})
        if any(not same_value(a, b) for a in values for b in values):
            problems.append(f'{CLASSES}: class "{cid}" holds quotes with different values '
                            f'({", ".join(values)}). A class is one claim with one value: put '
                            "each value in its own class.")
    for cid, old in r.existing["classes"].items():
        now = files.classes.get(cid)
        if not isinstance(now, dict):
            problems.append(f'{CLASSES}: class "{cid}" was removed. You cannot remove a class '
                            "of an earlier round.")
        elif now.get("created") != old["created"] \
                or now.get("last_updated") not in (old["last_updated"], NEW):
            problems.append(f'{CLASSES}: a stamp of class "{cid}" was changed. Only the core '
                            "writes commit ids.")
        elif lost := [q for q in old["quotes"] if q not in (_ids(now.get("quotes")) or [])]:
            problems.append(f'{CLASSES}: class "{cid}" lost the quote "{lost[0]}". You cannot '
                            "remove a quote from a class of an earlier round.")
    return problems


def register_problems(files: ClaimFiles, r: ClaimRound, methods: Collection[str],
                      fields: Mapping[str, inputs.FieldType], root: Path) -> list[str]:
    """What stops the REGISTER stage.

    One claim per class, with the class's id. A new claim has a statement, no
    `superseded_by` and no pin. A claim `vcheck fill-register` wrote keeps the statement
    it was given (`ClaimRound.written`). An old claim is unchanged, except that it may gain a
    source if it has none. Every claim parses, every link names a claim, and every artifact
    path to pin matches a file. A claim may rest on itself through other claims.

    Args:
        files: the claim files.
        r: the round in progress.
        methods: the computational methods a claim may name.
        fields: the claim keys the registered checks read, each with its type.
        root: the checkout, which artifact paths are read from.
    """
    problems, by_id = [], {}
    for n, e in enumerate(files.register, start=1):
        if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not e["id"].strip():
            problems.append(f'{REGISTER}: claim {n} must be an object with an "id".')
        elif e["id"] in by_id:
            problems.append(f'{REGISTER}: the id "{e["id"]}" is used by two claims.')
        else:
            by_id[e["id"]] = e
    problems += [f'Class "{c}" has no claim in {REGISTER}. Add one with the same id.'
                 for c in files.classes if c not in by_id]
    problems += [f'Claim "{c}" has no class in {CLASSES}. Every claim needs a class with the '
                 "same id." for c in by_id if c not in files.classes]
    for cid in r.existing["register"]:
        if cid not in by_id:
            problems.append(f'Claim "{cid}" was removed. You cannot remove a claim of an '
                            "earlier round.")
    for cid, e in by_id.items():
        old = r.existing["register"].get(cid)
        if old is None:
            problems += _stamp_problems(REGISTER, cid, e, {})
            if "superseded_by" in e:
                problems.append(f'Claim "{cid}": remove "superseded_by". A changed number is a '
                                "new claim, not a replaced one.")
            if not isinstance(e.get("statement"), str) or not e["statement"].strip():
                problems.append(f'Claim "{cid}" needs a "statement".')
            elif cid in r.written and e["statement"] != r.written[cid]:
                said = json.dumps(r.written[cid], ensure_ascii=False)
                problems.append(f'Claim "{cid}": the statement was changed. Code wrote it '
                                "(vcheck fill-register), and you cannot rewrite it. Put it "
                                f'back: "statement": {said}')
            problems += _value_problems_of_claim(files, cid, e)
        else:
            problems += _claim_changes(cid, agent_view(old), agent_view(e))
            if e.get("created") != old.get("created") \
                    or e.get("last_updated") not in (old.get("last_updated"), NEW):
                problems.append(f'Claim "{cid}": a stamp was changed. Only the core writes '
                                "commit ids.")
        if _pins_of(e) != pins_the_round_expects(r, cid, old):
            problems.append(f'Claim "{cid}": "sha256" or "proposed_sha256" was changed. Only '
                            "the core writes them: vcheck check-register pins a new source.")
        if _method_of(e) != r.methods.get(cid, _method_of(old or {})):
            problems.append(f'Claim "{cid}": "method" was changed. Only vcheck write-methods '
                            "writes it.")
        if why := _unparsable(e, methods, fields, root):
            problems.append(f'Claim "{cid}": {why}')
        if (path := _to_pin(e)) is not None:
            matched, others = artifact_matches(root, path)
            if not matched and not others:
                problems.append(f'Claim "{cid}": the source path {path} matches no file in '
                                "the working directory.")
            elif others:
                problems.append(f'Claim "{cid}": the source path {path} matches '
                                f"{os.path.relpath(others[0], root)}, which is not a file (a "
                                "folder or a broken link). A source path must match files "
                                "only: name them, for example with a pattern that ends in a "
                                "file extension such as *.json.")
    problems += _link_problems(by_id)
    return problems


def _value_problems_of_claim(files: ClaimFiles, cid: str, entry: dict) -> list[str]:
    """A new claim whose `value` is not the value of its class's quotes (`claim_value`)."""
    if cid not in files.classes:
        return []                           # the check of claims with no class names it
    want = claim_value(files, cid)
    if want is None:
        return []
    got = entry.get("value")
    printed = got.text if isinstance(got, inputs.PrintedFloat) else got
    if isinstance(printed, bool) or not isinstance(printed, int | float | str) \
            or not same_value(want, str(printed)):
        return [f'Claim "{cid}": its quotes have the value {want}, so the claim needs '
                f'"value": {inputs.PrintedFloat.plain(want)}. Sister claims (the same quote words) '
                "differ only by their value."]
    return []


def _kind(name: str) -> str:
    """The key of a file's entries in `ClaimRound.existing`."""
    return {QUOTES: "quotes", DROPPED: "dropped", CLASSES: "classes"}[name]


def _stamp_problems(name: str, key: str, entry: dict, existing: Entries) -> list[str]:
    """A new entry must have NEW in both stamps."""
    if key in existing or all(entry.get(k) == NEW for k in STAMPS):
        return []
    return [f'{name}: in the new entry "{key}", set "created": "" and "last_updated": "". '
            "Only the core writes commit ids."]


def _old_entry_problems(name: str, entries: Mapping[str, object], existing: Entries) -> list[str]:
    """An entry of an earlier round must still be there, unchanged."""
    problems = []
    for key, old in existing.items():
        if key not in entries:
            problems.append(f'{name}: the entry "{key}" was removed. You cannot remove an entry '
                            "of an earlier round.")
        elif entries[key] != old:
            problems.append(f'{name}: the entry "{key}" was changed. You cannot change an entry '
                            "of an earlier round.")
    return problems


def _claim_changes(cid: str, old: dict, now: dict) -> list[str]:
    """How a claim of an earlier round was changed beyond what is allowed, if it was."""
    old, now = dict(old), dict(now)
    before, after = old.pop("source", None), now.pop("source", None)
    if before is None and "value" not in old:
        now.pop("value", None)              # a claim gaining a source may gain its value
    allowed = before is None or after == before     # a claim with no source may gain one
    if allowed and now == old:
        return []
    return [f'Claim "{cid}" was changed. You cannot change a claim of an earlier round. You can '
            'only give a source to a claim that has none.']


def _unparsable(entry: dict, methods: Collection[str], fields: Mapping[str, inputs.FieldType],
                root: Path) -> str | None:
    """Why the register parser refuses the entry, or None.

    An artifact source with no method yet is read as if it named one: the method comes
    later, from `vcheck write-methods`.
    """
    raw = copy.deepcopy(entry)
    names = set(methods)
    source = raw.get("source")
    if isinstance(source, dict) and source.get("kind") == "artifact" and "method" not in source:
        source["method"] = NO_METHOD_YET
        names.add(NO_METHOD_YET)
    try:
        claim_register_parser.ClaimRegisterParser(names, fields).parse_claim_entry(raw, str(root))
    except inputs.ClaimRegisterError as e:
        return str(e)
    return None


def _to_pin(entry: dict) -> str | None:
    """The path of an artifact source that has no pin yet, or None."""
    source = entry.get("source")
    if isinstance(source, dict) and source.get("kind") == "artifact" \
            and isinstance(source.get("path"), str) and not _pin(entry):
        return source["path"]
    return None


def _link_problems(by_id: Mapping[str, dict]) -> list[str]:
    """Links to claims that do not exist.

    A cycle of links is allowed: the claim check `acyclic_support` reports it, and the
    checks run in a greedy order that breaks it (schema/claim/claim_links.py).
    """
    def deps(cid: str) -> list[str]:
        source = by_id[cid].get("source")
        if isinstance(source, dict) and source.get("kind") == "claims" \
                and isinstance(source.get("claims"), list):
            return [d for d in source["claims"] if isinstance(d, str)]
        return []
    return [f'Claim "{cid}" names "{d}" in "source.claims", but no claim has this id.'
            for cid in by_id for d in deps(cid) if d not in by_id]


# ---------------------------------------------------------------- what passing changes

def accept_classes(files: ClaimFiles, r: ClaimRound) -> int:
    """After the class check passes: stamp old classes that gained quotes, and update their claims.

    Returns:
        The number of quotes added to classes of earlier rounds.
    """
    added = 0
    for cid, old in r.existing["classes"].items():
        gained = len(files.classes[cid]["quotes"]) - len(old["quotes"])
        if gained:
            files.classes[cid]["last_updated"] = NEW
            added += gained
    for e in files.register:
        if isinstance(e, dict) and e.get("id") in files.classes:
            _set_core_keys(e, files)
    return added


def accept_register(files: ClaimFiles, r: ClaimRound, root: Path) -> int:
    """After check-register passes: write each claim's quotes and origin, and pin new sources.

    A pin is the SHA-256 of each file the source path matches, by file name. A pin is
    never replaced here (core/gate/artifact_pins.py changes a pin, after a person confirms it).

    Returns:
        The number of claims pinned now.
    """
    pinned = 0
    for e in files.register:
        _set_core_keys(e, files)
        old = r.existing["register"].get(e["id"])
        if old is not None and agent_view(e) != agent_view(old):
            e["last_updated"] = NEW          # it gained a source
        if _to_pin(e) is not None:
            pin(e, root)
            r.pinned[e["id"]] = e["source"]["sha256"]
            e["last_updated"] = NEW
            pinned += 1
    return pinned


def pin(entry: dict, root: Path) -> None:
    """Write the SHA-256 of each file the entry's artifact path matches, by `pin_key_of_file`.

    Raises:
        ValueError: the entry has no artifact source, is pinned already, or its path
            matches no file under `root` or matches a folder.
    """
    path = _to_pin(entry)
    if path is None:
        raise ValueError(f"{entry.get('id')}: no artifact source without a pin")
    matched, others = artifact_matches(root, path)
    if others:
        raise ValueError(f"{entry.get('id')}: {path} matches {others[0]}, which is not a file")
    if not matched:
        raise ValueError(f"{entry.get('id')}: {path} matches no file")
    entry["source"]["sha256"] = {pin_key_of_file(str(root / path), str(f)):
                                 checksums.sha256_of_file(str(f)) for f in matched}


def folder_before_first_wildcard(path_pattern: str) -> str:
    """The folders of `path_pattern` before its first folder with a glob wildcard.

    The file name never counts as a folder. "runs/*/metrics.json" gives "runs", and
    "data/neg_v*.json" gives "data".
    """
    folders = Path(path_pattern).parts[:-1]
    fixed = []
    for folder in folders:
        if glob.has_magic(folder):
            break
        fixed.append(folder)
    return os.path.join(*fixed) if fixed else ""


def pin_key_of_file(path_pattern: str, file: str) -> str:
    """The key of the pin of `file`, one of the files that `path_pattern` matches.

    The key is the path of the file from the folder before the first wildcard. Two
    files with the same name in different folders get different keys:
    "runs/*/metrics.json" gives the keys "a/metrics.json" and "b/metrics.json". When
    only the file name has a wildcard, the key is the file name.

    Raises:
        ValueError: `file` is not in the folder before the first wildcard of `path_pattern`.
    """
    folder = folder_before_first_wildcard(path_pattern)
    key = os.path.relpath(os.path.normpath(file), os.path.normpath(folder) if folder else ".")
    if key == ".." or key.startswith(".." + os.sep) or os.path.isabs(key):
        raise ValueError(f"{file} is not in {folder or '.'}, the folder of {path_pattern}")
    return key


def artifact_matches(root: Path, path: str) -> tuple[list[Path], list[Path]]:
    """What an artifact source path matches under `root`: (the files, everything else).

    Everything else is a folder or a broken link. Only a file can be pinned, so
    check-register refuses a path that matches anything else, and `pin` never hashes it.
    """
    matched = sorted(Path(p) for p in glob.glob(str(root / path)))
    return [p for p in matched if p.is_file()], [p for p in matched if not p.is_file()]


def stamp(state_dir: Path, commit: str, methods: Collection[str],
          fields: Mapping[str, inputs.FieldType], root: Path) -> bool:
    """At the end of a round: if the three checks pass, write `commit` in every NEW stamp.

    Returns:
        True if the stamps were written. False if a check fails; the NEW entries then
        stay, and the next round continues them.

    Raises:
        ClaimFilesError: there are no claim files, or no round has begun.
    """
    files, r = loaded(state_dir, root)
    if quote_problems(files, r) or class_problems(files, r) \
            or register_problems(files, r, methods, fields, root):
        return False
    accept_classes(files, r)                 # what accept-classes and check-register
    accept_register(files, r, root)          # write, in case a step did not run again
    for e in _all_entries(files):
        for k in STAMPS:
            if e.get(k) == NEW:
                e[k] = commit
    r.stamped = True
    r.existing = _existing(files)            # what the round ended with is the next start
    save(files, state_dir)
    save_round(r, state_dir)
    return True


def artifact_source(files: ClaimFiles, claim_id: str) -> dict:
    """The artifact source of a claim in the register.

    Raises:
        ValueError: the claim has no artifact source in the register.
    """
    source = (files.claim(claim_id) or {}).get("source")
    if not isinstance(source, dict) or source.get("kind") != "artifact":
        raise ValueError(f"{claim_id}: has no artifact source in {REGISTER}")
    return source


def set_method(files: ClaimFiles, r: ClaimRound, claim_id: str, method: str) -> None:
    """Record the method that `vcheck write-methods` wrote for a claim in its source.

    Raises:
        ValueError: the claim has no artifact source in the register.
    """
    entry = files.claim(claim_id)
    source = artifact_source(files, claim_id)
    source["method"] = method
    r.methods[claim_id] = method
    if claim_id in r.existing["register"]:
        entry["last_updated"] = NEW


def record_pins(state_dir: Path, entries: list[dict]) -> None:
    """Make the round expect the pins that core/gate/artifact_pins.py just wrote on these entries.

    A person confirmed or withdrew a checksum, or was asked to. With no round on record
    (a register used without the claim files), there is nothing to update.
    """
    if not (state_dir / ROUND).exists():
        return
    r = read_round(state_dir)
    for e in entries:
        cid = e.get("id")
        source = r.existing["register"].get(cid, {}).get("source")
        if isinstance(source, dict):
            for k in PIN_KEYS:
                source.pop(k, None)
            source.update(copy.deepcopy(_pins_of(e)))
        if cid in r.pinned:
            r.pinned[cid] = copy.deepcopy(e["source"].get("sha256"))
            proposed = e["source"].get("proposed_sha256")
            if proposed:
                r.proposed[cid] = copy.deepcopy(proposed)
            else:
                r.proposed.pop(cid, None)
    save_round(r, state_dir)


def pins_the_round_expects(r: ClaimRound, cid: str, old: dict | None) -> dict:
    """The pin and the proposed pin that the entry `cid` must have, as the core wrote them.

    For a claim pinned in this round, they are what `accept_register` and `record_pins`
    recorded in the round. For another claim, they are those of its entry at the start of
    the round (`old`).
    """
    if cid not in r.pinned:
        return _pins_of(old or {})
    pins = {"sha256": r.pinned[cid]}
    if cid in r.proposed:
        pins["proposed_sha256"] = r.proposed[cid]
    return pins


# ---------------------------------------------------------------- class proposals

def proposals_path(state_dir: Path, r: ClaimRound) -> Path:
    """Where `vcheck propose-classes` writes the proposal of this round."""
    return state_dir / PROPOSALS_DIR / f"class-proposals-{r.commit[:7]}.json"


def new_quotes(files: ClaimFiles) -> list[str]:
    """The ids of the quotes of the round in progress (no stamp yet)."""
    return [qid for qid, q in files.quotes.items() if not _stamped(q)]


def proposal_items(files: ClaimFiles) -> dict[str, str]:
    """What the models sort: each new quote, and one statement per class of earlier rounds.

    A class is sent as its claim's statement. Item ids are `quote:<id>` and `class:<id>`.
    With no new quote there is nothing to sort, and the result is empty. Classes the
    agent wrote in this round are left out: the models see the new quotes afresh.

    A quote of a whole table is never sent (`is_table_quote`): it gets a class of its own
    at accept-classes.
    """
    new = {}
    for qid, q in files.quotes.items():
        if _stamped(q) or is_table_quote(q, files):
            continue
        new[f"quote:{qid}"] = quote_text(files, qid)
    if not new:
        return {}
    classes = {}
    for cid, c in files.classes.items():
        if _stamped(c):
            statement = (files.claim(cid) or {}).get("statement")
            classes[f"class:{cid}"] = claim_text(files, cid) if isinstance(statement, str) \
                else " ".join(quote_text(files, q) for q in c["quotes"] if q in files.quotes)
    return new | classes


def item_values(files: ClaimFiles, items: Mapping[str, str]) -> dict[str, str | None]:
    """The value of each item of `proposal_items`: its quote's, or its class's (`claim_value`)."""
    return {k: files.quotes[k.removeprefix("quote:")]["value"] if k.startswith("quote:")
            else claim_value(files, k.removeprefix("class:")) for k in items}


def write_proposal(state_dir: Path, r: ClaimRound, items: Mapping[str, str],
                   partition: Partition) -> Path:
    """Write the proposed classes, as item ids and sorted, for `vcheck accept-classes`."""
    path = proposals_path(state_dir, r)
    data = {"commit": r.commit, "items": dict(sorted(items.items())),
            "classes": sorted(sorted(members) for members in partition)}
    file_storage.JsonFile(path, indent=1, ensure_ascii=False).write(data)
    return path


def read_proposal(state_dir: Path, r: ClaimRound) -> dict[str, object] | None:
    """The proposal `vcheck propose-classes` wrote in this round, or None if there is none.

    Raises:
        ClaimFilesBroken: the file is not a valid JSON object.
    """
    store = file_storage.JsonFile(proposals_path(state_dir, r))
    if not store.exists():
        return None
    try:
        data = store.read()
    except json.JSONDecodeError as e:
        raise ClaimFilesBroken(f"{store.path.name} is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise ClaimFilesBroken(f"{store.path.name} is not a JSON object")
    return data


def accept_proposal(files: ClaimFiles, proposal: Mapping[str, object],
                    notes: list[str] | None = None) -> Entries:
    """The classes after the proposal is taken as it is, for grouping in code.

    A group of new quotes becomes a new class, whose id is its first quote's id with each
    space made "_" (and "-2", "-3", ... if that id is taken). A group with a class of an
    earlier round (`class:<id>`) adds its quotes to that class. A group with two or more
    such classes adds its quotes to the first by id and leaves the others as they are:
    old classes are never merged, and one such group does not stop the others. A new
    quote in no group gets a class of its own. Quotes already in a class stay where they
    are.

    Args:
        files: the claim files.
        proposal: the proposal file `vcheck propose-classes` wrote.
        notes: filled with a line for each group with two or more old classes.

    Raises:
        ClaimFilesError: the file has no list of classes, or a group names an item that
            is not there: `class:<id>` must name a class, `quote:<id>` a quote, and
            nothing else is an item.
    """
    groups = proposal.get("classes")
    if not isinstance(groups, list):
        raise ClaimFilesError("the proposal file has no list \"classes\"; run this command "
                              "again: vcheck propose-classes")
    classes = copy.deepcopy(files.classes)
    placed = {q for c in classes.values() if isinstance(c, dict)
              for q in _ids(c.get("quotes")) or []}
    for group in groups:
        for g in group:
            g = str(g)
            if g.startswith("class:"):
                if g.removeprefix("class:") not in files.classes:
                    raise ClaimFilesError(f"a proposed group names {g}, but {g} names no class")
            elif g.startswith("quote:"):
                if g.removeprefix("quote:") not in files.quotes:
                    raise ClaimFilesError(f"a proposed group names {g}, but {g} names no quote")
            else:
                raise ClaimFilesError(f"a proposed group names {g}, which is neither "
                                      "class:<id> nor quote:<id>")
        olds = sorted(g.removeprefix("class:") for g in group if g.startswith("class:"))
        quotes = [g.removeprefix("quote:") for g in group
                  if g.startswith("quote:") and g.removeprefix("quote:") not in placed]
        if len(olds) > 1 and notes is not None:
            notes.append(f"a proposed group holds {len(olds)} classes of earlier rounds "
                         f"({', '.join(olds)}); its new quotes joined {olds[0]}, and the "
                         "others stay as they are")
        if not quotes:
            continue
        if olds:
            classes[olds[0]]["quotes"] = [*classes[olds[0]]["quotes"], *quotes]
        else:
            classes[_free_id(quotes[0], classes)] = new_entry(quotes=quotes)
        placed.update(quotes)
    for q in files.quotes:
        if q not in placed:
            classes[_free_id(q, classes)] = new_entry(quotes=[q])
    return classes


def _free_id(quote_id: str, classes: Entries) -> str:
    """A class id from a quote id: no spaces, and not one an existing class has."""
    return extract.free_id(re.sub(r"\s+", "_", quote_id.strip()) or "class", classes)


# ---------------------------------------------------------------- the register in code

def fill_register(files: ClaimFiles) -> list[str]:
    """Write a claim for each class that has none, with a statement taken from its quotes.

    Each new claim has the class's id, empty stamps, the value of its class (`claim_value`),
    the quotes and origin the core writes (`_set_core_keys`), and no source: the claims of a
    paper have no data or log, and `vcheck link-claims` writes the links between claims. A claim
    already in the register, of this round or an earlier one, is left as it is. Call it once
    the CLASSES stage passes, so every quote of a class exists.

    A class of whole-table quotes (`is_table_class`) gets a short `table_statement`; every
    other class gets `statement_of`.

    Returns:
        The ids of the claims written, in class order.
    """
    named = {e.get("id") for e in files.register if isinstance(e, dict)}
    added = []
    for cid, c in files.classes.items():
        if cid in named:
            continue
        table = is_table_class(c, files)
        statement = table_statement(c["quotes"], files) if table else \
            statement_of(c["quotes"], files)
        entry = {"id": cid, "statement": statement,
                 "created": NEW, "last_updated": NEW}
        if (value := claim_value(files, cid)) is not None:
            entry["value"] = inputs.PrintedFloat(inputs.PrintedFloat.plain(value))
        _set_core_keys(entry, files)
        files.register.append(entry)
        added.append(cid)
    return added


def is_table_quote(q: Mapping[str, object], files: ClaimFiles) -> bool:
    """True for a quote of a whole table: a claim of its own, with no value."""
    names = _ids(q.get("candidates")) or []
    return bool(names) and all((files.candidates.get(n) or {}).get("kind") == extract.TABLE
                               for n in names)


def is_table_class(c: Mapping[str, object], files: ClaimFiles) -> bool:
    """True for a class whose quotes are all quotes of whole tables."""
    quotes = [files.quotes.get(q) for q in c.get("quotes") or []]
    return bool(quotes) and all(isinstance(q, dict) and is_table_quote(q, files) for q in quotes)


def table_statement(quote_ids: list[str], files: ClaimFiles) -> str:
    """A table claim's statement, at most TABLE_STATEMENT_CHARS characters.

    It is the table's name, caption and columns (`extract.table_header`), since the
    paper sits beside the register wherever a model reads it.
    """
    text = extract.table_header(files.quotes[quote_ids[0]]["quote"])
    return " ".join(text.split())[:TABLE_STATEMENT_CHARS]


def statement_of(quote_ids: list[str], files: ClaimFiles) -> str:
    """The clearest quote of a class, as one line: the statement of its claim.

    A sentence is preferred over a whole table, a caption or an equation. Among the
    sentences, the shortest one that holds every number the other sentences print wins
    (a number is compared by value, so 171.7 matches 171.70). When none holds them all,
    the one that lacks the fewest wins, and of those the shortest. A class with no
    sentence quote gets its first quote. Numbers are counted as `extract.claim_numbers`
    finds them, so a table number or a citation year is not a number a sentence can lack.
    """
    words = {q: " ".join(files.quotes[q]["quote"].split()) for q in quote_ids}
    sentences = [q for q in quote_ids if _is_sentence(q, files)]
    if not sentences:
        return words[quote_ids[0]]
    found = {q: _values(words[q]) for q in sentences}

    def rank(q: str) -> tuple[int, int]:
        others = set().union(*(found[o] for o in sentences if o != q))
        return (len(others - found[q]), len(words[q]))
    return words[min(sentences, key=rank)]


def _is_sentence(quote_id: str, files: ClaimFiles) -> bool:
    """True if every candidate of the quote is a sentence (of a note or a commit message)."""
    names = _ids(files.quotes[quote_id].get("candidates")) or []
    return bool(names) and all((files.candidates.get(n) or {}).get("kind") in SENTENCE_KINDS
                               for n in names)


# ---------------------------------------------------------------- numbers of quotes

def _values(words: str) -> set[float]:
    """The numbers printed in `words`, by value.

    A number a table, figure or section reference or a citation put there is left out
    (`extract.claim_numbers`).
    """
    return {float(n.replace(",", "")) for n in extract.claim_numbers(words)}


# ---------------------------------------------------------------- what the commands print

def failed(problems: list[str], command: str) -> list[str]:
    """The output of a check that failed."""
    count = "is 1 problem" if len(problems) == 1 else f"are {len(problems)} problems"
    lines = [f"FAILED. There {count}."]
    lines += [f"  {n}. {p}" for n, p in enumerate(problems[:LISTED], start=1)]
    if len(problems) > LISTED:
        lines.append(f"  ... and {len(problems) - LISTED} more.")
    return [*lines, f"Fix the problems. Then run this command again: {command}"]


def next_steps(files: ClaimFiles) -> list[str]:
    """What the agent does after the register passes: the method needs, if a claim has none."""
    waiting = [e["id"] for e in files.register
               if _has_id(e) and isinstance(e.get("source"), dict)
               and e["source"].get("kind") == "artifact" and "method" not in e["source"]]
    if waiting:
        return [f"NEXT STEP: Write method_needs.json for the {len(waiting)} artifact claims that "
                "have no method. Then run: vcheck write-methods"]
    return []


def _short(text: object, limit: int = 60) -> str:
    """The start of a candidate's text, on one line."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit - 4] + " ..."


def claim_passages(files: ClaimFiles, claim_id: str) -> list[str]:
    """The text of each candidate that the quotes of a claim come from, each once, in order.

    The method writer reads them, with the claimed values masked. A claim whose class or
    quotes the agent is still writing has none.
    """
    c = files.classes.get(claim_id)
    out: list[str] = []
    for q in (_ids(c.get("quotes")) if isinstance(c, dict) else None) or []:
        for n in _ids((files.quotes.get(q) or {}).get("candidates")) or []:
            text = (files.candidates.get(n) or {}).get("text")
            if isinstance(text, str) and text not in out:
                out.append(text)
    return out
