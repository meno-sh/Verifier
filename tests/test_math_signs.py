#!/usr/bin/env python3
"""Check that the LaTeX reader keeps the math signs and Greek letters that carry a claim.

The reader deletes a LaTeX command it does not know. A sign such as \\leq or \\times, or a
Greek letter such as \\lambda, carries the claim, so it must become its symbol. The cases:
  - each sign and Greek letter becomes its symbol, next to its number;
  - a longer command that starts with a sign's name (\\left, \\infty, \\geq) is not read as
    the shorter sign;
  - an unknown command is still deleted, and \\top or \\pmb is not read as \\to or \\pm;
  - a number right before a Greek letter is still read by `numbers`;
  - a quote with the wrong sign does not match;
  - a paper's sentence candidate keeps the signs, and `numbers` still reads its values;
  - named operators (exp, log, argmin), fractions, roots and set signs survive, since a
    reviewer's error can sit in exactly that operator (ChMLTGRjFcU, 2ySt3cdGfJ, ptTt8mhS7n);
  - a display equation is its own candidate, stays at the end of the sentence that leads
    into it, and does not glue onto the next sentence; a [0]-argument macro and a
    \\DeclareMathOperator name are expanded;
  - a \\ref, \\eqref or \\cref reads as LaTeX prints it (Table 2, (1), Eq. (1)), a table
    takes its number at each caption, and a citation keeps its authors and year from the .bbl
    (a deleted citation hid a reviewer's error in WVX0NNVBBkV);
  - a sentence does not end at "et al.", "e.g." or "Fig.", a footnote is a sentence of its
    own, escaped characters (\\% \\& \\_) read as themselves, and a long sentence is not cut;
  - a sentence carries its section heading and the captions of the tables it names, as
    fields outside its text, and "153,932" is one number, 153932;
  - full-review cases: a list written with commas and no spaces ("256,512,1024") is
    several numbers, and \\frac12, \\sqrt2 and \\frac{1}2 read as (1)/(2), √(2), (1)/(2),
    and an escaped brace inside a fraction does not end its argument;
  - review cases: a spaced line break \\\\[2pt] is not display math, a longtable is numbered
    and is not read as sentences, "Appendix A." ends a sentence, citation years and section
    numbers are not a sentence's values, \\$5 keeps its dollar sign, and unlabelled equations
    numbered within sections keep their own numbers;
  - an unlabelled align line with & gets its number, and a sidewaysfigure is a float.
Returns 1 if any case fails.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from schema import inputs  # noqa: E402
from utils import extract  # noqa: E402

PLAIN = [
    (r"a $2\times$ speedup", "a 2× speedup"),
    (r"$\leq 0.1$ and $\geq 0.9$", "≤ 0.1 and ≥ 0.9"),
    (r"$\le 3$ and $\ge 4$", "≤ 3 and ≥ 4"),
    (r"error $\approx 3$ points", "error ≈ 3 points"),
    (r"$\sim$40 points fewer", "~ 40 points fewer"),
    (r"$-0.21 \neq 0$", "-0.21 ≠ 0"),
    (r"$1.2 \times 10^{4}$ tokens", "1.2 × 10^4 tokens"),
    (r"We set $\lambda = 0.5$ and $\alpha=10^{-3}$", "We set λ = 0.5 and α=10^-3"),
    (r"$\beta_1 = 0.9$, $\beta_2=0.999$", "β_1 = 0.9, β_2=0.999"),
    (r"beats $\Psi(x)$", "beats Ψ(x)"),
    (r"$n \ll d$", "n ≪ d"),
    (r"$\left( a \right)$ and $\infty$", "( a) and ∞"),
    (r"a $\mathcal{L}$ and $\foo$ term", "a L and term"),
    (r"$a \leqslant 0.1$ and $b\lesssim 2$ and $\Upsilon$", "a ≤ 0.1 and b≲ 2 and Υ"),
    (r"$W^\top x$ and $\pmb{x}$ and $a \to b$ and $3 \pm 1$", "W^⊤ x and x and a → b and 3 ± 1"),
    (r"$1 - 2.5 \exp[-k/18]$", "1 - 2.5 exp [-k/18]"),
    (r"$\hat\mu = \argmin_\mu W$ and $\arg\max_x f$", "μ = argmin _ μ W and arg max _x f"),
    (r"$\frac{\partial x_t}{\partial \eta}$", "(∂ x_t)/(∂ η)"),
    (r"$\nabla_z \log p(y|z) \propto \sqrt{k+1}$", "∇_z log p(y|z) ∝ √(k+1)"),
    (r"$Y \cap S = \emptyset$ and $B \cup \{x\}$", "Y ∩ S = ∅ and B ∪ x"),
    (r"$\epsilon = 0.1$ and $\phi$", "ε = 0.1 and φ"),
    (r"$\frac12$ and $\sqrt2$", "(1)/(2) and √(2)"),
    (r"$\frac{1}2$ and $\frac 3 4$", "(1)/(2) and (3)/(4)"),
    (r"$\frac{1}{\sqrt2}$ and $\dfrac\alpha2$", "(1)/(√(2)) and ( α)/(2)"),
    (r"$\frac{a\} b}{c}$ and $\frac{x}{\{y}$", "(a b)/(c) and (x)/( y)"),
]
# A number written right before a Greek letter, and the values `numbers` must still read.
NUMBERS = [
    (r"within $3\sigma$ of the mean", ["3"]),
    (r"a $2\pi$ period and $0.5\lambda$ step", ["2", "0.5"]),
    ("batch sizes 256,512,1024", ["256", "512", "1024"]),
    ("sizes 1234,567 and 0.5,0.6", ["1234", "567", "0.5", "0.6"]),
    ("from 1,024 to 2,048 and 12,345.6 tokens", ["1,024", "2,048", "12,345.6"]),
    ("a 1,000,000 token budget", ["1,000,000"]),
    (r"a half is $\frac12$, not 12", ["1", "2", "12"]),
]
# Quote checks must see the sign: the wrong direction is not the same quote.
QUOTES = [
    ("the loss stays ≥ 0.15", "the loss stays ≤ 0.15", False),
    ("the loss stays ≤ 0.15", "the loss stays ≤ 0.15", True),
    ("a 3× speedup", "a 3 × speedup", True),
    # a quote must not start or end inside a longer number
    ("1.2% accuracy", "We reach 91.2% accuracy", False),
    ("1.2% accuracy", "We reach 1.2% accuracy", True),
    ("accuracy of 9", "an accuracy of 91.2", False),
    ("accuracy of 9", "an accuracy of 9, then 12", True),
    ("accuracy of 9", "an accuracy of 9.", True),
    ("accuracy of 1", "an accuracy of 1,300 runs", False),
    ("300 runs", "we did 1,300 runs", False),
    ("1,300 runs", "we did 1,300 runs", True),
    ("between 0.1", "between 0.1, 0.3 and 0.5", True),
]


def _paper_candidates() -> list[dict]:
    """The candidates of a one-sentence LaTeX paper whose sentence has signs and a Greek letter."""
    tex = (r"\documentclass{article}" "\n" r"\begin{document}" "\n"
           r"With $\tau = 0.9$ the loss stays $\leq 0.15$ at $64\times64$ resolution." "\n"
           r"\end{document}" "\n")
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "paper.tex")
        with open(path, "w") as fh:
            fh.write(tex)
        return extract.candidates(extract.read_latex(path))


EQUATION_PAPER = r"""\documentclass{article}
\DeclareMathOperator*{\argmin}{arg\,min}
\newcommand{\model}[0]{\textsc{WebRL}\xspace}
\begin{document}
When comparing \model{} to DigiRL, both improve over training rounds.
Consequently, the objective can be rewritten as
\begin{equation}
\hat{\mu}_S = \argmin_{\mu} W(\mu, x_T) \label{eq:src}
\end{equation}
where W is the Wasserstein distance between the two task distributions.
\end{document}
"""


REF_PAPER = r"""\documentclass{article}
\begin{document}
\section{Results}\label{sec:res}
\begin{table}\begin{minipage}{.5\textwidth}\caption{One.}\label{tab:a}\end{minipage}
\begin{minipage}{.5\textwidth}\caption{Two.}\label{tab:b}\end{minipage}\end{table}
\begin{equation} y = 2x \label{eq:y} \end{equation}
We beat \citet{zhang20} on Table~\ref{tab:b} by Eq.~\eqref{eq:y}, see \cref{eq:y} and
Section~\ref{sec:res}, as also argued before \citep{zhang20,wong18}.
Accuracy rises by 3.5 points on the held-out split\footnote{The 50\% split follows prior
work exactly.} of the data.
The graph has 153,932 training nodes in all.
\end{document}
"""
REF_BBL = r"""\begin{thebibliography}{2}
\bibitem[{Zhang et~al.(2020)Zhang, Chen, and Li}]{zhang20} H.~Zhang.
\bibitem[Wong \& Kolter(2018)]{wong18} E.~Wong.
\end{thebibliography}
"""


def _ref_checks() -> list[tuple[str, bool, str]]:
    """Cases on a paper whose references and citations must read as LaTeX prints them."""
    with tempfile.TemporaryDirectory() as d:
        for name, text in (("main.tex", REF_PAPER), ("main.bbl", REF_BBL)):
            with open(os.path.join(d, name), "w") as fh:
                fh.write(text)
        cands = extract.candidates(extract.read_latex(os.path.join(d, "main.tex")))
    sents = " ".join(c["text"] for c in cands if c["kind"] == "sentence")
    caps = [c["text"] for c in cands if c["kind"] == "table caption"]
    eqs = [c["text"] for c in cands if c["kind"] == "equation"]
    return [
        ("a reference reads as LaTeX prints it",
         "on Table 2 by Eq. (1), see Eq. (1) and Section 1" in sents, sents),
        ("a citation keeps its authors and year, and 'et al.' does not end the sentence",
         "We beat Zhang et al. (2020)" in sents
         and "(Zhang et al., 2020; Wong & Kolter, 2018)" in sents, sents),
        ("two captions in one float are two tables", caps == ["Table 1: One.", "Table 2: Two."],
         str(caps)),
        ("an equation carries its number", eqs == ["Eq. (1): y = 2x"], str(eqs)),
        ("a sentence carries its section and the caption of the table it names",
         any(c["kind"] == "sentence" and c["text"].startswith("We beat")
             and c.get("section") == "Results" and c.get("referenced") == ["Table 2: Two."]
             for c in cands), str([(c.get("section"), c.get("referenced")) for c in cands
                                   if c["kind"] == "sentence"])),
        ("a number with a thousands separator is one number",
         any(c.get("values") == ["153,932"]
             and inputs.PrintedFloat(c["values"][0]) == 153932.0 for c in cands),
         str([c.get("values") for c in cands])),
        ("a footnote is its own sentence and an escaped percent reads as %",
         any(c["text"] == "The 50% split follows prior work exactly." for c in cands
             if c["kind"] == "sentence")
         and any(c["text"].startswith("Accuracy rises by 3.5 points on") for c in cands
                 if c["kind"] == "sentence"), sents),
    ]


REVIEW_PAPER = r"""\documentclass{article}
\numberwithin{equation}{section}
\begin{document}
\section{One}
\begin{table}\caption{Short.}\label{tab:s}
\begin{tabular}{lc} a & b \\[2pt] c & d \\ \end{tabular}\end{table}
\begin{equation} a = 1 \end{equation}
\begin{equation} b = 2 \label{eq:b} \end{equation}
Details are in Appendix A. We then train the model for long and it costs \$5 per run.
As shown in Section~\ref{sec:two}, X beats Y \citep{zhang20} by 3 points.
\section{Two}\label{sec:two}
\begin{equation} c = 3 \end{equation}
\[ z = 9 \]
\begin{longtable}{lc}\caption{Long results.}\label{tab:long}\\ Model & Acc \\ Ours & 0.912 \\
\end{longtable}
See \cref{tab:long} and \cref{tab:s}.
\end{document}
"""


def _review_checks() -> list[tuple[str, bool, str]]:
    """Cases from the code review of the parser changes."""
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "main.tex"), "w") as fh:
            fh.write(REVIEW_PAPER)
        cands = extract.candidates(extract.read_latex(os.path.join(d, "main.tex")))
    sents = [c for c in cands if c["kind"] == "sentence"]
    texts = [c["text"] for c in sents]
    eqs = sorted(c["text"] for c in cands if c["kind"] == "equation")
    return [
        ("a spaced line break is not display math, and labels after it keep their numbers",
         any("See Table 2 and Table 1" in x for x in texts), str(texts)),
        ("a longtable's rows are not sentences", not any("0.912" in x for x in texts), str(texts)),
        ("'Appendix A.' ends a sentence", "Details are in Appendix A." in texts, str(texts)),
        ("an escaped dollar keeps its sign", any("costs $5 per run" in x for x in texts),
         str(texts)),
        ("citation years and section numbers are not a sentence's values",
         any(c["text"].startswith("As shown in Section 2") and c["values"] == ["3"]
             for c in sents), str([(c["text"], c["values"]) for c in sents])),
        ("unlabelled equations numbered within sections keep their own numbers",
         eqs == ["Eq. (1.1): a = 1", "Eq. (1.2): b = 2", "Eq. (2.1): c = 3", "z = 9"], str(eqs)),
    ]


def _equation_checks() -> list[tuple[str, bool, str]]:
    """Cases on a paper with a display equation and two kinds of macro."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "main.tex")
        with open(path, "w") as fh:
            fh.write(EQUATION_PAPER)
        cands = extract.candidates(extract.read_latex(path))
    eqs = [c for c in cands if c["kind"] == "equation"]
    sents = [c["text"] for c in cands if c["kind"] == "sentence"]
    return [
        ("a display equation is its own candidate, with its label",
         len(eqs) == 1 and eqs[0]["label"] == "eq:src" and "argmin" in eqs[0]["text"], str(eqs)),
        ("the equation ends the sentence that leads into it",
         any(s.startswith("Consequently") and "argmin" in s for s in sents), str(sents)),
        ("the sentence after the equation does not carry it",
         any(s.startswith("where W is") and "argmin" not in s for s in sents), str(sents)),
        ("a [0]-argument macro is expanded",
         any("comparing WebRL to DigiRL" in s for s in sents), str(sents)),
    ]


FLOAT_PAPER = r"""\documentclass{article}
\begin{document}
\section{Intro}
We have
\begin{align} x &= y + 1 \\ z &= 2 \end{align}
and then
\begin{equation} a = b + c \end{equation}
A sideways figure follows.
\begin{sidewaysfigure}\centering The figure body prints 0.123 and is not prose.
\caption{Sideways results.}\label{fig:side}\end{sidewaysfigure}
\end{document}
"""


def _float_checks() -> list[tuple[str, bool, str]]:
    """Unlabelled align lines with & are numbered; a sidewaysfigure is a float."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "main.tex")
        with open(path, "w") as fh:
            fh.write(FLOAT_PAPER)
        paper = extract.read_latex(path)
    numbers = [e.number for e in paper.equations]
    return [
        ("unlabelled align lines with & get their numbers", numbers == ["1", "2", "3"],
         str(numbers)),
        ("a sidewaysfigure's body is not prose",
         not any("0.123" in s for s in paper.sentences), str(paper.sentences)),
        ("a sidewaysfigure's caption is a figure caption",
         [(c.kind, c.text) for c in paper.captions] == [("figure", "Sideways results.")],
         str(paper.captions)),
    ]


def main() -> int:
    """Run the cases and print each failure. Returns 1 if any case fails."""
    failures = []
    for latex, want in PLAIN:
        got = extract._plain(latex)
        if got != want:
            failures.append(f"_plain({latex!r}) = {got!r}, want {want!r}")
    for latex, want in NUMBERS:
        got = extract.numbers(extract._plain(latex))
        if got != want:
            failures.append(f"numbers(_plain({latex!r})) = {got!r}, want {want!r}")
    for quote, text, want in QUOTES:
        if extract.quote_in(quote, [text]) != want:
            failures.append(f"quote_in({quote!r}, [{text!r}]) is not {want}")
    sents = [c for c in _paper_candidates() if c["kind"] == "sentence"]
    want = "With τ = 0.9 the loss stays ≤ 0.15 at 64×64 resolution."
    if [c["text"] for c in sents] != [want]:
        failures.append(f"sentence candidates {[c['text'] for c in sents]!r}, want [{want!r}]")
    elif sents[0]["values"] != ["0.9", "0.15", "64", "64"]:
        failures.append(f"values {sents[0]['values']!r}, want ['0.9', '0.15', '64', '64']")
    eq_cases = _equation_checks() + _ref_checks() + _review_checks() + _float_checks()
    failures += [f"{name}: {detail}" for name, ok, detail in eq_cases if not ok]
    for f in failures:
        print("FAIL", f)
    total = len(PLAIN) + len(NUMBERS) + len(QUOTES) + 1 + len(eq_cases)
    print(f"{total - len(failures)} of {total} cases pass")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
