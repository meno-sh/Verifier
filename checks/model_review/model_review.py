"""Asks a model if the claims follow from the measurements.

It finds claims that do not follow from what was measured. No arithmetic check finds these.
It is off by default, because it costs money. Its run options (RUN_OPTIONS) turn it on,
name the model, and save or reuse a reading. The verdict comes from the saved reading of
the model. A rerun can give a different reading, but one reading always gives the same
verdict.
"""
import os
from collections.abc import Mapping

from checks import interface, model_reading
from schema.primitives import CheckResult, Off, ProblemKind, Severity, Skipped
from utils import models, shared_paper_prefix, spending

GROUP_NAME = "model_review"
WANT = interface.RunOption(key="want_model_review", kind=bool, flag="--model-review",
                          default=False,
                          help="run the model review (calls a model; costs money)")
MODEL = interface.RunOption(key="model_review_model", kind=str, flag="--model-review-model",
                           default=models.preset.HAIKU,
                           help="the model the model review asks, as a model name "
                                "(utils/models.py)")
OUT = interface.RunOption(key="model_review_out", kind=str, flag="--model-review-out",
                         watched="readings/model_review.txt", in_state=True,
                         help="write the model's reading here, so a person can read it and "
                              "reuse it with --frozen-model-review")
FROZEN = interface.RunOption(key="frozen_model_review", kind=str,
                            flag="--frozen-model-review",
                            help="use a saved reading in place of a model call")
RUN_OPTIONS = (WANT, MODEL, OUT, FROZEN)


@interface.options_rule(check=GROUP_NAME)
def options_fit(options: Mapping[str, object]) -> None:
    """Refuse model review options that cannot work, before any model is called.

    Raises:
        ValueError: both a model call and a saved reading are asked for, the saved
            reading is not a file, or the model is not a model name or its host is unknown.
        models.KeyMissing: a model call is asked for, and the model's key is not set.
    """
    frozen = FROZEN.value_in_options(options)
    if frozen is not None:
        if WANT.value_in_options(options):
            raise ValueError(f"{WANT.key} ({WANT.flag}) and {FROZEN.key} ({FROZEN.flag}) "
                             "cannot both be set; give one")
        if not os.path.isfile(frozen):
            raise ValueError(f"{FROZEN.key}: {frozen} is not a file")
    elif WANT.value_in_options(options):
        models.check_key(MODEL.value_in_options(options))

# The finding codes the prompt asks for, with their severities. A claim stronger than
# its evidence is a weak result; the rest misdescribe the paper. A finding with any
# other code is kept as UNUSABLE_FINDING, which blocks so a person reads it.
CLAIM_EXCEEDS_EVIDENCE = ProblemKind(code="claim-exceeds-evidence", severity=Severity.STRENGTH)
MECHANISM_NOT_ESTABLISHED = ProblemKind(code="mechanism-not-established",
                                        severity=Severity.PRESENTATION)
CONTRIBUTION_NOT_DEMONSTRATED = ProblemKind(code="contribution-not-demonstrated",
                                            severity=Severity.PRESENTATION)
METHOD_DRIFT = ProblemKind(code="method-drift", severity=Severity.PRESENTATION)
TERM_DRIFT = ProblemKind(code="term-drift", severity=Severity.PRESENTATION)
CAPTION_MISDESCRIBES = ProblemKind(code="caption-misdescribes", severity=Severity.PRESENTATION)
SCOPE_MISMATCH = ProblemKind(code="scope-mismatch", severity=Severity.PRESENTATION)
UNUSABLE_FINDING = ProblemKind(code="model-review-unusable-finding",
                               severity=Severity.PRESENTATION)
REVIEW_KINDS = (CLAIM_EXCEEDS_EVIDENCE, MECHANISM_NOT_ESTABLISHED, CONTRIBUTION_NOT_DEMONSTRATED,
                METHOD_DRIFT, TERM_DRIFT, CAPTION_MISDESCRIBES, SCOPE_MISMATCH)
REVIEW_CODES = {kind.code: kind for kind in REVIEW_KINDS}

PROMPT = """You are auditing a research paper for SEMANTIC COHERENCE.

You are NOT checking arithmetic, formatting, citations or spelling. Other tools do
that. You are checking whether what the paper CLAIMS follows from what it actually
DID and MEASURED.

Report a finding only when you can point to specific text. Do not speculate about
work the authors might have done. If the paper is coherent, return an empty list.

Look for these, in rough order of importance:

1. CLAIM EXCEEDS EVIDENCE. The abstract, introduction or conclusion asserts more
   than the results section supports: a stronger effect, a broader population, a
   causal claim from correlational evidence, or a general conclusion from one
   setting.

2. MECHANISM NOT ESTABLISHED. The paper names a mechanism for its result when the
   measurement it used cannot separate that mechanism from an alternative. Example
   shape: claiming a method removes bias X, when the metric used cannot distinguish
   bias X from unrelated property Y, and the paper never rules out Y.

3. CONTRIBUTION NOT DEMONSTRATED. Something listed as a contribution is never shown
   anywhere in the results.

4. METHOD DRIFT. The method described in the methodology is not the method the
   experiments report running --- a different model, a different objective, a
   different elicitation, a different dataset.

5. TERM DRIFT. A term is used to mean different things in different places, or two
   different terms are used for the same thing in a way that hides a difference.

6. CAPTION MISDESCRIBES. A figure or table caption describes something other than
   what the figure or table shows.

7. SCOPE MISMATCH. A limitation acknowledged in the discussion contradicts a claim
   made earlier without qualification, or a caveat that should apply to a headline
   claim appears only in a footnote.

Return STRICT JSON, with no commentary around it:

{"findings": [
   {"code": "claim-exceeds-evidence" | "mechanism-not-established" |
            "contribution-not-demonstrated" | "method-drift" | "term-drift" |
            "caption-misdescribes" | "scope-mismatch",
    "where": "<short location, e.g. 'abstract' or 'Section 4.2'>",
    "quote": "<the exact sentence from the paper, verbatim, under 200 characters>",
    "why": "<one sentence: what the paper claims, and what the evidence actually supports>"}
]}
"""


def run(full: str, model: str = MODEL.default, out_path: str | None = None,
        timeout: int = 180) -> CheckResult:
    """Ask a model to read the paper and report semantic incoherence.

    Writes the raw model output to `out_path` when given, so the reading itself is
    reviewable and can be amended by hand before it feeds a verdict.

    Returns:
        Passed or Flagged from the model's reading.

    Raises:
        models.KeyMissing: the model's key is not set.
        models.ProviderError, models.BadReply: the model failed after its retries
            (utils/models.py); a reply that is not a reading is asked for again.
        shared_paper_prefix.PaperTooLongForModelContext: the paper is too long for the model.
    """
    with spending.purpose("model_review"):
        text, result = models.ask_and_read(
            PROMPT, model, lambda text: (text, parse_reply(text)), timeout,
            shared_prefix_for_cache=shared_paper_prefix.shared_paper_prefix_for_model_calls(full))
    if out_path:
        with open(out_path, "w") as fh:
            fh.write(text)
    return result


# the reply reading both review checks share (checks/model_reading.py)
_READER = model_reading.FindingsReader(REVIEW_CODES, UNUSABLE_FINDING)
from_frozen = _READER.from_frozen
read_reply = _READER.read_reply
parse_reply = _READER.parse_reply


class ModelReviewChecks(interface.PaperCheckGroup):
    """Asks a model if the claims follow from the measurements.

    It gives one result for the whole group, because one model reading covers all its
    kinds. A requested review that gives no reading must not let the run pass.
    """
    name = GROUP_NAME
    title = "Model review"
    why = "Asks a model if the claims follow from what was measured."
    problem_kinds = (*REVIEW_KINDS, UNUSABLE_FINDING)
    must_run_if_requested = True

    def run(self, context: interface.PaperContext) -> interface.PaperOutcome:
        """A saved reading, a fresh one, or Off if not asked."""
        frozen = FROZEN.value_in_options(context.options)
        if frozen is not None:
            result = from_frozen(frozen)
        elif WANT.value_in_options(context.options):
            out = OUT.value_in_options(context.options)
            if out is not None:
                os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            try:
                result = run(context.paper.text, MODEL.value_in_options(context.options), out)
            except (models.ProviderError, models.BadReply) as e:
                # a review that was asked for and gives no reading must not let the run pass
                result = Skipped(because=f"the model review's model failed: {e}")
            except shared_paper_prefix.PaperTooLongForModelContext as e:
                # a long paper stops this review only, not the run
                result = Skipped(because=f"the model review did not run: {e}")
        else:
            result = Off()
        return interface.PaperOutcome(checks={"model_review": result}, facts={})

    def results_when_switched_off(self) -> dict[str, CheckResult]:
        """One Off result, named after the group."""
        return {GROUP_NAME: Off()}



