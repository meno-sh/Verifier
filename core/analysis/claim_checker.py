"""Running the gate and the claim checks on each claim, in dependency order.

ClaimChecker orders the claims and runs the gate. ClaimCheckRunner runs one claim check on
one claim, and decides Off, Skipped and NotChecked.
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import traceback
from collections.abc import Mapping, Sequence

from core.analysis import check_result_cache, findings_json
from core.gate import provenance, recompute
from core.registry import checks, run_options
from schema import findings, inputs
from schema.claim import claim_links
from schema.primitives import (
    CheckCrashed,
    CheckResult,
    NotChecked,
    Off,
    RecomputeResult,
    Skipped,
)
from schema.provenance import ProvenanceResult

CLAIM_CHECK_WORKERS = run_options.RunOption(
    key="claim_check_workers", kind=int, flag="--claim-check-workers", default=64,
    help="the claims checked at the same time; a claim waits for the claims it rests on "
         "only when a check that is on reads their findings")
RUN_OPTIONS = (CLAIM_CHECK_WORKERS,)


class ClaimChecker:
    """Runs the gate and the claim checks on each claim, after the claims it rests on."""

    def __init__(self, check_registry: checks.CheckRegistry,
                 run_options: Mapping[str, object],
                 switched_off: frozenset[str], provenance_checker: provenance.ProvenanceChecker,
                 recomputer: recompute.ClaimRecomputer,
                 runner: ClaimCheckRunner,
                 full_paper_text: str | None = None) -> None:
        """Check claims with the claim checks of `check_registry` that are not switched off.

        `full_paper_text` is the full text of the paper, which each ClaimContext gives to
        the checks; None when the run has none.
        """
        self.check_registry = check_registry
        self.run_options = run_options
        self.switched_off = switched_off
        self.provenance_checker = provenance_checker
        self.recomputer = recomputer
        self.runner = runner
        self.full_paper_text = full_paper_text

    def check_claims_in_dependency_order(self, claims: Sequence[inputs.Claim],
                                         claims_with_checks: frozenset[str] | None = None
                                         ) -> tuple[findings.ClaimFindings, ...]:
        """The findings of every claim that is not superseded, in register order.

        Up to `claim_check_workers` claims are checked at the same time. When a check that
        is on reads the findings of the claims a claim rests on, the claim waits for those
        that come before it in the dependency order (claim_links.order_by_dependency, where
        a cycle breaks at one claim) and gets their findings. Otherwise no claim waits, and
        no claim gets the findings of others. Each claim's result does not depend on the
        number of workers.

        Args:
            claims: the claims of the register.
            claims_with_checks: the ids of the claims that get claim checks; every claim
                when None. The others still go through the gate.

        Raises:
            ValueError: `claim_check_workers` is below 1.
        """
        workers = CLAIM_CHECK_WORKERS.value_in_options(self.run_options)
        if not isinstance(workers, int) or isinstance(workers, bool):
            raise TypeError(f"claim_check_workers must be an int, got {workers!r}")
        if workers < 1:
            raise ValueError(f"claim_check_workers must be at least 1, got {workers}")
        by_id = {c.id: c for c in claims}
        checked = [c for c in claims if c.superseded_by is None]
        ordered = claim_links.ClaimLinks.order_by_dependency(checked)
        place = {c.id: n for n, c in enumerate(ordered.claims)}
        waits = self.a_check_reads_findings_of_claims_rested_on()
        waits_for = {c.id: [d for d in c.ids_of_claims_it_is_inferred_from()
                            if waits and d in place and place[d] < place[c.id]]
                     for c in ordered.claims}
        done: dict[str, findings.ClaimFindings] = {}

        def check_claim(claim: inputs.Claim) -> findings.ClaimFindings:
            dependencies = {d: done[d] for d in waits_for[claim.id]}
            context = checks.ClaimContext(register=by_id, dependencies=dependencies,
                                          options=self.run_options,
                                          full_paper_text=self.full_paper_text)
            off = (self.switched_off if claims_with_checks is None or claim.id in claims_with_checks
                   else self.switched_off | frozenset(self.check_registry.claim_checks))
            return self.check_one_claim(claim, context, off)

        left = {cid: len(deps) for cid, deps in waits_for.items()}
        waiting_on: dict[str, list[inputs.Claim]] = {c.id: [] for c in ordered.claims}
        for claim in ordered.claims:
            for d in waits_for[claim.id]:
                waiting_on[d].append(claim)
        with concurrent.futures.ThreadPoolExecutor(workers) as pool:
            def submit(claim: inputs.Claim) -> concurrent.futures.Future[findings.ClaimFindings]:
                # each thread gets the caller's context, such as the spending purpose
                return pool.submit(contextvars.copy_context().run, check_claim, claim)
            running = {submit(c): c for c in ordered.claims if left[c.id] == 0}
            while running:
                finished, _ = concurrent.futures.wait(
                    running, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in finished:
                    claim = running.pop(future)
                    done[claim.id] = future.result()
                    for after in waiting_on[claim.id]:
                        left[after.id] -= 1
                        if left[after.id] == 0:
                            running[submit(after)] = after
        assert len(done) == len(ordered.claims), "every claim is checked once"
        return tuple(done[c.id] for c in checked)

    def a_check_reads_findings_of_claims_rested_on(self) -> bool:
        """Whether a claim check that is on reads the findings of the claims a claim rests on."""
        return any(check.reads_findings_of_claims_it_rests_on(self.run_options)
                   for name, check in self.check_registry.claim_checks.items()
                   if name not in self.switched_off)

    def check_one_claim(self, claim: inputs.Claim, context: checks.ClaimContext,
                        switched_off: frozenset[str]) -> findings.ClaimFindings:
        """Run the gate on one claim, then every claim check that applies to it."""
        found = self.provenance_checker.check_claim_source(claim)
        recomputed = (Off() if "recompute" in switched_off
                      else self.recomputer.recompute_claim(claim, found))
        results = {name: self.runner.run_check_on_claim(check, claim, found, recomputed,
                                                        context, switched_off)
                   for name, check in self.check_registry.claim_checks.items()
                   if check.applies_to_claim(claim)}
        return findings.ClaimFindings(claim=claim, provenance=found, recompute=recomputed,
                                      checks=results)


class ClaimCheckRunner:
    """Runs one claim check on one claim, and decides Off, Skipped and NotChecked.

    The check gets the context of its input scope only (CheckInputScope). A check only
    decides between Passed and Flagged. A result that reports a kind the
    check did not declare is refused: a trusted check that gives it stops the run, an
    untrusted one becomes Skipped.

    With a result cache, a check whose scope can be cached gives its stored result when
    nothing it reads changed. A crash and a model failure are never stored
    (findings_json.FindingsJsonCodec.STORED_RESULTS).
    """

    def __init__(self, cache: check_result_cache.CheckResultCache | None = None) -> None:
        """Run the checks, with `cache` when it is given."""
        self.cache = cache

    def run_check_on_claim(self, check: checks.ClaimCheck, claim: inputs.Claim,
                           provenance_result: ProvenanceResult,
                           recompute_result: RecomputeResult,
                           context: checks.ClaimContext,
                           switched_off_names: frozenset[str]) -> CheckResult:
        """Run one claim check on one claim, after the gate has run on it.

        The gate results are given directly, because the findings of the claim do not
        exist yet.

        Raises:
            RegistryError: the check does not apply to the claim (see
                ClaimCheck.applies_to_claim).
        """
        if not check.applies_to_claim(claim):
            raise run_options.RegistryError(
                f"claim check {check.name!r} does not apply to {claim.id!r}")
        if check.name in switched_off_names:
            return Off()
        if provenance_result.is_broken or recompute_result.is_broken:
            return Skipped(because="provenance or recompute failed for this claim")
        for key in check.needs:
            if key not in claim.fields:
                return NotChecked(missing=key)
        matched = recompute_result.recomputed_number()
        if matched is None and check.needs_recompute:
            return Skipped(because=recompute_result.reason_there_is_no_recomputed_number())
        scoped = check.input_scope.restricted_context(claim, context)
        cached = self.cache is not None and check.input_scope.can_be_cached
        if self.cache is not None and cached:
            fingerprint = self.cache.fingerprint(check, claim, scoped, provenance_result,
                                                 recompute_result)
            stored = (None if fingerprint is None
                      else self.cache.stored_result(fingerprint, check))
            if stored is not None:
                return stored
        try:
            result = check.check(claim, matched, scoped)
            self._refuse_invalid_result(check, result)
        except Exception as e:  # noqa: BLE001 - only untrusted code is caught; trusted re-raises
            if check.trusted:
                raise
            where = traceback.extract_tb(e.__traceback__)[-1:]
            line = f" at line {where[0].lineno}" if where else ""
            return CheckCrashed(because=f"the check failed{line}: {type(e).__name__}: {e}")
        # the exact type: CheckCrashed and ModelFailedAfterRetries are a Skipped, and
        # neither is stored, so the next run tries again
        if (self.cache is not None and cached
                and type(result) in findings_json.FindingsJsonCodec.STORED_RESULTS):
            # computed again: a check can write a file it reads, such as a readings file,
            # and the next run looks the result up with the file as it is now
            after = self.cache.fingerprint(check, claim, scoped, provenance_result,
                                           recompute_result)
            if after is not None:
                self.cache.store_result(after, result)
        return result

    @staticmethod
    def _refuse_invalid_result(check: checks.ClaimCheck, result: object) -> None:
        """Refuse a result that is not a CheckResult, or a problem of an undeclared kind."""
        if not isinstance(result, CheckResult):
            raise TypeError(f"claim check {check.name!r} returned {type(result).__name__}, "
                            "not a CheckResult")
        undeclared = [p.kind.code for p in result.problems_found()
                      if p.kind not in check.problem_kinds]
        if undeclared:
            raise run_options.RegistryError(
                f"claim check {check.name!r} reported undeclared kind(s) {', '.join(undeclared)}")
