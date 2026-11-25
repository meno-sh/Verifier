#!/usr/bin/env python3
"""Check that max_uncertainty_evals_per_claim caps every paid score call of one claim.

The red team's own score calls and the final scoring of every attack it returns both
count. An attack scored for the red team keeps its score, so each attack is scored once.
The cases cover the count saved in a reading, the limit reached before any call above
it, the prompt red team through `uncertainty.read`, and the options rule on the whole
budget of the prompt red team. The parts and the model call are fakes.
Returns 1 if any case fails, so it can gate a commit.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from checks.uncertainty import red_team, uncertainty  # noqa: E402
from checks.uncertainty.red_team import prompted  # noqa: E402
from utils import shared_paper_prefix  # noqa: E402

FAILED: list[str] = []
M = "https://openrouter.ai/api/v1/chat/completions#m"     # a model name; no call is made

CLAIM = uncertainty.Premise(id="claim", statement="Retrieval helps on every task.", value=None)
# the full paper is the one premise of claim uncertainty
PREMISES = (uncertainty.paper_premise("Retrieval adds 4 points on QA."),)


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def attack(k: int) -> uncertainty.Attack:
    """A distinct attack."""
    return uncertainty.Attack(hypotheticals=(uncertainty.Hypothetical(
        design=f"Design {k}.", result=f"Result {k}."),))


def never(without_attack: float) -> bool:
    """A gate that never stops the red team."""
    return False


class FakeRedTeam:
    """Asks for `feedback` scores, then returns `returned` attacks; the first ones are the same."""
    name, version, template = "fake-red-team", "1", False

    def __init__(self, feedback: int, returned: int) -> None:
        self.feedback, self.returned = feedback, returned

    def attack(self, claim, premises, score):
        for k in range(self.feedback):
            score(attack(k))
        return tuple(attack(k) for k in range(self.returned))


class CountingQuantifier:
    """Counts its paid calls; every attack gets the same scores."""
    name, version, template = "counting-quantifier", "1", False

    def __init__(self) -> None:
        self.assessed = 0

    def breakage_without_attack(self, claim, premises):
        return 0.0

    def assess(self, claim, premises, attack):
        self.assessed += 1
        return uncertainty.Assessment(believability=0.9, breakage=0.2)

    def uncertainty(self, assessed):
        worst = max(assessed, key=lambda pair: pair[1].breakage, default=None)
        return uncertainty.Local(value=worst[1].breakage if worst else 0.0,
                                 worst=worst[0] if worst else None)


def test_count() -> None:
    """A reading counts the red team's calls and the final scoring, one call per attack."""
    quantifier = CountingQuantifier()
    reading = uncertainty.read(CLAIM, PREMISES, FakeRedTeam(feedback=2, returned=3),
                               quantifier, max_evals=3, gate=never)
    expect("2 feedback calls, then 1 final call for the 1 new attack, give calls == 3",
           reading.calls == 3 and quantifier.assessed == 3,
           f"calls {reading.calls}, assessed {quantifier.assessed}")
    expect("every returned attack is scored", len(reading.assessed) == 3, repr(reading.assessed))


def test_limit() -> None:
    """The limit is reached before any call above it."""
    for name, red, limit in [
            ("a red team that returns more attacks than the limit allows",
             FakeRedTeam(feedback=0, returned=50), 8),
            ("feedback calls and final calls that only together pass the limit",
             FakeRedTeam(feedback=2, returned=3), 2)]:
        quantifier = CountingQuantifier()
        try:
            uncertainty.read(CLAIM, PREMISES, red, quantifier, max_evals=limit, gate=never)
            expect(f"{name} raises EvalLimitReached", False, "no error raised")
        except uncertainty.EvalLimitReached:
            expect(f"{name} raises EvalLimitReached after exactly {limit} calls",
                   quantifier.assessed == limit, f"{quantifier.assessed} calls")


class FakeAskAndRead:
    """models.ask_and_read that returns `proposals` new proposals on each call."""

    def __init__(self, proposals: int) -> None:
        self.proposals, self.asked = proposals, 0
        self.prefixes: list[str | None] = []     # the shared paper prefix of each call

    def __call__(self, prompt, model, read, timeout=300, *, system=None, reasoning=None,
                 shared_prefix_for_cache=None):
        self.asked += 1
        self.prefixes.append(shared_prefix_for_cache)
        items = [{"hypotheticals": [{"design": f"Round {self.asked} design {k}.",
                                     "result": f"Round {self.asked} result {k}."}]}
                 for k in range(self.proposals)]
        return read(json.dumps({"proposals": items}))


def test_prompt_red_team() -> None:
    """The prompt red team's whole budget is iterations * N: each proposal is scored once."""
    asked = FakeAskAndRead(2)
    team = red_team.PromptRedTeam(model=M, proposals=2, results=1, iterations=2,
                                  ask_and_read=asked)
    quantifier = CountingQuantifier()
    reading = uncertainty.read(CLAIM, PREMISES, team, quantifier, max_evals=4, gate=never)
    expect("2 rounds of 2 proposals make 2 feedback and 2 final calls, all counted",
           reading.calls == 4 and quantifier.assessed == 4 and len(reading.assessed) == 4,
           f"calls {reading.calls}, assessed {quantifier.assessed}")
    paper = shared_paper_prefix.shared_paper_prefix_for_model_calls(PREMISES[0].statement)
    expect("each round of the prompt red team sends the paper as the shared prefix",
           asked.prefixes == [paper, paper], repr(asked.prefixes))

    team = red_team.PromptRedTeam(model=M, proposals=2, results=1, iterations=2,
                                  ask_and_read=FakeAskAndRead(2))
    quantifier = CountingQuantifier()
    try:
        uncertainty.read(CLAIM, PREMISES, team, quantifier, max_evals=3, gate=never)
        expect("a limit of 3 for a budget of 4 raises EvalLimitReached", False,
               "no error raised")
    except uncertainty.EvalLimitReached:
        expect("a limit of 3 for a budget of 4 raises EvalLimitReached after 3 calls",
               quantifier.assessed == 3, f"{quantifier.assessed} calls")


def test_options_rule() -> None:
    """The options rule refuses a prompt red team whose whole budget is above the limit."""
    want = {"want_claim_uncertainty": True}
    prompted.options_fit(want)
    expect("the defaults (2 rounds of 8, limit 64, so 16 calls) pass", True)
    for name, options in [
            ("9 rounds of 8 with a limit of 64 (72 calls)",
             {"red_team_iterations": 9, "red_team_proposals": 8}),
            ("2 rounds of 8 with a limit of 15 (16 calls)",
             {"red_team_iterations": 2, "red_team_proposals": 8,
              "max_uncertainty_evals_per_claim": 15}),
            ("1 round of 8 with a limit of 7",
             {"red_team_iterations": 1, "red_team_proposals": 8,
              "max_uncertainty_evals_per_claim": 7})]:
        try:
            prompted.options_fit({**want, **options})
            expect(f"the options rule refuses {name}", False, "no error raised")
        except ValueError as e:
            expect(f"the options rule refuses {name}, naming the calls needed",
                   "score calls" in str(e), str(e))
    for name, options in [
            ("8 rounds of 8 with a limit of 64",
             {**want, "red_team_iterations": 8, "red_team_proposals": 8}),
            ("1 round of 8 with a limit of 8",
             {**want, "red_team_iterations": 1, "red_team_proposals": 8,
              "max_uncertainty_evals_per_claim": 8}),
            ("9 rounds of 8 with a limit of 4, with the check turned off",
             {"want_claim_uncertainty": False, "red_team_iterations": 9,
              "red_team_proposals": 8, "max_uncertainty_evals_per_claim": 4})]:
        prompted.options_fit(options)
        expect(f"the options rule passes {name}", True)


def main() -> int:
    """Run every group of cases and report the failures."""
    for group in (test_count, test_limit, test_prompt_red_team, test_options_rule):
        print(group.__name__)
        group()
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll score cap cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
