"""Uncertainty: how far does the full paper support a claim?

A quantifier scores the breakage with no attack: how far the full paper alone leaves the
claim open. Then a red team writes hypothetical follow-up results, and the quantifier
scores them. The claim's uncertainty is the higher of the two scores. The red team runs
also when the breakage with no attack alone passes the threshold: the flag stays the same,
and the worst attack gives the worst-case text of the report. Each reading is saved and
reused. The parts are registered in RED_TEAMS and QUANTIFIERS, and are built from the run
options, so their settings are part of every reading's binding.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from checks import interface
from checks.uncertainty import scenario
from schema import inputs, provenance, recompute
from schema.primitives import (
    CheckResult,
    ClaimId,
    Flagged,
    ModelFailedAfterRetries,
    Off,
    Passed,
    Problem,
    ProblemKind,
    Severity,
    Skipped,
    read_only,
    record,
)
from utils import alerts, file_storage, models, shared_paper_prefix

# The problem kinds this check reports, each with its severity.
# the claim's uncertainty (of its step, or compounded over every step it rests on) is above
# the threshold;
# also when the breakage with no attack alone puts it above the threshold
UNCERTAIN_INFERENCE = ProblemKind(code="uncertain-inference", severity=Severity.STRENGTH)

# The run options this check reads.
WANT = interface.RunOption(
    key="want_claim_uncertainty", kind=bool, flag="--claim-uncertainty", default=True,
    help="red-team each claim inferred from other claims, for each one with no saved "
         "reading (calls models; costs money; "
         "--no-claim-uncertainty turns it off)")
READINGS = interface.RunOption(
    key="claim_uncertainty_readings", kind=str, flag="--uncertainty-readings",
    watched="readings/claim_uncertainty.json", in_state=True,
    help="the saved readings of claim_uncertainty: read, and added to")
RED_TEAM = interface.RunOption(key="red_team", kind=str, flag="--red-team", default="prompt",
                                 help="the red team; only `prompt` exists")
QUANTIFIER = interface.RunOption(key="quantifier", kind=str, flag="--quantifier",
                                   default="judge",
                                   help="how attacks are scored: judge or coherence")
MAX_EVALS = interface.RunOption(
    key="max_uncertainty_evals_per_claim", kind=int, flag="--max-uncertainty-evals-per-claim",
    default=64, help="score calls for each claim; each attack the red team writes is scored once")
SCENARIO_MODEL = interface.RunOption(
    key="scenario_model", kind=str, flag="--scenario-model", default=models.preset.HAIKU,
    help="the model that writes the worst case of a flagged claim as the gap it shows, as a "
         "model name (utils/models.py)")
CUTOFF = interface.RunOption(
    key="uncertainty_cutoff", kind=float, flag="--uncertainty-cutoff", default=0.59,
    help="from 0 to 1: an attack counts when it scores above this on both scores, and a claim "
         "is flagged when the paper alone or a counting attack scores above this")
THRESHOLD = CUTOFF
PROPAGATE = interface.RunOption(
    key="propagate_claim_uncertainty", kind=bool, flag="--propagate-claim-uncertainty",
    default=False,
    help="compound the uncertainty of the claims a claim rests on into its own; then a claim "
         "whose premise has no known uncertainty is skipped (default: the claim's own step, "
         "with its premises taken as true)")
RUN_OPTIONS = (WANT, READINGS, RED_TEAM, QUANTIFIER, MAX_EVALS, SCENARIO_MODEL, THRESHOLD,
               PROPAGATE)


# What the red team and the quantifier read and write.

@record
class Premise:
    """One claim as the red team and the quantifier read it."""
    id: ClaimId
    statement: str
    value: float | None


@record
class Hypothetical:
    """A result of a follow-up experiment that nobody did."""
    design: str                         # the follow-up experiment: model, data, setting
    result: str                         # the made-up result, as a low-level claim would say it

    def __post_init__(self) -> None:
        """Refuse an empty design or result."""
        for name in ("design", "result"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Hypothetical.{name} must be non-empty text, got {value!r}")


@record
class Attack:
    """Hypothetical results the red team proposes together against one claim."""
    hypotheticals: tuple[Hypothetical, ...]

    def __post_init__(self) -> None:
        """Refuse an attack with no hypothetical results, or with something else in it."""
        if not isinstance(self.hypotheticals, tuple) or not self.hypotheticals:
            raise ValueError("an Attack needs a non-empty tuple of hypothetical results")
        if not all(isinstance(h, Hypothetical) for h in self.hypotheticals):
            raise TypeError("an Attack holds only Hypothetical results")


@record
class Assessment:
    """The quantifier's two numbers for one attack, each from 0 to 1."""
    believability: float                # how well the attack agrees with the premises
    breakage: float                     # how much premises and attack together go against the claim

    def __post_init__(self) -> None:
        """Refuse a number that is not a finite number from 0 to 1."""
        check_unit_number(self.believability, "Assessment.believability")
        check_unit_number(self.breakage, "Assessment.breakage")


type Assessed = tuple[tuple[Attack, Assessment], ...]     # attacks, each with its scores


@record
class Local:
    """The uncertainty of one step of inference, from its premises to the claim."""
    # 1 - P(claim | every premise true), from 0 to 1
    value: float
    # the believable attack that decreases belief in the claim most; None when no attack is
    worst: Attack | None

    def __post_init__(self) -> None:
        """Refuse a value that is not a finite number from 0 to 1."""
        check_unit_number(self.value, "Local.value")


@record
class Reading:
    """Everything one run of the two parts gave for one claim, as saved in the readings file."""
    local: Local
    assessed: Assessed                  # every attack returned, each scored once
    red_team: str                       # "name@version"
    quantifier: str                     # "name@version"
    calls: int                          # every score call; each attack is scored once
    # the breakage with no attack: how much the premises alone go against the claim
    without_attack: float
    # False when the breakage with no attack passed the threshold, so no red team ran
    attacked: bool

    def __post_init__(self) -> None:
        """Refuse a reading with no attack that is not the breakage with no attack alone."""
        check_unit_number(self.without_attack, "Reading.without_attack")
        if not isinstance(self.attacked, bool):
            raise TypeError(f"Reading.attacked must be a bool, got {self.attacked!r}")
        if not self.attacked and (self.assessed or self.calls or self.local.worst is not None
                                  or self.local.value != self.without_attack):
            raise ValueError("a reading with no red team must have no attacks, no score "
                             "calls, and the breakage with no attack as its value")


def check_unit_number(value: object, name: str) -> None:
    """Raise unless `value` is a finite int or float from 0 to 1 that is not a bool."""
    if (isinstance(value, bool) or not isinstance(value, int | float)
            or not math.isfinite(value) or not 0 <= value <= 1):
        raise ValueError(f"{name} must be a finite number from 0 to 1, got {value!r}")


# The two parts.

class EvalLimitReached(Exception):
    """A claim needed more score calls than max_uncertainty_evals_per_claim allows."""


class RedTeam(Protocol):
    """Part 2: writes attacks against one claim, given the claims it is inferred from."""
    name: str
    version: str                        # change it whenever the prompt or the model changes
    template: bool                      # True while `attack` is not written; never called

    def attack(self, claim: Premise, premises: tuple[Premise, ...],
               score: Callable[[Attack], Assessment]) -> tuple[Attack, ...]:
        """The attacks to score. `score` raises EvalLimitReached at the limit."""
        ...


class Quantifier(Protocol):
    """Part 3: scores attacks, and turns the scores into the uncertainty of one step."""
    name: str
    version: str                        # change it whenever the prompt or the model changes
    template: bool                      # True while `assess` is not written; never called

    def breakage_without_attack(self, claim: Premise, premises: tuple[Premise, ...]) -> float:
        """How much the premises alone go against the claim, from 0 to 1, with no attack."""
        ...

    def assess(self, claim: Premise, premises: tuple[Premise, ...],
               attack: Attack) -> Assessment:
        """Score one attack against the claim, taking the premises as true."""
        ...

    def uncertainty(self, assessed: Assessed) -> Local:
        """The uncertainty of the step from the premises to the claim."""
        ...


# The registered parts, by name. Each builds the part from the run options, and reads
# only its own keys. A concrete part adds itself here.
RED_TEAMS: dict[str, Callable[[Mapping[str, object]], RedTeam]] = {}
QUANTIFIERS: dict[str, Callable[[Mapping[str, object]], Quantifier]] = {}


# Compounding.

def propagate(local: float, dependencies: Sequence[float]) -> float:
    """The uncertainty of a claim, from its own step and the uncertainty of each dependency.

    P(claim) >= P(claim | every dependency true) * P(every dependency true), and
    P(every dependency true) is taken as 1 - the largest P(dependency false). That is
    exact when the dependencies fail together, and too high when they fail apart, so the
    result does not bound the uncertainty from above: with 1 - the sum of P(dependency
    false) it would, at the cost of a value that grows with the number of dependencies.
    """
    check_unit_number(local, "a local uncertainty")
    for u in dependencies:
        check_unit_number(u, "a dependency's uncertainty")
    together = 1.0 - max(dependencies, default=0.0)
    return 1.0 - (1.0 - local) * together


# The check.

CHECK = "claim_uncertainty"


class ClaimUncertaintyCheck(interface.ClaimCheck):
    """The claim's uncertainty as `measured`, flagged when it is above the threshold."""
    name = CHECK
    title = "Uncertainty of an inferred claim"
    why = ("Red-teams the step from the claims a claim is inferred from to the claim; "
           "with propagate_claim_uncertainty, also compounds the uncertainty of every step "
           "it rests on.")
    problem_kinds = (UNCERTAIN_INFERENCE,)
    needs_dependencies = True
    # every option, because the red team and the quantifier read their own keys
    input_scope = interface.CheckInputScope(
        claims=interface.PREMISES, option_keys=None,
        other_files_read=lambda claim, options: _readings_files(options),
        reads_full_paper_text=True)

    def reads_findings_of_claims_it_rests_on(self, options: Mapping[str, object]) -> bool:
        """Only with propagate_claim_uncertainty, which compounds the claims' uncertainty."""
        return bool(PROPAGATE.value_in_options(options))

    def check(self, claim: inputs.Claim, recomputed: recompute.Matches | None,
              context: interface.ClaimContext) -> CheckResult:
        """The claim's uncertainty as `measured`, flagged when it is above the threshold.

        Flagged with no attack when the breakage with no attack alone puts the claim above
        the threshold. Off when the check was not asked for and no reading is saved. Skipped
        when propagate_claim_uncertainty is on and a claim it rests on has no known
        uncertainty, a part is a template that is not written
        yet, the claim needs more score calls than max_uncertainty_evals_per_claim allows
        (also a `warn` in the alert log), or a part's model failed after its retries (also
        an `error` in the alert log). The core checks the options first (options_fit).
        """
        options = context.options
        want = WANT.value_in_options(options)
        path = READINGS.value_in_options(options)
        if not want and (path is None or not Path(path).exists()):
            return Off()
        max_evals = MAX_EVALS.value_in_options(options)
        threshold = THRESHOLD.value_in_options(options)
        red_team = _part(RED_TEAMS, RED_TEAM.value_in_options(options))(options)
        quantifier = _part(QUANTIFIERS, QUANTIFIER.value_in_options(options))(options)

        this = premise(claim)
        premises = tuple(premise(context.register[d])
                         for d in claim.ids_of_claims_it_is_inferred_from())
        global REASONS_FILE
        REASONS_FILE = Path(path).with_name("claim_uncertainty_reasons.jsonl")
        premises_of_register = premises
        premises = (paper_premise(context.full_paper_text),)
        key = binding(this, premises, red_team, quantifier)
        reading = load_readings(Path(path)).get(key) if path else None
        if reading is None and not want:
            return Off()
        below: list[float] = []
        if PROPAGATE.value_in_options(options):
            below, unknown = _dependency_uncertainties(claim, context)
            if unknown:
                return Skipped(because="; ".join(unknown))

        def passes(local: float) -> bool:
            """True when this step's uncertainty `local` puts the claim above the threshold."""
            return threshold is not None and propagate(local, below) > threshold

        if reading is not None and not reading.attacked and not passes(reading.without_attack):
            # saved with no red team, but the threshold or a dependency changed since then
            if not want:
                return Skipped(because="the saved reading has no attacks, and the breakage with "
                                       "no attack alone no longer passes the threshold")
            reading = None
        if reading is None:
            templates = [p.name for p in (red_team, quantifier) if p.template]
            if templates:
                return Skipped(because=f"claim uncertainty was not run: {' and '.join(templates)} "
                                       "is a template that is not written yet")
            try:
                # the red team always runs, also when the breakage with no attack alone
                # passes the threshold: the worst attack gives the worst-case text
                reading = read(this, premises, red_team, quantifier, max_evals,
                               gate=lambda _: False)
            except EvalLimitReached as e:
                alerts.notify("warn", "uncertainty.limit_reached", f"{claim.id}: {e}",
                              claim=claim.id, red_team=_named(red_team),
                              quantifier=_named(quantifier), max_evals=max_evals)
                return Skipped(because=f"claim uncertainty was not measured: {e}")
            except shared_paper_prefix.PaperTooLongForModelContext as e:
                # a long paper stops this check only, not the run
                alerts.notify("warn", "uncertainty.paper_too_long", f"{claim.id}: {e}",
                              claim=claim.id)
                return Skipped(because=f"claim uncertainty was not measured: {e}")
            except (models.ProviderError, models.BadReply) as e:
                # a model failure that outlasts its retries (utils/models.py) on one claim
                # must not stop the checks of the other claims; nothing is saved, so the
                # next run asks again
                alerts.notify("error", "uncertainty.model_failed", f"{claim.id}: {e}", error=e,
                              claim=claim.id, red_team=_named(red_team),
                              quantifier=_named(quantifier))
                return ModelFailedAfterRetries(because="claim uncertainty was not measured: a "
                                               f"model failed after its retries: {e}")
            save_reading(Path(path), key, reading)
        u = max(propagate(reading.local.value, below), propagate(reading.without_attack, below))
        if threshold is None or u <= threshold:
            return Passed(measured=u)
        worst = None if reading.local.worst is None else _worst_case(
            this, premises_of_register, reading.local.worst, Path(path),
            SCENARIO_MODEL.value_in_options(options), want)
        if passes(reading.without_attack):
            # two bullets: the judge's reasoning for the breakage with no attack, then the
            # worst case
            bullets = [f"- {no_attack_reason(this)}"] + ([] if worst is None else [f"- {worst[1]}"])
            worst = (WORST_CASE_LABEL, "\n".join(bullets))
        return Flagged(problems=(Problem(kind=UNCERTAIN_INFERENCE,
                                         detail=_detail(u, threshold, reading.local),
                                         facts=_facts(u, threshold, reading.local, worst)),),
                       measured=u)


def _worst_case(claim: Premise, premises: tuple[Premise, ...], attack: Attack, readings: Path,
                model: str, want: bool) -> tuple[str, str]:
    """The worst case as one labelled line: the distilled gap, or the attack when there is none.

    The distilled text is read from the file beside the readings. When it is not there and
    the check may call models (`want`), the model writes it and it is saved. When the model
    fails after its retries, the attack is the worst case (also an `error` in the alert log).
    """
    hypotheticals = [(h.design, h.result) for h in attack.hypotheticals]
    premise_texts = [p.statement for p in premises]
    path = scenario.path_beside(readings)
    name = scenario.key(claim.statement, premise_texts, hypotheticals, model)
    text = scenario.saved(path, name)
    if text is None and want:
        try:
            text = scenario.distil(claim.statement, premise_texts, hypotheticals, model)
        except (models.ProviderError, models.BadReply) as e:
            # the attack itself is the worst case; the next run asks again
            alerts.notify("error", "uncertainty.scenario_failed", f"{claim.id}: {e}", error=e,
                          claim=claim.id, model=model)
        else:
            scenario.save(path, name, text)
    if text is None:
        return (UNDISTILLED_LABEL, " ".join(f"{d}: {r}" for d, r in hypotheticals))
    return (WORST_CASE_LABEL, text)


@interface.options_rule(check=CHECK)
def options_fit(options: Mapping[str, object]) -> None:
    """Refuse options of this check that do not fit together.

    Raises:
        ValueError: the check is asked for without a readings file or a threshold, the
            readings file is empty text, a part is not registered, the limit of
            score calls is below 1, or the threshold is not from 0 to 1.
    """
    path = READINGS.value_in_options(options)
    if path is not None and not path.strip():
        raise ValueError(f"{READINGS.key} is empty; name a file")
    if WANT.value_in_options(options) and path is None:
        raise ValueError(f"{WANT.key} needs {READINGS.key} ({READINGS.flag}), so every "
                         "reading is saved")
    # each part has a default, so a name is always there
    for option, table in ((RED_TEAM, RED_TEAMS), (QUANTIFIER, QUANTIFIERS)):
        _part(table, option.value_in_options(options))
    max_evals = MAX_EVALS.value_in_options(options)
    if max_evals < 1:
        raise ValueError(f"{MAX_EVALS.key} must be a whole number of at least 1, got {max_evals!r}")
    threshold = THRESHOLD.value_in_options(options)
    if WANT.value_in_options(options) and threshold is None:
        raise ValueError(f"{WANT.key} needs {THRESHOLD.key} ({THRESHOLD.flag}), so it can "
                         f"flag a claim; or turn the check off with --no-claim-uncertainty")
    if threshold is not None:
        check_unit_number(threshold, THRESHOLD.key)


def _detail(u: float, threshold: float, local: Local) -> str:
    """The sentence for a flagged claim: its uncertainty, and the worst case to rule out.

    The attack illustrates a hypothetical scenario from the red team, not evidence. The
    sentence says so, because a reader can take it for a result that somebody measured.
    """
    out = (f"uncertainty {u:.3g}, above the threshold {threshold:g} "
           f"(this step alone {local.value:.3g})")
    if local.worst is not None:
        out += ("; the worst case to rule out, an illustration of a hypothetical scenario "
                "without necessarily empirical support: "
                + " and ".join(f"{h.design}: {h.result}" for h in local.worst.hypotheticals))
    return out


def _facts(u: float, threshold: float, local: Local,
           worst: tuple[str, str] | None) -> tuple[tuple[str, str], ...]:
    """The parts of `_detail` that the report shows: the uncertainty, then the worst case.

    The detail keeps the uncertainty of this step alone, because it is part of the flag id.
    """
    out = ((UNCERTAINTY_LABEL, f"{u:.3g} (cutoff {threshold:g})"),)
    return out if worst is None else (*out, worst)


def _facts_without_attack(u: float, threshold: float,
                          without_attack: float) -> tuple[tuple[str, str], ...]:
    """The parts of `_detail_without_attack` that the report shows."""
    return ((UNCERTAINTY_LABEL, f"{u:.3g} (cutoff {threshold:g}), with no attack, so no red "
                                "team ran"),)


UNCERTAINTY_LABEL = "Uncertainty"
WORST_CASE_LABEL = "Worst-case scenario to rule out"
# the label when no distilled text is saved and the check calls no model
UNDISTILLED_LABEL = "Worst case to rule out (the red team's text, not distilled)"


def _detail_without_attack(u: float, threshold: float, without_attack: float) -> str:
    """The sentence for a claim flagged before any attack."""
    return (f"uncertainty {u:.3g}, above the threshold {threshold:g} with no attack (this "
            f"step alone, with no attack, {without_attack:.3g}), so no red team ran")


def _part[P](table: Mapping[str, P], name: str) -> P:
    """A registered part by name."""
    if name not in table:
        known = ", ".join(sorted(table)) or "none is registered"
        raise ValueError(f"no part named {name!r}; known are {known}")
    return table[name]


# the judge's reasoning of each breakage, one JSON line each, beside the readings file
REASONS_FILE: Path | None = None
_REASONS_LOCK = threading.Lock()


def record_reason(claim: Premise, attack: Attack | None, breakage: float, reasoning: str) -> None:
    """Append one breakage reasoning of the judge to REASONS_FILE."""
    if REASONS_FILE is None:
        raise RuntimeError("REASONS_FILE is not set")
    row = {"claim": claim.statement, "breakage": breakage, "reasoning": reasoning,
           "attack": (None if attack is None
                      else [[h.design, h.result] for h in attack.hypotheticals])}
    with _REASONS_LOCK:
        REASONS_FILE.parent.mkdir(parents=True, exist_ok=True)     # a first run has no folder
        with REASONS_FILE.open("a") as f:
            f.write(json.dumps(row) + "\n")


def no_attack_reason(claim: Premise) -> str:
    """The last saved reasoning of the breakage with no attack for `claim`, for the report."""
    rows = ([json.loads(line) for line in REASONS_FILE.read_text().splitlines()]
            if REASONS_FILE and REASONS_FILE.exists() else [])
    found = [r for r in rows if r["claim"] == claim.statement and r["attack"] is None]
    return (found[-1]["reasoning"] if found
            else "(no reasoning was saved for the breakage with no attack)")


def paper_premise(full_paper_text: str | None) -> Premise:
    """The full text of the paper, as one premise, in place of the register premises.

    Raises:
        ValueError: the run has no paper text (ClaimContext.full_paper_text).
    """
    if full_paper_text is None:
        raise ValueError("claim uncertainty reads the full paper, but the run has no paper text")
    return Premise(id="paper", statement=full_paper_text, value=None)


def shared_paper_prefix_of_premises(premises: tuple[Premise, ...]) -> str:
    """The shared paper prefix (utils/shared_paper_prefix.py) of the one paper premise.

    Raises:
        ValueError: `premises` is not exactly the one premise that paper_premise() gives.
        shared_paper_prefix.PaperTooLongForModelContext: the paper is too long.
    """
    if len(premises) != 1 or premises[0].id != "paper":
        raise ValueError("the full paper must be the one premise, as paper_premise() gives "
                         f"it; got the premises {[p.id for p in premises]}")
    return shared_paper_prefix.shared_paper_prefix_for_model_calls(premises[0].statement)


def premise(claim: inputs.Claim) -> Premise:
    """A claim as the parts read it. The parser makes sure it has a statement."""
    if claim.statement is None:
        raise ValueError(f"claim {claim.id!r} has no statement, which the parser must refuse")
    return Premise(id=claim.id, statement=claim.statement, value=claim.value)


def _readings_files(options: Mapping[str, object]) -> tuple[str, ...]:
    """The readings file of the run options, and the scenarios file beside it.

    The check reads both and adds to both (_worst_case reads the scenarios).
    """
    path = READINGS.value_in_options(options)
    return (str(path), str(scenario.path_beside(Path(str(path))))) if path else ()


def _dependency_uncertainties(claim: inputs.Claim, context: interface.ClaimContext
                              ) -> tuple[list[float], list[str]]:
    """The uncertainty of each claim this claim is inferred from, and why any is unknown.

    A claim verified by the gate (its file intact and its number recomputed) has
    uncertainty 0. A derived claim has the value this check measured for it. Every other
    claim has no known uncertainty.
    """
    known: list[float] = []
    unknown: list[str] = []
    for dep in claim.ids_of_claims_it_is_inferred_from():
        found = context.dependencies.get(dep)
        if found is None:
            unknown.append(f"{dep} is checked after this claim, because they are on a cycle")
        elif isinstance(found.provenance, provenance.Derived):
            result = found.checks.get(CHECK)
            if isinstance(result, Passed | Flagged) and result.measured is not None:
                known.append(result.measured)
            else:
                why = result.because if isinstance(result, Skipped) else "it was not measured"
                unknown.append(f"{dep} has no known uncertainty: {why}")
        elif found.gate_failed:
            unknown.append(f"{dep} failed its gate")
        elif (isinstance(found.provenance, provenance.Intact)
              and isinstance(found.recompute, recompute.Matches)):
            known.append(0.0)
        else:
            unknown.append(f"{dep} is not verified (provenance "
                           f"{type(found.provenance).__name__}, recompute "
                           f"{type(found.recompute).__name__})")
    return known, unknown


def read(claim: Premise, premises: tuple[Premise, ...], red_team: RedTeam,
         quantifier: Quantifier, max_evals: int, *, gate: Callable[[float], bool]) -> Reading:
    """Score the breakage with no attack; then, unless `gate` is True for it, run the red team.

    `gate` gets the breakage with no attack. When it is True, the reading has no attacks and
    the red team is not called. The breakage with no attack is not a score call, so it does
    not count against `max_evals`.

    Otherwise the red team runs with the quantifier's scores as feedback, and every attack
    is scored. The red team never reports its own scores. This function keeps each score it gives
    the red team, and scores only the returned attacks that are not scored yet. So each
    attack is scored once, and every score call counts against `max_evals`.

    Raises:
        EvalLimitReached: the red team's calls and the final scoring need more than
            `max_evals` score calls. No call above the limit is made.
        models.ProviderError, models.BadReply: a part's model failed after its retries.
        shared_paper_prefix.PaperTooLongForModelContext: the paper is too long for a model
            of a part.
        TypeError, ValueError: a part returned something of the wrong type or range.
    """
    without_attack = quantifier.breakage_without_attack(claim, premises)
    check_unit_number(without_attack, f"the breakage with no attack from {quantifier.name}")
    passed = gate(without_attack)
    if not isinstance(passed, bool):
        raise TypeError(f"the gate must return a bool, got {passed!r}")
    if passed:
        return Reading(local=Local(value=without_attack, worst=None), assessed=(),
                       red_team=_named(red_team), quantifier=_named(quantifier), calls=0,
                       without_attack=without_attack, attacked=False)
    given: dict[Attack, Assessment] = {}       # every score so far; one call each

    def score(attack: Attack) -> Assessment:
        if not isinstance(attack, Attack):
            raise TypeError(f"the red team {red_team.name} asked to score "
                            f"{type(attack).__name__}, not an Attack")
        if attack not in given:
            if len(given) >= max_evals:
                raise EvalLimitReached(f"the limit of {max_evals} score calls is used up")
            given[attack] = _assessment(quantifier.assess(claim, premises, attack), quantifier)
        return given[attack]

    attacks = red_team.attack(claim, premises, score)
    if not isinstance(attacks, tuple) or not all(isinstance(a, Attack) for a in attacks):
        raise TypeError(f"the red team {red_team.name} must return a tuple of Attacks, "
                        f"got {attacks!r}")
    assessed = tuple((a, score(a)) for a in attacks)
    local = quantifier.uncertainty(assessed)
    if not isinstance(local, Local):
        raise TypeError(f"the quantifier {quantifier.name} must return a Local, got {local!r}")
    if local.worst is not None and local.worst not in attacks:
        raise ValueError(f"the quantifier {quantifier.name} named a worst attack the red "
                         "team did not return")
    return Reading(local=local, assessed=assessed, red_team=_named(red_team),
                   quantifier=_named(quantifier), calls=len(given),
                   without_attack=without_attack, attacked=True)


def _assessment(value: object, quantifier: Quantifier) -> Assessment:
    """The quantifier's answer, refused unless it is an Assessment."""
    if not isinstance(value, Assessment):
        raise TypeError(f"the quantifier {quantifier.name} must return an Assessment, "
                        f"got {value!r}")
    return value


def _named(part: RedTeam | Quantifier) -> str:
    """A part as "name@version"."""
    return f"{part.name}@{part.version}"


# Saved readings: one JSON file mapping a binding to a reading.

def binding(claim: Premise, premises: tuple[Premise, ...], red_team: RedTeam,
            quantifier: Quantifier) -> str:
    """The checksum of everything a reading depends on. A change to any of it needs a new one.

    The limit of score calls is not in it. A reading that needs more calls than the limit
    is never saved, so a saved reading is the same under any limit that allows it.
    """
    def plain(p: Premise) -> list:
        return [p.id, p.statement, p.value]
    key = [plain(claim), [plain(p) for p in premises], _named(red_team), _named(quantifier)]
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()


# The readings of each file as last read, with the file's size and time, so a run with
# many claims parses the file once and not once for each claim.
_LOADED: dict[Path, tuple[tuple[int, int], Mapping[str, Reading]]] = {}


def load_readings(path: Path) -> Mapping[str, Reading]:
    """Every reading in the file, by binding, read-only; none when the file does not exist.

    Raises:
        ValueError: the file is not a readings file as save_reading writes it.
    """
    if not path.exists():
        return {}
    stat = path.stat()
    stamp = (stat.st_size, stat.st_mtime_ns)
    cached = _LOADED.get(path.resolve())
    if cached is not None and cached[0] == stamp:
        return cached[1]
    readings = read_only(_parse_readings(path))
    _LOADED[path.resolve()] = (stamp, readings)
    return readings


def _parse_readings(path: Path) -> dict[str, Reading]:
    """The readings of the file at `path`.

    Raises:
        ValueError: the file is not a claim uncertainty readings file.
    """
    try:
        raw = json.loads(path.read_text())
    except ValueError as e:
        raise ValueError(f"{path}: not a claim uncertainty readings file: {e}") from e
    return _readings_from_json(path, raw)


def _readings_from_json(path: Path, raw: object) -> dict[str, Reading]:
    """The readings in `raw`, the JSON value of the file at `path`.

    Raises:
        ValueError: `raw` is not the value of a claim uncertainty readings file.
    """
    try:
        if not isinstance(raw, dict) or set(raw) != {"readings"}:
            raise ValueError("the file must be an object with only `readings`")
        return {key: _reading(value) for key, value in raw["readings"].items()}
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        raise ValueError(f"{path}: not a claim uncertainty readings file: {e}") from e


def save_reading(path: Path, key: str, reading: Reading) -> None:
    """Add one reading to the file.

    A lock makes two runs at the same time add their readings one after the other, so
    neither loses the other's. The lock file is in the system's temporary folder, so it
    never shows in the folder of the readings, which can be in a git repository.
    """
    def add(raw: object | None) -> object:
        """The value of the file with the new reading."""
        readings = {} if raw is None else _readings_from_json(path, raw)
        readings[key] = reading
        return {"readings": {k: _plain(r) for k, r in readings.items()}}

    file_storage.JsonFile(
        path, indent=1, sort_keys=True,
        lock=file_storage.FileLock.in_temporary_folder("readings", path)).change(add)
    _LOADED.pop(path.resolve(), None)


def _plain(reading: Reading) -> dict:
    """A reading as JSON values."""
    def attack(a: Attack) -> list:
        return [{"design": h.design, "result": h.result} for h in a.hypotheticals]
    return {"local": reading.local.value,
            "worst": attack(reading.local.worst) if reading.local.worst else None,
            "assessed": [{"attack": attack(a), "believability": s.believability,
                          "breakage": s.breakage} for a, s in reading.assessed],
            "red_team": reading.red_team, "quantifier": reading.quantifier,
            "calls": reading.calls, "without_attack": reading.without_attack,
            "attacked": reading.attacked}


def _reading(raw: dict) -> Reading:
    """A reading from JSON values, refused with an error unless every field is as written."""
    keys = {"local", "worst", "assessed", "red_team", "quantifier", "calls", "without_attack",
            "attacked"}
    if set(raw) != keys:
        raise ValueError(f"a reading must have exactly the keys {sorted(keys)}")

    def attack(items: list) -> Attack:
        if not isinstance(items, list):
            raise TypeError(f"an attack must be a list, got {items!r}")
        return Attack(hypotheticals=tuple(Hypothetical(**h) for h in items))
    assessed = tuple((attack(a["attack"]), Assessment(believability=a["believability"],
                                                      breakage=a["breakage"]))
                     for a in raw["assessed"])
    worst = None if raw["worst"] is None else attack(raw["worst"])
    if not isinstance(raw["calls"], int) or isinstance(raw["calls"], bool) or raw["calls"] < 0:
        raise ValueError(f"calls must be a whole number, got {raw['calls']!r}")
    if not all(isinstance(raw[k], str) for k in ("red_team", "quantifier")):
        raise TypeError("red_team and quantifier must be text")
    return Reading(local=Local(value=raw["local"], worst=worst), assessed=assessed,
                   red_team=raw["red_team"], quantifier=raw["quantifier"], calls=raw["calls"],
                   without_attack=raw["without_attack"], attacked=raw["attacked"])
