#!/usr/bin/env python3
"""Assert that each consistency check fires when it should.

Scientific-rigour checks are asserted separately; they live in science.py.

A check that never fires is indistinguishable from a check that is not there, so
`fixtures/broken_paper.tex` deliberately violates one invariant per construct.
`fixtures/broken_notes.md` makes the same mistakes in Markdown, read by the Markdown
reader, where the orphan-float check must be skipped rather than pass.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from checks.consistency import consistency
from schema.primitives import Flagged, Skipped
from utils import extract

HERE = os.path.dirname(os.path.abspath(__file__))

MUST_FIRE = [
    "duplicate-label",
    "interval-reversed",
    "value-outside-own-interval",
    "abstract-number-unsupported",
    "dangling-ref",
    "row-width",
    "caption-model-mismatch",
    "number-not-in-any-table",
]
# The fixtures, and the checks that must be skipped on each.
FIXTURES = {"broken_paper.tex": [], "broken_notes.md": ["orphan_float"]}


# Text, and the model names the extractor must find in it.
MODEL_CASES = [
    ("We compare Qwen2.5-7B and Llama 3.3 70B against gpt-4o-mini.",
     ["Llama-3.3-70B", "Qwen2.5-7B", "gpt-4o-mini"]),
    ("Our Qwen3-8B-sft model beats DeepSeek-R1.", ["DeepSeek-R1", "Qwen3-8B"]),
    ("Table 3 shows the 2024 results at 8B scale.", []),
]


def check_model_names():
    """The extractor finds known model names however they are written. Returns failures."""
    failed = 0
    for text, want in MODEL_CASES:
        got = extract.models_in(text)
        print(f"  {'ok  ' if got == want else 'MISS'}  model names in {text!r}: {got}")
        failed += got != want
    return failed


# Markdown, and what the Markdown reader must make of it.
NOTES = """\
---
date: 2026-10-17
---
# Results

Accuracy was 0.823, up 9.7% on 0.750; this clause must survive the percent sign.
The line above a table is prose, not its caption.
| arm | accuracy |
|:----|---------:|
| ours | 0.823 |

## Results

```
0.999 is in a code block
```
"""


def refused(path):
    """Whether the reader refuses to guess the format of `path`."""
    try:
        extract.paper_format(path)
    except extract.UnknownPaperFormat:
        return True
    return False


def check_markdown_reader():
    """The Markdown reader keeps % as a percent sign, reads pipe tables, skips code and
    front matter, and anchors repeated headings as GitHub does; a file's extension
    names its reader, and an unknown one is refused. Returns failures."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(NOTES)
    paper = extract.load([fh.name])
    os.unlink(fh.name)
    cases = [
        ("a % sign does not cut its line short", "must survive the percent sign" in paper.text),
        ("the pipe table is read with its declared columns",
         len(paper.tables) == 1 and paper.tables[0].ncol == 2
         and paper.tables[0].numbers == ("0.823",)),
        ("a prose line above a table is not its caption", paper.tables[0].caption == ""),
        ("code and front matter are not read", "0.999" not in paper.text
         and "2026" not in paper.text),
        ("a repeated heading gets a numbered anchor", {"results", "results-1"} <= paper.targets),
        ("the statements come from the prose", [s.numbers for s in paper.statements]
         == [("0.750", "0.823")]),
        ("each extension names its format, without regard to case",
         [extract.paper_format(p).value for p in ("a.tex", "b.LTX", "c.md", "d.Rmd", "e.qmd")]
         == ["latex", "latex", "markdown", "markdown", "markdown"]),
        ("a file of no known format is refused", refused("notes.txt")),
    ]
    for name, ok in cases:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    return sum(not ok for _, ok in cases)


# LaTeX, and what the LaTeX reader must make of it.
LATEX = r"""\documentclass{article}
\begin{document}
\begin{abstract}
We report a gain of $0.285$ over the base model \cite{smith2019}.
\end{abstract}
\section{Results}
The gain holds with $p{=}0.020$ on every seed.
\begin{table*}[t]
\label{tab:wide}
\caption{Brier for \textbf{all} runs.}
\begin{tabular}{lc}
\toprule
Run & Brier \\
\midrule
\multicolumn{2}{l}{\emph{Trained}} \\
ours & $0.734$ \\
\bottomrule
\end{tabular}
\end{table*}
\end{document}
"""


def check_latex_reader():
    """The LaTeX reader reads a `table*` float as a table, with its caption whatever its
    braces, its column names and the section heading over each row, and keeps its cells
    out of the sentences; a heading and the braces of `p{=}` do not become part of a
    sentence, and a citation stays as its key. Returns failures."""
    with tempfile.NamedTemporaryFile("w", suffix=".tex", delete=False) as fh:
        fh.write(LATEX)
    paper = extract.load([fh.name])
    os.unlink(fh.name)
    table = paper.tables[0] if len(paper.tables) == 1 else None
    cases = [
        ("there is one table", table is not None),
        ("a table* float is a table, named by its label",
         table is not None and table.name == "tab:wide"),
        ("a caption is read whole, braces and all",
         table is not None and "all} runs" in table.caption),
        ("rules are not cells, and the header comes first",
         table is not None and table.rows == (("Run", "Brier"), ("ours", "$0.734$"))),
        ("a spanning row is the section heading of the rows under it",
         table is not None and table.sections == ("", "Trained")),
        ("the table's cells are not sentences", not any("0.734" in s for s in paper.sentences)),
        ("a heading does not start the next sentence",
         "The gain holds with p=0.020 on every seed." in paper.sentences),
        ("a citation keeps its key (no .bbl here), and a closing period ends a number",
         "We report a gain of 0.285 over the base model (smith2019)." in paper.sentences
         and extract.numbers(paper.sentences[0]) == ["0.285"]),
        ("the caption is listed as plain text",
         [c.text for c in paper.captions] == ["Brier for all runs."]),
    ]
    for name, ok in cases:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    return sum(not ok for _, ok in cases)


# A table that names its model only in the caption, with one row marked as another
# model (Table 1 of the Martingale ICLR 2027 paper). Neither model check may fire.
CAPTION_NAMES_ROWS = r"""
On Llama-3.1-8B, training lowers Brier from 0.364 to 0.257.
\begin{table}
\caption{Held-out Brier. All rows are Llama-3.1-8B except the marked Qwen3-32B row.}
\label{tab:c1}
\begin{tabular}{lc}
\toprule
Run & after \\
\midrule
A on D (Qwen3-32B) & 0.234 \\
A on D & 0.257 \\
B on D & 0.246 \\
\bottomrule
\end{tabular}
\end{table}
See Table~\ref{tab:c1}.
"""
MUST_NOT_FIRE = ["model-not-in-any-table", "caption-model-mismatch"]


def check_caption_names_rows():
    """A caption may name the model of rows that name none. Returns failures."""
    with tempfile.NamedTemporaryFile("w", suffix=".tex", delete=False) as fh:
        fh.write(CAPTION_NAMES_ROWS)
    results = consistency.run(extract.load([fh.name])).checks
    os.unlink(fh.name)
    got = {p.kind.code for r in results.values() if isinstance(r, Flagged) for p in r.problems}
    failed = 0
    for c in MUST_NOT_FIRE:
        print(f"  {'ok  ' if c not in got else 'FIRED'}  {c} stays quiet when the caption "
              "names the rows")
        failed += c in got
    return failed


ROUNDED_TABLE = r"""\documentclass{article}
\begin{document}
The accuracy of the model is 0.713 on the test split, and 0.7128 before rounding.
The error rate of the baseline is 0.555 on the test split, as Table~\ref{tab:a} shows.
\begin{table}\caption{Run 1: accuracy in percent.}\label{tab:a}
\begin{tabular}{lc}
split & accuracy \\
test & 71.3 \\
\end{tabular}
\end{table}
\end{document}
"""


def check_rounded_table_numbers():
    """A text number matches a table number that rounds the same value. Returns failures."""
    with tempfile.NamedTemporaryFile("w", suffix=".tex", delete=False) as fh:
        fh.write(ROUNDED_TABLE)
    results = consistency.run(extract.load([fh.name])).checks
    os.unlink(fh.name)
    flagged = sorted(dict(p.facts)["Number"] for r in results.values()
                     if isinstance(r, Flagged) for p in r.problems
                     if p.kind.code == "number-not-in-any-table")
    ok = flagged == ["0.555"]
    print(f"  {'ok  ' if ok else 'FAIL'}  0.713 and 0.7128 match 71.3 in a table, and 0.555 "
          f"matches no table number, also not the 1 of the caption ({flagged})")
    return not ok


NARROW_ROWS_TABLE = r"""\documentclass{article}
\begin{document}
Table~\ref{tab:hp} gives the settings.
\begin{table}
\caption{Settings.}
\begin{tabular}{l|ccc}
Task & Depth & Interval \\
Parity & 1 & 1000 \\
Copy & 2 & 1000 \\
Addition & 3 & 1600 \\
\end{tabular}
\label{tab:hp}
\end{table}
\end{document}
"""


def check_row_width_once_per_table():
    """Rows of one table with the same wrong width are one flag. Returns failures."""
    with tempfile.NamedTemporaryFile("w", suffix=".tex", delete=False) as fh:
        fh.write(NARROW_ROWS_TABLE)
    results = consistency.run(extract.load([fh.name])).checks
    os.unlink(fh.name)
    details = [p.detail for r in results.values() if isinstance(r, Flagged)
               for p in r.problems if p.kind.code == "row-width"]
    ok = len(details) == 1 and "rows 2-4 have 3 cells, header declares 4" in details[0]
    print(f"  {'ok  ' if ok else 'FAIL'}  a table whose rows all miss one declared column "
          f"gives one row-width flag ({details})")
    return not ok


def check_citations_are_not_intervals():
    """A citation list is not an interval; a real interval is still checked. Returns failures."""
    flagged = []
    consistency._check_intervals(
        r"As in [21] and [3, 12], we get 0.80 [3, 12]. Backwards: [+0.21, +0.05]. "
        r"Outside: 0.9 [0.1,\, 0.3]. Whole numbers: 0.9 [91, 85].",
        lambda kind, detail, facts=(): flagged.append(detail))
    ok = sorted(flagged) == sorted(["interval [+0.21, +0.05] has its lower bound above its upper",
                                    "interval [91, 85] has its lower bound above its upper",
                                    "0.9 is reported outside its own interval [0.1, 0.3]"])
    print(f"  {'ok  ' if ok else 'FAIL'}  citations such as [21] and [3, 12] are not intervals, "
          f"and a real interval is still checked ({flagged})")
    return not ok


def main():
    """Run the consistency check on the fixture and report which expected findings
    fired. Returns 1 if any did not, so the script can gate a commit."""
    if check_model_names():
        print("\nFAILED: model names were not found as expected")
        return 1
    if check_markdown_reader():
        print("\nFAILED: the Markdown reader did not read the notes as expected")
        return 1
    if check_latex_reader():
        print("\nFAILED: the LaTeX reader did not read the paper as expected")
        return 1
    if check_caption_names_rows():
        print("\nFAILED: a model check fired on a table whose caption names its rows")
        return 1
    if check_rounded_table_numbers():
        print("\nFAILED: the table numbers were not matched as rounded values")
        return 1
    if check_row_width_once_per_table():
        print("\nFAILED: one table gave more than one row-width flag")
        return 1
    if check_citations_are_not_intervals():
        print("\nFAILED: a citation was read as an interval")
        return 1
    failed = []
    for name, must_skip in FIXTURES.items():
        print(f"\n  {name}")
        results = consistency.run(extract.load([os.path.join(HERE, "fixtures", name)])).checks
        got = {p.kind.code for r in results.values() if isinstance(r, Flagged)
               for p in r.problems}
        skipped = {k for k, r in results.items() if isinstance(r, Skipped)}
        for c in MUST_FIRE:
            print(f"  {'ok  ' if c in got else 'MISS'}  {c}")
        failed += [f"{name}: {c} did not fire" for c in MUST_FIRE if c not in got]
        failed += [f"{name}: {k} was not skipped" for k in must_skip if k not in skipped]
        failed += [f"{name}: {k} was skipped" for k in sorted(skipped - set(must_skip))]
        extra = sorted(got - set(MUST_FIRE))
        if extra:
            failed.append(f"{name}: also fired {', '.join(extra)}")
    if failed:
        print("\nFAILED:\n  " + "\n  ".join(failed))
        return 1
    print(f"\nAll {len(MUST_FIRE)} checks fired on both broken fixtures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
