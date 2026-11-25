#!/usr/bin/env python3
"""Check the candidates of a small research folder, and `vcheck write-methods`.

Builds a git repository with a LaTeX paper (a table with a section heading, a caption, a
sentence with no number), a Markdown note with a pipe table, a results file, and commits whose
messages state numbers. The method writer's model is replaced by a scripted function, so no
network is needed. The cases check that:
  - candidates come from LaTeX tables (one whole-table candidate each, with its column names
    and rows; no row is a candidate), captions, every sentence (numbers or not), Markdown
    tables and commit messages, tagged with origin;
  - a whole table is never cut, and one longer than LONG_TABLE_CHARACTERS is a warning;
  - a candidate id comes from the text: it stays when a sentence is added before it, and a
    repeat in one note gets -2;
  - write-methods never shows the claimed value (read from the register, so the agent cannot
    leak it), retries a crashing method with the error and no value, keeps every version, writes
    one module per call avoiding names already used, and records the method in the register.
Returns 1 if any case fails.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import cli
from core.commands import write_methods  # noqa: E402
from core.methods import method_writer
from utils import alerts, claim_files, extract, structure  # noqa: E402

PAPER = r"""\documentclass{article}
\begin{document}
\begin{abstract}
Training lowers the Brier score of the trained model from 0.205 to 0.075 on 2 questions.
The improvement is an order of magnitude larger than chance.
\end{abstract}
\begin{table}
\caption{Brier before and after training.}
\label{tab:main}
\begin{tabular}{lcc}
\toprule
Run & before & after \\
\midrule
\multicolumn{3}{l}{\emph{Trained model}}\\
trained & $0.205$ & $0.075$ \\
\bottomrule
\end{tabular}
\end{table}
\end{document}
"""
NOTE = "# Run notes\n\n| run | Brier after |\n|---|---|\n| trained | 0.075 |\n"
# Trained Brier = (0.16 + 0.09 + 0.01 + 0.04) / 4 = 0.075, the number the paper states.
RECORDS = {"records": [{"condition": "trained", "post": 0.6, "y": 1},
                       {"condition": "trained", "post": 0.7, "y": 1},
                       {"condition": "trained", "post": 0.9, "y": 1},
                       {"condition": "trained", "post": 0.2, "y": 0},
                       {"condition": "base", "post": 0.5, "y": 1}]}
MODULE = ("from checks.interface import Computed, method\n"
        "@method('{name}')\n"
        "def {name}(path):\n"
        "    \"\"\"Brier of trained records.\n\n    Definition: mean (post - y)^2.\n"
        "    Assumptions: belief = post, outcome = y, condition == 'trained'.\"\"\"\n"
        "    import json\n"
        "    rows = [r for r in json.load(open(path))['{key}'] if r['condition'] == 'trained']\n"
        "    return Computed(value=sum((r['post'] - r['y']) ** 2 for r in rows) / len(rows),\n"
        "                    n=len(rows))\n")


def git(root, *args):
    """Run git in the test repository."""
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def vcheck(state, *args):
    """Run a vcheck command quietly; return (exit code, output)."""
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.main(["--state", str(state), *args])
    return rc, out.getvalue()


def main():
    """Run every case and report."""
    ok = True

    def check(name, cond, detail=""):
        """Print one case and keep the overall result."""
        nonlocal ok
        print(f"{'ok  ' if cond else 'FAIL'} {name}"
              + (f": {detail}" if detail and not cond else ""))
        ok &= bool(cond)

    with tempfile.TemporaryDirectory() as root:
        git(root, "init", "-q")
        os.makedirs(os.path.join(root, "runs"))
        Path(root, "runs", "results.json").write_text(json.dumps(RECORDS))
        Path(root, "paper.tex").write_text(PAPER)
        Path(root, "notes.md").write_text(NOTE)
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "trained run: Brier 0.075")
        sha = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True,
                             text=True).stdout.strip()
        cands = {c["id"]: c for c in structure.folder_candidates(root)}
        table = next((c for c in cands.values() if c["kind"] == extract.TABLE
                      and c["origin"] == "paper.tex"), {})
        check("a LaTeX table is one candidate, with its column names and rows, and an id from "
              "its text", "Run: trained; before: 0.205; after: 0.075" in table.get("text", "")
              and table.get("id") == "paper.tex#" + extract.text_id("tab", table["text"], {}),
              str(table))
        check("no table row is a candidate of its own",
              {c["kind"] for c in cands.values()} <= {extract.TABLE, extract.SENTENCE,
                                                     extract.COMMIT, extract.EQUATION}
              | {k for k in (c["kind"] for c in cands.values()) if extract.is_caption(k)},
              str({c["kind"] for c in cands.values()}))
        check("a caption, and a sentence with no number",
              any(c["kind"] == "table caption" and c["id"].startswith("paper.tex#cap-")
                  for c in cands.values())
              and any(c["kind"] == "sentence" and not c["values"]
                      and "order of magnitude" in c["text"]
                      for c in cands.values()), str(list(cands)))
        check("a Markdown table and a commit message stating a number",
              any(i.startswith("notes.md#tab-") for i in cands)
              and any(c["kind"] == "commit message" and "0.075" in c["values"]
                      and c["id"].startswith(f"commit:{sha[:10]}#s-")
                      for c in cands.values()), str(list(cands)))
        magnitude = next(i for i, c in cands.items() if "order of magnitude" in c["text"])
        Path(root, "paper.tex").write_text(PAPER.replace(
            "\\begin{abstract}\n", "\\begin{abstract}\nWe study the Brier score of a "
            "trained forecaster.\n"))
        Path(root, "dup.md").write_text("The trained model is better than chance here.\n\n"
                                        "The trained model is better than chance here.\n")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "intro")
        later = {c["id"]: c for c in structure.folder_candidates(root)}
        check("a sentence keeps its id when a sentence is added before it",
              magnitude in later and len(later) == len(cands) + 3, str(list(later)))
        repeated = sorted(i for i, c in later.items() if c.get("origin") == "dup.md")
        check("a repeat in one note gets -2", len(repeated) == 2
              and repeated[1] == repeated[0] + "-2", str(repeated))
        git(root, "rm", "-q", "dup.md")
        git(root, "commit", "-q", "-m", "drop the duplicate note")

        state = Path(root) / ".verifier"
        state.mkdir()
        claim_files.save_scope(state, (structure.WHOLE,))
        claim_files.begin_round(Path(root), state, sha)
        quotes = {"magnitude": {"created": "", "last_updated": "", "candidates": [magnitude],
                                "quote": "an order of magnitude larger than chance",
                                "value": None},
                  "after": {"created": "", "last_updated": "", "candidates": [magnitude],
                            "quote": "an order of magnitude larger than chance",
                            "value": None}}
        (state / claim_files.QUOTES).write_text(json.dumps(quotes))
        (state / claim_files.CLASSES).write_text(json.dumps(
            {"paper/magnitude": {"created": "", "last_updated": "", "quotes": ["magnitude"]},
             "paper/trained/after": {"created": "", "last_updated": "", "quotes": ["after"]}}))
        (state / "claim_register.json").write_text(json.dumps({"claims": [
            {"id": "paper/trained/after", "created": "", "last_updated": "",
             "statement": "The trained model's Brier score is 0.075.", "value": 0.075,
             "origin": "paper.tex",
             "source": {"kind": "artifact", "path": "runs/results.json"}}]}))
        need = [{"claim": "paper/trained/after", "path": "runs/results.json",
                 "quantity": "mean over records with condition == 'trained' of (post - y)^2"}]
        (state / write_methods.NEEDS).write_text(json.dumps(need))
        prompts = []
        replies = iter([MODULE.format(name="brier_trained_a", key="rows"),      # crashes: no 'rows'
                        MODULE.format(name="brier_trained_a", key="records"),   # fixed
                        MODULE.format(name="brier_trained_b", key="records")])  # second call

        def fake(prompt, timeout=0, model=""):
            """The method writer, offline; records its prompts."""
            prompts.append(prompt)
            module = next(replies)
            name = "brier_trained_a" if "brier_trained_a" in module else "brier_trained_b"
            return json.dumps({"module": module,
                               "methods": [{"need": "paper/trained/after/n1", "method": name}]})
        real, method_writer.ask = method_writer.ask, fake
        try:
            rc1, out1 = vcheck(state, "write-methods", "--retries", "2")
            rc2, out2 = vcheck(state, "write-methods", "--retries", "0")
        finally:
            method_writer.ask = real
        check("write-methods retries a crash and links the claim",
              rc1 == 0 and "method brier_trained_a" in out1 and len(prompts) == 3, out1 + out2)
        source = json.loads((state / "claim_register.json").read_text())["claims"][0]["source"]
        check("write-methods records the method in the register",
              source.get("method") == "brier_trained_b", str(source))
        check("the retry names the crash and carries no value",
              "crashed" in prompts[1] and "KeyError" in prompts[1])
        check("the method writer never sees the claimed value",
              all("0.075" not in p for p in prompts))
        mods = sorted(p.name for p in (state / "methods").glob("written_methods_*.py"))
        check("one module per call, and every version kept",
              mods == ["written_methods_1.py", "written_methods_2.py"]
              and len(list((state / "versions").glob("*.py"))) == 3, str(mods))
        check("the second call is told which names are taken",
              "brier_trained_a" in prompts[2].split("NEEDS:")[0])
    long = "We measure " + "the accuracy of the model " * 200 + "at 71.2%."
    cands = extract.sentence_candidates(["A first sentence of " + "many words " * 40 + "here.",
                                         long])
    check("a long sentence is cut at MAX_PIECE characters, with no marker in the text",
          cands[1]["text"] == long[:extract.MAX_PIECE] and "characters cut" not in cands[1]["text"])
    check("the sentence before is cut at 300 characters, with no marker",
          len(cands[1]["previous"]) == 300 and "characters cut" not in cands[1]["previous"])
    check("a long sentence has the context field cut: true, and a short one has none",
          cands[1].get("cut") is True and "cut" not in cands[0])
    rows = "".join(f"| model {n} | {n}.25 | {1000 + n} |\n" for n in range(1, 400))
    with tempfile.TemporaryDirectory() as tmp:
        note = Path(tmp) / "big.md"
        note.write_text("# Results\n\nThe small model wins.\n\n| Model | Acc | Steps |\n"
                        "|---|---|---|\n" + rows)
        found = extract.candidates(extract.read_markdown(str(note)))
    table = next(c for c in found if c["kind"] == extract.TABLE)
    check("a long whole table is never cut, and has no cut field",
          "Model: model 399; Acc: 399.25" in table["text"] and "cut" not in table,
          table["text"][-80:])
    check("its values come from every row", "399.25" in table["values"],
          str(table["values"][-6:]))
    with tempfile.TemporaryDirectory() as tmp:
        log = alerts.configure(Path(tmp) / "alerts.jsonl", "test", Path(tmp))
        (Path(tmp) / "big.md").write_text("| Model | Acc |\n|---|---|\n" + rows)
        structure.note_candidates(tmp, "big.md")
        warned = [r for r in alerts.read(log.path) if r["key"] == "papers.long_table"]
    check("a whole table above LONG_TABLE_CHARACTERS is a warning",
          len(warned) == 1 and warned[0]["level"] == "warn", repr(warned))
    small = next(c for c in extract.candidates(extract.read_markdown(str(write_small())))
                 if c["kind"] == extract.TABLE)
    check("a small table has no cut field", "cut" not in small, str(small))
    print("all passed" if ok else "SOME FAILED")
    return 0 if ok else 1


def write_small() -> Path:
    """A note with one small table, in a new temporary folder."""
    note = Path(tempfile.mkdtemp()) / "small.md"
    note.write_text("# Results\n\n| Model | Acc |\n|---|---|\n| 7B | 68.4 |\n")
    return note


if __name__ == "__main__":
    sys.exit(main())
