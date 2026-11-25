#!/usr/bin/env python3
"""Check that the LaTeX reader finds the labels and references that LaTeX resolves.

The cases:
  - a label given as an environment option, \\begin{lstlisting}[label={lst:add}], is a label;
  - a reference split over two lines resolves to the label with one space;
  - \\ref{#1} in a macro definition is not a reference;
  - \\autoref, \\cref with a comma list, \\hyperref[...], and a paper's own \\figref or \\pref
    (a command whose name ends in "ref") are references; \\href is not;
  - so the consistency checks find no dangling reference in such a paper.
Returns 1 if any case fails.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import extract  # noqa: E402

PAPER = r"""\documentclass{article}
\def\figref#1{figure~\ref{#1}}
\newcommand{\pref}[1]{(\autoref{#1})}
\begin{document}
See \figref{fig:a}, \autoref{fig:b}, \cref{tab:c,tab:d}, \pref{fig:e}, \hyperref[sec:f]{F}
and \href{https://example.org}{a site}.
\section{F}\label{sec:f}
\begin{figure}\caption{A.}\label{fig:a}\end{figure}
\begin{figure}\caption{B.}\label{fig:b}\end{figure}
\begin{figure}\caption{E.}\label{fig:e}\end{figure}
\begin{table}\caption{C.}\label{tab:c}\end{table}
\begin{table}\caption{D.}\label{tab:d}\end{table}
See Listing~\ref{lst:add} and Equation~(\ref{eq:
step}).
\begin{equation}
T = n + 1 \label{eq: step}
\end{equation}
\begin{lstlisting}[language=Python, label={lst:add}, caption=Addition.]
def add(a, b): return a + b
\end{lstlisting}
\end{document}
"""


def main() -> int:
    """Run every case and report."""
    ok = True

    def check(label: str, cond: bool, detail: str = "") -> None:
        """Print one case; remember a failure."""
        nonlocal ok
        print(("ok   " if cond else "FAIL ") + label + ("" if cond else f": {detail}"))
        ok &= cond

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "main.tex")
        with open(path, "w") as f:
            f.write(PAPER)
        paper = extract.read_latex(path)
    check("an environment option label is a label", "lst:add" in paper.labels,
          str(paper.labels))
    check("a reference over two lines is one key with one space", "eq: step" in paper.refs,
          str(paper.refs))
    check("a macro parameter is not a reference", not any("#" in r for r in paper.refs),
          str(paper.refs))
    check("autoref, cref lists and ref macros are references",
          {"fig:a", "fig:b", "tab:c", "tab:d", "fig:e", "sec:f"} <= paper.refs, str(paper.refs))
    check("href is not a reference", not any("example" in r for r in paper.refs),
          str(paper.refs))
    check("every reference resolves", paper.refs <= set(paper.labels),
          str(paper.refs - set(paper.labels)))
    print("all passed" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
