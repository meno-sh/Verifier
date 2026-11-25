"""Disputed flags: the flags that a dispute says are false, and the verdict without them.

The core does not know who disputes a flag or why the Verifier raised it. An extension gives
the disputes through its `disputed_flags` hook (core/registry/extensions.py). Here they only change
what the report shows: each disputed flag is struck out, and the report also gives the
verdict of a copy of the tree with the disputed flags left out. The verdict with all flags
does not change, so neither do the exit codes.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping

from core.analysis import verdict_policy
from core.report_model.issue_collector import IssueCollector
from core.report_model.issues import FlagDispute, FlagPlaceInTree, IssueIdentity
from schema import findings
from schema.primitives import CheckResult, Flagged, Passed, record
from schema.verdict import Verdict


@record
class DisputedFlag(IssueIdentity):
    """One flag that a dispute says is false: the flag and the dispute.

    The flag is named by its code, subject and detail, as in the report the dispute was
    raised on. It applies to an issue of a later report only when all three are the same,
    so a changed detail is a new flag that nobody has disputed.
    """
    dispute: FlagDispute


class DisputedFlagSet:
    """The disputed flags of one run, and what they change: the findings and the verdict."""

    def __init__(self, disputed_flags: Iterable[DisputedFlag],
                 policy: verdict_policy.VerdictPolicy) -> None:
        """Hold the disputed flags, and compute verdicts with `policy`."""
        self.disputed_flags = tuple(disputed_flags)
        self.policy = policy

    def places_in_findings(self, tree: findings.Findings) -> list[FlagPlaceInTree]:
        """Where each disputed flag is in the tree. A flag nobody can dispute has no place."""
        named = {d.identity() for d in self.disputed_flags}
        return [li.place for li in IssueCollector().collect_issues(tree)
                if li.place is not None and li.issue.identity() in named]

    def findings_without_disputed_flags(self, tree: findings.Findings) -> findings.Findings:
        """A copy of the tree without the disputed flags.

        A disputed problem of a check leaves its Flagged result; a result with no problem
        left becomes Passed. A disputed gate flag (the number or the file of the claim
        failed) leaves the claim out of the copy.
        """
        found = self.places_in_findings(tree)
        gone = {p.claim for p in found if p.check is None}
        removed: dict[tuple[str | int | None, str], set[int]] = {}
        for p in found:
            if p.check is not None and p.index is not None:
                removed.setdefault((p.paper if p.paper is not None else p.claim, p.check),
                                   set()).add(p.index)
        papers = {path: dataclasses.replace(
                      paper, checks=self._results_without_problems(paper.checks, path, removed))
                  for path, paper in tree.papers.items()}
        claims = tuple(dataclasses.replace(
                           c, checks=self._results_without_problems(c.checks, n, removed))
                       for n, c in enumerate(tree.claims) if n not in gone)
        return dataclasses.replace(tree, papers=papers, claims=claims)

    @staticmethod
    def _results_without_problems(checks: Mapping[str, CheckResult], where: str | int,
                                  removed: Mapping[tuple[str | int | None, str], set[int]]
                                  ) -> dict[str, CheckResult]:
        """The check results of one document or claim, without the problems in `removed`."""
        out: dict[str, CheckResult] = {}
        for name, result in checks.items():
            indexes = removed.get((where, name))
            if indexes and isinstance(result, Flagged):
                left = tuple(p for k, p in enumerate(result.problems) if k not in indexes)
                result = (Flagged(problems=left, measured=result.measured) if left
                          else Passed(measured=result.measured))
            out[name] = result
        return out

    def verdict_without_disputed_flags(self, tree: findings.Findings) -> Verdict | None:
        """The verdict without the disputed flags, or None when no flag of the tree is disputed.

        A claim left out for a disputed gate flag had no other check run on it, so the
        verdict without it is PASS WITH CAVEATS at best.
        """
        found = self.places_in_findings(tree)
        if not found:
            return None
        outcome = self.policy.verdict_for(self.findings_without_disputed_flags(tree))
        if any(p.check is None for p in found):
            order = list(Verdict)
            outcome = min(outcome, Verdict.PASS_WITH_CAVEATS, key=order.index)
        return outcome
