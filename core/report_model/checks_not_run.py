"""The claim checks that did not run, and the table of them in the report."""
from __future__ import annotations

import collections
from collections.abc import Mapping
from typing import ClassVar

from schema import findings
from schema.primitives import NotChecked, Skipped, read_only, record


@record
class CheckNotRunOnClaim:
    """One claim check that did not run on one claim, and why."""
    check_name: str
    claim_id: str
    reason: str


@record
class ChecksNotRunRow:
    """One row of "Checks that did not run": a check, why it did not run, and on how many claims."""
    check_name: str
    reason: str
    claim_count: int


@record
class ChecksNotRunTable:
    """The rows of "Checks that did not run", and the claims of each check with many reasons.

    The rows have the most claims first. A check with more than MAX_REASONS reasons gets
    one row with its total, and its claims are listed apart, each with its reason.
    """
    rows: tuple[ChecksNotRunRow, ...]
    claims_of_checks_with_many_reasons: Mapping[str, tuple[CheckNotRunOnClaim, ...]]


class ChecksNotRunCollector:
    """Lists the claim checks that did not run, and makes the rows of their table."""
    # A check that did not run for more than this many reasons gets one row, and its claims
    # are listed apart, each with its reason.
    MAX_REASONS: ClassVar[int] = 3

    @staticmethod
    def checks_not_run_per_claim(tree: findings.Findings) -> list[CheckNotRunOnClaim]:
        """Each claim check that did not run on a claim whose gate did not fail.

        A check skipped because the gate of the claim failed is left out: that claim is
        already reported as REJECT.
        """
        out = []
        for c in tree.claims:
            if c.gate_failed:
                continue
            for name, result in c.checks.items():
                if isinstance(result, NotChecked | Skipped):
                    out.append(CheckNotRunOnClaim(check_name=name, claim_id=c.claim.id,
                                                  reason=result.describe_for_reader()))
        return out

    def table(self, tree: findings.Findings) -> ChecksNotRunTable:
        """The rows of the table of the checks that did not run."""
        return self.table_of_checks_not_run(self.checks_not_run_per_claim(tree))

    @classmethod
    def table_of_checks_not_run(cls, not_run: list[CheckNotRunOnClaim]) -> ChecksNotRunTable:
        """The rows of the table of these checks that did not run."""
        counted = collections.Counter((n.check_name, n.reason) for n in not_run)
        reasons = collections.Counter(name for name, _ in counted)
        many = [name for name, k in reasons.items() if k > cls.MAX_REASONS]
        rows = [ChecksNotRunRow(check_name=name, reason=reason, claim_count=n)
                for (name, reason), n in counted.items() if name not in many]
        rows += [ChecksNotRunRow(
                     check_name=name,
                     reason=f"{reasons[name]} different reasons; see each claim below",
                     claim_count=sum(n for (check, _), n in counted.items() if check == name))
                 for name in many]
        per_claim = {name: tuple(n for n in not_run if n.check_name == name) for name in many}
        return ChecksNotRunTable(
            rows=tuple(sorted(rows, key=lambda r: (-r.claim_count, r.check_name, r.reason))),
            claims_of_checks_with_many_reasons=read_only(per_claim))
