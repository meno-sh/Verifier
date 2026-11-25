"""The saved JSON of one report, as typed records.

`vcheck run` writes it. The daemon, `verifier status`, `verifier report`, `verifier diff`,
the refresh after a dispute or a decision, and the diagnosis extension read it.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import ClassVar

from core.analysis import findings_json
from core.report_model.axis import Axis
from core.report_model.issues import FlagDispute, Issue, IssueIdentity
from core.report_model.report_header import ReportHeaderRow
from core.report_model.verification_report import RunStartFailure, VerificationReport
from schema.errors import VerifierError
from schema.primitives import is_real_number, read_only, record
from schema.verdict import Verdict


class SavedReportError(VerifierError):
    """A saved report that does not have the keys and types that `vcheck run` writes."""


# The keys of a saved report, as `vcheck run` writes them.
SAVED_REPORT_KEYS = frozenset({"commit", "verdict", "verdict_without_disputed", "methods_ran_in",
                               "run_error", "findings", "issues", "report_id"})


@record
class SavedFlag(IssueIdentity):
    """One flag as a file saves it: its flag id, code, axis, subject, detail and facts.

    A dispute saves the flags it names in this form (diagnosis/records.py). SavedIssue
    extends it with what only the report saves.
    """
    JSON_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"id", "code", "axis", "subject", "detail", "facts"})
    flag_id: str
    axis: Axis
    facts: tuple[tuple[str, str], ...]

    def to_json(self) -> dict[str, object]:
        """The flag as a file saves it."""
        return {"id": self.flag_id, "code": self.code, "axis": self.axis.value,
                "subject": self.subject, "detail": self.detail,
                "facts": [list(f) for f in self.facts]}

    @classmethod
    def from_json(cls, item: object) -> SavedFlag:
        """Read one saved flag.

        Raises:
            SavedReportError: the item does not have the keys and types of a flag.
        """
        return cls.flag_in(_json_object(item, "a saved flag", set(cls.JSON_KEYS)))

    @staticmethod
    def flag_in(data: Mapping[str, object]) -> SavedFlag:
        """The flag in the JSON object `data`, whose keys are checked already.

        Raises:
            SavedReportError: a field does not have its type.
        """
        facts = data["facts"]
        if not isinstance(facts, list) or not all(
                isinstance(f, list) and len(f) == 2 and all(isinstance(x, str) for x in f)
                for f in facts):
            raise SavedReportError(f"the facts of saved flag {data['id']!r} are not "
                                   "pairs of text")
        return SavedFlag(flag_id=_text(data, "id"), code=_text(data, "code"),
                         axis=_axis(data), subject=_text(data, "subject"),
                         detail=_text(data, "detail"),
                         facts=tuple((f[0], f[1]) for f in facts))


@record
class SavedIssue(SavedFlag):
    """One item of `issues` in the saved report: an issue with its flag id and its dispute."""
    impact: str
    measured: float | None
    disputable: bool
    dispute: FlagDispute | None

    @classmethod
    def from_issue(cls, issue: Issue) -> SavedIssue:
        """The saved form of an issue of the report."""
        return cls(flag_id=issue.flag_id, code=issue.code, axis=issue.axis,
                   impact=issue.axis.impact(issue.disputable), subject=issue.subject,
                   detail=issue.detail, facts=issue.facts, measured=issue.measured,
                   disputable=issue.disputable, dispute=issue.dispute)

    def flag(self) -> SavedFlag:
        """The flag of this issue alone, as a dispute saves it."""
        return SavedFlag(flag_id=self.flag_id, code=self.code, axis=self.axis,
                         subject=self.subject, detail=self.detail, facts=self.facts)

    def to_json(self) -> dict[str, object]:
        """The item as `vcheck run` writes it."""
        return {**self.flag().to_json(), "impact": self.impact, "measured": self.measured,
                "disputable": self.disputable,
                "disputed": None if self.dispute is None else self.dispute.to_json()}

    @classmethod
    def from_json(cls, item: object) -> SavedIssue:
        """Read one item of `issues`.

        Raises:
            SavedReportError: the item does not have the keys and types of an issue.
        """
        data = _json_object(item, "an issue of the saved report",
                            set(cls.JSON_KEYS | {"impact", "measured", "disputable", "disputed"}))
        disputed = data["disputed"]
        dispute = None
        if disputed is not None:
            disputed = _json_object(disputed, "the dispute of a saved issue",
                                    {"dispute_id", "reason"})
            dispute = FlagDispute(dispute_id=_text(disputed, "dispute_id"),
                                  reason=_text(disputed, "reason"))
        measured = data["measured"]
        if measured is not None and not is_real_number(measured):
            raise SavedReportError(f"the measured number of saved issue {data['id']!r} is "
                                   "not a number")
        flag = cls.flag_in(data)
        return cls(flag_id=flag.flag_id, code=flag.code, axis=flag.axis, subject=flag.subject,
                   detail=flag.detail, facts=flag.facts, impact=_text(data, "impact"),
                   measured=measured, disputable=_boolean(data, "disputable"), dispute=dispute)


@record
class SavedReport:
    """The saved JSON of one report, with the keys that `vcheck run` writes (SAVED_REPORT_KEYS).

    A report that an older Verifier saved with other keys does not read: run the check
    again to write it with these keys.
    """
    # The length of the id of a report: the start of a SHA-256 in hex.
    REPORT_ID_LENGTH: ClassVar[int] = 12
    commit: str | None
    verdict: Verdict
    verdict_without_disputed_flags: Verdict | None
    methods_ran_in: str | None
    run_error: str | None
    findings_json: Mapping[str, object] | None      # FindingsJsonCodec output
    issues: tuple[SavedIssue, ...]
    report_id: str                                  # stored; never computed again on read

    @classmethod
    def from_report(cls, report: VerificationReport) -> SavedReport:
        """The saved form of the report of one run."""
        issues = tuple(SavedIssue.from_issue(i) for i in report.issues)
        second = report.verdict_without_disputed_flags
        encoded = findings_json.FindingsJsonCodec().encode(report.findings)
        assert isinstance(encoded, dict)                # a Findings encodes as an object
        return cls(commit=report.commit, verdict=report.verdict,
                   verdict_without_disputed_flags=second,
                   methods_ran_in=report.methods_ran_in, run_error=None,
                   findings_json=encoded, issues=issues,
                   report_id=cls.compute_report_id(report.verdict, second, issues, None,
                                                   report.rows_under_verdict))

    @classmethod
    def from_start_failure(cls, failure: RunStartFailure) -> SavedReport:
        """The saved form of the report of a run that could not start."""
        outcome = failure.VERDICT
        return cls(commit=failure.commit, verdict=outcome, verdict_without_disputed_flags=None,
                   methods_ran_in=None, run_error=failure.error, findings_json=None, issues=(),
                   report_id=cls.compute_report_id(outcome, None, (), failure.error, ()))

    @classmethod
    def compute_report_id(cls, verdict: Verdict, verdict_without_disputed_flags: Verdict | None,
                          issues: Sequence[SavedIssue], run_error: str | None,
                          rows_under_verdict: Sequence[ReportHeaderRow]) -> str:
        """The id of a report, which changes only when what a reader sees changes.

        The id is the start of the SHA-256 of the two verdicts, the issues, the error of a
        run that could not start, and the rows under the verdict. So a report of a later
        commit with all of these the same has the same id.
        """
        second = verdict_without_disputed_flags
        seen = [verdict.value, None if second is None else second.value,
                [i.to_json() for i in issues], run_error, [r.to_json() for r in rows_under_verdict]]
        digest = hashlib.sha256(json.dumps(seen, sort_keys=True).encode()).hexdigest()
        return digest[:cls.REPORT_ID_LENGTH]

    def issue_with_flag_id(self, flag_id: str) -> SavedIssue | None:
        """The issue of the report with this flag id, or None."""
        return next((i for i in self.issues if i.flag_id == flag_id), None)

    def to_json(self) -> dict[str, object]:
        """The report as `vcheck run` writes it."""
        second = self.verdict_without_disputed_flags
        return {
            "commit": self.commit, "verdict": self.verdict.value,
            "methods_ran_in": self.methods_ran_in, "run_error": self.run_error,
            "findings": None if self.findings_json is None else dict(self.findings_json),
            "verdict_without_disputed": None if second is None else second.value,
            "issues": [i.to_json() for i in self.issues], "report_id": self.report_id}

    @classmethod
    def from_json(cls, data: object) -> SavedReport:
        """Read a saved report.

        Raises:
            SavedReportError: the report does not have exactly SAVED_REPORT_KEYS (for example
                an older Verifier saved it), or a key has the wrong type.
        """
        if not isinstance(data, dict):
            raise SavedReportError("a saved report must be a JSON object")
        keys = frozenset(data)
        if keys != SAVED_REPORT_KEYS:
            raise SavedReportError(f"a saved report has the keys {sorted(keys)}, not the keys "
                                   f"that this Verifier writes ({sorted(SAVED_REPORT_KEYS)}). "
                                   "A Verifier from before 10 November 2026 wrote another set "
                                   "of keys: run the check again to write the latest report")
        found = data["findings"]
        if found is not None and not isinstance(found, dict):
            raise SavedReportError("`findings` of a saved report must be an object or null")
        issues = data["issues"]
        if not isinstance(issues, list):
            raise SavedReportError("`issues` of a saved report must be a list")
        return cls(commit=_optional_text(data, "commit"), verdict=_verdict(data, "verdict"),
                   verdict_without_disputed_flags=_optional_verdict(
                       data, "verdict_without_disputed"),
                   methods_ran_in=_optional_text(data, "methods_ran_in"),
                   run_error=_optional_text(data, "run_error"),
                   findings_json=None if found is None else read_only(found),
                   issues=tuple(SavedIssue.from_json(i) for i in issues),
                   report_id=_text(data, "report_id"))


def _json_object(value: object, label: str, keys: set[str]) -> dict[str, object]:
    """`value` as a JSON object with exactly these keys, or raise."""
    if not isinstance(value, dict) or set(value) != keys:
        raise SavedReportError(f"{label} must be an object with the keys {sorted(keys)}")
    return value


def _text(data: Mapping[str, object], key: str) -> str:
    """The text at `key`, or raise."""
    value = data[key]
    if not isinstance(value, str):
        raise SavedReportError(f"`{key}` of a saved report must be text")
    return value


def _axis(data: Mapping[str, object]) -> Axis:
    """The axis at "axis", or raise."""
    text = _text(data, "axis")
    try:
        return Axis(text)
    except ValueError:
        raise SavedReportError(f"`axis` of a saved report must be an axis, got {text!r}") from None


def _optional_text(data: Mapping[str, object], key: str) -> str | None:
    """The text at `key`, or None; raise for another type."""
    return None if data[key] is None else _text(data, key)


def _boolean(data: Mapping[str, object], key: str) -> bool:
    """The boolean at `key`, or raise."""
    value = data[key]
    if not isinstance(value, bool):
        raise SavedReportError(f"`{key}` of a saved report must be true or false")
    return value


def _verdict(data: Mapping[str, object], key: str) -> Verdict:
    """The verdict at `key`, or raise."""
    try:
        return Verdict(_text(data, key))
    except ValueError:
        raise SavedReportError(f"`{key}` of a saved report is not a verdict: "
                               f"{data[key]!r}") from None


def _optional_verdict(data: Mapping[str, object], key: str) -> Verdict | None:
    """The verdict at `key`, or None; raise for another value."""
    return None if data[key] is None else _verdict(data, key)
