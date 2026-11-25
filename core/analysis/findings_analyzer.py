"""Running every check that is on, and giving everything they found as one Findings."""
from __future__ import annotations

from core.analysis import check_result_cache
from core.analysis.analysis_request import AnalysisRequest, LoadedDocuments
from core.analysis.claim_checker import ClaimChecker, ClaimCheckRunner
from core.analysis.document_checker import DocumentChecker
from core.gate import artifact_pins, provenance, recompute
from core.registry import checks, run_options
from schema import findings, inputs
from schema.claim import claim_register_parser
from schema.primitives import read_only
from utils import checksums, version


class FindingsAnalyzer:
    """Runs all checks that are on, and gives one Findings.

    `verifier static-check` and the real-time verifier share it, so they can never drift
    apart. It runs whatever checks the registry holds, and names none.
    """

    def __init__(self, check_registry: checks.CheckRegistry,
                 result_cache: check_result_cache.CheckResultCache | None = None) -> None:
        """Analyse with the checks and methods of `check_registry`, and `result_cache`."""
        self.check_registry = check_registry
        self.result_cache = result_cache

    def analyse(self, request: AnalysisRequest) -> findings.Findings:
        """Run the checks that are on, as the request says, and give what they found.

        Raises:
            ClaimRegisterError: the claim register is not valid JSON or not a register.
            OptionsError: a run option is wrong; raised before any check runs.
        """
        reg = self.check_registry
        switches = ("recompute", *reg.names_that_can_be_switched_off())
        # Read the claim register first, so a broken one stops the run before the paid
        # model review.
        parser = claim_register_parser.ClaimRegisterParser(reg.computational_methods,
                                                           reg.claim_field_types())
        register = (parser.parse_file(request.claim_register_path,
                                      request.claim_register_base_folder)
                    if request.uses_claim_register and request.claim_register_path is not None
                    else inputs.ClaimRegister(claims=(), unreadable=()))
        on = (frozenset(switches) if request.switches_on is None
              else frozenset(request.switches_on))
        switched_off = frozenset(switches) - on
        self._refuse_options_that_do_not_fit(request, register, switched_off)
        loaded = LoadedDocuments.load(request.documents)
        document_checker = DocumentChecker(reg, request.run_options, on)
        paper_results = {key: document_checker.check_document(
                             paper, loaded.labels_of_other_documents(key))
                         for key, paper in loaded.papers.items()}
        claim_checker = ClaimChecker(
            reg, request.run_options, switched_off,
            provenance.ProvenanceChecker(artifact_pins.ArtifactPins()),
            recompute.ClaimRecomputer(reg.computational_methods, self.result_cache),
            ClaimCheckRunner(self.result_cache),
            full_paper_text=(request.full_paper_text if request.full_paper_text is not None
                             else next(iter(loaded.papers.values())).text
                             if len(loaded.papers) == 1 else None))
        claims = claim_checker.check_claims_in_dependency_order(
            register.claims, request.claims_with_claim_checks)
        if self.result_cache is not None:
            self.result_cache.finish_run()
        paths = request.all_document_paths()
        register_path = request.claim_register_path if request.uses_claim_register else None
        return findings.Findings(
            inputs=inputs.Inputs(
                paper_paths=paths,
                paper_sha256=checksums.sha256_of_files(paths) if paths else None,
                claim_register_path=register_path,
                claim_register_sha256=(checksums.sha256_of_file(register_path)
                                       if register_path is not None else None),
                claim_register_base=register.base,
                switched_off=switched_off,
                tool_commit=version.verifier_short_commit(),
            ),
            papers=read_only(paper_results), claims=claims,
            unreadable_claims=register.unreadable,
            superseded=tuple(c for c in register.claims if c.superseded_by is not None),
            must_run=reg.checks_that_must_not_be_skipped(switched_off),
            claim_checks_not_run_because=request.claim_checks_not_run_because)

    def _refuse_options_that_do_not_fit(self, request: AnalysisRequest,
                                        register: inputs.ClaimRegister,
                                        switched_off: frozenset[str]) -> None:
        """Check the options before any check runs, so a wrong one stops the run first.

        A claim check that applies to no claim in the register runs nowhere, and a paper
        group runs nowhere without a document, so their options are not checked.

        Raises:
            OptionsError: an option is wrong; the message says which.
        """
        reg = self.check_registry
        idle = frozenset(name for name, check in reg.claim_checks.items()
                         if not any(check.applies_to_claim(c) for c in register.claims))
        if not request.documents:
            idle |= frozenset(reg.paper_check_groups)
        try:
            reg.refuse_options_that_do_not_fit(request.run_options, switched_off | idle)
        except (TypeError, ValueError) as e:
            raise run_options.OptionsError(str(e)) from e
