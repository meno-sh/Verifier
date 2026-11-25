"""Giving each issue its flag id, and sorting the issues into the sections of the report."""
from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
from collections.abc import Iterable, Sequence
from typing import ClassVar

from core.report_model.axis import Axis
from core.report_model.disputed_flags import DisputedFlag
from core.report_model.issues import (
    ClaimIssueGroup,
    Issue,
    IssueGroup,
    IssueIdentity,
    LocatedIssue,
    ReportSection,
    ReportSectionKind,
)
from schema import inputs


class FlagIdAssigner:
    """Gives each issue its flag id.

    A flag id is a prefix and the start of the SHA-256 of the code, subject and detail of
    the flag: FLAG_ID_LENGTH hex characters, or FLAG_ID_LENGTH_ON_COLLISION when two flags
    of one report would get the same id.
    """
    FLAG_ID_PREFIX: ClassVar[str] = "F-"
    FLAG_ID_LENGTH: ClassVar[int] = 6
    FLAG_ID_LENGTH_ON_COLLISION: ClassVar[int] = 10

    def assign_flag_ids(self, issues: list[Issue]) -> list[str]:
        """The flag id of each issue, in order.

        The id depends only on the code, subject and detail of the issue, so the same flag
        has the same id in every report. Issues that would share an id in this list get a
        longer one. Two issues that are the same in all three get the same id.
        """
        digests = [self._sha256_of_issue(i) for i in issues]
        short = collections.Counter(d[:self.FLAG_ID_LENGTH] for d in set(digests))
        return [self.FLAG_ID_PREFIX
                + d[:self.FLAG_ID_LENGTH_ON_COLLISION if short[d[:self.FLAG_ID_LENGTH]] > 1
                    else self.FLAG_ID_LENGTH]
                for d in digests]

    @staticmethod
    def _sha256_of_issue(issue: Issue) -> str:
        """The SHA-256 of the code, subject and detail of the issue, in hex."""
        identity = issue.identity()
        text = json.dumps([identity.code, identity.subject, identity.detail])
        return hashlib.sha256(text.encode()).hexdigest()


class IssueSectioner:
    """Sorts the issues into the sections of the report, and marks the disputed ones."""

    def __init__(self, flag_id_assigner: FlagIdAssigner) -> None:
        """Sort the issues, with flag ids from `flag_id_assigner`."""
        self.flag_id_assigner = flag_id_assigner

    def sort_into_sections(self, located: list[LocatedIssue], claims: Sequence[inputs.Claim],
                           disputed_flags: Iterable[DisputedFlag] = ()
                           ) -> tuple[ReportSection, ...]:
        """The three sections of the report, in ReportSectionKind order, each maybe empty.

        An issue on the caveat axis is a caveat. Another issue whose flag is at claim n of
        the findings is an issue of `claims[n]`. All other issues are setup issues. The
        claims section has one group for each claim with an issue, in register order, and
        the issues of each claim are most severe first. The other two sections group their
        issues by type (group_by_type). Each issue has its flag id, and the dispute in
        `disputed_flags` that names it, if its flag can be disputed.
        """
        issues = [li.issue for li in located]
        by_identity: dict[IssueIdentity, DisputedFlag] = {
            d.identity(): d for d in disputed_flags}
        setup: list[Issue] = []
        caveats: list[Issue] = []
        of_claim: dict[int, list[Issue]] = {}
        for li, flag in zip(located, self.flag_id_assigner.assign_flag_ids(issues), strict=True):
            d = by_identity.get(li.issue.identity()) if li.place is not None else None
            issue = dataclasses.replace(li.issue, flag_id=flag, disputable=li.place is not None,
                                        dispute=None if d is None else d.dispute)
            if issue.axis is Axis.CAVEAT:
                caveats.append(issue)
            elif li.place is not None and li.place.claim is not None:
                of_claim.setdefault(li.place.claim, []).append(issue)
            else:
                setup.append(issue)
        place = {c.id: n + 1 for n, c in enumerate(claims)}
        claim_groups = tuple(
            ClaimIssueGroup(claim_id=claims[n].id, statement=claims[n].statement,
                            register_place=n + 1, register_size=len(claims),
                            inferred_from_places=tuple(sorted(
                                place[c] for c in self.claims_inferred_from(claims[n]))),
                            issues=tuple(sorted(of_claim[n], key=lambda i: i.axis.rank)))
            for n in sorted(of_claim))
        return (ReportSection(kind=ReportSectionKind.SETUP,
                              groups=tuple(self.group_by_type(setup))),
                ReportSection(kind=ReportSectionKind.CLAIMS, groups=claim_groups),
                ReportSection(kind=ReportSectionKind.CAVEATS,
                              groups=tuple(self.group_by_type(caveats))))

    @staticmethod
    def claims_inferred_from(claim: inputs.Claim) -> tuple[str, ...]:
        """The claims that `claim` is inferred from, or none when it has another source."""
        return (tuple(claim.source.claims) if isinstance(claim.source, inputs.ClaimsSource)
                else ())

    def group_by_type(self, issues: list[Issue]) -> list[IssueGroup]:
        """The issues grouped by type: most severe axis first, then fewest issues, then name.

        Each group keeps the order of its issues. A group whose issues all have a measured
        number has the highest number first.
        """
        groups: dict[str, list[Issue]] = {}
        for issue in issues:
            groups.setdefault(issue.code, []).append(issue)
        ordered = sorted(groups.items(),
                         key=lambda kv: (kv[1][0].axis.rank, len(kv[1]), kv[0]))
        return [IssueGroup(code=code, issues=tuple(self.order_by_measured_number(issues)))
                for code, issues in ordered]

    @staticmethod
    def order_by_measured_number(issues: list[Issue]) -> list[Issue]:
        """The issues with the highest measured number first, when every issue has one."""
        measured = [i.measured for i in issues]
        if any(m is None for m in measured):
            return issues
        return sorted(issues, key=lambda i: -(i.measured or 0.0))
