"""Checks the paper against itself, without external data.

These checks examine formatting and bookkeeping only.
Invariant checks hold for any quantitative paper. Incident checks come from failures in a
real audit, so they find only known mistakes. Each check is a deterministic predicate, so a
rerun cannot change the verdict.
science.py and model_review.py examine evidence.
"""
import bisect
import re
from collections.abc import Collection, Iterable, Sequence
from typing import Protocol

from checks import interface
from schema import inputs
from schema.primitives import Problem, ProblemKind, Severity
from utils import extract

# The kinds this group reports, one check per kind. The paper misdescribes its own
# contents. Each one is a caveat: the report gives it under "Caveats", and it does not block.
DUPLICATE_LABEL = ProblemKind(code="duplicate-label", severity=Severity.CAVEAT)
INTERVAL_REVERSED = ProblemKind(code="interval-reversed", severity=Severity.CAVEAT)
VALUE_OUTSIDE_OWN_INTERVAL = ProblemKind(code="value-outside-own-interval",
                                         severity=Severity.CAVEAT)
ABSTRACT_NUMBER_UNSUPPORTED = ProblemKind(code="abstract-number-unsupported",
                                          severity=Severity.CAVEAT)
MODEL_NOT_IN_ANY_TABLE = ProblemKind(code="model-not-in-any-table", severity=Severity.CAVEAT)
NUMBER_NOT_IN_ANY_TABLE = ProblemKind(code="number-not-in-any-table",
                                      severity=Severity.CAVEAT)
DANGLING_REF = ProblemKind(code="dangling-ref", severity=Severity.CAVEAT)
ORPHAN_FLOAT = ProblemKind(code="orphan-float", severity=Severity.CAVEAT)
ROW_WIDTH = ProblemKind(code="row-width", severity=Severity.CAVEAT)
CAPTION_MODEL_MISMATCH = ProblemKind(code="caption-model-mismatch", severity=Severity.CAVEAT)
KINDS = (
    DUPLICATE_LABEL, INTERVAL_REVERSED, VALUE_OUTSIDE_OWN_INTERVAL, ABSTRACT_NUMBER_UNSUPPORTED,
    MODEL_NOT_IN_ANY_TABLE, NUMBER_NOT_IN_ANY_TABLE, DANGLING_REF, ORPHAN_FLOAT, ROW_WIDTH,
    CAPTION_MODEL_MISMATCH,
)
# The checks that need to see every table, so are Skipped when an \input file is unreadable.
NEEDS_EVERY_TABLE = (
    ABSTRACT_NUMBER_UNSUPPORTED, MODEL_NOT_IN_ANY_TABLE,
    NUMBER_NOT_IN_ANY_TABLE, ORPHAN_FLOAT, ROW_WIDTH,
    CAPTION_MODEL_MISMATCH,
)

# An interval as typeset in this literature: [+0.05,\, +0.21] or [-0.33, -0.25].
# the separator is required, so a citation such as "[21]" is not the interval [2, 1]
SEP = r"\s*(?:,\s*(?:\\,)?|\\,)\s*"      # "," or "\," or ",\,"
INTERVAL_PAT = re.compile(r"\[\s*([+-]?\d*\.?\d+)" + SEP + r"([+-]?\d*\.?\d+)\s*\]")
# A value immediately followed by its own interval.
VALUE_INTERVAL_PAT = re.compile(
    r"([+-]?\d*\.\d+)\s*\\?\s*\[\s*([+-]?\d*\.?\d+)" + SEP + r"([+-]?\d*\.?\d+)\s*\]")

class Flag(Protocol):
    """The function that each check below calls to report one problem."""

    def __call__(self, kind: ProblemKind, detail: str,
                 facts: tuple[tuple[str, str], ...] = ()) -> None:
        """Record one problem, with the parts of its detail as (label, text) facts."""


def _models_in_tables(tabs: Iterable[extract.PaperTable]) -> set[str]:
    """Every model name a table names, in its rows or its caption.

    The text's claim about a model needs some table that reports that model, and a
    table often names its model only in the caption ("All rows are Llama-3.1-8B
    except the marked Qwen3-32B row"), with rows labelled by method. Whether the
    caption names the rows' model correctly is a separate question, answered by
    `_check_caption_models`.
    """
    found = set()
    for t in tabs:
        found |= set(t.row_models) | set(t.caption_models)
    return found


def _numbers_in_tables(tabs: Iterable[extract.PaperTable]) -> list[inputs.PrintedFloat]:
    """Every number printed anywhere in any table, captions included, sorted by value.

    These are all printed numbers (extract.numbers), not only those with 3 or 4 decimals,
    so a table that prints 71.3 holds a number for the text's 0.713.
    """
    found = {n for t in tabs
             for n in extract.numbers(t.caption + " " + " ".join(" ".join(r) for r in t.rows))}
    return sorted((inputs.PrintedFloat(n) for n in found), key=float)


def _could_be_in_a_table(number: inputs.PrintedFloat,
                         table_numbers: Sequence[inputs.PrintedFloat],
                         values: Sequence[float]) -> bool:
    """Whether a table number could be `number` (could_be_same_value_at_similar_precision).

    `table_numbers` is sorted by value, and `values` holds their values. An interval is
    at most 1 wide, so only the table numbers within 1 of the number at each scale can
    match.
    """
    for scale in inputs.PrintedFloat.SCALES:
        low, high = sorted((float(number) * scale - 1, float(number) * scale + 1))
        start = bisect.bisect_left(values, low)
        end = bisect.bisect_right(values, high)
        if any(number.could_be_same_value_at_similar_precision(t)
               for t in table_numbers[start:end]):
            return True
    return False


# --------------------------------------------------------------- invariants

def _check_duplicate_labels(labels: Iterable[str], flag: Flag) -> None:
    """Flag each label or Markdown anchor that the paper defines more than once."""
    seen, dupes = set(), set()
    for label in labels:
        if label in seen:
            dupes.add(label)
        seen.add(label)
    for label in sorted(dupes):
        flag(DUPLICATE_LABEL,
             f"label {label!r} is defined more than once; references to it are ambiguous")


def _is_citation(low: str, high: str) -> bool:
    """True if both bounds after a decimal value are whole numbers, as in "0.80 [3, 12]".

    VALUE_INTERVAL_PAT reads only a value with a decimal point. An interval of such a
    value has a decimal point or a sign in its bounds, and a citation list has neither.
    """
    return low.isdigit() and high.isdigit()


def _check_intervals(full: str, flag: Flag) -> None:
    """An interval must not run backwards, and a value must lie inside its own interval.

    A citation list after a value, such as "0.80 [3, 12]", is not an interval
    (`_is_citation`).
    """
    for m in INTERVAL_PAT.finditer(full):
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo > hi:
            flag(INTERVAL_REVERSED,
                 f"interval [{m.group(1)}, {m.group(2)}] has its lower bound above its upper")
    for m in VALUE_INTERVAL_PAT.finditer(full):
        if _is_citation(m.group(2), m.group(3)):
            continue
        val, lo, hi = float(m.group(1)), float(m.group(2)), float(m.group(3))
        if lo <= hi and not (lo <= val <= hi):
            flag(VALUE_OUTSIDE_OWN_INTERVAL,
                 f"{m.group(1)} is reported outside its own interval "
                 f"[{m.group(2)}, {m.group(3)}]")


def _check_abstract_supported(paper: extract.Paper, flag: Flag) -> None:
    """Every number in the abstract should appear somewhere in the body.

    An abstract quoting a figure the body never states leaves a reader with nothing
    to check it against. This is the general form of the headline-versus-table
    failure, and it does not depend on knowing what the paper is about.
    """
    if not paper.abstract:
        return
    abs_nums = set(extract.NUM_PAT.findall(paper.abstract))
    if not abs_nums:
        return
    body_nums = set(extract.NUM_PAT.findall(paper.after_abstract))
    for n in sorted(abs_nums - body_nums):
        flag(ABSTRACT_NUMBER_UNSUPPORTED,
             f"{n} appears in the abstract but nowhere in the body of the paper")


# --------------------------------------------------------------- incident-derived

def _check_models_have_a_table(statements: Iterable[extract.PaperStatement],
                               table_models: Collection[str], flag: Flag) -> None:
    """A numeric claim about a model that no table names, in its rows or caption."""
    for s in statements:
        if not s.numbers:
            continue
        for model in s.models:
            if model not in table_models:
                flag(MODEL_NOT_IN_ANY_TABLE,
                     f"the text claims a number for {model!r}, which no table "
                     f"names -- \"{s.text[:84]}...\"")


def _check_numbers_have_a_table(statements: Iterable[extract.PaperStatement],
                                table_numbers: Sequence[inputs.PrintedFloat],
                                flag: Flag) -> None:
    """A number stated in the text that no table number could be (_could_be_in_a_table).

    0.713 in the text matches 71.3 in a table, and 0.7128 matches 0.713, because each
    printed number stands for the values that round to it.
    """
    values = [float(t) for t in table_numbers]
    for s in statements:
        for n in s.numbers:
            if not _could_be_in_a_table(inputs.PrintedFloat(n), table_numbers, values):
                flag(NUMBER_NOT_IN_ANY_TABLE,
                     f"{n} is stated in the text but appears in no table "
                     f"-- \"{s.text[:84]}...\"",
                     facts=(("Number", n), ("Sentence", f"{s.text[:84]}...")))


def _check_dangling_refs(targets: frozenset[str], refs: frozenset[str], flag: Flag) -> None:
    r"""Flag each \ref without a \label and each Markdown link to an undefined anchor.

    `targets` holds the labels and anchors of this document and of the project's others.
    """
    for r in sorted(refs - targets):
        flag(DANGLING_REF, f"the reference to {r!r} resolves to nothing")


def _check_structure(tabs: Iterable[extract.PaperTable], labels: set[str], refs: frozenset[str],
                     flag: Flag) -> None:
    """Flag floats without references and table rows with the wrong number of cells.

    The rows of one table with the same wrong width are one flag, so one table whose
    header declares a column it never fills does not crowd out the other flags.
    """
    for label in sorted(x for x in labels - refs if x.startswith(("tab:", "fig:"))):
        flag(ORPHAN_FLOAT, f"{label} is never referenced in the text")
    for t in tabs:
        by_width: dict[int, list[int]] = {}
        for i, row in enumerate(t.rows):
            if t.ncol and len(row) != t.ncol and "multicolumn" not in " ".join(row):
                by_width.setdefault(len(row), []).append(i + 1)
        for width, rows in by_width.items():
            which = (f"row {rows[0]} has" if len(rows) == 1
                     else f"rows {_number_ranges(rows)} have")
            flag(ROW_WIDTH, f"{t.name}: {which} {width} cells, header declares {t.ncol}")


def _number_ranges(numbers: list[int]) -> str:
    """Sorted numbers as ranges: [2, 3, 4, 7] gives "2-4, 7"."""
    spans: list[list[int]] = []
    for n in numbers:
        if spans and n == spans[-1][1] + 1:
            spans[-1][1] = n
        else:
            spans.append([n, n])
    return ", ".join(f"{a}-{b}" if a != b else f"{a}" for a, b in spans)


def _check_caption_models(tabs: Iterable[extract.PaperTable], flag: Flag) -> None:
    """A caption naming a model that none of its own rows carry.

    Only when every data row names its model: if some rows name none, the caption
    may be what says which model they are, so it cannot be told wrong from the rows.
    """
    for t in tabs:
        caption_models = set(t.caption_models)
        if not t.row_models or t.unnamed_rows:
            continue
        for m in caption_models - set(t.row_models):
            if len(t.row_models) == 1:
                flag(CAPTION_MODEL_MISMATCH,
                     f"{t.name}: caption names {m!r} but every row is "
                     f"{t.row_models[0]!r}")


def run(paper: extract.Paper, elsewhere: frozenset[str] = frozenset()) -> interface.PaperOutcome:
    """Run all consistency checks on one paper.

    Args:
        paper: the document.
        elsewhere: the labels and anchors of the project's other documents. A reference
            to one of them is not dangling.

    Returns:
        One result for each check, and the counts of tables and numeric sentences.
    """
    tabs, stmts = paper.tables, paper.statements
    problems: list[Problem] = []

    def flag(kind: ProblemKind, detail: str, facts: tuple[tuple[str, str], ...] = ()) -> None:
        """Record one problem; every check below reports through this."""
        problems.append(Problem(kind=kind, detail=detail, facts=facts))

    _check_duplicate_labels(paper.labels, flag)
    _check_intervals(paper.text, flag)
    _check_dangling_refs(paper.targets | elsewhere, paper.refs, flag)
    _check_abstract_supported(paper, flag)
    _check_models_have_a_table(stmts, _models_in_tables(tabs), flag)
    _check_numbers_have_a_table(stmts, _numbers_in_tables(tabs), flag)
    _check_structure(tabs, set(paper.labels), paper.refs, flag)
    _check_caption_models(tabs, flag)

    skipped = {}
    if extract.PaperFormat.LATEX not in paper.formats:
        skipped[ORPHAN_FLOAT] = "a Markdown table or figure has no label to refer to"
    if paper.unreadable:
        skipped |= {kind: "an \\input file could not be read" for kind in NEEDS_EVERY_TABLE}
    return interface.PaperOutcome(
        checks=interface.PaperCheckGroupResultBuilder(KINDS).results_from_problems(
            problems, skipped),
        facts={"tables": len(tabs), "numeric_sentences": len(stmts)})


class ConsistencyChecks(interface.PaperCheckGroup):
    """The checks of the paper against itself: formatting and bookkeeping."""
    name = "checks"
    title = "Consistency"
    why = "Checks the paper against itself, without external data."
    problem_kinds = KINDS

    def run(self, context: interface.PaperContext) -> interface.PaperOutcome:
        """The results of the group, and the two counts it read."""
        return run(context.paper, context.elsewhere)
