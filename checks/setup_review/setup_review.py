"""Asks a model to review the paper with its setup register as a checklist.

The setup register (utils/claim_files.SETUP_REGISTER_FILE) holds the specific setup facts that
the labeller keeps out of the claims: models, data, metrics, settings,
hyperparameters, definitions, notation, equations and method steps. Claim checks never read
them. This check gives a model the paper and the register, and asks it to check each fact
against the rest of the paper, plus the usual semantic review. It shares the
reply reading of checks/model_review (checks/model_reading.py), with the prompt tuned on 28 papers
outside the 23 of the ICLR eval. It is on by default, but runs only when the project has a
setup register, so a plain static check calls no model.
"""
import os
from collections.abc import Mapping
from pathlib import Path

from checks import interface, model_reading
from schema.primitives import CheckResult, Off, ProblemKind, Severity, Skipped
from utils import checksums, claim_files, file_storage, models, shared_paper_prefix, spending

GROUP_NAME = "setup_review"
TIMEOUT = 900          # seconds for one reading: the whole paper, with reasoning
WANT = interface.RunOption(key="want_setup_review", kind=bool, flag="--setup-review",
                          default=True,
                          help="run the setup review when the paper has a setup register: a "
                               "model reads the paper with the register (on by default; "
                               "calls a model; costs money)")
MODEL = interface.RunOption(key="setup_review_model", kind=str, flag="--setup-review-model",
                           default=models.preset.OPUS,
                           help="the model the setup review asks, as a model name "
                                "(utils/models.py)")
EFFORT = interface.RunOption(key="setup_review_effort", kind=str, flag="--setup-review-effort",
                            default="medium",
                            help="the reasoning effort of the setup review's model")
REGISTER = interface.RunOption(key="setup_register_path", kind=str, flag="--setup-register",
                              watched="setup_register.json", in_state=True,
                              help="the setup register the review reads (written by "
                                   "vcheck label-candidates)")
OUT = interface.RunOption(key="setup_review_out", kind=str, flag="--setup-review-out",
                         watched="readings/setup_review.txt", in_state=True,
                         help="write the model's reading here, so a person can read it and "
                              "reuse it with --frozen-setup-review")
FROZEN = interface.RunOption(key="frozen_setup_review", kind=str,
                            flag="--frozen-setup-review",
                            help="use a saved reading in place of a model call")
RUN_OPTIONS = (WANT, MODEL, EFFORT, REGISTER, OUT, FROZEN)


@interface.options_rule(check=GROUP_NAME)
def options_fit(options: Mapping[str, object]) -> None:
    """Refuse setup review options that cannot work, before any model is called.

    Raises:
        ValueError: both a model call and a saved reading are asked for, the saved
            reading is not a file, or the model is not a model name or its host is unknown.
        models.KeyMissing: a model call is asked for, the paper has a setup register, and
            the model's key is not set.
    """
    frozen = FROZEN.value_in_options(options)
    if frozen is not None:
        # the review is on by default, so only a request set by hand clashes with a reading
        if options.get(WANT.key) is True:
            raise ValueError(f"{WANT.key} ({WANT.flag}) and {FROZEN.key} ({FROZEN.flag}) "
                             "cannot both be set; give one")
        if not os.path.isfile(frozen):
            raise ValueError(f"{FROZEN.key}: {frozen} is not a file")
    elif WANT.value_in_options(options):
        if problem := models.settings_problem(MODEL.value_in_options(options),
                                              EFFORT.value_in_options(options), TIMEOUT,
                                              (MODEL.key, EFFORT.key, "the timeout")):
            raise ValueError(problem)
        if has_register(options):
            models.check_key(MODEL.value_in_options(options))


def has_register(options: Mapping[str, object]) -> bool:
    """Whether the options name a setup register file that exists.

    The review is on by default but runs only on a paper with a register, which
    vcheck label-candidates writes. A plain static check has none, so it neither calls a
    model nor needs a key.
    """
    path = REGISTER.value_in_options(options)
    return path is not None and os.path.isfile(path)


def _kind(code: str, severity: Severity) -> tuple[str, ProblemKind]:
    """The prompt's code, and its kind here.

    The kind's code has "setup-review-" in front, since model_review claims the same codes
    and a code belongs to one check.
    """
    return code, ProblemKind(code=f"setup-review-{code}", severity=severity)


# The finding codes the prompt asks for. A fault in the setup, a definition, a derivation,
# an ablation or a number makes the result unsound; a claim stronger than its evidence is a
# weak result; the rest misdescribe the paper. Any other code is UNUSABLE_FINDING.
REVIEW_CODES = dict([
    _kind("claim-exceeds-evidence", Severity.STRENGTH),
    _kind("mechanism-not-established", Severity.PRESENTATION),
    _kind("contribution-not-demonstrated", Severity.PRESENTATION),
    _kind("method-drift", Severity.PRESENTATION),
    _kind("term-drift", Severity.PRESENTATION),
    _kind("caption-misdescribes", Severity.PRESENTATION),
    _kind("scope-mismatch", Severity.PRESENTATION),
    _kind("definition-fault", Severity.SOUNDNESS),
    _kind("derivation-fault", Severity.SOUNDNESS),
    _kind("setup-inconsistency", Severity.SOUNDNESS),
    _kind("ablation-mismatch", Severity.SOUNDNESS),
    _kind("number-inconsistency", Severity.SOUNDNESS),
])
REVIEW_KINDS = tuple(REVIEW_CODES.values())
UNUSABLE_FINDING = ProblemKind(code="setup-review-unusable-finding",
                               severity=Severity.PRESENTATION)

# The prompt, after the shared paper prefix: {register} is the setup register block (or nothing).
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

CHECK THE DETAILS AS CAREFULLY AS THE CLAIMS.

Errors are often not in the headline claims. They sit in the paper's details: the
problem setup, definitions, notation, equations and derivations, the description of
the data and models, the design of baselines and ablations, and the entries of
tables. A wrong detail can break a result even when every claim sounds reasonable.
Read these parts line by line, and look for these kinds of faults:

8. DEFINITION FAULT. A definition is circular (it uses the term it defines), is
   missing, or is too vague to carry out: the paper relies on a quantity, criterion
   or procedure but never says how it is computed. Also: a symbol or quantity is
   defined one way and later used in a way that does not fit that definition.

9. DERIVATION FAULT. A step in an equation, derivation or proof does not follow.
   Check each step against the paper's own setting: a cited theorem or known result
   is applied outside the conditions under which it holds (for example, a result
   proved for one objective or assumption is used for a modified one); one quantity
   is silently swapped for another (a different variable, distribution, index or
   set); the stated conclusion of a proof is about a different object than the
   equation it claims to establish; or an equation does not match the text that
   describes it.

10. SETUP INCONSISTENCY. The setup, data or model description disagrees with
   itself or with what the experiments use: dataset sizes, splits, model names,
   hyperparameters or evaluation settings that differ between sections, tables and
   appendices.

11. ABLATION OR BASELINE MISMATCH. A baseline or ablation does not test what its
   name or the text says it tests: it removes or changes something other than the
   named component, changes several things at once, or no variant removes the
   component that the paper credits for the result.

12. TABLE OR NUMBER INCONSISTENCY. Numbers that the paper itself makes comparable
   disagree: a reported difference or percentage change does not match the values
   it is computed from, a number in the text differs from the same number in a
   table, or totals do not match their parts. This is not routine rounding; report
   it only when the mismatch changes what a reader would conclude. Also: a
   comparison table marks a property for a method (the paper's own or a prior one)
   that conflicts with the paper's own description of that method or with
   well-established facts about it.

If a SETUP REGISTER follows, it lists the paper's specific setup facts,
copied from it: which model, data, metric, test or setting is used and how,
hyperparameters and other numbers of the setup, definitions and notation, equations,
and steps of the method or algorithm. Use it as a checklist for faults 4, 5 and 8 to
12: go through its lines one by one, find every other place in the paper that states
or uses the same thing, and check that they agree. The register may be incomplete,
so still read the whole paper. Quote the paper's own words in your findings, not the
register id.

Order the findings from the most to the least damaging to the paper's results.

Return STRICT JSON, with no commentary around it:

{"findings": [
   {"code": "claim-exceeds-evidence" | "mechanism-not-established" |
            "contribution-not-demonstrated" | "method-drift" | "term-drift" |
            "caption-misdescribes" | "scope-mismatch" | "definition-fault" |
            "derivation-fault" | "setup-inconsistency" | "ablation-mismatch" |
            "number-inconsistency",
    "where": "<short location, e.g. 'abstract', 'Section 4.2', 'Eq. 3' or 'Table 2'>",
    "quote": "<the exact sentence from the paper, verbatim, under 200 characters>",
    "why": "<one sentence: what the paper says, and what is wrong with it>"}
]}

{register}"""
REGISTER_HEAD = "SETUP REGISTER (specific setup facts copied from the paper above):"
REGISTER_TEXT_CHARACTERS = 600


def register_lines(register: Mapping[str, Mapping[str, object]]) -> str:
    """The setup register as the prompt shows it: one line "[id | kind | section] text" a fact."""
    lines = []
    for cid, fact in register.items():
        section = str(fact.get("section") or "").split(" > ")[-1][:60]
        words = " ".join(str(fact.get("text", "")).split())[:REGISTER_TEXT_CHARACTERS]
        lines.append(f"[{cid.split('#')[-1]} | {fact.get('kind')} | {section}] {words}")
    return "\n".join(lines)


def prompt_text(register: Mapping[str, Mapping[str, object]] | None) -> str:
    """The prompt after the paper, with its setup register (no register block when None)."""
    block = ("" if not register
             else f"\n{REGISTER_HEAD}\n---\n{register_lines(register)}\n---\n")
    return PROMPT.replace("{register}", block)


def run(full: str, register: Mapping[str, Mapping[str, object]] | None,
        model: str = MODEL.default, effort: str = EFFORT.default, out_path: str | None = None,
        timeout: int = TIMEOUT) -> CheckResult:
    """Ask a model to review the paper with its setup register.

    Writes the raw model output to `out_path` when given, so the reading itself is
    reviewable and can be amended by hand before it feeds a verdict. A reading saved before
    from the same prompt, model and effort is read in place of a call (_saved_reading).

    Raises:
        models.KeyMissing: the model's key is not set.
        models.ProviderError, models.BadReply: the model failed after its retries
            (utils/models.py); a reply that is not a reading is asked for again.
        shared_paper_prefix.PaperTooLongForModelContext: the paper is too long for the model.
    """
    paper_prefix = shared_paper_prefix.shared_paper_prefix_for_model_calls(full)
    prompt = prompt_text(register)
    saved = _saved_reading(out_path,
                           checksums.sha256_of_json([model, effort, paper_prefix, prompt]))
    if saved is not None and saved.is_file():
        return from_frozen(str(saved))
    with spending.purpose("setup_review"):
        reply, result = models.ask_and_read(prompt, model, lambda t: (t, parse_reply(t)),
                                            timeout, reasoning=effort,
                                            shared_prefix_for_cache=paper_prefix)
    if out_path:
        file_storage.AtomicallyWrittenFile(Path(out_path)).write_text(reply)
        assert saved is not None
        saved.parent.mkdir(parents=True, exist_ok=True)
        file_storage.AtomicallyWrittenFile(saved).write_text(reply)
    return result


def _saved_reading(out_path: str | None, key: str) -> Path | None:
    """Where the reading of the prompt with hash `key` is kept, next to `out_path`.

    Each reading is kept under the hash of its prompt, model and effort, so a round whose
    document and register did not change reads it in place of a new call, and the
    documents of one project do not overwrite each other's. `out_path` holds the latest
    reading, for a person to read.
    """
    return None if not out_path else Path(out_path + ".by-prompt") / f"{key}.txt"


# the reply reading both review checks share (checks/model_reading.py)
_READER = model_reading.FindingsReader(REVIEW_CODES, UNUSABLE_FINDING)
from_frozen = _READER.from_frozen
read_reply = _READER.read_reply
parse_reply = _READER.parse_reply


class SetupReviewChecks(interface.PaperCheckGroup):
    """Asks a model to review the paper with its setup register as a checklist.

    It gives one result for the whole group, because one model reading covers all its
    kinds. A requested review that gives no reading must not let the run pass, so a model
    failure is Skipped. With no setup register file the review does not run (Off), since it
    is on by default. The register covers the whole project, and each document is reviewed
    with all of it (the check is not told which document it reads).
    """
    name = GROUP_NAME
    title = "Setup review"
    why = "Asks a model to check the paper's setup facts against the rest of the paper."
    problem_kinds = (*REVIEW_KINDS, UNUSABLE_FINDING)
    must_run_if_requested = True

    def run(self, context: interface.PaperContext) -> interface.PaperOutcome:
        """A saved reading, a fresh one, or Off if not asked or there is no setup register."""
        frozen = FROZEN.value_in_options(context.options)
        if frozen is not None:
            result = from_frozen(frozen)
        elif WANT.value_in_options(context.options) and has_register(context.options):
            out = OUT.value_in_options(context.options)
            if out is not None:
                os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            try:
                path = REGISTER.value_in_options(context.options)
                assert isinstance(path, str)              # has_register found the file
                register = claim_files.read_setup_register(Path(path))
            except claim_files.ClaimFilesError as e:
                return interface.PaperOutcome(
                    checks={GROUP_NAME: Skipped(because=f"the setup register cannot be read: {e}")},
                    facts={})
            if not register:
                result = Off()
            else:
                try:
                    result = run(context.paper.text, register,
                                 MODEL.value_in_options(context.options),
                                 EFFORT.value_in_options(context.options), out)
                except (models.ProviderError, models.BadReply) as e:
                    # a review that was asked for and gives no reading must not let the run pass
                    result = Skipped(because=f"the setup review's model failed: {e}")
                except shared_paper_prefix.PaperTooLongForModelContext as e:
                    # a long paper stops this review only, not the run
                    result = Skipped(because=f"the setup review did not run: {e}")
        else:
            result = Off()
        return interface.PaperOutcome(checks={GROUP_NAME: result}, facts={})

    def results_when_switched_off(self) -> dict[str, CheckResult]:
        """One Off result, named after the group."""
        return {GROUP_NAME: Off()}
