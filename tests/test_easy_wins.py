#!/usr/bin/env python3
"""Check the small fixes of the easy-wins PR, one case or more for each issue it closes.

The cases, by issue number of the v8 issue list:
  - 12: an error in the daemon's loop still stops the OpenCode server;
  - 26: a `uv sync` timeout with stderr as bytes is logged, not a crash;
  - 30: a pin change keeps a decimal as printed (0.120 stays 0.120);
  - 31: the same row in two tables is in two candidates;
  - 33: a tabular* with its width on the next line is read;
  - 44: a checkout deleted by hand is made again;
  - 54: a job whose pid is another user's process has ended;
  - 55: git runs in the C locale where its messages are read;
  - 59: a changed cap of score calls keeps the saved readings;
  - 60: the red team prompt keeps every digit of a value;
  - 64: a repeated proposal is kept and scored once;
  - 20, 68, 74: the judge's prompts and version;
  - 72: a red team model that is not URL#MODEL is refused;
  - 81: a wrapfigure caption is a candidate;
  - 82: a \\makecell header keeps its column labels in place.
Returns 1 if any case fails.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_uncertainty  # noqa: E402

from checks.uncertainty import quantifier, uncertainty  # noqa: E402
from checks.uncertainty.red_team import prompt, prompted  # noqa: E402
from core.gate import artifact_pins  # noqa: E402
from runtime import daemon  # noqa: E402
from runtime.agent import jobs  # noqa: E402
from runtime.project import git  # noqa: E402
from schema import inputs  # noqa: E402
from schema.claim import claim_register_parser  # noqa: E402
from utils import alerts, extract, models, structure  # noqa: E402

# the made-up model hosts of these tests need no key
models.KEYS.update(dict.fromkeys(("gpu", "gpu-host", "127.0.0.1", "10.0.0.5", "10.0.0.9",
                                  "example.invalid"), None))

FAILED: list[str] = []
M = "https://openrouter.ai/api/v1/chat/completions#m"     # a model name; no call is made


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def candidates(tex: str) -> list[dict]:
    """The candidates of one LaTeX document."""
    with tempfile.TemporaryDirectory() as d:
        Path(d, "main.tex").write_text(tex)
        return extract.candidates(extract.read_latex(str(Path(d, "main.tex"))))


def document(body: str) -> str:
    """A LaTeX document with `body` in it."""
    return "\\documentclass{article}\n\\begin{document}\n" + body + "\n\\end{document}\n"


class FakeServer:
    """A server process that records whether it was stopped."""

    def __init__(self) -> None:
        self.stopped = False

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.stopped = True


class FailingDaemon(daemon.Daemon):
    """A daemon whose server is a fake and whose first tick raises a non-OpenCode error."""

    def prepare(self) -> None:
        pass

    def ensure_server(self) -> None:
        if self.server is None:
            self.server = FakeServer()

    def tick(self, now: float | None = None) -> None:
        raise ValueError("a fatal error in a round")


def test_daemon(tmp: Path) -> None:
    """Issues 12 and 26."""
    root = tmp / "d"
    root.mkdir()
    d = FailingDaemon(root)
    server = None
    try:
        d.paths.rounds.mkdir(parents=True, exist_ok=True)
        d.run()
    except ValueError:
        server = d.server
    expect("12: a fatal error in the loop still stops the OpenCode server",
           isinstance(server, FakeServer) and server.stopped)

    alerts.configure(tmp / "alerts.jsonl", "test", root)
    (root / "uv.lock").write_text("")
    real = daemon.subprocess.run

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 1, output=b"", stderr=b"still resolving\n")
    daemon.subprocess.run = timeout
    try:
        daemon.sync_environment(root, tmp / "state")
    finally:
        daemon.subprocess.run = real
    logged = [r for r in alerts.read(tmp / "alerts.jsonl") if r["key"] == "daemon.uv_lock_failed"]
    expect("26: a uv sync timeout logs its stderr as text",
           len(logged) == 1 and logged[0]["details"]["stderr"] == "still resolving\n",
           repr(logged))


def test_runtime(tmp: Path) -> None:
    """Issues 44, 54 and 55."""
    root = tmp / "w"
    root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", "one"],
                   check=True, env=env)
    tree, sha = root / ".verifier" / "checkout", git.head(root)
    git.check_out(root, tree, sha)
    shutil.rmtree(root / ".verifier")
    (root / ".verifier").mkdir()        # the daemon makes the state folder again first
    try:
        git.check_out(root, tree, sha)
        made = tree.is_dir()
    except subprocess.CalledProcessError as e:
        made = False
        print(e.stderr)
    expect("44: a checkout deleted by hand is made again", made)

    real_kill = jobs.os.kill

    def other_user(pid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")
    jobs.os.kill = other_user
    try:
        expect("54: a job whose pid is now another user's process has ended",
               jobs.process_is_running(12345) is False)
    finally:
        jobs.os.kill = real_kill

    seen = {}
    real_run = structure.subprocess.run

    def record(*a, **k):
        seen.update(k.get("env") or {})
        return real_run(*a, **k)
    structure.subprocess.run = record
    try:
        structure._git(str(tmp), "status")
    finally:
        structure.subprocess.run = real_run
    expect("55: git runs with LC_ALL=C where its messages are read", seen.get("LC_ALL") == "C")


def test_pins(tmp: Path) -> None:
    """Issue 30."""
    (tmp / "runs").mkdir()
    (tmp / "runs" / "r.json").write_text("{}")
    register = tmp / "claim_register.json"
    pin = artifact_pins.ArtifactPins.current_checksum(tmp, "runs/r.json")
    register.write_text('{"claims": [{"id": "c0", "statement": "Accuracy is 0.120.", '
                        '"value": 0.120, "source": {"kind": "artifact", "path": "runs/r.json", '
                        '"method": "acc", "sha256": "' + pin + '"}}]}')
    editor = artifact_pins.ClaimRegisterPinEditor(
        register, tmp, claim_register_parser.ClaimRegisterParser({"acc"}, {}))
    changed = editor.propose_checksum("runs/r.json", "0" * 64)
    text = register.read_text()
    expect("30: a proposed checksum keeps the value as printed",
           changed == ["c0"] and '"value": 0.120' in text and "proposed_sha256" in text, text)
    # `vcheck check-register` pins an entry before `vcheck write-methods` gives it a method
    register.write_text('{"claims": [{"id": "c0", "statement": "Accuracy is 0.120.", '
                        '"value": 0.120, "source": {"kind": "artifact", "path": "runs/r.json", '
                        '"sha256": "' + pin + '"}}]}')
    changed = editor.propose_checksum("runs/r.json", "0" * 64)
    expect("a checksum is proposed also on a pinned entry that has no method yet",
           changed == ["c0"] and "proposed_sha256" in register.read_text(), repr(changed))
    # a pin change writes every entry as written: an unreadable entry and its key order stay
    broken = {"statement": "No id.", "zeta": 1, "alpha": 2}
    register.write_text(json.dumps({"claims": [
        {"id": "c0", "value": 0.5, "source": {"kind": "artifact", "path": "runs/r.json",
                                              "method": "acc", "sha256": pin}}, broken]}))
    changed = editor.propose_checksum("runs/r.json", "0" * 64)
    after = json.loads(register.read_text())["claims"]
    expect("a pin change keeps an unreadable entry, with its keys in their order",
           changed == ["c0"] and after[1] == broken and list(after[1]) == list(broken),
           repr(after))


def test_latex() -> None:
    """Issues 31, 33, 81 and 82."""
    table = "\\begin{table}\\begin{tabular}{lr}\nModel & Acc \\\\\n7B & 71.2 \\\\\n\\end{tabular}"
    found = candidates(document(table + "\\caption{Main.}\\end{table}\n"
                                + table + "\\caption{Ablation.}\\end{table}"))
    rows = [c["id"] for c in found if c["kind"] == "table" and "Model: 7B; Acc: 71.2" in c["text"]]
    expect("31: the same row in two tables is in two candidates",
           len(rows) == 2 and rows[0] != rows[1], repr(rows))

    try:
        found = candidates(document(
            "\\begin{tabular*}\n{\\textwidth}{lr}\nModel & Acc \\\\\n7B & 71.2 \\\\\n"
            "\\end{tabular*}"))
        texts = [c["text"] for c in found]
    except ValueError as e:
        texts = [f"ValueError: {e}"]
    expect("33: a tabular* with its width on the next line is read",
           any(t.endswith("Rows: Model: 7B; Acc: 71.2") for t in texts), repr(texts))

    found = candidates(document("\\begin{wrapfigure}{r}{0.4\\textwidth}\n"
                                "\\caption{The 13B model gains 4.1 points.}\n\\end{wrapfigure}"))
    expect("81: a wrapfigure caption is a candidate",
           any(c["text"] == "The 13B model gains 4.1 points." for c in found), repr(found))

    found = candidates(document("\\begin{tabular}{lr}\n\\makecell{Model\\\\size} & Acc \\\\\n"
                                "7B & 71.2 \\\\\n\\end{tabular}"))
    texts = [row for c in found if c["kind"] == "table"
             for row in c["text"].split(" Rows: ", 1)[1].split(" || ")]
    expect("82: a \\makecell header keeps its column labels in place",
           texts == ["Model size: 7B; Acc: 71.2"], repr(texts))


def test_red_team() -> None:
    """Issues 60, 64 and 72."""
    claim = uncertainty.Premise(id="c", statement="Retrieval adds tokens.", value=1234567.0)
    printed = uncertainty.Premise(id="p", statement="Accuracy is 0.120.",
                                  value=inputs.PrintedFloat("0.120"))
    # the full paper is the one premise; only the claim is given with its value
    paper = (uncertainty.paper_premise("Retrieval adds tokens. Accuracy is 0.120."),)
    text = prompt.user_text(claim, paper) + "\n" + prompt.user_text(printed, paper)
    expect("60: the red team prompt keeps every digit of a value",
           "1234567)" in text and "0.120)" in text and "e+06" not in text, text)

    same = json.dumps({"proposals": [
        {"hypotheticals": [{"design": "A.", "result": "Worse."}]},
        {"hypotheticals": [{"design": "A.", "result": "Worse."}]}]})
    scored = []

    def score(attack: uncertainty.Attack) -> uncertainty.Assessment:
        scored.append(attack)
        return uncertainty.Assessment(believability=0.9, breakage=0.5)
    team = prompted.PromptRedTeam(model=M, proposals=2, results=1, iterations=2,
                                  ask_and_read=lambda text, model, read, *a, **k: read(same))
    got = team.attack(claim, paper, score)
    expect("64: a proposal repeated in one round and in a later round is kept and scored once",
           len(got) == 1 and len(scored) == 1, repr(got))

    url = "http://gpu-host:8000/v1/chat/completions"
    try:
        prompted.model_is_reachable({uncertainty.WANT.key: True, prompted.MODEL.key: url})
        refused = False
    except ValueError:
        refused = True
    expect("72: a red team model with no '#MODEL' part is refused", refused)
    prompted.model_is_reachable({uncertainty.WANT.key: True,
                                 prompted.MODEL.key: url + "#Qwen/Qwen3",
                                 uncertainty.RED_TEAM.key: "prompt"})
    saved = os.environ.get("OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = "test-key"
    try:
        prompted.model_is_reachable({uncertainty.WANT.key: True})
    finally:
        restore("OPENROUTER_API_KEY", saved)
    prompted.model_is_reachable({uncertainty.WANT.key: True, prompted.MODEL.key: url,
                                 uncertainty.RED_TEAM.key: "other"})
    expect("72: a complete name, the default model with its key, or another red team passes",
           True)
    os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        prompted.model_is_reachable({uncertainty.WANT.key: True})
        refused = ""
    except ValueError as e:
        refused = str(e)
    finally:
        restore("OPENROUTER_API_KEY", saved)
    expect("72: the default model with no OPENROUTER_API_KEY is refused",
           "OPENROUTER_API_KEY is not set" in refused, refused)


def restore(name: str, value: str | None) -> None:
    """Set the variable `name` back to `value`, or remove it when `value` is None."""
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def test_judge() -> None:
    """Issues 20, 68 and 74."""
    claim = uncertainty.Premise(id="c", statement="Retrieval helps all sizes.", value=None)
    # the full paper is the one premise; the judge gets it in the shared prefix
    paper = (uncertainty.paper_premise("Retrieval helps the 7B model."),)
    attack = uncertainty.Attack(hypotheticals=(uncertainty.Hypothetical(
        design="13B, same data", result="retrieval lowers accuracy by 2 points"),))
    believe = quantifier.believe_prompt(paper, attack)
    expect("20: believability asks how likely the hypothetical result is, given the paper",
           "How likely is it for the hypothetical result to occur, given the paper,"
           " and assuming we run the hypothetical experiment?" in believe
           and claim.statement not in believe and "the 7B model" not in believe, believe)
    broken = quantifier.break_prompt(claim, paper, attack)
    expect("68: breakage gives the result plainly, with no word on which is hypothetical",
           "ypothetical" not in broken and "erified" not in broken
           and "the paper in the instructions as true. Also take this result as true:\n"
               "- 13B, same data:" in broken
           and "the 7B model" not in broken, broken)
    version = quantifier.JudgeQuantifier(M, uncertainty.CUTOFF.default).version
    expect("74: the judge's version holds the cutoff",
           version.endswith(f"cutoff-{uncertainty.CUTOFF.default}")
           and quantifier.JudgeQuantifier(M, 0.3).version != version, version)


def test_readings(tmp: Path) -> None:
    """Issue 59."""
    red, judge = test_uncertainty.FakeRedTeam(), test_uncertainty.FakeQuantifier()
    claims = test_uncertainty.register()
    test_uncertainty.run(tmp, claims, **test_uncertainty.parts(red, judge, tmp))
    first = judge.assessed
    test_uncertainty.run(tmp, claims, **test_uncertainty.parts(
        red, judge, tmp, **{uncertainty.MAX_EVALS.key: 7}))
    expect("59: a changed cap of score calls reuses every saved reading",
           first > 0 and judge.assessed == first, f"{first} then {judge.assessed}")


def main() -> int:
    """Run every case and report."""
    tests = (test_daemon, test_runtime, test_pins, test_latex, test_red_team, test_judge,
             test_readings)
    for test in tests:
        print(test.__name__)
        if test.__code__.co_argcount:
            with tempfile.TemporaryDirectory() as tmp:
                test(Path(tmp))
        else:
            test()
    print("all passed" if not FAILED else f"FAILED: {len(FAILED)} case(s).")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
