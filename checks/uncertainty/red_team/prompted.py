"""PromptRedTeam: N proposals against a claim per round, each round shown the scores so far."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import ClassVar

from checks import interface
from checks.uncertainty import uncertainty
from checks.uncertainty.red_team import prompt, reply
from utils import models, spending

# Haiku 5.5: it reads the shared paper prefix from the cache that the linker and the quantifier
# write, so every model call that reads the paper uses one model. DeepSeek V4.1 Flash (about
# $0.01 a call on the PR 144 eval) read only 53% to 62% of its prompt from the cache.
MODEL = interface.RunOption(key="red_team_model", kind=str, flag="--red-team-model",
                           default=models.preset.HAIKU,
                           help="the model the red team asks, as a model name (utils/models.py)")
PROPOSALS = interface.RunOption(key="red_team_proposals", kind=int, flag="--red-team-proposals",
                               default=8, help="proposals in the request of each round "
                                                 "(see --red-team-iterations)")
RESULTS = interface.RunOption(key="red_team_results", kind=int, flag="--red-team-results",
                             default=3, help="the most hypothetical results in one proposal")
EFFORT = interface.RunOption(key="red_team_effort", kind=str, flag="--red-team-effort",
                            default="high",
                            help="the reasoning effort of the red team's model")
TIMEOUT = interface.RunOption(key="red_team_timeout", kind=int, flag="--red-team-timeout",
                             default=1800, help="seconds to wait for one reply")
ITERATIONS = interface.RunOption(key="red_team_iterations", kind=int,
                                flag="--red-team-iterations", default=2,
                                help="rounds of N proposals; each round after the first is "
                                     "shown the scores of every earlier proposal")
RUN_OPTIONS = (MODEL, PROPOSALS, RESULTS, EFFORT, TIMEOUT, ITERATIONS)
# the spending purpose of the red team's calls; under the claude CLI it picks the
# Claude model they get, so the version names it too (models.label)
PURPOSE = "red_team"

# whether the check calls models, its red team, and its limit on score calls

type AskAndRead = Callable[..., object]     # the signature of utils.models.ask_and_read


@interface.options_rule(check=uncertainty.CHECK)
def options_fit(options: Mapping[str, object]) -> None:
    """Refuse red team options that cannot work, before any model is called.

    Raises:
        ValueError: N, the most results, the timeout or the rounds is below 1, the
            effort is not known, or, with the check asked for and this red team chosen,
            the rounds need more score calls than max_uncertainty_evals_per_claim allows.
    """
    for option in (PROPOSALS, RESULTS, TIMEOUT, ITERATIONS):
        if option.value_in_options(options) < 1:
            raise ValueError(f"{option.key} must be at least 1")
    if EFFORT.value_in_options(options) not in models.EFFORTS:
        raise ValueError(f"{EFFORT.key} must be one of {', '.join(models.EFFORTS)}")
    # with the check not asked for, no model is called, so no score call is made
    if (not uncertainty.WANT.value_in_options(options)
            or uncertainty.RED_TEAM.value_in_options(options) != PromptRedTeam.name):
        return
    # the check scores every proposal of every round once; the scores of the rounds but
    # the last are also the feedback; each score is one call
    needed = ITERATIONS.value_in_options(options) * PROPOSALS.value_in_options(options)
    limit = uncertainty.MAX_EVALS.value_in_options(options)
    if needed > limit:
        raise ValueError(f"{ITERATIONS.key} and {PROPOSALS.key} need {needed} score calls "
                         f"for each claim, more than {uncertainty.MAX_EVALS.key} ({limit})")


class PromptRedTeam:
    """Asks a model for N proposals against a claim, in one JSON reply per round."""
    name = "prompt"
    template = False
    # change it when the prompt or the reply format changes
    PROMPT_VERSION: ClassVar[str] = "7-paper-or-notes-prefix"

    def __init__(self, model: str, proposals: int = PROPOSALS.default,
                 results: int = RESULTS.default, effort: str = EFFORT.default,
                 timeout: int = TIMEOUT.default, iterations: int = ITERATIONS.default,
                 ask_and_read: AskAndRead = models.ask_and_read) -> None:
        """Keep the settings. `ask_and_read` is the model call; tests give a fake one.

        Raises:
            ValueError: `iterations` is below 1, or `model` is not a model name.
        """
        if iterations < 1:
            raise ValueError(f"iterations must be at least 1, got {iterations}")
        self.model, self.proposals, self.results = model, proposals, results
        self.effort, self.timeout, self.ask_and_read = effort, timeout, ask_and_read
        self.iterations = iterations
        # every setting that changes the proposals is part of every reading's binding
        self.version = (f"{self.PROMPT_VERSION}/{models.label(model, PURPOSE)}/{proposals}/"
                        f"{results}/{effort}/{iterations}")

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> PromptRedTeam:
        """The red team with the settings the run options give."""
        return cls(model=MODEL.value_in_options(options),
                   proposals=PROPOSALS.value_in_options(options),
                   results=RESULTS.value_in_options(options),
                   effort=EFFORT.value_in_options(options),
                   timeout=TIMEOUT.value_in_options(options),
                   iterations=ITERATIONS.value_in_options(options))

    def attack(self, claim: uncertainty.Premise, premises: tuple[uncertainty.Premise, ...],
               score: Callable[[uncertainty.Attack], uncertainty.Assessment]
               ) -> tuple[uncertainty.Attack, ...]:
        """Write N proposals against `claim` in each round, with `premises` taken as true.

        Each round after the first is shown every earlier proposal with its two scores.
        Only the proposals of the earlier rounds are scored here, because only they are
        shown to the model again.

        Args:
            claim: the claim to attack.
            premises: the claims that `claim` is inferred from.
            score: the quantifier's Assessment of one proposal. The check keeps each
                score, and scores only the returned proposals that are not scored yet.

        Returns:
            The proposals of every round, at most N per round, in order. A proposal that
            repeats one given before is left out. Each holds 1 to `results` results.

        Raises:
            models.ProviderError, models.BadReply: the model failed after its retries
                (utils/models.py asks once more for a reply that reply.read refuses).
            uncertainty.EvalLimitReached: the rounds need more score calls than the
                check allows (options_fit refuses such options first).
        """
        scored: list[tuple[uncertainty.Attack, uncertainty.Assessment]] = []    # the feedback
        written: list[uncertainty.Attack] = []
        for round_ in range(1, self.iterations + 1):
            with spending.purpose(PURPOSE):
                proposals = self.ask_and_read(
                    prompt.user_text(claim, premises, tuple(scored), round_, self.iterations),
                    self.model, lambda text: reply.read(text, self.proposals, self.results),
                    self.timeout, reasoning=self.effort,
                    system=prompt.system_text(self.proposals, self.results, feedback=round_ > 1),
                    shared_prefix_for_cache=uncertainty.shared_paper_prefix_of_premises(
                        premises))
            # a proposal the model repeats is kept and scored once
            proposals = [p for i, p in enumerate(proposals)
                         if p not in written and p not in proposals[:i]]
            written += proposals
            if round_ < self.iterations:
                scored += [(p, score(p)) for p in proposals]
        return tuple(written)


@interface.options_rule(check=uncertainty.CHECK)
def model_is_reachable(options: Mapping[str, object]) -> None:
    """Refuse a red team model that cannot be called, before any model is called.

    Raises:
        ValueError: the check is asked for, this red team is chosen, and its model is
            not a model name or its API key is not set.
    """
    if (uncertainty.WANT.value_in_options(options)
            and uncertainty.RED_TEAM.value_in_options(options) == PromptRedTeam.name):
        models.check_key(MODEL.value_in_options(options))
