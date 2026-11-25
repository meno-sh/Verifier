"""Running every paper check group that is on, over one document."""
from __future__ import annotations

from collections.abc import Mapping

from core.registry import checks
from schema import findings
from schema.primitives import CheckResult, read_only
from utils import extract


class DocumentChecker:
    """Runs, on one document, each paper check group that is switched on."""

    def __init__(self, check_registry: checks.CheckRegistry,
                 run_options: Mapping[str, object],
                 switches_on: frozenset[str]) -> None:
        """Run the groups of `check_registry` that are in `switches_on`, with these options."""
        self.check_registry = check_registry
        self.run_options = run_options
        self.switches_on = switches_on

    def check_document(self, paper: extract.Paper,
                       labels_elsewhere: frozenset[str]) -> findings.PaperFindings:
        """The results of every group on one document; a group that is off gives Off results.

        `labels_elsewhere` holds the labels and anchors of the other documents of the project.
        """
        context = checks.PaperContext(paper=paper, options=self.run_options,
                                        elsewhere=labels_elsewhere)
        results: dict[str, CheckResult] = {}
        facts: dict[str, int] = {}
        for group in self.check_registry.paper_check_groups.values():
            if group.name in self.switches_on:
                outcome = group.run(context)
                results.update(outcome.checks)
                facts.update(outcome.facts)
            else:
                results.update(group.results_when_switched_off())
        return findings.PaperFindings(unreadable_inputs=paper.unreadable,
                                      facts=read_only(facts), checks=results)
