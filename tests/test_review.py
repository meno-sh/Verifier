#!/usr/bin/env python3
"""Assert that human review works end to end as an extension, for the file-regenerated kind.

The fixture working directory has two claims on one data file. Editing the file's
"base" record moves one claim's number (a mean over every record) and leaves the
other's alone (a Brier score over the "trained" records only). Both use the
fixture's own methods, in `fixtures/workdir/methods/`.

Covers:
  - with no review, a changed pinned file rejects both claims;
  - asking raises one request for the file and proposes its new checksum, and asking
    again returns the same request;
  - while waiting: the unmoved claim is recomputed and disclosed, the moved one is
    rejected by recomputation, and the report says what is waiting;
  - decline: the unmoved claim is disclosed with the note, the moved one stays rejected;
  - confirm: the pins move to the new file and nothing is disclosed;
  - a further change supersedes the request, and the claims are rejected again;
  - withdrawing removes the proposal; the interactive review, `verifier review --json`,
    the decision event for the agent, and the checks on a kind class, such as two options
    with the same first letter;
  - a claim resting on two changed files names both waiting requests;
  - asks and answers made at the same time record one request and one decision;
  - a change of the claim register that failed is applied again by the next review
    command, and only once, and `verifier status` changes no file;
  - bad facts from the agent print an error, and a bug in a kind is not caught;
  - the review check computes its claims again for a new register at a freed address;
  - the agent's AGENTS.md gets the "Asking a person" section only with the extension;
  - the schema, the checks and the core never import the review package.

Returns 1 if any case fails, so it can gate a commit.
"""
import argparse
import ast
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import cli as vcheck  # noqa: E402
from core.analysis import verdict_policy  # noqa: E402
from core.project import watched_project
from core.registry import checks, extensions
from review import extension as review_extension  # noqa: E402
from review import store  # noqa: E402
from review import types as rv  # noqa: E402
from review.channels import cli as review_cli  # noqa: E402
from review.claim_checks import ReviewClaimCheck  # noqa: E402
from review.commands import RaiseReviewRequestCommand, ReviewCommand, review_recorder  # noqa: E402
from review.kinds import REVIEW_KINDS, ReviewKindRegistry  # noqa: E402
from review.kinds.base import ReviewKind  # noqa: E402
from review.kinds.file_regenerated import FileRegeneratedReviewKind  # noqa: E402
from review.states import ReviewStateResolver  # noqa: E402
from runtime import cli as verifier_cli  # noqa: E402
from runtime import daemon  # noqa: E402
from runtime.agent import inbox, rounds  # noqa: E402
from runtime.project import git as runtime_git  # noqa: E402
from runtime.project.paths import StatePaths  # noqa: E402
from schema import provenance, recompute, verdict  # noqa: E402
from schema.primitives import Flagged, Passed  # noqa: E402
from utils import claim_files  # noqa: E402

VERDICT_POLICY = verdict_policy.VerdictPolicy()

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DATA = "runs/main/results.json"
CHECK = "review:file-regenerated"
FAILED = []

CLAIMS = [
    {"id": "trained-brier", "value": 0.075,
     "source": {"kind": "artifact", "path": DATA, "method": "trained_brier_post"}},
    {"id": "mean-post", "value": 0.54,
     "source": {"kind": "artifact", "path": DATA, "method": "mean_post"}},
]
REGISTER_FILE = watched_project.WatchedProject.REGISTER_FILE


def analyse_project(root: Path, state: Path) -> object:
    """The findings of the watched project in `root`, with the extensions of the environment."""
    return watched_project.WatchedProject.load(root, state).analyse()

def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(name)
        if detail:
            print(f"        {detail}")


def git(root: Path, *args: str) -> str:
    """Run git with a fixed identity."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "worker", "GIT_AUTHOR_EMAIL": "w@w",
           "GIT_COMMITTER_NAME": "worker", "GIT_COMMITTER_EMAIL": "w@w"}
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def setup(tmp: Path) -> tuple[Path, Path]:
    """A committed working directory whose two claims are registered and pinned."""
    root = tmp / "w"
    shutil.copytree(HERE / "fixtures" / "workdir", root)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    state = root / ".verifier"
    state.mkdir()
    extensions.ExtensionSet.save_names(state, ("review.extension:ReviewExtension",))
    shutil.copytree(root / "methods", state / "methods")
    claims = [dict(c, source=dict(c["source"])) for c in CLAIMS]
    for c in claims:
        claim_files.pin(c, root)
    (state / REGISTER_FILE).write_text(json.dumps({"claims": claims}))
    return root, state


def edit_base_record(root: Path, post: float, message: str) -> None:
    """Change the "base" record's posterior and commit it."""
    path = root / DATA
    data = json.loads(path.read_text())
    for r in data["records"]:
        if r["condition"] == "base":
            r["post"] = post
    path.write_text(json.dumps(data))
    git(root, "commit", "-qam", message)


def by_id(tree) -> dict:
    """The claims of a run by id."""
    return {c.claim.id: c for c in tree.claims}


def ask(state: Path) -> int:
    """The agent's request for the data file."""
    return vcheck.main(["--state", str(state), "ask", "file-regenerated", f"file={DATA}",
                        "--reason", "commit says rerun; record base looks hand-edited"])


def codes(result) -> list[str]:
    """The problem codes in one check result."""
    return [p.kind.code for p in result.problems] if isinstance(result, Flagged) else []


def state_of_request(root: Path, state: Path, request_id: str) -> rv.ReviewState:
    """The state of one request now."""
    ledger = store.ReviewStore(state).read_ledger()
    return ReviewStateResolver(REVIEW_KINDS, root).state_of_request(
        ledger.request_with_id(request_id), ledger)


def register(state: Path) -> dict:
    """The saved claim register, as JSON."""
    return json.loads((state / REGISTER_FILE).read_text())


def test_waiting_and_decline(tmp: Path) -> None:
    """No review rejects both; waiting and declined disclose the unmoved claim only."""
    root, state = setup(tmp)
    edit_base_record(root, 0.9, "rerun eval")
    c = by_id(analyse_project(root, state))
    expect("no review: both claims are rejected as resting on a changed file",
           all(isinstance(c[i].provenance, provenance.Changed) for i in c))

    expect("ask: raises a request", ask(state) == 0)
    expect("ask: asking again returns the same request", ask(state) == 0
           and len(store.ReviewStore(state).read_ledger().requests) == 1)
    expect("ask: the file's new checksum is proposed on both claims",
           all("proposed_sha256" in e["source"] for e in register(state)["claims"]))

    tree = analyse_project(root, state)
    c = by_id(tree)
    trained, mean = c["trained-brier"], c["mean-post"]
    expect("waiting: the unmoved claim is recomputed, and disclosed as waiting",
           isinstance(trained.provenance, provenance.ProvisionallyIntact)
           and isinstance(trained.recompute, recompute.Matches)
           and codes(trained.checks[CHECK]) == ["regenerated-file-unconfirmed"],
           f"{trained.provenance!r} {trained.checks.get(CHECK)!r}")
    expect("waiting: the moved claim is rejected by recomputation",
           isinstance(mean.recompute, recompute.Disagrees)
           and mean.gate_failed)
    vcheck.main(["--state", str(state), "run", "--commit", git(root, "rev-parse", "HEAD")])
    text = (state / "reports" / "latest.md").read_text()
    expect("waiting: the report opens with what is waiting and what each answer does",
           "Waiting for you:** 1 review(s)" in text and "- R-1 Was runs/main/results.json"
           in text and "- decline claims whose recomputed number moved" in text, text[:600])

    recorder = review_recorder(root, state)
    for option, name in (("maybe", "an answer the kind does not allow"),):
        try:
            recorder.record_decision("R-1", option, "", "cli")
            expect(f"decide: {name} is refused", False)
        except store.ReviewError:
            expect(f"decide: {name} is refused", True)
    recorder.record_decision("R-1", "decline", "base record edited by hand", "cli")
    try:
        recorder.record_decision("R-1", "confirm", "", "cli")
        expect("decide: a second answer is refused", False)
    except store.ReviewError:
        expect("decide: a second answer is refused", True)
    c = by_id(analyse_project(root, state))
    result = c["trained-brier"].checks[CHECK]
    expect("decline: the unmoved claim is disclosed with the note",
           codes(result) == ["regenerated-file-declined"]
           and "edited by hand" in result.problems[0].detail, repr(result))
    expect("decline: the moved claim stays rejected",
           isinstance(c["mean-post"].recompute, recompute.Disagrees))
    expect("decline: the pins are unchanged and the proposal stays",
           all("proposed_sha256" in e["source"] for e in register(state)["claims"])
           and isinstance(state_of_request(root, state, "R-1"), rv.ReviewDeclined))


def test_confirm_and_supersede(tmp: Path) -> None:
    """Confirm moves the pins; a further change supersedes the request."""
    root, state = setup(tmp)
    edit_base_record(root, 0.5000001, "rerun eval")       # no number moves past tolerance
    ask(state)
    review_recorder(root, state).record_decision("R-1", "confirm", "", "cli")
    tree = analyse_project(root, state)
    c = by_id(tree)
    expect("confirm: both claims are intact, with nothing disclosed",
           all(isinstance(c[i].provenance, provenance.Intact) for i in c)
           and all(isinstance(c[i].checks[CHECK], Passed) for i in c)
           # optional fields unset
           and VERDICT_POLICY.verdict_for(tree) is verdict.Verdict.PASS_WITH_CAVEATS,
           VERDICT_POLICY.verdict_for(tree).value)
    claims = register(state)["claims"]
    new_sha256 = store.ReviewStore(state).read_ledger().request_with_id("R-1").facts["new_sha256"]
    expect("confirm: the pins hold the new checksum, and the proposal is gone",
           all(e["source"]["sha256"]["results.json"] == new_sha256
               and "proposed_sha256" not in e["source"] for e in claims))

    edit_base_record(root, 0.3, "another change")
    c = by_id(analyse_project(root, state))
    expect("supersede: a further change supersedes the request",
           isinstance(state_of_request(root, state, "R-1"), rv.ReviewSuperseded))
    expect("supersede: the claims are rejected as resting on a changed file",
           all(isinstance(c[i].provenance, provenance.Changed) for i in c))


def test_broken_project_file(tmp: Path) -> None:
    """A broken method file stops a pin change with a one-line error, not a traceback."""
    root, state = setup(tmp)
    edit_base_record(root, 0.5000001, "rerun eval")
    ask(state)
    broken = state / "methods" / "broken.py"
    broken.write_text("def (\n")
    try:
        review_recorder(root, state).record_decision("R-1", "confirm", "", "cli")
        error = ""
    except store.ReviewError as e:
        error = str(e)
    expect("broken file: the error names the file, and nothing is recorded or changed",
           "cannot be changed" in error and "broken.py" in error
           and not store.ReviewStore(state).read_ledger().decisions
           and all("proposed_sha256" in e["source"] for e in register(state)["claims"]),
           error)
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        code = ReviewCommand().execute(argparse.Namespace(root=root, state=state, json=False))
    expect("broken file: `verifier review` prints the error on one line and exits 1",
           code == 1 and err.getvalue().startswith("error: the checksums in")
           and "Traceback" not in err.getvalue(), err.getvalue())
    broken.unlink()
    review_recorder(root, state).record_decision("R-1", "confirm", "", "cli")
    expect("broken file: once the file is fixed, the same answer moves the pins",
           all("proposed_sha256" not in e["source"] for e in register(state)["claims"]))


def test_withdraw_and_commands(tmp: Path) -> None:
    """Withdrawing, the interactive review, and the decision event for the agent."""
    root, state = setup(tmp)
    edit_base_record(root, 0.9, "rerun eval")
    ask(state)
    expect("withdraw: the agent can withdraw its waiting request",
           vcheck.main(["--state", str(state), "withdraw", "R-1", "--reason", "restored"]) == 0)
    c = by_id(analyse_project(root, state))
    expect("withdraw: the proposal is removed and the claims fall back to their pins",
           isinstance(c["trained-brier"].provenance, provenance.Changed)
           and not any("proposed_sha256" in e["source"] for e in register(state)["claims"]))

    ask(state)
    answers = iter(["x", "d", "not a rerun"])
    said = []
    made = review_cli.InteractiveReviewSession(
        review_recorder(root, state), root,
        ask=lambda _: next(answers), say=said.append).review_waiting_requests()
    expect("review: shows how the file changed and records the answer",
           [d.option for d in made] == ["decline"]
           and any("line(s) differ" in s for s in said), "\n".join(said)[:600])

    edit_base_record(root, 0.95, "rerun again")
    ask(state)
    paths = StatePaths(root=root)
    runtime_git.check_out(root, paths.checkout, git(root, "rev-parse", "HEAD"))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = verifier_cli.main(["review", str(root), "--json"])
    listed = json.loads(out.getvalue())["requests"] if code == 0 else []
    expect("review --json: gives the waiting request, its evidence and its options, and asks "
           "nothing", [(q["id"], sorted(q["options"])) for q in listed]
           == [("R-3", ["confirm", "decline"])] and "line(s) differ" in listed[0]["evidence"],
           out.getvalue()[:600])
    code = verifier_cli.main(["decide", str(root), "R-3", "decline", "--note", "nope"])
    events = [inbox.AgentInbox.read_event(p) for p in inbox.AgentInbox(paths.inbox).pending_files()]
    expect("decide: the agent gets a decision event and the report is refreshed",
           code == 0 and {"kind": "decision", "request": "R-3", "option": "decline",
                          "note": "nope"} in events
           and (state / "reports" / "latest.md").exists(), repr(events))
    prompt = daemon.build_prompt(rounds.Round(n=9, events=events, sent_at=0), root, False,
                                 extensions.ExtensionSet.of_project(state),
                                 round_timeout=daemon.ROUND_TIMEOUT_SECONDS)
    expect("decide: the agent's next round names the answer",
           "a person answered review R-3 with decline: nope" in prompt, prompt)


def test_two_files(tmp: Path) -> None:
    """A claim on two regenerated files is disclosed once per waiting request."""
    root, state = setup(tmp)
    seeds = root / "runs" / "seeds"
    seeds.mkdir()
    for name in ("s0.json", "s1.json"):
        shutil.copy(root / DATA, seeds / name)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "two seeds")
    raw = register(state)
    raw["claims"].append({"id": "pooled", "value": 0.54, "source": {
        "kind": "artifact", "path": "runs/seeds/*.json", "method": "mean_post"}})
    claim_files.pin(raw["claims"][-1], root)
    (state / REGISTER_FILE).write_text(json.dumps(raw))
    for name in ("s0.json", "s1.json"):
        path = seeds / name
        path.write_text(path.read_text().replace('"trained"', '"trained" '))
        assert vcheck.main(["--state", str(state), "ask", "file-regenerated",
                            f"file=runs/seeds/{name}", "--reason", "rerun"]) == 0
    got = by_id(analyse_project(root, state))["pooled"].checks[CHECK]
    details = [p.detail for p in got.problems] if isinstance(got, Flagged) else []
    expect("two files: the claim names both waiting requests",
           len(details) == 2 and "(R-1)" in details[0] and "(R-2)" in details[1], repr(got))


def run_at_once(function: object, count: int) -> list[object]:
    """Run `function` in `count` threads at once, and give what each call returned or raised."""
    out: list[object] = []
    start = threading.Barrier(count)

    def call() -> None:
        start.wait()
        try:
            out.append(function())
        except store.ReviewError as e:
            out.append(e)
    threads = [threading.Thread(target=call) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def test_changes_at_once(tmp: Path) -> None:
    """Requests and decisions recorded at the same time keep one request and one decision."""
    root, state = setup(tmp)
    edit_base_record(root, 0.9, "rerun eval")
    project = watched_project.WatchedProject.load(root, state)
    claims = project.claim_register(project.check_registry()).claims
    kind = REVIEW_KINDS.kind_named("file-regenerated")
    facts, question = kind.build_request_facts(root, claims, {"file": DATA})
    run_at_once(lambda: review_recorder(root, state).raise_request(
        kind.name, facts, question, "rerun", "agent", "c"), 8)
    ids = [r.id for r in store.ReviewStore(state).read_ledger().requests]
    expect("at once: eight asks with the same facts record one request", ids == ["R-1"],
           repr(ids))
    results = run_at_once(lambda: review_recorder(root, state).record_decision(
        "R-1", "decline", "", "cli"), 8)
    decisions = store.ReviewStore(state).read_ledger().decisions
    expect("at once: eight answers to one request record one decision, and refuse seven",
           len(decisions) == 1
           and sum(isinstance(r, store.ReviewError) for r in results) == 7, repr(results))
    expect("at once: the lock is outside the git history of .verifier/",
           (state / store.LOCK_FILE).exists()
           and not list((state / store.REVIEWS_DIR).glob("*.lock")))


class KindWhoseRegisterChangeFailsOnce(FileRegeneratedReviewKind):
    """The file-regenerated kind, whose next change of the claim register fails once.

    It stands for a real failure after the store recorded the change, such as a full disk.
    """
    hook_to_fail: str | None = None

    def fail_once(self, hook: str) -> None:
        """Make the next call of this hook fail."""
        self.hook_to_fail = hook

    def stop_if_told(self, hook: str) -> None:
        """Raise OSError once, when `hook` is the hook given to fail_once."""
        if self.hook_to_fail == hook:
            self.hook_to_fail = None
            raise OSError("the claim register could not be written")

    def after_request_raised(self, request: rv.ReviewRequest, root: Path,
                             state_dir: Path) -> None:
        """Fail once, or propose the checksum."""
        self.stop_if_told("after_request_raised")
        super().after_request_raised(request, root, state_dir)

    def after_decision_recorded(self, request: rv.ReviewRequest, decision: rv.ReviewDecision,
                                root: Path, state_dir: Path) -> None:
        """Fail once, or move the pins."""
        self.stop_if_told("after_decision_recorded")
        super().after_decision_recorded(request, decision, root, state_dir)

    def after_request_withdrawn(self, request: rv.ReviewRequest, root: Path,
                                state_dir: Path) -> None:
        """Fail once, or remove the proposal."""
        self.stop_if_told("after_request_withdrawn")
        super().after_request_withdrawn(request, root, state_dir)


def os_error_of(function: object) -> OSError | None:
    """The OSError that `function` raises, or None."""
    try:
        function()
    except OSError as e:
        return e
    return None


def run_vcheck_command(command: object, argv_namespace: argparse.Namespace, root: Path,
                       state: Path) -> int:
    """Run one review command object, with its printed text hidden."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        argv_namespace.root, argv_namespace.state = root, state
        return command.execute(argv_namespace)


def test_failed_kind_changes_are_repaired(tmp: Path) -> None:
    """A change of the claim register that fails is applied again by the next review command."""
    kind = KindWhoseRegisterChangeFailsOnce()
    kinds = ReviewKindRegistry([kind])
    ask_args = argparse.Namespace(kind=kind.name, facts=[f"file={DATA}"], reason="rerun",
                                  commit="c")
    root, state = setup(tmp / "raise-and-decide")
    edit_base_record(root, 0.9, "rerun eval")
    kind.fail_once("after_request_raised")
    error = os_error_of(lambda: run_vcheck_command(RaiseReviewRequestCommand(kinds), ask_args,
                                                   root, state))
    expect("repair: the failed proposal leaves the request recorded and no proposal",
           error is not None and len(store.ReviewStore(state).read_ledger().requests) == 1
           and not any("proposed_sha256" in e["source"] for e in register(state)["claims"]),
           repr(error))
    before = (state / REGISTER_FILE).read_text()
    review_extension.ReviewExtension().status_lines(root, state)
    expect("repair: `verifier status` reads only, so it changes no file",
           (state / REGISTER_FILE).read_text() == before)
    expect("repair: asking again gives the open request and proposes the checksum",
           run_vcheck_command(RaiseReviewRequestCommand(kinds), ask_args, root, state) == 0
           and len(store.ReviewStore(state).read_ledger().requests) == 1
           and all("proposed_sha256" in e["source"] for e in register(state)["claims"]))
    proposed = (state / REGISTER_FILE).read_text()
    expect("repair: asking a third time does not write the claim register again",
           run_vcheck_command(RaiseReviewRequestCommand(kinds), ask_args, root, state) == 0
           and (state / REGISTER_FILE).read_text() == proposed)
    kind.fail_once("after_decision_recorded")
    error = os_error_of(lambda: review_recorder(root, state, kinds).record_decision(
        "R-1", "confirm", "", "cli"))
    expect("repair: the failed confirm records the decision and leaves the pins",
           error is not None and len(store.ReviewStore(state).read_ledger().decisions) == 1
           and all("proposed_sha256" in e["source"] for e in register(state)["claims"]),
           repr(error))
    session = review_cli.InteractiveReviewSession(
        review_recorder(root, state, kinds), root,
        ask=lambda _: "", say=lambda _: None)
    session.review_waiting_requests()
    new_sha256 = store.ReviewStore(state).read_ledger().request_with_id("R-1").facts[
        "new_sha256"]
    expect("repair: `verifier review` moves the pins of the confirmed request",
           all(e["source"]["sha256"]["results.json"] == new_sha256
               and "proposed_sha256" not in e["source"] for e in register(state)["claims"]),
           repr(register(state)["claims"]))

    root, state = setup(tmp / "withdraw")
    edit_base_record(root, 0.9, "rerun eval")
    run_vcheck_command(RaiseReviewRequestCommand(kinds), ask_args, root, state)
    kind.fail_once("after_request_withdrawn")
    error = os_error_of(lambda: review_recorder(root, state, kinds).withdraw_request("R-1", "x"))
    expect("repair: the failed withdrawal leaves the proposal",
           error is not None
           and all("proposed_sha256" in e["source"] for e in register(state)["claims"]),
           repr(error))
    run_vcheck_command(RaiseReviewRequestCommand(kinds), ask_args, root, state)
    expect("repair: the next `vcheck ask` removes the proposal of the withdrawn request, and "
           "raises a new one, which proposes it again",
           [r.id for r in store.ReviewStore(state).read_ledger().requests] == ["R-1", "R-2"]
           and all("proposed_sha256" in e["source"] for e in register(state)["claims"]))
    review_recorder(root, state, kinds).withdraw_request("R-2", "restored")
    expect("repair: withdrawing the new request removes the proposal",
           not any("proposed_sha256" in e["source"] for e in register(state)["claims"]))


class KindWithABug(FileRegeneratedReviewKind):
    """A kind whose code has a bug that raises ValueError."""

    def build_request_facts(self, root: Path, claims: object,
                            given: object) -> tuple[dict, str]:
        """Fail as a bug in the kind would."""
        raise ValueError("a bug in the kind")


def test_ask_errors(tmp: Path) -> None:
    """Bad facts from the agent are an error message; a bug in a kind is a traceback."""
    root, state = setup(tmp)
    edit_base_record(root, 0.9, "rerun eval")
    printed = io.StringIO()
    with contextlib.redirect_stderr(printed):
        code = vcheck.main(["--state", str(state), "ask", "file-regenerated", "--reason", "r"])
    expect("ask errors: a missing file is an error message for the agent",
           code == 1 and "give the file: file=PATH" in printed.getvalue(), printed.getvalue())
    command = RaiseReviewRequestCommand(ReviewKindRegistry([KindWithABug()]))
    args = argparse.Namespace(kind="file-regenerated", facts=[f"file={DATA}"], reason="r",
                              commit="c", root=root, state=state)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            command.execute(args)
        expect("ask errors: a ValueError of a kind is not shown as a wrong input", False)
    except ValueError as e:
        expect("ask errors: a ValueError of a kind is not shown as a wrong input",
               not isinstance(e, store.ReviewError), repr(e))


def test_new_register_at_old_address(tmp: Path) -> None:
    """The review check computes its claims again for each new register object.

    CPython often gives a new object the address of one just freed. The check must not
    take the new register for the old one, so it must not key its claims by `id()`.
    """
    root, state = setup(tmp)
    edit_base_record(root, 0.9, "rerun eval")
    ask(state)
    project = watched_project.WatchedProject.load(root, state)
    claims = {c.id: c for c in project.claim_register(project.check_registry()).claims}
    kind = REVIEW_KINDS.kind_named("file-regenerated")
    ledger = store.ReviewStore(state).read_ledger()
    states = ReviewStateResolver(REVIEW_KINDS, root)
    check = ReviewClaimCheck(kind, [(r, states.state_of_request(r, ledger))
                                    for r in ledger.requests], root)
    claim = claims["trained-brier"]
    stale = []
    for _ in range(50):
        check.check(claim, None, checks.ClaimContext(register={}))
        result = check.check(claim, None, checks.ClaimContext(register=claims))
        if not isinstance(result, Flagged):
            stale.append(result)
    expect("new register: the claims are computed for the register given each time",
           not stale, f"{len(stale)} of 50 calls used the claims of the freed register")


def test_agent_instructions(tmp: Path) -> None:
    """The review section reaches AGENTS.md through the extension, and only then."""
    root, _state = setup(tmp)
    daemon.Daemon(root).prepare()
    rules = (root / ".verifier" / "AGENTS.md").read_text()
    template = (REPO / "runtime" / "templates" / "AGENTS.md").read_text()
    expect("agent rules: the extension adds the review section and its commands",
           "### commit: review requests" in rules
           and "### answer to a review request" in rules and "vcheck ask file-regenerated" in rules
           and "vcheck ask" not in template and "file-regenerated" not in template)
    extensions.ExtensionSet.save_names(root / ".verifier", ())
    daemon.Daemon(root).prepare()
    expect("agent rules: without the extension there is no review section",
           "review request" not in (root / ".verifier" / "AGENTS.md").read_text())


def test_registry(tmp: Path) -> None:
    """A malformed kind class is refused, and nothing below the extension imports it."""
    concrete_methods = {name: (lambda *_args: None) for name in ReviewKind.__abstractmethods__}
    try:
        type("BrokenReviewKind", (ReviewKind,), {"name": "broken", **concrete_methods})
        expect("registry: a kind missing required names is refused", False)
    except TypeError:
        expect("registry: a kind missing required names is refused", True)
    attributes = {name: getattr(FileRegeneratedReviewKind, name) for name in (
        "title", "problem_kinds", "raised_by", "agent_guide")}
    for options, name in (({"accept": "positive", "abandon": "negative"},
                           "two options with the same first letter"),
                          ({"confirm": "positive", "skip-it": "negative"},
                           "an option with the first letter of skip"),
                          ({"Confirm": "positive", "decline": "negative"},
                           "an option with a capital first letter"),
                          ({"confirm": "positive", "Stop": "negative"},
                           "an option with the capital first letter of skip")):
        try:
            type("SameLetterReviewKind", (FileRegeneratedReviewKind,),
                 {**attributes, "name": "same-letter", "options": options,
                  "option_effects": dict.fromkeys(options, "effect")})
            expect(f"registry: refused: {name}", False)
        except TypeError:
            expect(f"registry: refused: {name}", True)
    importers = []
    for folder in ("schema", "checks", "core"):
        for path in sorted((REPO / folder).rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                if any(n == "review" or n.startswith("review.") for n in names):
                    importers.append(str(path.relative_to(REPO)))
    expect("the schema, the checks and the core never import the review package",
           not importers, repr(importers))


def main() -> int:
    """Run every group of cases, each in a fresh temporary directory."""
    for group in (test_waiting_and_decline, test_confirm_and_supersede, test_broken_project_file,
                  test_withdraw_and_commands, test_two_files, test_changes_at_once,
                  test_failed_kind_changes_are_repaired, test_ask_errors,
                  test_new_register_at_old_address,
                  test_agent_instructions,
                  test_registry):
        print(group.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            group(Path(tmp))
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll review cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
