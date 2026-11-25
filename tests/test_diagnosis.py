#!/usr/bin/env python3
"""Assert that disputes work end to end as an extension: `verifier dispute` and the report.

The fixture working directory is committed, with three claims: one whose number the data
does not give (a gate flag), and two whose comparison is flagged (check flags).

Covers:
  - a dispute of two flags writes one line, and the report shows both struck out at once,
    with the verdict without them and a notice line;
  - a flag id that is not in the report, a flag nobody can dispute, a missing reason, a
    reason longer than 200 characters, and a flag that is already disputed are refused;
  - a withdrawal makes the flags count again, and an unknown or repeated one is refused;
  - a dispute leaves one event, which the auditing agent's next round names;
  - `vcheck check-diagnosis` passes a complete diagnosis, and fails a missing heading, a
    missing answer and an unknown cause, and passes a claim id or number in the last section;
  - `verifier diagnosis` lists each dispute with its state, also as JSON with `--json`,
    and `--share` writes a new
    file each time, with every section of each written diagnosis and its reason, and the
    model of the round that got each dispute; `-o` in a missing folder is refused;
  - after a round, a dispute it got and did not diagnose, and a dispute with no event,
    give a warning;
  - a failed event write is an error that says to run the command again, and running
    it again writes only the missing event;
  - AGENTS.md gets the section "Event: dispute", and the report notice gives the command;
  - the schema, the checks and the core never import the diagnosis package.

Returns 1 if any case fails, so it can gate a commit.
"""
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_core  # noqa: E402

from core import cli as vcheck  # noqa: E402
from core.project import watched_project  # noqa: E402
from core.registry import extensions  # noqa: E402
from core.report_model.axis import Axis  # noqa: E402
from core.report_output import report_store  # noqa: E402
from core.report_output.saved_report import SavedFlag  # noqa: E402
from diagnosis import diagnosis_document, dispute_store  # noqa: E402
from diagnosis import extension as diagnosis_extension  # noqa: E402
from runtime import cli as verifier_cli  # noqa: E402
from runtime import daemon
from runtime.agent import agent_launchers, inbox, rounds
from runtime.project import git as runtime_git  # noqa: E402
from runtime.project.paths import StatePaths  # noqa: E402
from utils import version  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
WHY = "The method reads runs/old/; the notes use runs/fixed/."
FAILED = []
REGISTER_FILE = watched_project.WatchedProject.REGISTER_FILE
EXTENSIONS = extensions.DEFAULT_EXTENSIONS


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


def committed_project_with_first_report(tmp: Path) -> tuple[Path, Path]:
    """A committed working directory with its checkout and a first report."""
    root = tmp / "w"
    shutil.copytree(test_core.WORKDIR, root)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    state = root / ".verifier"
    state.mkdir()
    extensions.ExtensionSet.save_names(state, EXTENSIONS)
    shutil.copytree(root / "methods", state / "methods")
    claims = [test_core.claim_entry(c) for c in ("disagrees", "good", "prior", "no-source")]
    (state / REGISTER_FILE).write_text(json.dumps({"claims": claims}))
    test_core.turn_off_paid_model_checks(state)
    paths = StatePaths(root=root)
    runtime_git.check_out(root, paths.checkout, git(root, "rev-parse", "HEAD"))
    run_and_capture_output(vcheck.main, ["--state", str(state), "--root", str(paths.checkout),
                                         "run",
                        "--commit", git(root, "rev-parse", "HEAD")])
    return root, state


def run_and_capture_output(command: object, argv: list[str]) -> tuple[int, str]:
    """Run a command's main() and return its exit code and what it printed, both streams."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = command(argv)
    return code, out.getvalue()


def latest_saved_report(state: Path) -> dict:
    """The latest saved report."""
    return json.loads((state / report_store.ReportStore.REPORTS_DIR / "latest.json").read_text())


def flag_id_of_type(state: Path, code: str, n: int = 0) -> str:
    """The id of the n-th flag of a type in the latest report."""
    return [i["id"] for i in latest_saved_report(state)["issues"] if i["code"] == code][n]


def run_verifier_dispute(root: Path, *args: str) -> tuple[int, str]:
    """Run `verifier dispute` on the working directory."""
    return run_and_capture_output(verifier_cli.main, ["dispute", str(root), *args])


def test_dispute(tmp: Path) -> None:
    """A dispute of two flags is one line, and the report strikes both out at once."""
    root, state = committed_project_with_first_report(tmp)
    gate = flag_id_of_type(state, "number-disagrees")
    check = flag_id_of_type(state, "undeclared-difference")
    code, out = run_verifier_dispute(root, "--flag", gate, "--flag", check, "--why", WHY)
    lines = (state / dispute_store.DIAGNOSES_DIR / dispute_store.DISPUTES).read_text().splitlines()
    expect("dispute: one line with both flags and the reason",
           code == 0 and len(lines) == 1 and json.loads(lines[0])["reason"] == WHY
           and [f["id"] for f in json.loads(lines[0])["flags"]] == [gate, check], out)
    expect("dispute: the command names the dispute and the new verdicts",
           "raised D-1 (2 flags)." in out and "without the disputed flags:" in out, out)
    report = latest_saved_report(state)
    marked = {i["id"]: i["disputed"] for i in report["issues"]}
    expect("dispute: the report marks both flags, and only them",
           marked[gate] == marked[check] == {"dispute_id": "D-1", "reason": WHY}
           and sum(v is not None for v in marked.values()) == 2, repr(marked))
    expect("dispute: the verdict stays, and the verdict without the flags is given",
           report["verdict"] == "REJECT" and report["verdict_without_disputed"] is not None,
           repr(report["verdict_without_disputed"]))
    text = (state / report_store.ReportStore.REPORTS_DIR / "latest.md").read_text()
    expect("dispute: the report has the notice line", "- **DisputedFlag:** 2 flags" in text, text)


def test_refused(tmp: Path) -> None:
    """Unknown, undisputable and already disputed flags, and bad reasons, are refused."""
    root, state = committed_project_with_first_report(tmp)
    gate = flag_id_of_type(state, "number-disagrees")
    caveat = flag_id_of_type(state, "no-source")
    cases = [("an unknown flag id", ["--flag", "F-000000", "--why", WHY], "no flag F-000000"),
             ("a flag nobody can dispute", ["--flag", caveat, "--why", WHY], "cannot be disputed"),
             ("no reason", ["--flag", gate], "--why"),
             ("no flag", ["--why", WHY], "--flag"),
             ("a flag named twice", ["--flag", gate, "--flag", gate, "--why", WHY], "twice")]
    for name, args, said in cases:
        code, out = run_verifier_dispute(root, *args)
        expect(f"refused: {name}", code == 1 and said in out, out)
    expect("refused: nothing is recorded",
           dispute_store.DisputeStore(state).read_ledger().disputes == ())
    run_verifier_dispute(root, "--flag", gate, "--why", WHY)
    code, out = run_verifier_dispute(root, "--flag", gate, "--why", "again")
    expect("refused: a flag that is already disputed", code == 1 and "D-1" in out, out)


def test_free_reason(tmp: Path) -> None:
    """A long reason with a line break is recorded, and the report shows it on one line."""
    root, state = committed_project_with_first_report(tmp)
    gate = flag_id_of_type(state, "number-disagrees")
    reason = "The method reads runs/old/.\nThe notes use runs/fixed/. " + "x" * 300
    code, out = run_verifier_dispute(root, "--flag", gate, "--why", reason)
    text = (state / report_store.ReportStore.REPORTS_DIR / "latest.md").read_text()
    expect("free reason: a long reason with a line break is recorded",
           code == 0 and dispute_store.DisputeStore(state).read_ledger().disputes[0].reason
           == reason.strip(), out)
    expect("free reason: the report shows the reason on one line",
           "runs/old/. The notes use runs/fixed/." in text, text[-600:])


def test_withdraw(tmp: Path) -> None:
    """A withdrawal makes the flags count again; unknown and repeated ones are refused."""
    root, state = committed_project_with_first_report(tmp)
    gate = flag_id_of_type(state, "number-disagrees")
    run_verifier_dispute(root, "--flag", gate, "--why", WHY)
    code, out = run_verifier_dispute(root, "--withdraw", "D-1")
    report = latest_saved_report(state)
    expect("withdraw: the flag counts again in the report",
           code == 0 and all(i["disputed"] is None for i in report["issues"])
           and report["verdict_without_disputed"] is None, out)
    for name, wanted in (("D-1", "withdrawn already"), ("D-9", "no dispute D-9")):
        code, out = run_verifier_dispute(root, "--withdraw", name)
        expect(f"withdraw: {name} is refused", code == 1 and wanted in out, out)
    code, out = run_verifier_dispute(root, "--flag", gate, "--why", WHY)
    expect("withdraw: the flag can be disputed again, as D-2", code == 0 and "D-2" in out, out)


def complete_diagnosis_text(flags: list[str], cause: str = "auditor-agent",
                   change: str = "Name the file of each number in the claim's source.") -> str:
    """A diagnosis of the flags, complete unless the arguments break it."""
    return "\n".join([
        "## Flags", "", *(f"- `{f}`" for f in flags), "",
        "## Why the Verifier flagged them", "", "The method read the old run.", "",
        "## Is each flag false", "", *(f"- `{f}`: yes. The data is in runs/fixed/." for f in flags),
        "", "## Cause", "", cause, "", "## What to change in the Verifier", "", change, ""])


def write_diagnosis(state: Path, dispute_id: str, text: str) -> None:
    """Write a diagnosis, as the auditing agent does."""
    (state / dispute_store.DIAGNOSES_DIR / f"{dispute_id}.md").write_text(text)


def test_event(tmp: Path) -> None:
    """A dispute leaves one event; the agent's next round names it, and its rules say what to do."""
    root, state = committed_project_with_first_report(tmp)
    gate = flag_id_of_type(state, "number-disagrees")
    run_verifier_dispute(root, "--flag", gate, "--why", WHY)
    paths = StatePaths(root=root)
    events = [inbox.AgentInbox.read_event(p) for p in inbox.AgentInbox(paths.inbox).pending_files()]
    expect("event: one dispute event", events == [{"kind": "dispute", "dispute_id": "D-1",
                                                   "flag_count": 1}], repr(events))
    prompt = daemon.build_prompt(rounds.Round(n=3, events=events, sent_at=0), root, False,
                                 extensions.ExtensionSet.of_project(state),
                                 round_timeout=daemon.ROUND_TIMEOUT_SECONDS)
    expect("event: the agent's next round names the dispute",
           "disputed 1 flag in D-1; write the diagnosis as AGENTS.md says" in prompt, prompt)
    rules = "\n".join(extensions.ExtensionSet.of_project(state).agent_instructions())
    expect("event: AGENTS.md gets the section, with the rule on the dispute files",
           "\n### dispute\n" in "\n" + rules
           and "Never edit `./diagnoses/disputes.jsonl`" in rules)
    guide = version.VERIFIER_HOME / diagnosis_extension.GUIDE_PATH
    expect("event: the section names the diagnosis guide, which exists in the Verifier",
           f"`$VERIFIER_HOME/{diagnosis_extension.GUIDE_PATH}`" in rules and guide.is_file()
           and "## 4. An example diagnosis" in guide.read_text(), str(guide))
    text = (state / report_store.ReportStore.REPORTS_DIR / "latest.md").read_text()
    expect("event: the report notice gives the command to share the diagnoses",
           "Please run `verifier diagnosis . --share`, read the file to double-check its "
           "anonymity, and send it to verifier@meno.sh." in text, text[:800])


def test_check_diagnosis(tmp: Path) -> None:
    """`vcheck check-diagnosis` passes a complete diagnosis and names each problem otherwise."""
    root, state = committed_project_with_first_report(tmp)
    gate = flag_id_of_type(state, "number-disagrees")
    other = flag_id_of_type(state, "undeclared-difference")
    run_verifier_dispute(root, "--flag", gate, "--flag", other, "--why", WHY)
    run_check = ["--state", str(state), "check-diagnosis", "D-1"]
    code, out = run_and_capture_output(vcheck.main, run_check)
    expect("check: a missing file fails", code == 1 and "Write the file first" in out, out)
    write_diagnosis(state, "D-1", complete_diagnosis_text([gate, other]))
    code, out = run_and_capture_output(vcheck.main, run_check)
    expect("check: a complete diagnosis passes", code == 0 and "PASSED" in out, out)
    cases = [("a missing heading",
              complete_diagnosis_text([gate, other]).replace("## Cause", "Cause"),
              "The headings must be"),
             ("a missing answer",
              complete_diagnosis_text([gate]).replace("## Flags", f"## Flags\n`{other}`"),
              f"one line for {other}"),
             ("an unknown cause", complete_diagnosis_text([gate, other], cause="the-model"),
              "Cause")]
    for name, text, said in cases:
        write_diagnosis(state, "D-1", text)
        code, out = run_and_capture_output(vcheck.main, run_check)
        expect(f"check: {name} fails", code == 1 and said in out, out)
    write_diagnosis(state, "D-1", complete_diagnosis_text(
        [gate, other], change="Read the claim disagrees again, with a tolerance above 0.075."))
    code, out = run_and_capture_output(vcheck.main, run_check)
    expect("check: a claim id and a number in the last section pass",
           code == 0 and "PASSED" in out, out)


def test_diagnosis_command(tmp: Path) -> None:
    """The list, one diagnosis, and the share file with every section of each diagnosis."""
    root, state = committed_project_with_first_report(tmp)
    gate = flag_id_of_type(state, "number-disagrees")
    other = flag_id_of_type(state, "undeclared-difference")
    run_verifier_dispute(root, "--flag", gate, "--why", WHY)
    run_verifier_dispute(root, "--flag", other, "--why", "The two runs use the same judge.")
    write_diagnosis(state, "D-1", complete_diagnosis_text([gate]))
    code, out = run_and_capture_output(verifier_cli.main, ["diagnosis", str(root)])
    expect("diagnosis: the list gives each dispute and its state",
           code == 0 and "D-1    1 flag   diagnosis written" in out
           and "D-2    1 flag   waiting for a diagnosis" in out, out)
    code, out = run_and_capture_output(verifier_cli.main, ["diagnosis", str(root), "D-1"])
    expect("diagnosis: one written diagnosis is printed", "## Cause" in out, out)
    code, out = run_and_capture_output(verifier_cli.main, ["diagnosis", str(root), "--json"])
    listed = json.loads(out)["disputes"] if code == 0 else []
    expect("diagnosis: --json gives each dispute, its flags, its state and the written text",
           [(d["id"], d["flags"], d["state"], d["reason"]) for d in listed]
           == [("D-1", [gate], "diagnosis written", WHY),
               ("D-2", [other], "waiting for a diagnosis", "The two runs use the same judge.")]
           and "## Cause" in listed[0]["diagnosis"] and listed[1]["diagnosis"] is None, out)
    code, out = run_and_capture_output(verifier_cli.main, ["diagnosis", str(root), "--share"])
    shared = sorted(root.glob("verifier-diagnoses-*.md"))
    text = shared[0].read_text() if shared else ""
    expect("diagnosis: --share writes one file at the project root and prints its path",
           code == 0 and len(shared) == 1 and str(shared[0]) in out, out)
    expect("diagnosis: --share names the waiting dispute, asks to check the anonymity and says "
           "not to commit the file",
           "D-2" in out and "double-check its anonymity" in out
           and "Do not commit this file." in out, out)
    expect("diagnosis: the file has the written diagnosis, the type, the answer and the cause",
           "## D-1" in text and "`number-disagrees`" in text and f"- `{gate}`: yes" in text
           and "### Cause" in text and "Name the file of each number" in text, text)
    expect("diagnosis: the file has every section, the reason and the evidence",
           all(f"### {h}" in text for h in diagnosis_document.HEADINGS)
           and f"Reason of the dispute: {WHY}" in text
           and "runs/fixed" in text and "The method read the old run" in text, text)
    expect("diagnosis: the file has no waiting dispute", "D-2" not in text, text)
    expect("diagnosis: the file names the Verifier version, and no model with no round record",
           "- Verifier version: " in text
           and "Model of the auditing agent: not recorded" in text, text)
    paths = StatePaths(root=root)
    paths.rounds.mkdir(parents=True, exist_ok=True)
    event = {"kind": "dispute", "dispute_id": "D-1", "flag_count": 1}
    for n, model, error in ((1, "first/model", None), (2, "failed/model", "timeout"),
                            (3, "second/model", None)):
        (paths.rounds / f"{n}.json").write_text(json.dumps(rounds.Round(
            n=n, events=[event] if n != 3 else [], sent_at=0, ended_at=1, error=error,
            model=model).__dict__))
    run_and_capture_output(verifier_cli.main,
                           ["diagnosis", str(root), "--share", "-o", str(tmp / "m.md")])
    text = (tmp / "m.md").read_text()
    expect("diagnosis: each diagnosis names the model of the last round that got it with no error",
           "Model of the auditing agent: first/model" in text, text)
    out_file = tmp / "shared.md"
    run_and_capture_output(verifier_cli.main,
                           ["diagnosis", str(root), "--share", "-o", str(out_file)])
    expect("diagnosis: -o gives the file to write", out_file.exists())
    code, out = run_and_capture_output(verifier_cli.main, [
        "diagnosis", str(root), "--share", "-o", str(tmp / "new-folder") + "/"])
    expect("diagnosis: -o with a folder that does not exist is refused with a message",
           code == 1 and "does not exist" in out and not (tmp / "new-folder").exists(), out)
    code, out = run_and_capture_output(verifier_cli.main, [
        "diagnosis", str(root), "--share", "-o", str(tmp / "missing" / "shared.md")])
    expect("diagnosis: -o with a file in a folder that does not exist is refused",
           code == 1 and "is not a folder" in out and not (tmp / "missing").exists(), out)


def test_after_round(tmp: Path) -> None:
    """A dispute the last round got and did not diagnose, and one with no event, are named."""
    root, state = committed_project_with_first_report(tmp)
    gate = flag_id_of_type(state, "number-disagrees")
    run_verifier_dispute(root, "--flag", gate, "--why", WHY)
    paths = StatePaths(root=root)
    paths.rounds.mkdir(parents=True, exist_ok=True)
    events = [inbox.AgentInbox.read_event(p)
              for p in inbox.AgentInbox(paths.inbox).pending_files()]
    rounds.RoundHistory(paths).save(rounds.Round(n=1, events=events, sent_at=0))
    warnings = diagnosis_extension.DiagnosisExtension().after_round_warnings(paths.checkout, state)
    expect("after round: the dispute the round got and did not diagnose",
           warnings == ["round 1 got dispute D-1 and wrote no complete diagnosis"],
           repr(warnings))
    write_diagnosis(state, "D-1", complete_diagnosis_text([gate]))
    with open(state / dispute_store.DIAGNOSES_DIR / dispute_store.DISPUTES, "a") as fh:
        fh.write(json.dumps({"id": "D-2", "commit": "x", "reason": "w", "at": "t",
                             "flags": []}) + "\n")
    warnings = diagnosis_extension.DiagnosisExtension().after_round_warnings(paths.checkout, state)
    expect("after round: a dispute line with no event, and no warning for D-1 now",
           warnings == ["dispute D-2 has no event: it was written without `verifier dispute`, "
                        "or that command could not write its event; running the same command "
                        "again writes it"],
           repr(warnings))
    expect("after round: the share format reads the answers of the diagnosis",
           diagnosis_document.ANSWER_LINE.findall(complete_diagnosis_text([gate]))[0]
           == (gate, "yes"))


def test_event_write_fails(tmp: Path) -> None:
    """A failed event write is an error that says what to do, and running again repairs it."""
    root, state = committed_project_with_first_report(tmp)
    gate = flag_id_of_type(state, "number-disagrees")
    folder = StatePaths(root=root).inbox
    if os.geteuid() == 0:
        print("  skipped: the root user can write to a read-only folder, so the write cannot fail")
        return
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o500)
    try:
        code, out = run_verifier_dispute(root, "--flag", gate, "--why", WHY)
    finally:
        folder.chmod(0o700)
    expect("event fails: the command fails and says to run it again",
           code == 1 and "D-1 is recorded, but the auditing agent could not be told" in out
           and "Run the same command again" in out, out)
    code, out = run_verifier_dispute(root, "--flag", gate, "--why", WHY)
    events = [inbox.AgentInbox.read_event(p) for p in inbox.AgentInbox(folder).pending_files()]
    rows = dispute_store.DisputeStore(state).read_ledger().disputes
    expect("event fails: running again writes only the event of D-1",
           code == 0 and "D-1 was recorded before" in out and len(rows) == 1
           and events == [{"kind": "dispute", "dispute_id": "D-1", "flag_count": 1}],
           out + repr(events))
    code, out = run_verifier_dispute(root, "--flag", gate, "--why", WHY)
    expect("event fails: a third run is refused, as the flag is disputed and the agent told",
           code == 1 and "already disputed in D-1" in out, out)
    other = flag_id_of_type(state, "undeclared-difference")
    folder.chmod(0o500)
    try:
        run_verifier_dispute(root, "--flag", other, "--why", WHY)
    finally:
        folder.chmod(0o700)
    for name, args in (("another reason", ["--why", "another reason"]),
                       ("an empty reason", ["--why", ""])):
        code, out = run_verifier_dispute(root, "--flag", other, *args)
        expect(f"event fails: a run with {name} does not reuse D-2, and is refused",
               code == 1 and "D-2 was recorded before" not in out, out)


def test_review_fixes(tmp: Path) -> None:
    """A crashed refresh is an error, ids stay unique at once, and old round files are read."""
    root, state = committed_project_with_first_report(tmp)
    (state / dispute_store.DIAGNOSES_DIR).mkdir(parents=True, exist_ok=True)
    with open(state / dispute_store.DIAGNOSES_DIR / dispute_store.DISPUTES, "a") as fh:
        fh.write("{not json\n")
    checkout = StatePaths(root=root).checkout
    code, out = run_and_capture_output(
        lambda argv: agent_launchers.ReportRefresher(checkout, state)
        .refresh_report_and_print_verdicts(), [])
    expect("refresh: a vcheck that crashes is reported as an error, not as a new report",
           "the report could not be updated" in out and "The report is updated" not in out, out)
    (state / dispute_store.DIAGNOSES_DIR / dispute_store.DISPUTES).unlink()
    st = dispute_store.DisputeStore(state)
    flags = [SavedFlag(flag_id=f"F-{n}", code="c", axis=Axis.STRENGTH, subject=f"s{n}",
                               detail="d", facts=()) for n in range(8)]
    threads = [threading.Thread(target=st.raise_dispute, args=([f], WHY, "x")) for f in flags]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [d.dispute_id for d in st.read_ledger().disputes]
    expect("store: disputes raised at once get different ids",
           sorted(ids) == sorted(f"D-{n}" for n in range(1, 9)), repr(ids))
    expect("store: the lock is outside the git history of .verifier/",
           (state / dispute_store.LOCK_FILE).exists()
           and not list((state / dispute_store.DIAGNOSES_DIR).glob("*.lock")))
    paths = StatePaths(root=root)
    paths.rounds.mkdir(parents=True, exist_ok=True)
    # a round file from before the daemon recorded errors, cost and model
    (paths.rounds / "1.json").write_text(json.dumps({
        "n": 1, "events": [], "sent_at": 0, "seen_busy": False, "aborted": False,
        "ended_at": 1, "verdict": None}))
    history = rounds.RoundHistory(paths)
    (paths.rounds / "2.json").write_text(json.dumps({**rounds.Round(
        n=2, events=[], sent_at=0, ended_at=1, aborted=True).__dict__}))
    (paths.rounds / "3.json").write_text(json.dumps(rounds.Round(n=3, events=[],
                                                                 sent_at=2).__dict__))
    (paths.rounds / "0.json").write_text(json.dumps({"n": 0, "an unknown key": True}))
    last_two = history.last_rounds(2)
    expect("rounds: the last two rounds are read without the bad old round file",
           [r.n for r in last_two] == [2, 3] and last_two[0].aborted, repr(last_two))
    (paths.rounds / "0.json").unlink()
    (paths.rounds / "2.json").unlink()
    (paths.rounds / "3.json").unlink()
    expect("share: a round file with no error key is read",
           history.model_of_last_clean_round_for_each_event_value("dispute", "dispute_id") == {}
           and history.last_round() is not None and history.last_round().error is None)


def test_imports(tmp: Path) -> None:
    """The schema, the checks and the core never import the diagnosis package."""
    importers = []
    for folder in ("schema", "checks", "core"):
        for path in sorted((REPO / folder).rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                if any(n == "diagnosis" or n.startswith("diagnosis.") for n in names):
                    importers.append(str(path.relative_to(REPO)))
    expect("the schema, the checks and the core never import the diagnosis package",
           not importers, repr(importers))


def main() -> int:
    """Run every group of cases, each in a fresh temporary directory."""
    for group in (test_dispute, test_refused, test_free_reason, test_withdraw, test_event,
                  test_check_diagnosis,
                  test_diagnosis_command, test_after_round, test_event_write_fails,
                  test_review_fixes, test_imports):
        print(group.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            group(Path(tmp))
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll diagnosis cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
