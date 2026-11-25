#!/usr/bin/env python3
"""Check that the LaTeX reader reads every file that \\include and \\input put in, at any depth.

The cases:
  - a file named by \\include, and a file that it puts in with \\input, are read;
  - the folder reader lists each of their sentences one time, under the main file;
  - a path is read from the main file's folder, as LaTeX reads it;
  - a commented-out \\input is not followed, and the folder reader leaves that file out: it
    is not part of the paper that main.tex compiles (utils/structure.py, unused_tex);
  - a file that includes itself through another file stops the reading and is unreadable;
  - a missing file is unreadable;
  - a Markdown file read before a LaTeX one keeps each sentence on its own section.
Returns 1 if any case fails.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import extract, structure  # noqa: E402

MAIN = r"""\documentclass{article}
\begin{document}
\begin{abstract}Retrieval raises the 7B model's accuracy to 71.2 percent.\end{abstract}
\include{sections/a}
\end{document}
"""
SECTION_A = r"""\section{Results}
The 13B model gains 4.1 points with retrieval.
\input{sections/b}
% \input{sections/never}
"""
SECTION_B = r"""\section{Ablation}
Without reranking the 7B model falls to 63.5 percent.
"""
NEVER = r"""The old run reached 91.4 percent.
"""
CYCLE_MAIN = r"""\documentclass{article}
\begin{document}
The baseline scores 50.2 percent.
\input{c}
\end{document}
"""
CYCLE_C = r"""The first loop file reports 12.5 points.
\input{d}
"""
CYCLE_D = r"""The second loop file reports 13.5 points.
\input{c}
"""
MISSING_MAIN = r"""\documentclass{article}
\begin{document}
The model scores 80.1 percent.
\include{gone}
\end{document}
"""

SENTENCES = ("71.2 percent", "gains 4.1 points", "falls to 63.5 percent")


def _write(root: str, files: dict[str, str]) -> None:
    """Write each file of `files` (relative path to text) under `root`."""
    for rel, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
        with open(os.path.join(root, rel), "w") as fh:
            fh.write(text)


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
        _write(root, {"main.tex": MAIN, "sections/a.tex": SECTION_A, "sections/b.tex": SECTION_B,
                      "sections/never.tex": NEVER})
        paper = extract.read_latex(os.path.join(root, "main.tex"))
        texts = [c["text"] for c in extract.candidates(paper)]
        for s in SENTENCES:
            check(f"the sentence with '{s}' is a candidate", any(s in t for t in texts), str(texts))
        check("a commented-out \\input is not followed", paper.unreadable == (),
              str(paper.unreadable))
        subprocess.run(["git", "-C", root, "init", "-q"], check=True)
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        found = structure.folder_candidates(root)
        for s in SENTENCES:
            hits = [c["id"] for c in found if s in c["text"]]
            check(f"the folder reader lists '{s}' one time, under main.tex",
                  len(hits) == 1 and hits[0].startswith("main.tex#"), str(hits))
        hits = [c["id"] for c in found if "91.4 percent" in c["text"]]
        check("the folder reader leaves out a file whose \\input is commented out",
              hits == [], str(hits))

    with tempfile.TemporaryDirectory() as root:
        _write(root, {"main.tex": CYCLE_MAIN, "c.tex": CYCLE_C, "d.tex": CYCLE_D})
        paper = extract.read_latex(os.path.join(root, "main.tex"))
        texts = [c["text"] for c in extract.candidates(paper)]
        check("files in a cycle are read one time each",
              sum("12.5 points" in t for t in texts) == 1
              and sum("13.5 points" in t for t in texts) == 1, str(texts))
        check("a file that includes itself is unreadable",
              paper.unreadable == ("c.tex (includes itself)",), str(paper.unreadable))

    with tempfile.TemporaryDirectory() as root:
        _write(root, {"main.tex": MISSING_MAIN})
        paper = extract.read_latex(os.path.join(root, "main.tex"))
        texts = [c["text"] for c in extract.candidates(paper)]
        check("a missing file is unreadable", paper.unreadable == ("gone.tex",),
              str(paper.unreadable))
        check("the main file is still read", any("80.1 percent" in t for t in texts), str(texts))
    with tempfile.TemporaryDirectory() as root:
        _write(root, {"notes.md": "# Notes\n\nThe first note says the loss fell by 2.0 points.\n"
                                  "The second note says the run took 3 days in all.\n",
                      "main.tex": MAIN, "sections/a.tex": SECTION_A, "sections/b.tex": SECTION_B})
        paper = extract.load([os.path.join(root, "notes.md"), os.path.join(root, "main.tex")])
        gain = next(n for n, t in enumerate(paper.sentences) if "gains 4.1 points" in t)
        check("a Markdown file before a LaTeX one keeps each sentence on its own section",
              len(paper.sentence_sections) == len(paper.sentences)
              and paper.sentence_sections[gain] == "Results",
              str(list(zip(paper.sentences, paper.sentence_sections, strict=False))))
    with tempfile.TemporaryDirectory() as root:
        _write(root, {"main.tex": "\\documentclass{article}\n\\begin{document}\n"
                                  "The plot shows a gain of 4.2 points in all runs.\n"
                                  "\\input{plot.pgf}\n\\end{document}\n",
                      "plot.pgf": "\\pgfpathmoveto{\\pgfqpoint{1.5in}{2.5in}}\n"})
        paper = extract.read_latex(os.path.join(root, "main.tex"))
        texts = [c["text"] for c in extract.candidates(paper)]
        check("a graphic input (.pgf) is not unreadable and is not read as text",
              paper.unreadable == () and not any("pgfqpoint" in t for t in texts),
              f"{paper.unreadable} {texts}")
    with tempfile.TemporaryDirectory() as root:
        _write(root, {"main.tex": "\\documentclass{article}\n\\begin{document}\n"
                                  "\\input{results_v1.2}\n\\end{document}\n",
                      "results_v1.2.tex": "The dotted file says the gain was 7.5 points.\n"})
        paper = extract.read_latex(os.path.join(root, "main.tex"))
        texts = [c["text"] for c in extract.candidates(paper)]
        check("an input with a dot in its name is read, with .tex added",
              paper.unreadable == () and any("7.5 points" in t for t in texts),
              f"{paper.unreadable} {texts}")
    for body, want in ((r"\caption{Long {x} text}", "Long {x} text"),
                       (r"\caption[Short]{Long text}", "Long text"),
                       (r"\caption*{Star text}", "Star text"),
                       (r"\captionof{table}{Of text}", "Of text"),
                       (r"\captionof*{table}{Star of text}", "Star of text")):
        got = extract._latex_caption(body)
        check(f"the caption of {body} is read", got == want, repr(got))
    numbers = extract._numbering(
        "\\begin{document}\n\\begin{table}\\caption*{No number}\\end{table}\n"
        "\\begin{table}\\caption{One}\\label{tab:one}\\end{table}\n", "")
    check("a \\caption* takes no table number",
          numbers.get("tab:one", ("", "", ""))[1] == "1", repr(numbers))
    print("all passed" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
