#!/usr/bin/env python3
"""Check the claim files across rounds: utils/claim_files.py and the five claim commands.

Runs the two-round example of the design end to end on a small git repository, with a
fake model for `vcheck propose-classes` and no network. Round 1 is a first round: the
test writes the quotes, the classes and the register into the claim files, the checks
that the claim steps run read them, and the core pins the artifact claim and stamps every
entry. In round 2 the worker
changed a number, deleted a sentence, edited and moved others, and added new ones. The
cases check that the core removes and moves what it must before the agent starts, that
the checks refuse each change to an entry of round 1 (except the allowed ones), and
that the stamps hold the right commits at the end. Other cases cover the register that
code writes (fill-register), whole-table quotes, partial and old-format claim files, and
a round whose checks fail. Returns 1 if any case fails.
"""
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import cli  # noqa: E402
from core.claim_steps import duplicates  # noqa: E402
from core.gate import artifact_pins  # noqa: E402
from core.project import watched_project  # noqa: E402
from core.registry import extensions  # noqa: E402
from runtime import daemon  # noqa: E402
from runtime.agent import rounds  # noqa: E402
from schema import inputs  # noqa: E402
from schema.claim import claim_register_parser  # noqa: E402
from utils import claim_files, extract, models, structure, text  # noqa: E402

FAILED: list[str] = []
NOTE = "notes/retrieval-results.md"
ROUND_1 = """# Retrieval results

With retrieval, the 7B model answers 68.4% of the questions correctly.
Without retrieval, the 7B model answers 61.0% of the questions correctly.
Retrieval raises the accuracy of the 7B model by 7.4 points.
The 7B model gains 7.4 points from retrieval.
Retrieval helps language models answer factual questions.
We ran three seeds for each condition.
"""
# The 7B number changed, one sentence of a pair was deleted, the seeds sentence moved to
# the top, a sentence was edited (so its quote is removed), and two sentences are new.
ROUND_2 = """# Retrieval results

We ran three seeds for each condition.
This note reports a study of retrieval.
With retrieval, the 7B model answers 71.2% of the questions correctly.
Without retrieval, the 7B model answers 61.0% of the questions correctly on the test set.
Retrieval raises the accuracy of the 7B model by 7.4 points.
The 13B model gains 4.1 points with retrieval.
Retrieval helps language models answer factual questions.
"""
ANSWERS = {"records": [{"condition": "retrieval", "correct": True}]}


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def git(root: Path, *args: str) -> str:
    """Run git in the test repository and return its output."""
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, "GIT_AUTHOR_NAME": "t",
                                          "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                                          "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


def commit(root: Path, note: str, message: str) -> str:
    """Write the note, commit everything, and return the commit id."""
    (root / NOTE).write_text(note)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


def vcheck(root: Path, *args: str) -> tuple[int, str]:
    """Run a vcheck command quietly; return (exit code, output)."""
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.main(["--state", str(root / ".verifier"), "--root", str(root), *args])
    return rc, out.getvalue()


def record_checked_commit(state: Path, sha: str) -> None:
    """Record `sha` as the commit the daemon checks, as the daemon does."""
    path = state / structure.RUNTIME_DIR / structure.CHECKED_COMMIT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sha + "\n")


def read(root: Path, name: str) -> object:
    """One claim file as JSON."""
    return json.loads((root / ".verifier" / name).read_text())


def write(root: Path, name: str, data: object) -> None:
    """Write one claim file as JSON."""
    (root / ".verifier" / name).write_text(json.dumps(data, indent=1))


def candidate(root: Path, start: str) -> str:
    """The id of the current candidate whose text starts with `start`."""
    return next(cid for cid, c in read(root, claim_files.CANDIDATES).items()
                if c["text"].startswith(start))


def entry(**fields: object) -> dict:
    """A new entry: empty stamps, then the fields."""
    return {"created": "", "last_updated": "", **fields}


class FakeModel:
    """models.ask_and_read for the proposing calls of the shortlist method: a candidate is a
    duplicate of a Q when both hold "7.4 points", with or without the value marked."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str, model: str, read, timeout: int = 300, **kwargs) -> object:
        self.prompts.append(prompt)
        head, rest = prompt.split("\n\nCandidates:\n")
        qs = [line.split(". ", 1)[1] for line in head.splitlines()[1:]]
        cands = [line.split(". ", 1)[1] for line in rest.split("\n\n")[0].splitlines()]
        def says(item: str) -> bool:
            return "7.4 points" in item.replace("{{", "").replace("}}", "")
        return read(json.dumps({f"Q{j}": [i for i, c in enumerate(cands, 1) if c != q
                                          and says(q) and says(c)]
                                for j, q in enumerate(qs, 1)}))


def check_classes(root: Path) -> tuple[int, str]:
    """Check the classes written in the file, and accept them, as `vcheck accept-classes` does
    after it takes the proposal. Returns the exit code and what it printed."""
    state = root / ".verifier"
    with claim_files.lock(state).held():
        files, r = claim_files.loaded(state, root)
        if problems := claim_files.class_problems(files, r):
            return 1, "\n".join(claim_files.failed(problems, "vcheck accept-classes"))
        added = claim_files.accept_classes(files, r)
        claim_files.save(files, state)
    new = sum(c["created"] == claim_files.NEW for c in files.classes.values())
    if r.first:
        return 0, f"PASSED. {len(files.classes)} classes hold the {len(files.quotes)} quotes."
    return 0, f"PASSED. {new} new classes. {added} quotes added to existing classes."


def propose(root: Path) -> tuple[int, str, FakeModel]:
    """Run `vcheck propose-classes` with the fake model."""
    fake, real = FakeModel(), models.ask_and_read
    models.ask_and_read = fake
    try:
        rc, out = vcheck(root, "propose-classes")
    finally:
        models.ask_and_read = real
    return rc, out, fake


def round_one(root: Path) -> str:
    """The first round, from the extraction to the stamps. Returns the commit."""
    first = commit(root, ROUND_1, "first notes")
    state = root / ".verifier"
    summary = claim_files.begin_round(root, state, first)
    expect("the first extraction lists 6 new candidates", summary.startswith("6 new candidates"),
           summary)
    ids = {k: candidate(root, k) for k in ("With retrieval", "Without", "Retrieval raises",
                                           "The 7B model gains", "Retrieval helps", "We ran")}
    quotes = {"acc-7b": entry(candidates=[ids["With retrieval"]],
                              quote="the 7B model answers 68.4% of the questions correctly",
                              value="68.4"),
              "acc-7b-no": entry(candidates=[ids["Without"]],
                                 quote="the 7B model answers 61.0%", value="61.0"),
              "gain-7b": entry(candidates=[ids["Retrieval raises"]],
                               quote="Retrieval raises the accuracy of the 7B model by 7.4 points",
                               value="7.4"),
              "gain-7b-again": entry(candidates=[ids["The 7B model gains"]],
                                     quote="The 7B model gains 7.4 points from retrieval",
                                     value="7.4"),
              "helps": entry(candidates=[ids["Retrieval helps"]],
                             quote="Retrieval helps language models answer factual questions",
                             value=None),
              "seeds": entry(candidates=[ids["We ran"]], quote="three seeds for each condition",
                             value=None)}
    write(root, claim_files.QUOTES, {**quotes, "seeds": entry(candidates=[ids["We ran"]],
                                                              quote="five seeds", value=None)})
    rc, out = vcheck(root, "check-quotes")
    expect("check-quotes names a quote that is not word for word in its candidate",
           rc == 1 and f'Entry "seeds": the quote does not occur word for word in candidate '
           f'{ids["We ran"]}.' in out and "vcheck check-quotes" in out.splitlines()[-1], out)
    write(root, claim_files.QUOTES, {k: v for k, v in quotes.items() if k != "seeds"})
    rc, out = vcheck(root, "check-quotes")
    expect("check-quotes names an unanswered candidate",
           rc == 1 and f"Candidate {ids['We ran']} has no answer." in out, out)
    write(root, claim_files.QUOTES, {**quotes, "seeds": {**quotes["seeds"], "created": "abc"}})
    rc, out = vcheck(root, "check-quotes")
    expect("check-quotes refuses a commit id the agent wrote",
           rc == 1 and 'in the new entry "seeds", set "created": ""' in out, out)
    write(root, claim_files.QUOTES, quotes)
    rc, out = vcheck(root, "check-quotes")
    expect("check-quotes passes",
           rc == 0 and out == "PASSED. 6 quotes and 0 drops answer the 6 new candidates.\n", out)

    classes = {"acc-7b-retrieval": entry(quotes=["acc-7b"]),
               "acc-7b-no-retrieval": entry(quotes=["acc-7b-no"]),
               "gain-small-models": entry(quotes=["gain-7b", "gain-7b-again"]),
               "retrieval-helps": entry(quotes=["helps"]), "seeds": entry(quotes=["seeds"])}
    write(root, claim_files.CLASSES, classes)
    rc, out, fake = propose(root)
    path = state / "tmp" / f"class-proposals-{first[:7]}.json"
    proposal = json.loads(path.read_text()) if path.exists() else {}
    expect("propose-classes asks the model and writes the proposal for the 6 quotes, mapped back",
           rc == 0 and fake.prompts and f".verifier/tmp/class-proposals-{first[:7]}.json"
           in out and proposal.get("classes") == [["quote:gain-7b", "quote:gain-7b-again"]]
           and len(proposal.get("items", {})) == 6, out + json.dumps(proposal))
    write(root, claim_files.CLASSES, {**classes, "seeds": entry(quotes=["seeds", "helps"])})
    rc, out = check_classes(root)
    expect("the class check names a quote in two classes",
           rc == 1 and 'Quote "helps" is in class "retrieval-helps" and in class "seeds".' in out,
           out)
    write(root, claim_files.CLASSES, classes)
    rc, out = check_classes(root)
    expect("the class check passes", rc == 0 and out.startswith(
        "PASSED. 5 classes hold the 6 quotes."), out)

    os.makedirs(root / "runs")
    (root / "runs" / "answers.json").write_text(json.dumps(ANSWERS))
    register = [entry(id="acc-7b-retrieval", statement="With retrieval, the 7B model answers "
                      "68.4% of the questions.", value=68.4,
                      source={"kind": "artifact", "path": "runs/answers.json"}),
                entry(id="acc-7b-no-retrieval", statement="Without retrieval, the 7B model "
                      "answers 61.0% of the questions.", value=61.0),
                entry(id="gain-small-models", statement="Retrieval adds 7.4 points at 7B.",
                      value=7.4),
                entry(id="retrieval-helps", statement="Retrieval helps factual accuracy.",
                      source={"kind": "claims", "claims": ["acc-7b-retrieval",
                                                           "gain-small-models"]}),
                entry(id="seeds", statement="Each condition ran with three seeds.",
                      source={"kind": "claims", "claims": ["retrieval-helps"]})]
    loop = [*register[:2], {**register[2], "source": {"kind": "claims", "claims": ["seeds"]}},
            *register[3:]]
    expect("the register accepts a cycle of links; the check acyclic_support reports it",
           claim_files._link_problems({c["id"]: c for c in loop}) == [])
    expect("the register still refuses a link to a claim that does not exist",
           claim_files._link_problems({"a": {"id": "a", "source": {
               "kind": "claims", "claims": ["gone"]}}})
           == ['Claim "a" names "gone" in "source.claims", but no claim has this id.'])
    register[4].pop("source")
    write(root, claim_files.REGISTER, {"claims": register[:4]})
    rc, out = vcheck(root, "check-register")
    expect("check-register names a class with no claim",
           rc == 1 and 'Class "seeds" has no claim in claim_register.json.' in out, out)
    os.makedirs(root / "runs" / "seed1")
    globbed = [{**register[0], "source": {"kind": "artifact", "path": "runs/*"}}, *register[1:]]
    write(root, claim_files.REGISTER, {"claims": globbed})
    rc, out = vcheck(root, "check-register")
    expect("check-register refuses a source path that matches a folder, and does not crash",
           rc == 1 and 'the source path runs/* matches runs/seed1, which is not a file' in out, out)
    globbed[0]["source"]["path"] = "runs/*.json"
    write(root, claim_files.REGISTER, {"claims": globbed})
    files, r = claim_files.loaded(state, root)
    expect("a pattern that matches files only, beside a folder, has no problem",
           not any("runs/*.json" in p for p in claim_files.register_problems(
               files, r, set(), {}, root)))
    (root / "runs" / "seed1" / "x.json").write_text("{}")
    os.symlink(root / "gone.json", root / "runs" / "broken.json")
    globbed[0]["source"]["path"] = "runs/b*"
    write(root, claim_files.REGISTER, {"claims": globbed})
    rc, out = vcheck(root, "check-register")
    expect("check-register refuses a source path that matches a broken link",
           rc == 1 and "matches runs/broken.json, which is not a file" in out, out)
    os.remove(root / "runs" / "broken.json")
    globbed[0]["source"]["path"] = str(root / "runs" / "*")
    write(root, claim_files.REGISTER, {"claims": globbed})
    rc, out = vcheck(root, "check-register")
    expect("an absolute path that matches a folder is listed as a problem, not a crash",
           rc == 1 and "which is not a file" in out and "Traceback" not in out, out)
    os.makedirs(root / "runs" / "00-first")
    held = artifact_pins.ArtifactPins().pinned_files(inputs.ArtifactSource(
        path=str(root / "runs" / "*"), method="m", sha256="0" * 64))
    expect("a single pin goes to the first matching file, not a folder before it",
           list(held) == ["answers.json"], str(held))
    shutil.rmtree(root / "runs" / "seed1")
    os.rmdir(root / "runs" / "00-first")
    write(root, claim_files.REGISTER, {"claims": register})
    rc, out = vcheck(root, "check-register")
    claims = {c["id"]: c for c in read(root, claim_files.REGISTER)["claims"]}
    pin = claims["acc-7b-retrieval"]["source"].get("sha256")
    expect("check-register passes, pins the artifact claim, and asks for methods",
           rc == 0 and "PASSED. 5 claims. The core pinned the files of 1 artifact claims." in out
           and "method_needs.json for the 1 artifact claims" in out
           and isinstance(pin, dict) and list(pin) == ["answers.json"], out)
    expect("the core writes each claim's quotes, as id and words, and its origin",
           claims["gain-small-models"]["quotes"] == [
               ["gain-7b", "Retrieval raises the accuracy of the 7B model by 7.4 points"],
               ["gain-7b-again", "The 7B model gains 7.4 points from retrieval"]]
           and claims["seeds"]["origin"] == NOTE, json.dumps(claims["gain-small-models"]))
    # raises if the schema refuses it
    claim_register_parser.ClaimRegisterParser(set(), {}).parse_claim_entry(claims["seeds"],
                                                                           str(root))
    reg = watched_project.WatchedProject.load(root, state).check_registry()
    done = claim_files.stamp(state, first, reg.computational_methods,
                         reg.claim_field_types(), root)
    stamps = {(e["created"], e["last_updated"]) for name in claim_files.FILES[:4]
              for e in read(root, name).values()} | \
        {(e["created"], e["last_updated"]) for e in read(root, claim_files.REGISTER)["claims"]}
    expect("at the end of the round every stamp holds the commit", done and stamps == {(first,
                                                                                        first)},
           repr(stamps))
    return first


def round_two(root: Path, first: str) -> None:
    """The later round: what the core removes, and what the checks allow."""
    state = root / ".verifier"
    old_cands = read(root, claim_files.CANDIDATES)
    seeds_id = candidate(root, "We ran")
    second = commit(root, ROUND_2, "second notes")
    claim_files.begin_round(root, state, second)
    cands, quotes = read(root, claim_files.CANDIDATES), read(root, claim_files.QUOTES)
    classes = read(root, claim_files.CLASSES)
    claims = {c["id"]: c for c in read(root, claim_files.REGISTER)["claims"]}
    expect("a sentence that moved keeps its candidate id, its stamps and its quote",
           candidate(root, "We ran") == seeds_id and cands[seeds_id]["created"] == first
           and quotes["seeds"]["candidates"] == [seeds_id])
    expect("new candidates come at the end, with empty stamps",
           list(cands)[:len(set(cands) & set(old_cands))] == [c for c in old_cands if c in cands]
           and all(cands[c]["created"] == "" for c in cands if c not in old_cands)
           and len(cands) - len(set(cands) & set(old_cands)) == 4, json.dumps(cands, indent=1))
    without = candidate(root, "Without")
    expect("the quote of an edited sentence is removed with its class and claim, though the "
           "new sentence holds its words: the new sentence is a new candidate to answer",
           "acc-7b-no" not in quotes and "acc-7b-no-retrieval" not in classes
           and "acc-7b-no-retrieval" not in claims and cands[without]["created"] == "",
           json.dumps(quotes))
    expect("the quote of the changed number, its class and its claim are removed",
           "acc-7b" not in quotes and "acc-7b-retrieval" not in classes
           and "acc-7b-retrieval" not in claims)
    expect("a class that lost one quote keeps the other, and its claim's quotes follow",
           classes["gain-small-models"]["quotes"] == ["gain-7b"]
           and classes["gain-small-models"]["last_updated"] == ""
           and [q for q, _ in claims["gain-small-models"]["quotes"]] == ["gain-7b"]
           and claims["gain-small-models"]["created"] == first, json.dumps(classes))
    expect("a link to the removed claim is removed",
           claims["retrieval-helps"]["source"]["claims"] == ["gain-small-models"]
           and claims["retrieval-helps"]["last_updated"] == "")

    new_quotes = {"acc-7b-71": entry(candidates=[candidate(root, "With retrieval")],
                                     quote="the 7B model answers 71.2%", value="71.2"),
                  "gain-13b": entry(candidates=[candidate(root, "The 13B")],
                                    quote="The 13B model gains 4.1 points with retrieval",
                                    value="4.1")}
    drop = {candidate(root, "This note"): entry(why="pointer to the study"),
            without: entry(why="a repeat of the round-1 result")}
    write(root, claim_files.QUOTES, {**quotes, **new_quotes,
                                     "seeds": {**quotes["seeds"], "quote": "three seeds"}})
    write(root, claim_files.DROPPED, drop)
    rc, out = vcheck(root, "check-quotes")
    expect("check-quotes refuses a changed entry of round 1",
           rc == 1 and 'the entry "seeds" was changed. You cannot change an entry of an '
           'earlier round.' in out, out)
    write(root, claim_files.QUOTES, {**quotes, **new_quotes})
    rc, out = vcheck(root, "check-quotes")
    expect("check-quotes passes",
           rc == 0 and out == "PASSED. 2 new quotes and 2 new drops answer the 4 new "
           "candidates.\n", out)
    rc, out, fake = propose(root)
    later = claim_files.proposals_path(state, claim_files.read_round(state))
    items = json.loads(later.read_text()).get("items", {}) if later.exists() else {}
    sent = "\n".join(fake.prompts)
    expect("in a later round, propose-classes sends the new quotes and one item per class",
           rc == 0 and len(items) == 2 + 3 and "Retrieval adds {{7.4}} points at 7B." in sent,
           json.dumps(items) + sent)
    new_classes = {"acc-7b-retrieval-71": entry(quotes=["acc-7b-71"]),
                   "gain-13b": entry(quotes=["gain-13b"])}
    write(root, claim_files.CLASSES, {**classes, "acc-7b-retrieval-71": new_classes[
        "acc-7b-retrieval-71"], "seeds": {**classes["seeds"], "quotes": ["gain-13b"]}})
    rc, out = check_classes(root)
    expect("the class check refuses a class of round 1 that lost a quote",
           rc == 1 and 'class "seeds" lost the quote "seeds". You cannot remove a quote' in out,
           out)
    other_value = {**classes, "acc-7b-retrieval-71": new_classes["acc-7b-retrieval-71"],
                   "gain-small-models": {**classes["gain-small-models"],
                                         "quotes": ["gain-7b", "gain-13b"]}}
    write(root, claim_files.CLASSES, other_value)
    rc, out = check_classes(root)
    expect("the class check refuses a class whose quotes have two values (7.4 and 4.1)",
           rc == 1 and 'class "gain-small-models" holds quotes with different values (4.1, '
           '7.4)' in out, out)
    gained = {**classes, "acc-7b-retrieval-71": new_classes["acc-7b-retrieval-71"],
              "retrieval-helps": {**classes["retrieval-helps"], "quotes": ["helps", "gain-13b"]}}
    write(root, claim_files.CLASSES, gained)
    rc, out = check_classes(root)
    claims = {c["id"]: c for c in read(root, claim_files.REGISTER)["claims"]}
    expect("a class of round 1 may gain a quote (a quote with no value joins one with a value), "
           "and its claim's quotes follow",
           rc == 0 and "1 quotes added to existing classes" in out
           and [q for q, _ in claims["retrieval-helps"]["quotes"]] == ["helps", "gain-13b"],
           out)
    write(root, claim_files.CLASSES, {**classes, **new_classes})
    rc, out = check_classes(root)
    expect("the class check passes", rc == 0 and out.startswith(
        "PASSED. 2 new classes. 0 quotes added to existing classes."), out)

    register = read(root, claim_files.REGISTER)["claims"]
    new = [entry(id="acc-7b-retrieval-71", statement="With retrieval, the 7B model answers "
                 "71.2% of the questions.", value=71.2),
           entry(id="gain-13b", statement="Retrieval adds 4.1 points at 13B.", value=4.1)]

    def with_claim(cid: str, **change: object) -> list[dict]:
        return [{**c, **change} if c["id"] == cid else c for c in register] + new
    write(root, claim_files.REGISTER, {"claims": with_claim("seeds", statement="Changed.")})
    rc, out = vcheck(root, "check-register")
    expect("check-register refuses a changed claim of round 1",
           rc == 1 and 'Claim "seeds" was changed.' in out, out)
    hand_pin = [*register, {**new[0], "source": {"kind": "artifact", "path": "runs/answers.json",
                                                 "sha256": {"answers.json": "ab"}}}, new[1]]
    write(root, claim_files.REGISTER, {"claims": hand_pin})
    rc, out = vcheck(root, "check-register")
    expect("check-register refuses a pin the agent wrote",
           rc == 1 and 'Claim "acc-7b-retrieval-71": "sha256" or "proposed_sha256" was changed'
           in out, out)
    write(root, claim_files.REGISTER, {"claims": with_claim("seeds", last_updated="abc")})
    rc, out = vcheck(root, "check-register")
    expect("check-register refuses a stamp the agent changed",
           rc == 1 and 'Claim "seeds": a stamp was changed.' in out, out)
    grown = [{**c, "source": {"kind": "claims", "claims": [
        "gain-small-models", "acc-7b-retrieval-71"]}} if c["id"] == "retrieval-helps" else c
        for c in with_claim("gain-small-models")]
    write(root, claim_files.REGISTER, {"claims": grown})
    rc, out = vcheck(root, "check-register")
    expect("check-register refuses a claim of round 1 that gains claim ids in its source",
           rc == 1 and 'Claim "retrieval-helps" was changed.' in out, out)
    gains_source = with_claim("gain-small-models",
                              source={"kind": "artifact", "path": "runs/answers.json"})
    write(root, claim_files.REGISTER, {"claims": gains_source})
    rc, out = vcheck(root, "check-register")
    claims = {c["id"]: c for c in read(root, claim_files.REGISTER)["claims"]}
    expect("a claim with no source may gain one, which is pinned; check-register passes",
           rc == 0 and out.startswith("PASSED. 2 new claims. The core pinned the files of 1 ")
           and "sha256" in claims["gain-small-models"]["source"]
           and claims["gain-small-models"]["last_updated"] == "", out)
    reg = watched_project.WatchedProject.load(root, state).check_registry()
    done = claim_files.stamp(state, second, reg.computational_methods,
                         reg.claim_field_types(), root)
    claims = {c["id"]: c for c in read(root, claim_files.REGISTER)["claims"]}
    classes = read(root, claim_files.CLASSES)
    expect("the stamps tell what is new and what changed in round 2",
           done and (claims["gain-small-models"]["created"],
                     claims["gain-small-models"]["last_updated"]) == (first, second)
           and (claims["gain-13b"]["created"], claims["gain-13b"]["last_updated"])
           == (second, second)
           and (claims["seeds"]["created"], claims["seeds"]["last_updated"]) == (first, first)
           and classes["gain-small-models"]["last_updated"] == second,
           json.dumps(claims, indent=1))


def test_rounds() -> None:
    """Two rounds, end to end."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "notes").mkdir()
        git(root, "init", "-q")
        (root / ".gitignore").write_text(".verifier/\n")
        (root / ".verifier").mkdir()
        claim_files.save_scope(root / ".verifier", (structure.WHOLE,))
        round_two(root, round_one(root))


def test_files() -> None:
    """Partial and old-format claim files are refused; a failed check leaves the stamps empty."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "notes").mkdir()
        git(root, "init", "-q")
        (root / ".gitignore").write_text(".verifier/\n")
        sha = commit(root, ROUND_1, "first notes")
        state = root / ".verifier"
        state.mkdir()
        claim_files.save_scope(state, (structure.WHOLE,))
        claim_files.begin_round(root, state, sha)
        reg = watched_project.WatchedProject.load(root, state).check_registry()
        expect("a round whose checks fail is not stamped",
               not claim_files.stamp(state, sha, reg.computational_methods,
                         reg.claim_field_types(), root)
               and all(c["created"] == "" for c in read(root, claim_files.CANDIDATES).values()))
        (state / claim_files.CLASSES).unlink()
        rc, out = vcheck(root, "check-register")
        message = ""
        try:
            claim_files.begin_round(root, state, sha)
        except claim_files.ClaimFilesError as e:
            message = str(e)
        expect("partial state: every claim command and the round start stop, naming the "
               "missing file", rc == 1 and "claim_equiv_classes.json" in message
               and "these claim files are missing" in message, message)
        shutil.rmtree(state)
        state.mkdir()
        claim_files.save_scope(state, (structure.WHOLE,))
        for name in claim_files.FILES:
            write(root, name, {"claims": []} if name == claim_files.REGISTER else {})
        write(root, claim_files.CANDIDATES, [{"id": f"{NOTE}#S1", "text": "x"}])
        try:
            claim_files.load(state)
            message = ""
        except claim_files.ClaimFilesError as e:
            message = str(e)
        expect("old-format candidates are refused", "old format" in message, message)
        write(root, claim_files.CANDIDATES, {})
        write(root, claim_files.QUOTES, {"a": {"candidates": [], "quote": "q", "value": None,
                                               "statement": "s"}})
        try:
            claim_files.load(state)
            message = ""
        except claim_files.ClaimFilesError as e:
            message = str(e)
        expect("an old-format quote with register keys is refused", "old format" in message,
               message)
        write(root, claim_files.QUOTES, {})
        (state / claim_files.REGISTER).write_text("{not json")
        try:
            claim_files.begin_round(root, state, sha)
            broken = None
        except claim_files.ClaimFilesBroken as e:
            broken = str(e)
        prompt = daemon.build_prompt(rounds.Round(n=2, events=[], sent_at=0), root, False,
                                     extensions.ExtensionSet.load_named(()), broken,
                                 round_timeout=daemon.ROUND_TIMEOUT_SECONDS)
        expect("a claim file the agent broke is named in the round's prompt, to fix",
               broken is not None and "claim_register.json is not valid JSON" in prompt
               and "Fix the file first. Then run this command: vcheck check-register" in prompt,
               prompt)


def stamped_project(root: Path, note: str) -> tuple[Path, str]:
    """A repository whose first round, with one quote per sentence, passed and was stamped."""
    (root / "notes").mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text(".verifier/\n")
    (root / "runs").mkdir()
    (root / "runs" / "answers.json").write_text(json.dumps(ANSWERS))
    state = root / ".verifier"
    state.mkdir()
    claim_files.save_scope(state, (structure.WHOLE,))
    sha = commit(root, note, "first notes")
    claim_files.begin_round(root, state, sha)
    cands = read(root, claim_files.CANDIDATES)
    quotes = {f"q{n}": entry(candidates=[cid], quote=c["text"], value=None)
              for n, c in enumerate(cands.values()) for cid in [list(cands)[n]]}
    write(root, claim_files.QUOTES, quotes)
    write(root, claim_files.CLASSES, {f"c{n}": entry(quotes=[f"q{n}"]) for n in range(len(quotes))})
    register = [entry(id=f"c{n}", statement=q["quote"]) for n, q in enumerate(quotes.values())]
    register[0]["value"] = 68.4
    register[0]["source"] = {"kind": "artifact", "path": "runs/answers.json"}
    write(root, claim_files.REGISTER, {"claims": register})
    propose(root)
    reg = watched_project.WatchedProject.load(root, state).check_registry()
    assert vcheck(root, "check-register")[0] == 0
    assert claim_files.stamp(state, sha, reg.computational_methods,
                         reg.claim_field_types(), root)
    return state, sha


def test_review_cases() -> None:
    """Edits refused in a failed round do not stick; pins and methods are the core's alone."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state, sha = stamped_project(root, ROUND_1)
        reg = watched_project.WatchedProject.load(root, state).check_registry()
        claim_files.begin_round(root, state, sha)          # a new round, on the same commit
        claims = read(root, claim_files.REGISTER)["claims"]
        forged = [{**claims[0], "statement": "Forged.", "source": {**claims[0]["source"],
                   "sha256": {"answers.json": "0" * 64}}}, *claims[1:]]
        write(root, claim_files.REGISTER, {"claims": forged})
        rc, out = vcheck(root, "check-register")
        expect("a forged pin on a claim of an earlier round is refused",
               rc == 1 and '"sha256" or "proposed_sha256" was changed' in out, out)
        expect("a round with a refused edit is not stamped",
               not claim_files.stamp(state, sha, reg.computational_methods,
                         reg.claim_field_types(), root))
        claim_files.begin_round(root, state, sha)
        again = read(root, claim_files.REGISTER)["claims"][0]
        rc, out = vcheck(root, "check-register")
        expect("the next round puts the refused edit back, so it never becomes the start",
               again["statement"] != "Forged." and again["source"]["sha256"]
               == claims[0]["source"]["sha256"] and rc == 0, json.dumps(again) + out)
        method = [{**claims[0], "source": {**claims[0]["source"], "method": "made_up"}},
                  *claims[1:]]
        write(root, claim_files.REGISTER, {"claims": method})
        rc, out = vcheck(root, "check-register")
        expect("a method the agent wrote is refused", rc == 1 and '"method" was changed' in out,
               out)
        write(root, claim_files.REGISTER, {"claims": claims})
        (state / "methods").mkdir()
        (state / "methods" / "count.py").write_text(
            "from checks.interface import Computed, method\n\n\n@method('count_answers')\n"
            "def count_answers(path):\n    \"\"\"The number of answers.\"\"\"\n"
            "    import json\n    return Computed(value=len(json.load(open(path))['records']),"
            " n=1)\n")
        files, r = claim_files.loaded(state, root)
        claim_files.set_method(files, r, "c0", "count_answers")    # as write-methods does
        claim_files.save(files, state)
        claim_files.save_round(r, state)
        (root / "runs" / "answers.json").write_text(json.dumps({"records": []}))
        project = watched_project.WatchedProject.load(root, state)
        editor = artifact_pins.ClaimRegisterPinEditor(
            state / claim_files.REGISTER, root,
            project.claim_register_parser(project.check_registry()))
        new_pin = artifact_pins.ArtifactPins.current_checksum(root, "runs/answers.json")
        changed = editor.propose_checksum("runs/answers.json", new_pin)
        rc, out = vcheck(root, "check-register")
        expect("a checksum proposed through the pin editor in the round is not a forged pin",
               changed == ["c0"] and rc == 0, out)
        r = claim_files.ClaimRound(commit="x", first=True, existing={
            "quotes": {}, "dropped": {}, "classes": {}, "register": {}},
            pinned={"c9": {"m.json": "a" * 64}})
        pinned_now = {"id": "c9", "source": {"kind": "artifact", "path": "m.json",
                                             "sha256": {"m.json": "a" * 64},
                                             "proposed_sha256": {"m.json": "b" * 64}}}
        with tempfile.TemporaryDirectory() as other:
            claim_files.save_round(r, Path(other))
            claim_files.record_pins(Path(other), [pinned_now])
            r = claim_files.read_round(Path(other))
        empty_round = claim_files.ClaimRound(commit="x", first=True, existing={
            "quotes": {}, "dropped": {}, "classes": {}, "register": {}})
        quote = {"created": "", "last_updated": "", "candidates": [], "quote": "acc 5",
                 "value": "n/a"}
        one_class = {"c": {"created": "", "last_updated": "", "quotes": ["q1"]}}
        for name, quotes, classes in (
                ("a class written as a list", {"q1": quote}, {"c": ["q1"]}),
                ("a quote whose value is not a number", {"q1": quote}, one_class),
                ("a quote written as a list", {"q1": ["oops"]}, one_class)):
            malformed = claim_files.ClaimFiles({}, quotes, {}, classes, [])
            problems = claim_files.class_problems(malformed, empty_round)   # no crash
            expect(f"the class check gives problems for {name}, with no crash",
                   isinstance(problems, list), repr(problems))
        expect("a checksum proposed for a claim pinned in this round is what the round expects",
               claim_files.pins_the_round_expects(r, "c9", None)
               == claim_files._pins_of(pinned_now), repr(r.proposed))
        # another process holds the lock of the claim files: the pin editor waits for it
        holder = subprocess.Popen(
            [sys.executable, "-c", "import sys; from pathlib import Path; "
             "from utils import claim_files; "
             f"held = claim_files.lock(Path({str(state)!r})).held(); held.__enter__(); "
             "print('held', flush=True); sys.stdin.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))})
        holder.stdout.readline()
        withdrawn: list[list[str]] = []
        waiting = threading.Thread(target=lambda: withdrawn.append(
            editor.withdraw_proposed_checksum("runs/answers.json", new_pin)))
        waiting.start()
        waiting.join(1)
        blocked = waiting.is_alive()
        holder.stdin.close()
        holder.wait(30)
        waiting.join(30)
        expect("the pin editor waits while another process holds the lock of the claim files",
               blocked and withdrawn == [["c0"]], f"blocked={blocked} withdrawn={withdrawn}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "notes").mkdir()
        git(root, "init", "-q")
        (root / ".gitignore").write_text(".verifier/\n")
        state = root / ".verifier"
        state.mkdir()
        claim_files.save_scope(state, (structure.WHOLE,))
        sha = commit(root, ROUND_1, "first notes")
        claim_files.begin_round(root, state, sha)
        write(root, claim_files.DROPPED, {cid: entry(why="heading")
                                          for cid in read(root, claim_files.CANDIDATES)})
        reg = watched_project.WatchedProject.load(root, state).check_registry()
        rc, out = check_classes(root)
        expect("a first round with no quotes needs no proposals, and is stamped",
               rc == 0 and claim_files.stamp(state, sha, reg.computational_methods,
                         reg.claim_field_types(), root), out)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repeat = ROUND_1 + "We ran three seeds for each condition.\n"
        state, sha = stamped_project(root, repeat)
        quotes = read(root, claim_files.QUOTES)
        seeds = [cid for cid, c in read(root, claim_files.CANDIDATES).items()
                 if c["text"].startswith("We ran")]
        drop_id = seeds[1]
        holder = next(q for q, v in quotes.items() if v["candidates"] == [drop_id])
        rest = {k: v for k, v in quotes.items() if k != holder}
        files = claim_files.load(state)
        files.quotes = rest
        files.dropped = {drop_id: {"created": sha, "last_updated": sha, "why": "a repeat"}}
        claim_files.save(files, state)
        second = commit(root, ROUND_1.replace("We ran three seeds for each condition.\n", "")
                        + "We ran three seeds for each condition.\n", "move")
        claim_files.begin_round(root, state, second)
        rc, out = vcheck(root, "check-quotes")
        expect("a quote that moves onto a dropped repeat takes its place, and the drop goes",
               rc == 0, out)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state, sha = stamped_project(root, ROUND_1)
        (root / "notes" / "0-draft.md").write_text("We ran three seeds for each condition.\n")
        second = commit(root, ROUND_1, "a draft")
        summary = claim_files.begin_round(root, state, second)
        expect("a new note that repeats a quoted sentence moves the quote, and removes nothing",
               "0 quotes removed, 1 moved; 0 claims removed" in summary, summary)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state, sha = stamped_project(root, ROUND_1)
        quotes = read(root, claim_files.QUOTES)
        write(root, claim_files.QUOTES, {**quotes, "bad": entry(candidates=[{"id": "x"}],
                                                                quote="x", value=None)})
        rc, out = vcheck(root, "check-quotes")
        expect("a quote naming candidates that are not ids is a problem, not a crash",
               rc == 1 and '"candidates" must list one or more candidate ids' in out, out)
        second = commit(root, ROUND_2, "second notes")
        classes = read(root, claim_files.CLASSES)
        write(root, claim_files.CLASSES, {**classes, "bad": entry(quotes=[["q0"]])})
        claim_files.begin_round(root, state, second)
        rc, out = check_classes(root)
        expect("a class naming quotes that are not ids is a problem, not a crash",
               rc == 1 and "FAILED" in out, out)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state, sha = stamped_project(root, ROUND_1)
        record_checked_commit(state, sha)
        second = commit(root, ROUND_2, "second notes")      # the HEAD moves; the daemon's does not
        rc, out = vcheck(root, "check-register")
        expect("a claim command begins no round for a HEAD that the daemon does not check",
               claim_files.read_round(state).commit == sha, out)
        record_checked_commit(state, second)
        rc, out = vcheck(root, "check-register")
        expect("a claim command begins the round itself when the round on record is for an "
               "older commit than the daemon checks",
               claim_files.read_round(state).commit == second, out)


def test_drop_reasons() -> None:
    """A drop's reason is free words; a repeat names a real quote; an auto: drop is the core's."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "notes").mkdir()
        git(root, "init", "-q")
        (root / ".gitignore").write_text(".verifier/\n")
        state = root / ".verifier"
        state.mkdir()
        claim_files.save_scope(state, (structure.WHOLE,))
        sha = commit(root, ROUND_1, "first notes")
        claim_files.begin_round(root, state, sha)
        cands = list(read(root, claim_files.CANDIDATES))
        first, rest = cands[0], cands[1:]
        write(root, claim_files.QUOTES, {"q0": entry(candidates=[first],
                                                     quote=read(root, claim_files.CANDIDATES)
                                                     [first]["text"], value=None)})
        for why, ok, says in (
                ("not a claim", True, ""),
                ("Heading of the results", True, ""),
                ("repeat of q9", False, 'says it repeats "q9", but no quote has this id'),
                ("repeat of q0", True, ""),
                ("repeat of quote q0.", True, ""),
                ("background: what Smith et al. found", True, ""),
                ("auto:R4 acknowledgement", False, "not what the rules give"),
                ("auto:R11 pointer", False, "not what the rules give"),
                ("auto:label 10", False, "the labeller did not drop"),
                ("auto:anything", False, "not what the rules give"),
                ("Auto:R4 acknowledgement", False, "not what the rules give"),
                ("AUTO:label 10", False, "the labeller did not drop")):
            write(root, claim_files.DROPPED, {cid: entry(why=why) for cid in rest})
            rc, out = vcheck(root, "check-quotes")
            expect(f"check-quotes {'takes' if ok else 'refuses'} the drop reason {why!r}",
                   (rc == 0) == ok and says in out, out)
        files, r = claim_files.loaded(state, root)
        files.dropped = {cid: entry(why="auto:label 12") for cid in rest}
        r.keep_scores = {cid: 12 for cid in rest}
        got = claim_files.quote_problems(files, r)
        expect("a label drop passes when the labeller's record holds it", got == [], repr(got))
        files.dropped = {cid: entry(why="AUTO:Label 12") for cid in rest}
        got = claim_files.quote_problems(files, r)
        expect("a label drop in other case is checked as one, and passes on the record",
               got == [], repr(got))
        r.keep_scores = {cid: 70 for cid in rest}
        got = claim_files.quote_problems(files, r)
        expect("a label drop whose score differs from the labeller's record is refused",
               len(got) == len(rest) and all("the labeller did not drop" in g for g in got),
               repr(got))
        files.dropped = {cid: {"created": sha, "last_updated": sha,
                               "why": "auto:R4 acknowledgement"} for cid in rest}
        r.existing[claim_files._kind(claim_files.DROPPED)] = copy.deepcopy(files.dropped)
        got = claim_files.quote_problems(files, r)
        expect("an old stamped auto drop is left as it is", got == [], repr(got))
def test_accept_classes() -> None:
    """`vcheck accept-classes` makes the classes from the models' proposal, in code."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "notes").mkdir()
        git(root, "init", "-q")
        (root / ".gitignore").write_text(".verifier/\n")
        state = root / ".verifier"
        state.mkdir()
        claim_files.save_scope(state, (structure.WHOLE,))
        sha = commit(root, ROUND_1, "first notes")
        claim_files.begin_round(root, state, sha)
        cands = read(root, claim_files.CANDIDATES)
        quotes = {f"q {n}" if n in (2, 5) else f"q{n}": entry(candidates=[cid], quote=c["text"],
                                                               value=None)
                  for n, (cid, c) in enumerate(cands.items())}
        write(root, claim_files.QUOTES, quotes)
        rc, out = vcheck(root, "accept-classes")
        expect("accept-classes needs the proposal first",
               rc == 1 and "vcheck propose-classes" in out, out)
        rc, out, _ = propose(root)
        rc, out = vcheck(root, "accept-classes")
        classes = read(root, claim_files.CLASSES)
        gains = sorted(q for q, v in quotes.items() if "7.4 points" in v["quote"])
        expect("a proposed group of quotes becomes one class, named by its first quote "
               "with no space", rc == 0 and gains == ["q 2", "q3"]
               and classes.get("q_2", {}).get("quotes") == gains, out + repr(classes))
        expect("each quote in no group gets a class of its own, with no space in its id",
               classes.get("q_5", {}).get("quotes") == ["q 5"]
               and len(classes) == len(quotes) - 1, repr(classes))
        expect("the classes pass the class check", check_classes(root)[0] == 0)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state, sha = stamped_project(root, ROUND_1)
        second = commit(root, ROUND_2, "second notes")
        record_checked_commit(state, second)        # the daemon begins the round on it
        files, r = claim_files.loaded(state, root)
        new = [cid for cid, c in files.candidates.items() if c["created"] == ""]
        files.quotes.update({f"n{k}": entry(
            candidates=[cid], quote=files.candidates[cid]["text"], value=None)
                             for k, cid in enumerate(new)})
        claim_files.save(files, state)
        files, r = claim_files.loaded(state, root)
        items = claim_files.proposal_items(files)
        claim_files.write_proposal(state, r, items, [frozenset({"quote:n0", "class:c2"}),
                                                     frozenset({"quote:n1", "quote:n2"})])
        rc, out = vcheck(root, "accept-classes")
        classes = read(root, claim_files.CLASSES)
        expect("a later round: a quote grouped with an old class joins that class",
               rc == 0 and classes["c2"]["quotes"] == ["q2", "n0"]
               and classes["c2"]["last_updated"] == "", out + repr(classes.get("c2")))
        expect("a later round: the new group is a new class, the rest one class each",
               classes["n1"]["quotes"] == ["n1", "n2"] and classes["n3"]["quotes"] == ["n3"],
               repr(classes))
        files, r = claim_files.loaded(state, root)
        files.classes = {k: v for k, v in files.classes.items() if v["created"] != ""}
        notes: list[str] = []
        got = claim_files.accept_proposal(files, {"commit": second, "items": items, "classes": [
            ["class:c4", "class:c2", "quote:n0"], ["quote:n1", "quote:n2"]]}, notes)
        expect("a proposed group with two old classes joins its new quotes to the first old "
               "class by id, leaves the other as it is, and says so",
               got["c2"]["quotes"] == ["q2", "n0"] and got["c4"] == files.classes["c4"]
               and got["n1"]["quotes"] == ["n1", "n2"] and len(notes) == 1
               and "c2" in notes[0] and "c4" in notes[0], repr(got) + repr(notes))
        for group, says in ((["class:q2", "quote:n0"], "class:q2 names no class"),
                            (["quote:c2", "quote:n0"], "quote:c2 names no quote"),
                            (["thing:n0"], "thing:n0, which is neither")):
            try:
                claim_files.accept_proposal(files, {"classes": [group]})
                refused = ""
            except claim_files.ClaimFilesError as e:
                refused = str(e)
            except Exception as e:  # noqa: BLE001 - the case is about which error
                refused = f"{type(e).__name__}: {e}"
            expect(f"a proposed group naming {group[0]} is refused with a clear error",
                   says in refused, refused)


def fill_fixture() -> claim_files.ClaimFiles:
    """Claim files whose classes mix sentences, captions and a table, and two claims written."""
    kinds = {"s1": "sentence", "s2": "sentence", "s3": "sentence", "s4": "sentence",
             "s5": "sentence", "s6": "sentence", "row1": "table caption",
             "row2": "figure caption", "tab1": "table"}
    words = {"row1": "Table 1: ours, PQ 52.82 at 171.70 GFLOPs.",
             "s1": "Our model reaches a PQ of 52.82 at a cost of 171.7 GFLOPs on COCO.",
             "s2": "Our model reaches a PQ of 52.82.",
             "row2": "Figure 2: base, PQ 50.10.",
             "tab1": "Table 1: Columns: Model | PQ. Rows: Model: base; PQ: 50.10",
             "s3": "With retrieval, the 7B model answers 68.4% of the questions on the test set.",
             "s4": "The 7B model answers 68.4% with retrieval.",
             "s5": "The cost is 171.70 GFLOPs, and the PQ is 52.82, on the COCO validation set.",
             "s6": "PQ 52.82 at 171.7 GFLOPs."}
    candidates = {f"paper.tex#{k}": entry(kind=kind, text=words[k], origin="paper.tex")
                  for k, kind in kinds.items()}
    quotes = {k: entry(candidates=[f"paper.tex#{k}"], quote=words[k], value=None) for k in kinds}
    classes = {"ours-pq": entry(quotes=["row1", "s2", "s1"]),
               "base-pq": entry(quotes=["row2", "tab1"]),
               "acc-7b": entry(quotes=["s3", "s4"]),
               "cost": entry(quotes=["s5", "s6"]),
               "old": {"created": "abc", "last_updated": "abc", "quotes": []},
               "agent": entry(quotes=[])}
    register = [{"id": "old", "statement": "the agent's words", "created": "abc",
                 "last_updated": "abc"},
                entry(id="agent", statement="the agent's words of this round")]
    return claim_files.ClaimFiles(candidates, quotes, {}, classes, register)


def test_fill_register() -> None:
    """`vcheck fill-register` writes the claims of new classes in code, statement from a quote."""
    files = fill_fixture()
    kept = copy.deepcopy(files.register)
    added = claim_files.fill_register(files)
    got = {e["id"]: e for e in files.register}
    expect("fill-register writes a claim for each class with none, in class order",
           added == ["ours-pq", "base-pq", "acc-7b", "cost"], repr(added))
    expect("fill-register leaves the claims already written as they were",
           files.register[:2] == kept, repr(files.register[:2]))
    expect("a sentence beats a caption, and the shorter sentence that lacks a number of "
           "the other sentence loses", got["ours-pq"]["statement"] == fill_fixture().quotes[
               "s1"]["quote"], got["ours-pq"]["statement"])
    expect("a class with no sentence takes its first quote",
           got["base-pq"]["statement"] == "Figure 2: base, PQ 50.10.",
           got["base-pq"]["statement"])
    expect("of sentences with the same numbers, the shortest wins",
           got["acc-7b"]["statement"] == "The 7B model answers 68.4% with retrieval.",
           got["acc-7b"]["statement"])
    expect("numbers are compared by value: 171.7 matches 171.70",
           got["cost"]["statement"] == "PQ 52.82 at 171.7 GFLOPs.", got["cost"]["statement"])
    expect("a new claim has the shape of one the agent writes and the core completes: id, "
           "statement, empty stamps, quotes, origin, and no source",
           list(got["acc-7b"]) == ["id", "statement", "created", "last_updated", "quotes",
                                   "origin"]
           and got["acc-7b"]["quotes"] == [["s3", fill_fixture().quotes["s3"]["quote"]],
                                           ["s4", fill_fixture().quotes["s4"]["quote"]]]
           and got["acc-7b"]["origin"] == "paper.tex"
           and got["acc-7b"]["created"] == got["acc-7b"]["last_updated"] == "",
           json.dumps(got["acc-7b"]))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "notes").mkdir()
        git(root, "init", "-q")
        (root / ".gitignore").write_text(".verifier/\n")
        state = root / ".verifier"
        state.mkdir()
        claim_files.save_scope(state, (structure.WHOLE,))
        sha = commit(root, ROUND_1, "first notes")
        claim_files.begin_round(root, state, sha)
        cands = read(root, claim_files.CANDIDATES)
        write(root, claim_files.QUOTES, {f"q{n}": entry(candidates=[cid], quote=c["text"],
                                                        value=None)
                                         for n, (cid, c) in enumerate(cands.items())})
        rc, out = vcheck(root, "fill-register")
        expect("fill-register needs the CLASSES stage to pass first",
               rc == 1 and "Stage 2 (CLASSES)" in out, out)
        propose(root)
        vcheck(root, "accept-classes")
        rc, out = vcheck(root, "fill-register")
        register = read(root, claim_files.REGISTER)["claims"]
        by_text = {e["statement"]: e["id"] for e in register}
        expect("fill-register passes and writes one claim per class",
               rc == 0 and out.startswith("PASSED. Code wrote 5 new claims")
               and len(register) == len(read(root, claim_files.CLASSES)), out)
        expect("of two sentences with the same number, the shorter is the statement",
               "The 7B model gains 7.4 points from retrieval." in by_text
               and not any(t.startswith("Retrieval raises") for t in by_text), repr(by_text))
        expect("check-register passes on the register code wrote",
               vcheck(root, "check-register")[0] == 0)
        rc, out = vcheck(root, "check-register")
        expect("check-register says nothing of code's claims once it ran",
               rc == 0 and "Code wrote" not in out, out)
        # vcheck link-claims links the general claim to the claims it rests on
        register = read(root, claim_files.REGISTER)
        helps = by_text["Retrieval helps language models answer factual questions."]
        gain = by_text["The 7B model gains 7.4 points from retrieval."]
        acc = by_text["With retrieval, the 7B model answers 68.4% of the questions correctly."]
        for e in register["claims"]:
            if e["id"] == helps:
                e["source"] = {"kind": "claims", "claims": [gain, acc]}
        write(root, claim_files.REGISTER, register)
        rc, out = vcheck(root, "check-register")
        expect("the register passes with a claims source added to a claim code wrote",
               rc == 0, out)
        reg = watched_project.WatchedProject.load(root, state).check_registry()
        expect("the round with code's register is stamped",
               claim_files.stamp(state, sha, reg.computational_methods,
                                 reg.claim_field_types(), root))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state, sha = stamped_project(root, ROUND_1)
        before = read(root, claim_files.REGISTER)["claims"]
        record_checked_commit(state, commit(root, ROUND_2, "second notes"))
        files, r = claim_files.loaded(state, root)
        new = [cid for cid, c in files.candidates.items() if c["created"] == ""]
        files.quotes.update({f"n{k}": entry(
            candidates=[cid], quote=files.candidates[cid]["text"], value=None)
                             for k, cid in enumerate(new)})
        files.classes.update({f"n{k}": entry(quotes=[f"n{k}"]) for k in range(len(new))})
        claim_files.save(files, state)
        rc, out = vcheck(root, "check-register")
        expect("a later round: check-register names the classes with no claim before "
               "fill-register", rc == 1 and 'Class "n0" has no claim' in out, out)
        rc, out = vcheck(root, "fill-register")
        after = {e["id"]: e for e in read(root, claim_files.REGISTER)["claims"]}
        kept = {e["id"]: e for e in before if e["id"] in after}
        expect("a later round: fill-register passes, adds the new claims, and leaves every "
               "stamped claim as it was", rc == 0 and len(after) == len(kept) + len(new)
               and all(after[cid] == e for cid, e in kept.items()), out)
        rc, out = vcheck(root, "check-register")
        expect("a later round: check-register passes, and asks the agent for no link",
               rc == 0 and "Code wrote" not in out and "link" not in out, out)
        agents = (Path(daemon.TEMPLATES) / "AGENTS.md").read_text()
        expect("AGENTS.md gives only the artifact and document sources, and no link steps",
               '{"kind": "artifact"' in agents and '{"kind": "document"' in agents
               and '"kind": "claims"' not in agents and "source.claims" not in agents)


class SameNumberModel:
    """models.ask_and_read for the shortlist calls: a candidate is a duplicate of a Q when
    both state `number` and they are not both table rows (a row's item starts with "Table")."""

    def __init__(self, number: str) -> None:
        self.number = number

    def __call__(self, prompt: str, model: str, read, timeout: int = 300, **kwargs) -> object:
        head, rest = prompt.split("\n\nCandidates:\n")
        qs = [line.split(". ", 1)[1] for line in head.splitlines()[1:]]
        cands = [line.split(". ", 1)[1] for line in rest.split("\n\n")[0].splitlines()]
        return read(json.dumps({f"Q{j}": [i for i, c in enumerate(cands, 1) if c != q
                                          and self.number in q and self.number in c
                                          and not (q.startswith("Table")
                                                   and c.startswith("Table"))]
                                for j, q in enumerate(qs, 1)}))


def sentence_files(sentences: dict[str, str]) -> claim_files.ClaimFiles:
    """New quotes of a paper: each sentence as one quote, its id the sentence's key."""
    candidates = {f"p.tex#{k}": entry(kind="sentence", text=t, origin="p.tex")
                  for k, t in sentences.items()}
    quotes = {k: entry(candidates=[f"p.tex#{k}"], quote=t, value=None)
              for k, t in sentences.items()}
    return claim_files.ClaimFiles(candidates, quotes, {}, {}, [])


def test_claim_numbers_only() -> None:
    """Numbers of table or section references and citation years count for nothing."""
    files = sentence_files({"s1": "Ours reaches 85.2 accuracy, as Table 3 shows (Lee et al., "
                                  "2020).", "s2": "Ours reaches 85.2 accuracy."})
    said = claim_files.statement_of(["s1", "s2"], files)
    expect("a statement does not lose to a sentence for a table number or a citation year",
           said == "Ours reaches 85.2 accuracy.", said)



def test_values_with_units() -> None:
    """A value with a unit right after it is read; a model size and a scientific number are not."""
    for printed, want in (("0.1967s; 0.2190s", ["0.1967", "0.2190"]), ("12ms per step", ["12"]),
                       ("3x faster", ["3"]), ("uses 8GB", ["8"]), ("-0.5s", ["-0.5"]),
                       ("a 70B model", []), ("1.2k samples", []), ("lr 5e-4", []),
                       ("a 3x3 grid", []), ("85.3\\%", ["85.3"])):
        got = extract.claim_numbers(printed)
        expect(f"{printed!r} has the values {want}", got == want, repr(got))
    marked = extract.value_marked_text("took 0.1967s and 12ms", "12", ["0.1967"],
                                       anonymized=True)
    expect("a value with a unit is marked", marked == "took *redacted*s and {{VALUE}}ms", marked)
    expect("the other number readers do not read a unit",
           extract.numbers("0.1967s and 5e-4") == [], repr(extract.numbers("0.1967s and 5e-4")))


TABLE_NOTE = """# Results

| Model | Layers | Acc |
|---|---|---|
| Ours | 12 | 85.2 |
| Ours | 24 | 87.9 |
"""


def test_only_tables() -> None:
    """A first round whose new quotes are all whole tables is not stuck."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "notes").mkdir()
        git(root, "init", "-q")
        (root / ".gitignore").write_text(".verifier/\n")
        state = root / ".verifier"
        state.mkdir()
        claim_files.save_scope(state, (structure.WHOLE,))
        sha = commit(root, TABLE_NOTE, "a table")
        claim_files.begin_round(root, state, sha)
        cands = read(root, claim_files.CANDIDATES)
        quotes, dropped = {}, {}
        for cid, c in cands.items():
            if c["kind"] == "table":
                quotes[cid.split("#")[1]] = entry(candidates=[cid], quote=c["text"], value=None)
        for cid, c in cands.items():
            if c["kind"] != "table":
                dropped[cid] = entry(why=f"repeat of {next(iter(quotes))}")
        write(root, claim_files.QUOTES, quotes)
        write(root, claim_files.DROPPED, dropped)
        rc, out = vcheck(root, "check-quotes")
        expect("the quotes are all whole tables and pass check-quotes", rc == 0, out)
        expect("no quote is sent to grouping", claim_files.proposal_items(
            claim_files.load(state)) == {})
        rc, out, fake = propose(root)
        files, r = claim_files.loaded(state, root)
        expect("propose-classes writes a proposal file with no group and calls no model",
               rc == 0 and not fake.prompts
               and claim_files.read_proposal(state, r)["classes"] == [], out)
        rc, out = vcheck(root, "accept-classes")
        expect("accept-classes gives each table a class of its own",
               rc == 0 and len(read(root, claim_files.CLASSES)) == len(quotes), out)



def filled_project(root: Path) -> Path:
    """A first round of ROUND_1 with classes made in code and the register filled in code."""
    (root / "notes").mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text(".verifier/\n")
    state = root / ".verifier"
    state.mkdir()
    claim_files.save_scope(state, (structure.WHOLE,))
    sha = commit(root, ROUND_1, "first notes")
    claim_files.begin_round(root, state, sha)
    cands = read(root, claim_files.CANDIDATES)
    write(root, claim_files.QUOTES, {f"q{n}": entry(candidates=[cid], quote=c["text"], value=None)
                                     for n, (cid, c) in enumerate(cands.items())})
    propose(root)
    vcheck(root, "accept-classes")
    assert vcheck(root, "fill-register")[0] == 0
    return state


def test_written_statements() -> None:
    """The agent cannot rewrite a statement that fill-register wrote."""
    agents = (Path(daemon.TEMPLATES) / "AGENTS.md").read_text()
    expect("AGENTS.md no longer says each code-written statement is one of its quotes",
           "which is one of its quotes" not in " ".join(agents.split()))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        filled_project(root)
        register = read(root, claim_files.REGISTER)
        changed = copy.deepcopy(register)
        changed["claims"][0]["statement"] += " Rewritten."
        write(root, claim_files.REGISTER, changed)
        rc, out = vcheck(root, "check-register")
        cid = changed["claims"][0]["id"]
        expect("check-register refuses a rewritten statement of a claim code wrote",
               rc == 1 and f'Claim "{cid}"' in out and "statement" in out, out)
        write(root, claim_files.REGISTER, register)
        rc, out = vcheck(root, "check-register")
        expect("with the statement put back, check-register passes", rc == 0, out)
        write(root, claim_files.REGISTER, changed)
        rc, out = vcheck(root, "check-register")
        expect("after check-register passed once, a rewrite is still refused", rc == 1, out)



def sister_files() -> claim_files.ClaimFiles:
    """One sentence with three values: three sister quotes with the same words, in one note."""
    words = "The effect is 0.42 (95% CI 0.31 to 0.53, n = 212), as in (Lee, 2020)."
    candidates = {"p.tex#s-1": entry(kind="sentence", text=words, origin="p.tex")}
    quotes = {f"q-{n}": entry(candidates=["p.tex#s-1"], quote=words, value=v)
              for n, v in enumerate(("0.42", "0.31", "0.53"), start=1)}
    return claim_files.ClaimFiles(candidates, quotes, {}, {}, [])


def test_values() -> None:
    """Sister quotes: one text for each value, marked by one function, never joined."""
    words = "The effect is 0.42 (95% CI 0.31 to 0.53)."
    listed = {"q": entry(candidates=["c"], quote=words, values=["0.42", "0.31%", "0.53"]),
              "one": entry(candidates=["d"], quote="It reaches 7.4 points.", values=["7.4"]),
              "none": entry(candidates=["e"], quote="It helps.", values=[]),
              "q-2": entry(candidates=["f"], quote="taken", value=None)}
    split = claim_files.split_values(listed)
    expect("split_values gives one quote for each listed value, with the same words and "
           "candidates; one value or none keeps the id; a value is read as a number",
           {k: v["value"] for k, v in split.items()} == {
               "q-1": "0.42", "q-2-2": "0.31", "q-3": "0.53", "one": "7.4", "none": None,
               "q-2": None}
           and all(v["quote"] == words and "values" not in v for k, v in split.items()
                   if k.startswith("q-") and k != "q-2"), repr(split))
    files = sister_files()
    expect("a quote's text marks its own value in double braces, and leaves its sisters",
           claim_files.quote_text(files, "q-2") == "The effect is 0.42 (95% CI {{0.31}} to "
           "0.53, n = 212), as in (Lee, 2020).", claim_files.quote_text(files, "q-2"))
    expect("anonymized, its own value is {{VALUE}} and each sister value is *redacted*",
           claim_files.quote_text(files, "q-2", anonymized=True) == "The effect is *redacted* "
           "(95% CI {{VALUE}} to *redacted*, n = 212), as in (Lee, 2020).",
           claim_files.quote_text(files, "q-2", anonymized=True))
    expect("a number of a citation is never marked",
           extract.value_marked_text("as in (Lee, 2020) we get 2020", "2020")
           == "as in (Lee, 2020) we get {{2020}}")
    expect("the files keep each quote's words as they are",
           all(q["quote"].startswith("The effect is 0.42 (95% CI 0.31") for q in
               files.quotes.values()))
    items = claim_files.proposal_items(files)
    expect("grouping gets one item for each sister, each with its own value marked",
           len(items) == 3 and len({text.comparable(t) for t in items.values()}) == 3
           and not duplicates.identical_pairs(items), repr(items))
    yes_to_all = {q: {c: True for c in items if c != q} for q in items}
    expect("grouping never joins two quotes whose values differ, though the model says yes",
           duplicates.decide(items, yes_to_all, values=claim_files.item_values(files, items))
           == [], repr(items))
    expect("two values are one value within 1% or 2 steps of the cruder last digit, "
           "whichever is larger",
           claim_files.same_value("85.20", "85.2") and claim_files.same_value("1000", "1009")
           and not claim_files.same_value("1000", "1011")
           and claim_files.same_value("85", "86.9") and not claim_files.same_value("85", "87.1")
           and claim_files.same_value("0.42", "0.44") and not claim_files.same_value("0.42", "0.45")
           and not claim_files.same_value("85.2", "86.1")
           and claim_files.same_value(None, "3"))
    r = claim_files.ClaimRound(commit="c", first=True, existing={
        "candidates": {}, "quotes": {}, "dropped": {}, "classes": {}, "register": {}})
    bad = copy.deepcopy(files)
    bad.quotes["q-2"]["value"] = "0.30"
    bad.quotes["q-3"]["value"] = "0.42"
    problems = claim_files.quote_problems(bad, r)
    expect("check-quotes refuses a value the quote does not print, and two sisters with one "
           "value", any("has the value \"0.30\", which its quote does not print" in p
                        for p in problems)
           and any('"q-1" and "q-3" have the same words, candidates and value' in p
                   for p in problems), "\n".join(problems))
    files.classes = {"c": entry(quotes=["q-1", "q-2"]), "d": entry(quotes=["q-3"])}
    problems = claim_files.class_problems(files, r)
    expect("the class check refuses a class with two values",
           any('class "c" holds quotes with different values (0.31, 0.42)' in p
               for p in problems), "\n".join(problems))
    files.classes = {f"c{n}": entry(quotes=[f"q-{n}"]) for n in (1, 2, 3)}
    claim_files.fill_register(files)
    said = {e["id"]: (e["statement"], e["value"]) for e in files.register}
    expect("fill-register gives the sister claims one statement, the quote's words, and each "
           "its own value", {v for _, v in said.values()} == {0.42, 0.31, 0.53}
           and len({s for s, _ in said.values()}) == 1
           and next(iter(said.values()))[0] == files.quotes["q-1"]["quote"], repr(said))
    expect("a claim's anonymized text hides its value and its sister claims' values",
           claim_files.claim_text(files, "c3", anonymized=True) == "The effect is *redacted* "
           "(95% CI *redacted* to {{VALUE}}, n = 212), as in (Lee, 2020).",
           claim_files.claim_text(files, "c3", anonymized=True))


def test_table_claims() -> None:
    """Whole-table quotes skip grouping, and code writes their short statements."""
    header = "Table 1: Main results. Columns: Task | Steps"
    files = sentence_files({"s1": "Task A finishes in 77 steps."})
    files.candidates["p.tex#tab"] = entry(kind="table", table="Table 1",
                                          text=header + ". Rows: " + "x" * 6000)
    files.quotes["tab"] = entry(candidates=["p.tex#tab"],
                                quote=header + ". Rows: " + "x" * 6000, value=None)
    items = claim_files.proposal_items(files)
    expect("a whole-table quote is not sent to grouping; a sentence is",
           "quote:tab" not in items and "quote:s1" in items, repr(sorted(items)))
    files.classes = {"tab": {"quotes": ["tab"]}, "s1": {"quotes": ["s1"]}}
    added = claim_files.fill_register(files)
    said = {e["id"]: e["statement"] for e in files.register}
    expect("the statement of a table claim is the table's name, caption and columns",
           "tab" in added and said["tab"] == header + ".", repr(said))
    long = "Table 2: " + "y" * 6000 + " Columns: A | B. Rows: x"
    files.candidates["p.tex#long"] = entry(kind="table", table="Table 2", text=long)
    files.quotes["long"] = entry(candidates=["p.tex#long"], quote=long, value=None)
    files.classes["long"] = {"quotes": ["long"]}
    claim_files.fill_register(files)
    said = {e["id"]: e["statement"] for e in files.register}
    expect("a table statement is at most TABLE_STATEMENT_CHARS characters",
           len(said["long"]) == claim_files.TABLE_STATEMENT_CHARS == 5000, len(said["long"]))


def main() -> int:
    """Run every case and report."""
    for test in (test_rounds, test_files, test_review_cases, test_drop_reasons,
                 test_accept_classes, test_fill_register, test_table_claims,
                 test_claim_numbers_only, test_values_with_units, test_only_tables,
                 test_written_statements, test_values):
        print(test.__doc__.splitlines()[0])
        test()
    print("all passed" if not FAILED else f"{len(FAILED)} FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
