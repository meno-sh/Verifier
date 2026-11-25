"""Provenance: is the source behind each claim real, present and unchanged?

The half that earns its keep is refusing to pass a claim whose artifact no longer
exists: that turns a silent future problem into a loud present one, at the moment
the claim enters the paper. It also checks the artifact's recorded checksum. It only
looks at files, never at what is in them.

Whether the number comes back from the artifact, and whether the run's own setup
matches the claim's, is a separate question, answered by core/gate/recompute.py through
the claim's method. It runs only after provenance finds the artifact intact.
"""
import glob
import os

from core.gate import artifact_pins
from schema import inputs, provenance


class ProvenanceChecker:
    """Checks that the source of a claim exists and has not changed."""

    def __init__(self, pins: artifact_pins.ArtifactPins) -> None:
        """Check the files of artifact sources with `pins`."""
        self.pins = pins

    def check_claim_source(self, claim: inputs.Claim) -> provenance.ProvenanceResult:
        """Check the source of one claim.

        Returns:
            Intact or Traced when the claim can go on; Derived when it is inferred from
            other claims, which leaves no file to check; ProvisionallyIntact when a file
            matches only a checksum a person has been asked to confirm; NoSource when the
            claim register gives no source, and SelfTraced when it traces the claim to
            the note the claim was written in; and one of the broken results when the
            file or its anchor is gone or has changed.
        """
        source = claim.source
        if source is None:
            return provenance.NoSource()
        if isinstance(source, inputs.ClaimsSource):
            return provenance.Derived(claims=source.claims)
        if isinstance(source, inputs.DocumentSource):
            if claim.origin is not None and os.path.normpath(source.path) == claim.origin:
                return provenance.SelfTraced(path=source.path)
            return self._check_traced_document(source)
        # only a file counts, as only a file can be pinned (ArtifactPins.pinned_files_at)
        if not any(os.path.isfile(p) for p in glob.glob(source.path)):
            return provenance.Missing(path=source.path)
        compared = self.pins.compare_files_with_pins(source)
        if isinstance(compared, provenance.Changed | provenance.Missing):
            return compared
        if compared:
            return provenance.ProvisionallyIntact(path=source.path, files=compared)
        return provenance.Intact(path=source.path)

    @staticmethod
    def _check_traced_document(source: inputs.DocumentSource
                               ) -> provenance.ProvenanceResult:
        """A traced claim: the log must exist and still contain the anchor text."""
        if not os.path.exists(source.path):
            return provenance.Missing(path=source.path)
        with open(source.path, errors="ignore") as fh:
            text = fh.read()
        if source.must_contain not in text:
            return provenance.AnchorMissing(path=source.path, anchor=source.must_contain)
        return provenance.Traced(path=source.path, anchor=source.must_contain,
                                 reason=source.why)
