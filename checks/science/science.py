"""Scientific rigour --- is the evidence good enough to carry the claim?

Distinct from `consistency` (formatting and basic mistakes) and from `model_review`
(whether the claims follow from what was measured). This asks a narrower question:
where the paper asserts something quantitative, is there uncertainty behind it?

It reads the paper alone, and answers whether uncertainty is reported at all,
whether significance claims have anything behind them, and how many comparisons
are made without correction. Whether a recomputed interval supports a claim is the
`interval` claim check.
"""
import re
from collections.abc import Iterable, Sequence

from checks import interface
from schema.primitives import CheckResult, Problem, ProblemKind, Severity

# The kinds this group reports, one check per kind. The evidence may be weaker than the
# wording. Each kind is found by the words of the paper alone, and often the sentence is
# not a statistical claim, so every one is a caveat.
COMPARISON_WITHOUT_UNCERTAINTY = ProblemKind(code="comparison-without-uncertainty",
                                             severity=Severity.CAVEAT)
SIGNIFICANCE_UNSUPPORTED = ProblemKind(code="significance-unsupported", severity=Severity.CAVEAT)
UNCORRECTED_MULTIPLE_COMPARISONS = ProblemKind(code="uncorrected-multiple-comparisons",
                                               severity=Severity.CAVEAT)
KINDS = (COMPARISON_WITHOUT_UNCERTAINTY, SIGNIFICANCE_UNSUPPORTED, UNCORRECTED_MULTIPLE_COMPARISONS)

COMPARATIVE = re.compile(
    r"\b(outperform\w*|beats?|better than|worse than|improve[sd]?|reduce[sd]?|"
    r"increase[sd]?|decrease[sd]?|higher than|lower than|superior|matches?)\b", re.I)
SIGNIF = re.compile(r"\bsignifican(?:t|tly|ce)\b", re.I)
# An interval, a p-value, a standard deviation, or an explicit sample size.
UNCERTAINTY = re.compile(r"\[|\bp\s*[=<>]|\\pm|±|\bCI\b|confidence interval|"
                         r"\bs\.?d\.?\b|\bstd\b|\bn\s*=", re.I)
NUM = re.compile(r"(?<![\w.])(\d\.\d{2,4})(?![\w])")


def _comparisons_without_uncertainty(sents: Iterable[str]) -> list[Problem]:
    """Comparative claims carrying no interval, p-value, spread or sample size."""
    found = []
    for s in sents:
        if COMPARATIVE.search(s) and NUM.search(s) and not UNCERTAINTY.search(s):
            found.append(Problem(kind=COMPARISON_WITHOUT_UNCERTAINTY, detail=(
                f"comparative claim with no interval, p-value or n -- \"{s[:88]}...\"")))
    return found


def _significance_without_evidence(sents: Iterable[str]) -> list[Problem]:
    """Significance asserted with nothing behind it in the same sentence."""
    found = []
    for s in sents:
        if SIGNIF.search(s) and not UNCERTAINTY.search(s):
            found.append(Problem(kind=SIGNIFICANCE_UNSUPPORTED, detail=(
                f"significance claimed with no interval or p-value -- \"{s[:88]}...\"")))
    return found


def _many_comparisons(sents: Sequence[str]) -> list[Problem]:
    """Flag a paper making many comparisons with no mention of correcting for them.

    Not an accusation. With enough comparisons some will clear a threshold by
    chance, and a reader deserves to know how many were made.
    """
    n = sum(1 for s in sents if COMPARATIVE.search(s) and NUM.search(s))
    corrected = any(re.search(r"bonferroni|holm|benjamini|false discovery|"
                              r"multiple comparison|family-wise", s, re.I) for s in sents)
    if n >= 12 and not corrected:
        return [Problem(kind=UNCORRECTED_MULTIPLE_COMPARISONS, detail=(
            f"{n} comparative numeric claims and no mention of correcting "
            f"for multiple comparisons"))]
    return []


# Not checked yet: selective reporting.
#
# Selective reporting matters: an experiment was run, the result was not liked, and
# it was quietly left out of the paper. It is invisible from the paper alone. We do
# not yet have a check for it that is both fair and useful, so there is deliberately
# no check rather than a weak one.
#
# Why the earlier version was switched off (2026-10-13):
#   - it only saw conditions the author chose to list in the claim register, so a
#     result someone meant to hide was never listed and passed, while an author who
#     listed everything was flagged;
#   - it matched the condition's label string against the LaTeX verbatim, so a run the
#     paper describes in words still counted as unreported;
#   - it filed every hit as an integrity failure (REJECT), although conditions are often
#     left out for honest reasons, such as a setup that turned out to be wrong and
#     was never cleaned out of the codebase.
#
# What a real solution needs, still open: an independent inventory of what was run
# (the run folder, W&B, job logs), not the author's own list; a way to match runs
# to what the paper describes; and a fixed list of acceptable reasons for leaving a
# run out, so honest exclusions are recorded rather than punished.


def run(sents: tuple[str, ...]) -> dict[str, CheckResult]:
    """Run the rigour checks on the paper's body sentences: one result per check, by check name."""
    found = (_comparisons_without_uncertainty(sents) + _significance_without_evidence(sents)
             + _many_comparisons(sents))
    return interface.PaperCheckGroupResultBuilder(KINDS).results_from_problems(found)


class ScienceChecks(interface.PaperCheckGroup):
    """The checks of scientific rigour: is there uncertainty behind each quantitative claim?"""
    name = "science"
    title = "Scientific rigour"
    why = "Asks whether the evidence is good enough to carry the claim."
    problem_kinds = KINDS

    def run(self, context: interface.PaperContext) -> interface.PaperOutcome:
        """The results of the group; it counts no facts."""
        return interface.PaperOutcome(checks=run(context.paper.sentences), facts={})
