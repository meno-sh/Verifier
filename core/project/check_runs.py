"""The two ways to check a paper, with no command-line code: a watched project, or once.

WatchedProjectCheckRun checks a watched project and saves its report files, for
`vcheck run`. StaticCheckRun checks one paper and its claim register once, with the
built-in checks only, for `verifier static-check`. The commands parse their arguments,
call one of these, and print the result.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from core.analysis import analysis_request, findings_analyzer
from core.project import watched_project
from core.registry import checks, extensions
from core.report_model import (
    report_builder,
    verification_report,
)
from core.report_output import (
    markdown_renderer,
    report_store,
    saved_report,
    typst_renderer,
)
from schema.errors import VerifierError
from schema.primitives import record
from schema.verdict import Verdict


class UnknownCheckError(VerifierError):
    """A claim check that a static check run was asked for is not registered."""


@record
class CheckRunOutcome:
    """What a check of a watched project gives: the report as Markdown, and the verdict."""
    markdown: str
    verdict: Verdict


class StaticCheckRun:
    """Checks one paper and its claim register once, with the built-in checks only.

    It needs no watched project and no state folder. It has no computational method, so
    a claim with an artifact source is unreadable here.
    """

    def __init__(self, checks: checks.CheckRegistry | None = None) -> None:
        """Run the checks of the registry `checks`; the built-in checks by default."""
        self.check_registry = extensions.builtin_check_registry() if checks is None else checks

    def run(self, papers: Sequence[str], claim_register_path: str | None, paper_only: bool,
            chosen_claim_checks: frozenset[str] | None,
            run_options_given: Mapping[str, object]) -> verification_report.VerdictSummary:
        """Check the paper, read from the files `papers` as one document named by the first.

        Args:
            papers: the paper files, read as one paper in the order given.
            claim_register_path: the claim register, or None.
            paper_only: ignore the claim register.
            chosen_claim_checks: the only claim checks to run, or None for all of them.
            run_options_given: the run options, by key; a value of None is the default.

        Raises:
            UnknownCheckError: a chosen claim check is not registered.
            ClaimRegisterError, UnknownPaperFormat, OptionsError: an input cannot be used.
        """
        reg = self.check_registry.copy_for_one_run()
        switches_on = None
        if chosen_claim_checks is not None:
            unknown = chosen_claim_checks - set(reg.claim_checks)
            if unknown:
                raise UnknownCheckError(f"unknown check(s): {', '.join(sorted(unknown))}")
            switches_on = frozenset({"recompute", *reg.names_that_can_be_switched_off()}
                                    - set(reg.claim_checks) | chosen_claim_checks)
        tree = findings_analyzer.FindingsAnalyzer(reg).analyse(analysis_request.AnalysisRequest(
            documents={papers[0]: tuple(papers)}, claim_register_path=claim_register_path,
            paper_only=paper_only, switches_on=switches_on,
            run_options=dict(run_options_given)))
        return verification_report.VerdictSummary.of_findings(tree)


class WatchedProjectCheckRun:
    """One check of a watched project: analyse, build the report, and write its files.

    When the inputs of the project cannot be read (a method file or plug-in check that does
    not load, a claim register that is not one, a run option that does not fit, a uv.lock
    that did not build), the run cannot start. That is a problem in the project, not in the
    Verifier, so the report says so, with the verdict BLOCK UNTIL FIXED.
    """

    def __init__(self, root: Path, state_dir: Path,
                 extension_set: extensions.ExtensionSet) -> None:
        """Run the watched project in `root` with its state folder and these extensions."""
        self.root = root
        self.state_dir = state_dir
        self.extension_set = extension_set
        self.project = watched_project.WatchedProject(root, state_dir, extension_set)
        self.store = report_store.ReportStore(state_dir)
        self.markdown = markdown_renderer.MarkdownReportRenderer()
        self.typst = typst_renderer.TypstReportRenderer()

    def run(self, commit: str | None, use_cache: bool = True,
            claim_checks_not_run_because: str | None = None) -> CheckRunOutcome:
        """Check every claim, save the report files, and give the Markdown and the verdict.

        The verdict is the one with all flags, also when some are disputed. `use_cache` and
        `claim_checks_not_run_because` are as in WatchedProject.analyse.
        """
        try:
            # the registry first, so a file that does not load is named before the uv.lock
            tree = self.project.analyse(self.project.check_registry(), use_cache,
                                        claim_checks_not_run_because)
        except (*watched_project.PROJECT_INPUT_ERRORS,
                watched_project.MethodEnvironmentError) as e:
            return self._save_start_failure(verification_report.RunStartFailure(
                commit=commit, error=f"{type(e).__name__}: {e}"))
        notices = [self.project.method_environment_row(),
                   *self.extension_set.report_notices(tree, self.root, self.state_dir)]
        report = report_builder.VerificationReportBuilder().build_report(
            tree, commit, notices, self.extension_set.disputed_flags(self.root, self.state_dir))
        layout = self.typst.report_layout_data(report)
        text, markup = self.markdown.render_report(report), self.typst.markup_of_layout_data(layout)
        self.store.save_report_files(commit, text, markup,
                                     self.typst.compile_pdf(markup, self.store.pdf_title),
                                     saved_report.SavedReport.from_report(report), layout)
        return CheckRunOutcome(markdown=text, verdict=report.verdict)

    def _save_start_failure(self, failure: verification_report.RunStartFailure
                            ) -> CheckRunOutcome:
        """Save the report of a run that could not start, and give its Markdown and verdict."""
        text = self.markdown.render_start_failure(failure)
        layout = self.typst.start_failure_layout_data(failure)
        markup = self.typst.markup_of_layout_data(layout)
        self.store.save_report_files(failure.commit, text, markup,
                                     self.typst.compile_pdf(markup, self.store.pdf_title),
                                     saved_report.SavedReport.from_start_failure(failure),
                                     layout)
        return CheckRunOutcome(markdown=text, verdict=failure.VERDICT)
