"""The rows under the verdict of a report: the lines of the extensions, then what was checked."""
from __future__ import annotations

import collections

from schema import findings
from schema.primitives import record

# The name of the row that says what the computational methods ran in (WatchedProject).
METHODS_RAN_IN = "Methods ran in"
# Why a count in the header is zero.
NO_DOCUMENT = "no tracked .tex file has \\documentclass, so the paper checks are off"
EMPTY_REGISTER = "the claim register is empty, so nothing was verified"
CLAIM_CHECKS_NOT_RUN = "a step that builds the claim register failed, so no claim was checked"


@record
class ReportHeaderRow:
    """One row under the verdict: a name, a text, and its indented details as (depth, text).

    The depth of a detail is 1 or 2.
    """
    name: str
    text: str
    details: tuple[tuple[int, str], ...] = ()

    def to_json(self) -> list[object]:
        """The row as JSON: [name, text, [[depth, text], ...]]."""
        return [self.name, self.text, [list(d) for d in self.details]]


class ReportHeaderBuilder:
    """Makes the rows under the verdict: the rows given by the run, then what was checked."""

    def build_header_rows(self, tree: findings.Findings,
                          rows_under_verdict: tuple[ReportHeaderRow, ...]
                          ) -> tuple[ReportHeaderRow, ...]:
        """The rows under the verdict: `rows_under_verdict`, then the counts of the run.

        The "Methods ran in" row is left out: the saved report keeps it (methods_ran_in).
        """
        rows: list[tuple[str, str, list[tuple[int, str]]]] = []
        rows.append(("Documents checked", f"{len(tree.papers)} ({', '.join(tree.papers)})"
                     if tree.papers else f"0 ({NO_DOCUMENT})", []))
        rows.append(("Claims checked",
                     str(len(tree.claims)) if tree.claims or tree.unreadable_claims
                     else f"0 ({CLAIM_CHECKS_NOT_RUN})" if tree.claim_checks_not_run_because
                     else f"0 ({EMPTY_REGISTER})", []))
        if tree.claims:
            rows.append(("Sources", ", ".join(f"{word} {n}"
                                              for word, n in self._source_counts(tree)), []))
        if tree.superseded:
            rows.append(("Superseded, not checked", f"{len(tree.superseded)} ("
                         + ", ".join(c.superseded_label() for c in tree.superseded)
                         + ")", []))
        shown = tuple(r for r in rows_under_verdict if r.name != METHODS_RAN_IN)
        return (*shown,
                *(ReportHeaderRow(name=name, text=text, details=tuple(details))
                  for name, text, details in rows))

    @staticmethod
    def _source_counts(tree: findings.Findings) -> list[tuple[str, int]]:
        """How many claims have each kind of source, the most common first."""
        counts = collections.Counter(c.provenance.report_word for c in tree.claims)
        return sorted(counts.items(), key=lambda kv: -kv[1])
