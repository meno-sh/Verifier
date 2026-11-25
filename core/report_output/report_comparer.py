"""Comparing two saved reports, so a reader sees only what changed between two commits.

A finding is a result that did not pass: the provenance, recompute or check of a claim, a
check of a paper, or an unreadable claim register entry. Its state is the type of the
result and its problem codes. The change is `none` when the reports differ only in their
commit, `major` when the verdict changed or a finding was added, removed or changed state,
and `minor` otherwise, for example the wording of a problem or a measured value.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from core.report_output.saved_report import SavedReport
from schema import primitives, provenance, recompute


class SavedReportComparer:
    """Compares two saved reports for `verifier diff`."""
    SCHEMA_VERSION: ClassVar[int] = 1
    # The result classes that pass, by the name the saved findings give them ("type").
    PASSING: ClassVar[frozenset[str]] = frozenset(cls.__name__ for cls in (
        primitives.Passed, primitives.Off, provenance.Intact, provenance.Traced,
        provenance.Derived, recompute.Matches))

    def compare_reports(self, old: SavedReport, new: SavedReport) -> dict[str, object]:
        """The change from report `old` to report `new`."""
        before, after = self._finding_states(old), self._finding_states(new)
        added = sorted(k for k in after if k not in before)
        removed = sorted(k for k in before if k not in after)
        changed = sorted(k for k in after if k in before and after[k] != before[k])
        if old.verdict is not new.verdict or added or removed or changed:
            change = "major"
        elif {**old.to_json(), "commit": None} != {**new.to_json(), "commit": None}:
            change = "minor"
        else:
            change = "none"
        return {"schema_version": self.SCHEMA_VERSION, "change": change,
                "from": old.commit, "to": new.commit,
                "verdict": {"from": old.verdict.value, "to": new.verdict.value},
                "added": [{"finding": k, "state": after[k]} for k in added],
                "removed": [{"finding": k, "state": before[k]} for k in removed],
                "changed": [{"finding": k, "from": before[k], "to": after[k]} for k in changed]}

    def _finding_states(self, report: SavedReport) -> dict[str, str]:
        """Every finding of a saved report, by name, with its state.

        A report whose run could not start has no findings; its `run_error` is its one
        finding.
        """
        tree: Any = report.findings_json
        if tree is None:
            return {"run": f"could not start: {report.run_error}"}
        out: dict[str, str] = {}
        for path, paper in tree["papers"].items():
            self._add_results_that_did_not_pass(out, f"paper {path}", paper["checks"])
        for claim in tree["claims"]:
            name = f"claim {claim['claim']['id']}"
            self._add_results_that_did_not_pass(
                out, name, {"provenance": claim["provenance"], "recompute": claim["recompute"],
                            **claim["checks"]})
        for entry in tree["unreadable_claims"]:
            out[f"unreadable claim {entry['id'] or 'entry ' + str(entry['entry'])}"] = "Unreadable"
        return out

    @classmethod
    def _add_results_that_did_not_pass(cls, out: dict[str, str], where: str,
                                       results: Mapping[str, Any]) -> None:
        """Add each result in `results` that did not pass, named `where: check`."""
        for check, result in results.items():
            if result["type"] not in cls.PASSING:
                codes = sorted({p["kind"] for p in result.get("problems", ())})
                out[f"{where}: {check}"] = " ".join([result["type"], *codes])
