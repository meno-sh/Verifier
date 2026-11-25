"""Recomputes the number of an artifact claim with its computational method.

The recompute runs only after provenance finds the claim files intact. A changed file gives
a meaningless number.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

from core.analysis import check_result_cache
from core.methods import computational_methods
from schema import inputs, provenance, recompute
from schema.primitives import MethodName, RecomputeResult, Skipped, scalars_are_equal


class ClaimRecomputer:
    """Recomputes the number of a claim with the computational method the claim names."""

    def __init__(self, methods: Mapping[MethodName,
                                        computational_methods.ComputationalMethodEntry],
                 cache: check_result_cache.CheckResultCache | None = None) -> None:
        """Recompute with the methods of the method files; each claim names one.

        With `cache`, a method that read nothing that changed gives its stored Computed.
        """
        self.computational_methods = methods
        self.cache = cache

    def recompute_claim(self, claim: inputs.Claim,
                        provenance_result: provenance.ProvenanceResult) -> RecomputeResult:
        """Recompute one claim from its data, once provenance found the files intact.

        Args:
            claim: the claim to recompute. Its source names the method and the path.
            provenance_result: the provenance result for the claim files.

        Returns:
            The recompute result, or Skipped if provenance did not find intact files.
        """
        if not provenance_result.allows_recompute:
            return Skipped(because=provenance_result.reason_recompute_did_not_run())
        source = claim.source
        assert isinstance(source, inputs.ArtifactSource)    # only artifacts are ever intact
        try:
            got = self._computed(source, provenance_result)
        except Exception as e:  # noqa: BLE001 - a method's failure is reported on its claim
            return recompute.MethodFailed(method=source.method,
                                          error=f"{type(e).__name__}: {e}")
        if not isinstance(got, computational_methods.Computed):
            return recompute.MethodFailed(method=source.method,
                                          error=f"returned {type(got).__name__}, not a Computed")
        if claim.setup:
            disagreement = self._first_setup_disagreement(claim.setup, got.setup)
            if disagreement is not None:
                return disagreement
        if math.isnan(got.value):
            return recompute.NotANumber(method=source.method)
        assert claim.value is not None              # an artifact claim always has a value
        tol = claim.recompute_tolerance()
        if abs(got.value - claim.value) > tol:
            return recompute.Disagrees(paper_value=claim.value, value=got.value, n=got.n,
                                       tolerance=tol)
        return recompute.Matches(value=got.value, n=got.n, interval=got.interval, tolerance=tol)

    def _computed(self, source: inputs.ArtifactSource,
                  provenance_result: provenance.ProvenanceResult) -> object:
        """What the method of `source` gives for its path, stored or from a new call.

        Only a Computed that is not NaN is stored; a failure can be temporary, such as a
        time-out, so it is never stored.
        """
        method = self.computational_methods[source.method]
        fingerprint = (self.cache.recompute_fingerprint(method, source, provenance_result)
                       if self.cache is not None else None)
        if fingerprint is not None and self.cache is not None:
            stored = self.cache.stored_computed(fingerprint)
            if stored is not None:
                return stored
        got = method.function(source.path)
        if (fingerprint is not None and self.cache is not None
                and isinstance(got, computational_methods.Computed) and not math.isnan(got.value)):
            self.cache.store_computed(fingerprint, got)
        return got

    @staticmethod
    def _first_setup_disagreement(stated: Mapping[str, inputs.Scalar],
                                  reported: Mapping[str, inputs.Scalar] | None
                                  ) -> recompute.SetupDisagrees | recompute.SetupUnrecorded | None:
        """The first field of the setup of the claim that the method does not report as stated."""
        if reported is None:
            return recompute.SetupUnrecorded(field=None)
        for field, want in stated.items():
            got = reported.get(field)
            if got is None:
                return recompute.SetupUnrecorded(field=field)
            if not scalars_are_equal(got, want):
                return recompute.SetupDisagrees(field=field, paper_says=str(want),
                                                record_says=str(got))
        return None
