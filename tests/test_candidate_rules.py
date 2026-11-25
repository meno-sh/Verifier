#!/usr/bin/env python3
"""Check the code rules that drop clear non-claims (utils/candidate_rules.py).

Each rule has a case it must catch and a case it must not. Then `vcheck drop-non-claims`
on a small project: it drops only unanswered candidates, with an `auto:Rn` reason and empty stamps,
and `vcheck check-quotes` refuses a repeat drop whose twin has no quote, and an equation
drop whose twin's quotes leave the equation out. Returns 1 if any case fails.
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
from core import cli  # noqa: E402
from utils import candidate_rules, claim_files, structure  # noqa: E402

FAILED: list[str] = []


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def sentence(text: str, section: str = "Results", **fields: object) -> dict:
    """A sentence candidate."""
    return {"kind": "sentence", "text": text, "section": section, "previous": "", **fields}


def table(text: str, **fields: object) -> dict:
    """A whole-table candidate."""
    return {"kind": "table", "text": text, "table": "Table 1", **fields}


def reason(cand: dict, others: dict | None = None) -> str | None:
    """The reason the rules give one candidate, among `others`."""
    return candidate_rules.non_claim_reasons({"x": cand, **(others or {})}).get("x")


# (rule, the case it must catch, a case it must not catch)
RULE_CASES = [
    ("R1", sentence("% We tried 5 seeds but only report 3"),
     sentence("We report 50% accuracy on 3 seeds.")),
    ("R2", sentence("Jane Doe, Department of Computer Science, Stanford University, "
                    "jane@stanford.edu", section="", origin="paper/main.tex"),
     sentence("Looped transformers generalize to longer inputs than they were trained on.",
              section="")),
    ("R3", sentence("We are thankful to the reviewers.", section="Acknowledgements"),
     sentence("Experiments used 512 TPUv4 chips for 3 weeks.", section="Acknowledgements")),
    ("R4", sentence("We thank Jane Doe for helpful discussions."),
     sentence("Thanks to its sparse attention, the model runs 3x faster.")),
    ("R5", sentence("The answer NA means that the paper does not include experiments.",
                    section="NeurIPS Paper Checklist > Experimental result reproducibility"),
     sentence("We provide a description of the method and an experimental evaluation using "
              "several datasets.")),
    ("R6", sentence("All tables should be centered and numbered in the camera-ready version."),
     sentence("The model should be trained for 10 epochs.")),
    ("R7", sentence("Answer: [Yes]"),
     sentence("Answer: [Yes] Error bars are reported in Table 3.")),
    ("R9", sentence("[width=0.5\\linewidth]"),
     sentence("(b) ResNet-50, 76.1%")),
    ("R10", sentence("Code: \\url{https://github.com/org/repo}"),
     sentence("Code is available at https://github.com/org/repo.")),
    ("R11", sentence("Detailed results are in Appendix C."),
     sentence("Table 2 shows that our method beats the baseline.")),
]


def test_rules() -> None:
    """Each rule catches its case and leaves the other."""
    for rule, catch, keep in RULE_CASES:
        got = reason(catch)
        expect(f"{rule} catches {catch['text']!r}",
               got is not None and got.startswith(f"auto:{rule} "), repr(got))
        got = reason(keep)
        expect(f"{rule} leaves {keep['text']!r}", got is None, repr(got))
    expect("R4 also catches a funding sentence",
           reason(sentence("This work was supported by NSF grant 1234.")) == "auto:R4 "
           "acknowledgement")
    expect("R7 catches a checklist answer that is only a placeholder",
           reason(sentence("Justification: [TODO]")) is not None)
    expect("no rule reads a whole table",
           reason(table("Table 1: Columns: Method | Acc. Rows: Method: Method; Acc: Acc"))
           is None)
    expect("R9 catches a caption with no words after its address",
           reason({"kind": "figure caption", "text": "Figure 3: (a)", "float": "Figure 3"})
           == "auto:R9 layout residue")
    for text in ("Table 7 shows the differences in performance between two or three layer "
                 "architectures.", "The rest of this paper is organized as follows."):
        expect(f"R11 catches {text!r}", reason(sentence(text)) == "auto:R11 pointer")
    for text in ("In this paper, we propose a looped transformer, shown in Figure 1.",
                 "Figure 2 shows the loss is lower than the baseline.",
                 "We show that looping helps in Table 3."):
        expect(f"R11 leaves {text!r}", reason(sentence(text)) is None)
    titled = {"y": sentence("Some text.", section="Experiments > Experimental Setup")}
    expect("R12 catches a sentence that is a section title of the paper",
           reason(sentence("Experimental Setup"), titled) == "auto:R12 heading")
    expect("R12 leaves the same words when no section has that title",
           reason(sentence("Experimental Setup")) is None)
    expect("a commit message is never dropped",
           reason({"kind": "commit message", "text": "We thank Jane."}) is None)


TEX = "paper/main.tex"
CHECKLIST = "NeurIPS Paper Checklist > Theory assumptions and proofs"


def test_review_findings() -> None:
    """No rule drops a real claim or a setup fact: the cases the code review found."""
    for text in ("Further experiments in Section 5 confirm the robustness of the method.",
                 "Additional ablations in Appendix B show the same trend.",
                 "Additional ablations are given in Appendix B and confirm the trend.",
                 "More experiments in Appendix C reach the same accuracy."):
        expect(f"R11 leaves a result that names where it is: {text!r}",
               reason(sentence(text)) is None, repr(reason(sentence(text))))
    for text in ("Further details are provided in Appendix D.",
                 "More details can be found in Appendix C.",
                 "Full details of training and evaluation are in Appendix E."):
        expect(f"R11 still catches the pure pointer {text!r}",
               reason(sentence(text)) == "auto:R11 pointer", repr(reason(sentence(text))))

    for text in ("We will release the code and data with the camera-ready version.",
                 "Our paper should not be read as an endorsement of RLHF.",
                 "The inputs should be centered and scaled to unit variance.",
                 "The learning rate should be set in the range 1e-4 to 1e-3."):
        expect(f"R6 leaves {text!r}", reason(sentence(text)) is None, repr(reason(sentence(text))))
    for text in ("Use 10 point type for the main text.",
                 "The style file sets the margins for you.",
                 "Section headings should be flush left."):
        expect(f"R6 still catches the style instruction {text!r}",
               reason(sentence(text)) == "auto:R6 template", repr(reason(sentence(text))))

    for cand in (sentence("The paper does not include theoretical results.", section=CHECKLIST),
                 sentence("Justification: The paper does not include theoretical results.",
                          section=CHECKLIST),
                 sentence("The paper does not use existing assets.", section="Conclusion"),
                 sentence("The answer NA means that the paper does not include experiments.",
                          section="Results")):
        expect(f"R5 leaves {cand['text']!r} in {cand['section']!r}", reason(cand) is None,
               repr(reason(cand)))
    guideline = sentence("The answer NA means that the paper does not include theoretical "
                         "results.", section=CHECKLIST)
    expect("R5 still catches a guideline bullet of the checklist",
           reason(guideline) == "auto:R5 template", repr(reason(guideline)))

    for text in ("$\\lambda = 0.5$.", "\\(\\alpha\\)", "$k$", "T = 4", "(c) 512"):
        expect(f"R9 leaves the math or number {text!r}", reason(sentence(text)) is None,
               repr(reason(sentence(text))))
    expect("R9 still catches graphics options",
           reason(sentence("[width=0.5\\linewidth]")) == "auto:R9 layout residue")

    note = sentence("Results were emailed to alice@lab.org on Monday: accuracy 72%.",
                    section="", origin="notes/log.md")
    expect("R2 leaves a sentence of a note with no headings", reason(note) is None,
           repr(reason(note)))
    plain = sentence("Ping alice@lab.org for access to the logs.", section="",
                     origin="notes/log.md")
    expect("R2 never fires in a markdown note", reason(plain) is None, repr(reason(plain)))
    for text in ("Results were emailed to alice@lab.org on Monday: accuracy 72%.",
                 "Contact alice@lab.org, who ran the 3 seeds."):
        cand = sentence(text, section="", origin=TEX)
        expect(f"R2 leaves a .tex piece with a digit or a verb: {text!r}", reason(cand) is None,
               repr(reason(cand)))
    later = {"intro": sentence("We study retrieval.", section="Introduction", origin=TEX),
             "x": sentence("Jane Doe, Stanford University, jane@stanford.edu", section="",
                           origin=TEX)}
    got = candidate_rules.non_claim_reasons(later).get("x")
    expect("R2 leaves a .tex piece after the first section", got is None, repr(got))


def test_repeats() -> None:
    """R13 and R14 drop a repeat only when its twin is not dropped, and name the twin."""
    eq = {"kind": "equation", "text": "Eq. (2): L = - log p(x | z)", "label": None,
          "address": None}
    holder = sentence("We train with the loss L = -log p(x|z) on all runs.")
    got = candidate_rules.non_claim_reasons({"e": eq, "s": holder})
    expect("R13 drops an equation whose text is inside a sentence, naming the sentence",
           got == {"e": "auto:R13 equation repeat of s"}, repr(got))
    got = candidate_rules.non_claim_reasons({"e": eq, "s": holder}, dropped={"s"})
    expect("R13 leaves the equation when that sentence is dropped", "e" not in got, repr(got))
    got = candidate_rules.non_claim_reasons(
        {"e": eq, "s": sentence("We train with a cross-entropy loss.")})
    expect("R13 leaves an equation that no sentence holds", "e" not in got, repr(got))
    same = sentence("Same words here.")
    spaced = candidate_rules.non_claim_reasons({"paper (v2)/main.tex#s-1": same,
                            "paper (v2)/main.tex#s-2": dict(same)})
    expect("a repeat's twin is read back whole when its id holds spaces",
           candidate_rules.twin(spaced["paper (v2)/main.tex#s-2"]) == "paper (v2)/main.tex#s-1",
           repr(spaced))
    a = sentence("We use the prompt shown below for every model.")
    got = candidate_rules.non_claim_reasons({"a": a, "b": dict(a), "c": dict(a)})
    expect("R14 keeps the first of three copies and drops the others, naming the first",
           got == {"b": "auto:R14 repeat of a", "c": "auto:R14 repeat of a"}, repr(got))
    got = candidate_rules.non_claim_reasons({"a": a, "b": dict(a)}, dropped={"a"})
    expect("R14 leaves a copy whose first copy was dropped", "b" not in got, repr(got))
    got = candidate_rules.non_claim_reasons({"a": a, "b": dict(a)},
                                            quoted={"b": ["We use the prompt"]})
    expect("R14 drops no candidate that already has an answer", got == {}, repr(got))
    got = candidate_rules.non_claim_reasons({"a": table("Table 1: Rows: Method: Ours; Acc: 71.2"),
                                             "b": table("Table 1: Rows: Method: Ours; Acc: 71.2")})
    expect("R14 leaves repeated whole tables", got == {}, repr(got))


def vcheck(root: Path, *args: str) -> tuple[int, str]:
    """Run a vcheck command quietly; return (exit code, output)."""
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.main(["--state", str(root / ".verifier"), "--root", str(root), *args])
    return rc, out.getvalue()


def git(root: Path, *args: str) -> str:
    """Run git in the test repository."""
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, "GIT_AUTHOR_NAME": "t",
                                          "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                                          "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


NOTE = """# Results

Detailed results are in Appendix C.
With retrieval, the 7B model answers 68.4% of the questions correctly.
We thank Jane Doe for helpful discussions.
We ran three seeds for each condition.
We ran three seeds for each condition.
"""


def test_command() -> None:
    """`vcheck drop-non-claims` drops unanswered non-claims; check-quotes guards repeats."""
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
        ids = {c["text"][:12]: cid for cid, c in cands.items()}
        thanks = ids["We thank Jan"]
        (state / claim_files.QUOTES).write_text(json.dumps({"q-thanks": {
            "created": "", "last_updated": "", "candidates": [thanks],
            "quote": "We thank Jane Doe", "value": None}}))
        rc, out = vcheck(root, "drop-non-claims")
        dropped = json.loads((state / claim_files.DROPPED).read_text())
        seeds = sorted(cid for cid, c in cands.items() if c["text"].startswith("We ran"))
        expect("drop-non-claims drops the pointer and the second copy, and prints a count per rule",
               rc == 0 and set(dropped) == {ids["Detailed res"], seeds[1]}
               and "R11" in out and "R14" in out, out + json.dumps(dropped))
        expect("drop-non-claims leaves a candidate the agent already quoted", thanks not in dropped)
        expect("the drops of drop-non-claims have empty stamps and an auto: reason",
               all(d["created"] == "" and d["last_updated"] == "" and d["why"].startswith("auto:")
                   for d in dropped.values()), json.dumps(dropped))
        rc, out = vcheck(root, "check-quotes")
        expect("check-quotes refuses a repeat drop whose twin has no quote",
               rc == 1 and f'it repeats candidate {seeds[0]}, which has no quote' in out, out)
        quotes = json.loads((state / claim_files.QUOTES).read_text())
        quotes["q-seeds"] = {"created": "", "last_updated": "", "candidates": [seeds[0]],
                             "quote": "We ran three seeds for each condition.", "value": None}
        quotes["q-acc"] = {"created": "", "last_updated": "", "candidates": [ids["With retriev"]],
                           "quote": "the 7B model answers 68.4%", "value": "68.4"}
        (state / claim_files.QUOTES).write_text(json.dumps(quotes))
        rc, out = vcheck(root, "check-quotes")
        expect("check-quotes passes once the twin is quoted", rc == 0, out)
        rc, out = vcheck(root, "drop-non-claims")
        expect("a second drop-non-claims finds nothing new", rc == 0 and "dropped 0" in out, out)


def test_quote_not_text() -> None:
    """A candidate named by a quote whose "quote" is not text yet is answered: no rule drop."""
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
        thanks = next(cid for cid, c in cands.items() if c["text"].startswith("We thank"))
        (state / claim_files.QUOTES).write_text(json.dumps({"q-thanks": {
            "created": "", "last_updated": "", "candidates": [thanks], "quote": None,
            "value": None}}))
        rc, out = vcheck(root, "drop-non-claims")
        dropped = json.loads((state / claim_files.DROPPED).read_text())
        expect("drop-non-claims gives no rule drop to a candidate whose quote has no text yet",
               rc == 0 and thanks not in dropped, out + json.dumps(dropped))
        files, _ = claim_files.loaded(state, root)
        expect("quote_words names that candidate, with no words",
               claim_files.quote_words(files).get(thanks) == [])


EQ_TEX = r"""\documentclass{article}
\begin{document}
\section{Method}
We train every model with the loss $L = -\log p(x \mid z) + 0.1 \|z\|^2$ on all runs.
\begin{equation}
L = -\log p(x \mid z) + 0.1 \|z\|^2
\end{equation}
\end{document}
"""


def test_equation_quote() -> None:
    """R13 stands only when a quote of the twin holds the equation's text."""
    eq = {"kind": "equation", "text": "Eq. (2): L = - log p(x | z)", "label": None,
          "address": None}
    holder = sentence("We train with the loss L = -log p(x|z) on all runs.")
    got = candidate_rules.non_claim_reasons({"e": eq, "s": holder},
                                            quoted={"s": ["We train with the loss"]})
    expect("R13 leaves an equation that the twin's quote does not hold", "e" not in got,
           repr(got))
    got = candidate_rules.non_claim_reasons({"e": eq, "s": holder},
                                            quoted={"s": ["the loss L = -log p(x|z)"]})
    expect("R13 drops an equation that the twin's quote holds",
           got == {"e": "auto:R13 equation repeat of s"}, repr(got))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        git(root, "init", "-q")
        (root / ".gitignore").write_text(".verifier/\n")
        (root / "main.tex").write_text(EQ_TEX)
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "paper")
        state = root / ".verifier"
        state.mkdir()
        claim_files.save_scope(state, (structure.WHOLE,))
        claim_files.begin_round(root, state, git(root, "rev-parse", "HEAD"))
        cands = json.loads((state / claim_files.CANDIDATES).read_text())
        eq_id = next(cid for cid, c in cands.items() if c["kind"] == "equation")
        lead = next(cid for cid, c in cands.items() if c["text"].startswith("We train"))
        quotes = {f"q-{n}": claim_files.new_entry(candidates=[cid], quote=cands[cid]["text"],
                                                  value=None)
                  for n, cid in enumerate(cands) if cid not in (eq_id, lead)}
        quotes["q-lead"] = claim_files.new_entry(candidates=[lead],
                                                 quote="We train every model with the loss",
                                                 value=None)
        (state / claim_files.QUOTES).write_text(json.dumps(quotes))
        (state / claim_files.DROPPED).write_text(json.dumps({eq_id: claim_files.new_entry(
            why=f"auto:R13 equation repeat of {lead}")}))
        rc, out = vcheck(root, "check-quotes")
        expect("check-quotes refuses an equation drop whose twin's quote leaves it out",
               rc == 1 and f'drop "{eq_id}"' in out and "does not hold" in out, out)
        quotes["q-lead"]["quote"] = cands[lead]["text"]
        (state / claim_files.QUOTES).write_text(json.dumps(quotes))
        rc, out = vcheck(root, "check-quotes")
        expect("check-quotes passes once the twin's quote holds the equation", rc == 0, out)


def main() -> int:
    """Run every case and report."""
    for test in (test_rules, test_review_findings, test_repeats, test_equation_quote,
                 test_command, test_quote_not_text):
        print(test.__doc__.splitlines()[0])
        test()
    print("all passed" if not FAILED else f"{len(FAILED)} FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
