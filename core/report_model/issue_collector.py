"""Listing each issue of a Findings that a reader must see, with the place of its flag."""
from __future__ import annotations

from typing import ClassVar

from core.analysis import verdict_policy
from core.report_model.axis import Axis
from core.report_model.issues import (
    CLAIM_STATEMENT,
    INFERRED_FROM,
    FlagPlaceInTree,
    Issue,
    LocatedIssue,
)
from schema import findings, inputs, provenance, recompute
from schema.primitives import Flagged, NotChecked, Skipped


class IssueCollector:
    """Lists each issue of a Findings, with the place of its flag in the tree.

    The report sorts these issues into its sections, and the triage board puts them on its axes.
    """
    # A check in Findings.must_run that was Skipped is a missing input "<check>-not-run".
    NOT_RUN_SUFFIX: ClassVar[str] = "-not-run"
    # No claim was checked, because the claim register could not be built.
    CLAIM_CHECKS_NOT_RUN: ClassVar[str] = "claim-checks-not-run"

    def collect_issues(self, tree: findings.Findings) -> list[LocatedIssue]:
        """Every issue a reader must see, with the place of its flag in the tree.

        The issues of the documents come first, then those of the claim register. The place
        is None for an issue whose flag nobody can dispute: an unread file, a check that did
        not run, a claim with nothing to check it against, or a claim register entry that
        cannot be read.
        """
        out: list[LocatedIssue] = []
        required = (frozenset(tree.must_run)
                    if verdict_policy.VerdictPolicy().a_required_check_was_skipped(tree)
                    else frozenset())
        for path, paper in tree.papers.items():
            out += self._issues_of_document(path, paper, required)
        out += [LocatedIssue(issue=Issue(code="claim-unreadable", axis=Axis.MISSING_INPUTS,
                                         subject=u.position_label(),
                                         detail=u.reason), place=None)
                for u in tree.unreadable_claims]
        if tree.claim_checks_not_run_because:
            out.append(LocatedIssue(issue=Issue(
                code=self.CLAIM_CHECKS_NOT_RUN, axis=Axis.MISSING_INPUTS, subject="claim checks",
                detail=tree.claim_checks_not_run_because), place=None))
        register_folder = tree.inputs.claim_register_base
        for n, c in enumerate(tree.claims):
            out += self._issues_of_claim(c, register_folder, n)
        return out

    @classmethod
    def _issues_of_document(cls, path: str, paper: findings.PaperFindings,
                            required: frozenset[str]) -> list[LocatedIssue]:
        """The issues of one document: unread files, its problems, and checks that did not run.

        A check in `required` that was Skipped is a missing input; another check that did
        not run is a caveat.
        """
        out = [LocatedIssue(issue=Issue(
                   code="unresolved-input", axis=Axis.MISSING_INPUTS, subject=path,
                   detail=f"{name}: the file could not be read, so the checks that need it "
                          f"did not run"), place=None)
               for name in paper.unreadable_inputs]
        for name, result in paper.checks.items():
            if isinstance(result, Skipped) and name in required:
                out.append(LocatedIssue(issue=Issue(
                    code=name.replace("_", "-") + cls.NOT_RUN_SUFFIX, axis=Axis.MISSING_INPUTS,
                    subject=path, detail=result.because), place=None))
                continue
            if isinstance(result, NotChecked | Skipped):
                out.append(LocatedIssue(issue=Issue(
                    code="not-checked", axis=Axis.CAVEAT, subject=path,
                    detail=f"{name}: {result.describe_for_reader()}"), place=None))
                continue
            out += [LocatedIssue(issue=Issue(code=p.kind.code,
                                             axis=Axis.of_severity(p.kind.severity),
                                             subject=path, detail=p.text_for_reader(),
                                             facts=p.facts),
                                 place=FlagPlaceInTree(paper=path, check=name, index=k))
                    for k, p in enumerate(result.problems_found())]
        return out

    @staticmethod
    def _claim_label(claim: inputs.Claim) -> str:
        """The id of the claim, with its number or the claims it is inferred from."""
        if isinstance(claim.source, inputs.ClaimsSource):
            return f"{claim.id} (inferred from {', '.join(claim.source.claims)})"
        if claim.value is None:
            return claim.id
        return f"{claim.id} (value {claim.value})"

    def _issues_of_claim(self, c: findings.ClaimFindings, register_folder: str | None,
                         n: int) -> list[LocatedIssue]:
        """The issues of claim `n`: a broken gate, a caveat, or what its checks found."""
        label = self._claim_label(c.claim)
        if c.gate_failed:
            broken = c.provenance if c.provenance.is_broken else c.recompute
            assert broken.board_code is not None        # every broken result has a code
            detail = "; ".join(f"{step}: {text}"
                               for step, text in self.gate_steps(c, register_folder))
            if (isinstance(broken, recompute.Disagrees)
                    and isinstance(c.provenance, provenance.Intact)):
                return [LocatedIssue(issue=Issue(code=broken.board_code, axis=Axis.INTEGRITY,
                                                 subject=c.claim.id, detail=detail,
                                                 facts=self._disagreement_facts(broken)),
                                     place=FlagPlaceInTree(claim=n))]
            return [LocatedIssue(issue=Issue(code=broken.board_code, axis=Axis.INTEGRITY,
                                             subject=label, detail=detail),
                                 place=FlagPlaceInTree(claim=n))]
        out: list[LocatedIssue] = []
        if c.provenance.counts_as_caveat:
            assert c.provenance.board_code is not None  # every caveat result has a code
            out.append(LocatedIssue(issue=Issue(
                code=c.provenance.board_code, axis=Axis.CAVEAT, subject=label,
                detail=c.provenance.describe_with_paths_relative_to(register_folder)),
                place=None))
        for name, result in c.checks.items():
            if not isinstance(result, Flagged):
                continue
            problems, measured = result.problems, result.measured
            out += [LocatedIssue(
                        issue=(Issue(code=p.kind.code, axis=Axis.of_severity(p.kind.severity),
                                     subject=label, detail=p.detail) if not p.facts else
                               Issue(code=p.kind.code, axis=Axis.of_severity(p.kind.severity),
                                     subject=c.claim.id, detail=p.detail,
                                     facts=self._claim_facts(c.claim) + p.facts,
                                     measured=measured)),
                        place=FlagPlaceInTree(claim=n, check=name, index=k))
                    for k, p in enumerate(problems)]
        return out

    @staticmethod
    def gate_steps(c: findings.ClaimFindings, register_folder: str | None
                   ) -> tuple[tuple[str, str], ...]:
        """What provenance and then recompute found for a claim, each as (step, text)."""
        return (("provenance", c.provenance.describe_with_paths_relative_to(register_folder)),
                ("recompute", c.recompute.describe_with_paths_relative_to(register_folder)))

    @staticmethod
    def _disagreement_facts(d: recompute.Disagrees) -> tuple[tuple[str, str], ...]:
        """A number the data does not give, as labelled lines: the paper's, the data's, the gap."""
        return (("Paper", f"{d.paper_value:.6g}"), ("Data", f"{d.value:.6g}"),
                ("Difference", f"{d.value - d.paper_value:+.6g}"),
                ("Tolerance", f"{d.tolerance:g}"))

    @staticmethod
    def _claim_facts(claim: inputs.Claim) -> tuple[tuple[str, str], ...]:
        """The statement of the claim, and the claims it is inferred from, as labelled lines."""
        out = []
        if claim.statement:
            out.append((CLAIM_STATEMENT, claim.statement))
        if isinstance(claim.source, inputs.ClaimsSource):
            out.append((INFERRED_FROM, ", ".join(claim.source.claims)))
        return tuple(out)
