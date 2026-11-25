#!/usr/bin/env python3
"""Check the breakage with no attack, which claim_uncertainty scores before the red team.

Covers:
  - `uncertainty.read`: a gate that passes gives a reading with no attack and calls no red
    team; a gate that does not pass runs the red team and keeps the breakage with no attack;
  - a reading with no attack must have no attacks, no score calls, and that breakage as value;
  - the check: the red team always runs, and a claim whose breakage with no attack passes
    the cutoff is Flagged with that breakage and a two-bullet worst case; a saved reading is
    reused; a saved reading with no attack is reused, Skipped, or replaced by a red team run
    when the cutoff goes up;
  - the judge: the breakage prompt with the paper alone, read as for an attack, with its
    reasoning saved;
  - the coherence quantifier: the chat with no hypothetical result, and a request with an
    empty attack list for the command.
The parts and the model calls are fakes. Returns 1 if any case fails.
"""
import json
import math
import os
import shlex
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_uncertainty  # noqa: E402

from checks.uncertainty import quantifier, uncertainty  # noqa: E402
from schema.primitives import Flagged, Passed, Skipped  # noqa: E402
from utils import models, shared_paper_prefix  # noqa: E402

FAILED: list[str] = []
M = "https://openrouter.ai/api/v1/chat/completions#m"     # a model name; no call is made

CLAIM = uncertainty.Premise(id="c", statement="Retrieval helps every model size.", value=None)
PREMISES = (uncertainty.Premise(id="p1", statement="Retrieval adds 4.1 points at 13B.",
                                value=None),
            uncertainty.Premise(id="p2", statement="Retrieval adds 2 points at 7B.", value=2.0))
ATTACK = uncertainty.Attack(hypotheticals=(uncertainty.Hypothetical(
    design="the 70B model on the same data", result="retrieval lowers accuracy by 2 points"),))


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


class CountingRedTeam(test_uncertainty.FakeRedTeam):
    """The fake red team, which counts the claims it attacks."""

    def __init__(self) -> None:
        super().__init__()
        self.attacked: list[str] = []

    def attack(self, claim, premises, score):
        self.attacked.append(claim.id)
        return super().attack(claim, premises, score)


def test_read() -> None:
    """The gate decides whether the red team runs; the reading keeps the breakage."""
    red, quant = CountingRedTeam(), test_uncertainty.FakeQuantifier(without={"mid": 0.6})
    claim = uncertainty.Premise(id="mid", statement="mid follows.", value=None)
    seen: list[float] = []

    def passes(without_attack: float) -> bool:
        seen.append(without_attack)
        return without_attack > 0.5
    got = uncertainty.read(claim, PREMISES, red, quant, max_evals=3, gate=passes)
    expect("a gate that passes gives a reading with no attack, and no red team call",
           seen == [0.6] and not red.attacked and quant.assessed == 0 and not got.attacked
           and got.assessed == () and got.calls == 0 and got.local.value == 0.6
           and got.without_attack == 0.6, repr(got))
    got = uncertainty.read(claim, PREMISES, red, quant, max_evals=3, gate=lambda w: False)
    expect("a gate that does not pass runs the red team, and keeps the breakage",
           red.attacked == ["mid"] and got.attacked and got.without_attack == 0.6
           and math.isclose(got.local.value, 0.2) and got.calls == 1, repr(got))
    for name, gate, error in (("a gate that does not return a bool is refused",
                               lambda w: 1, TypeError),):
        try:
            uncertainty.read(claim, PREMISES, red, quant, max_evals=3, gate=gate)
            expect(name, False, "no error raised")
        except error:
            expect(name, True)
    bad = test_uncertainty.FakeQuantifier(without={"mid": 1.5})
    try:
        uncertainty.read(claim, PREMISES, red, bad, max_evals=3, gate=lambda w: False)
        expect("a breakage with no attack above 1 is refused", False, "no error raised")
    except ValueError:
        expect("a breakage with no attack above 1 is refused", True)
    for name, fields in (("an attack", {"assessed": ((ATTACK, uncertainty.Assessment(
                              believability=0.9, breakage=0.2)),)}),
                         ("a score call", {"calls": 1}),
                         ("another value", {"local": uncertainty.Local(value=0.1, worst=None)})):
        try:
            uncertainty.Reading(**{"local": uncertainty.Local(value=0.6, worst=None),
                                   "assessed": (), "red_team": "r@1", "quantifier": "q@1",
                                   "calls": 0, "without_attack": 0.6, "attacked": False,
                                   **fields})
            expect(f"a reading with no red team and {name} is refused", False,
                   "no error raised")
        except ValueError:
            expect(f"a reading with no red team and {name} is refused", True)


class ReasoningQuantifier(test_uncertainty.FakeQuantifier):
    """The fake quantifier, which also saves a reasoning for each breakage with no attack."""

    def breakage_without_attack(self, claim, premises):
        value = super().breakage_without_attack(claim, premises)
        uncertainty.record_reason(claim, None, value, f"Why {claim.id} breaks with no attack.")
        return value


def test_check() -> None:
    """The check end to end on the register of test_uncertainty, with fake parts.

    The red team always runs. A claim is flagged when the larger of its breakage with no
    attack and its worst attack's breakage is above the cutoff.
    """
    gap = "The claim assumes that the result holds at every size."
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        red = CountingRedTeam()
        quant = ReasoningQuantifier(without={"mid": 0.6})
        options = test_uncertainty.parts(red, quant, folder, uncertainty_cutoff=0.5)
        with test_uncertainty.fake_replies({"other": [gap] * 10}, []):
            found = test_uncertainty.run(folder, test_uncertainty.register(), **options)
        mid = found["mid"].checks["claim_uncertainty"]
        facts = dict(mid.problems[0].facts) if isinstance(mid, Flagged) else {}
        expect("a breakage with no attack above the cutoff flags the claim with that breakage",
               isinstance(mid, Flagged) and math.isclose(mid.measured, 0.6), repr(mid))
        expect("the worst case of that claim is two bullets: the reasoning with no attack, "
               "then the distilled worst attack",
               facts.get(uncertainty.WORST_CASE_LABEL)
               == f"- Why mid breaks with no attack.\n- {gap}", repr(facts))
        expect("the red team runs for every claim, also when the breakage with no attack "
               "alone passes the cutoff",
               sorted(red.attacked) == ["mid", "mid2", "top", "top2"], repr(red.attacked))
        top = found["top"].checks["claim_uncertainty"]
        expect("a claim on it compounds both: 1 - (1 - 0.3) * (1 - 0.6) = 0.72, flagged",
               isinstance(top, Flagged) and math.isclose(top.measured, 0.72), repr(top))
        mid2 = found["mid2"].checks["claim_uncertainty"]
        expect("a claim below the cutoff with and without attack passes",
               isinstance(mid2, Passed) and math.isclose(mid2.measured, 0.1), repr(mid2))
        saved = json.loads((folder / "readings.json").read_text())["readings"].values()
        expect("each saved reading has the breakage with no attack, and is attacked",
               sorted((r["attacked"], r["without_attack"]) for r in saved)
               == [(True, 0.0), (True, 0.0), (True, 0.0), (True, 0.6)], repr(list(saved)))
        reasons = [json.loads(line) for line in
                   (folder / "claim_uncertainty_reasons.jsonl").read_text().splitlines()]
        expect("the reasonings are saved beside the readings, one for each claim",
               sorted(r["claim"] for r in reasons)
               == ["mid follows.", "mid2 follows.", "top follows.", "top2 follows."],
               repr(reasons))

        asked = quant.asked_without, quant.assessed
        again = test_uncertainty.run(folder, test_uncertainty.register(),
                                     **{**options, uncertainty.WANT.key: False})
        expect("the saved readings are reused with no model call",
               (quant.asked_without, quant.assessed) == asked
               and isinstance(again["mid"].checks["claim_uncertainty"], Flagged))

        higher = test_uncertainty.run(folder, test_uncertainty.register(),
                                      **{**options, uncertainty.WANT.key: False,
                                         "uncertainty_cutoff": 0.7})
        mid = higher["mid"].checks["claim_uncertainty"]
        expect("with a higher cutoff, the same reading passes",
               isinstance(mid, Passed) and math.isclose(mid.measured, 0.6), repr(mid))

        default = test_uncertainty.run(folder, test_uncertainty.register(), **{
            **options, uncertainty.WANT.key: False, "uncertainty_cutoff": None})
        mid = default["mid"].checks["claim_uncertainty"]
        expect("with no cutoff given, the default 0.59 flags the breakage 0.6",
               isinstance(mid, Flagged) and math.isclose(mid.measured, 0.6), repr(mid))

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        red, quant = CountingRedTeam(), ReasoningQuantifier(without={"mid": 0.6})
        options = test_uncertainty.parts(red, quant, folder, uncertainty_cutoff=0.5)
        # a reading saved with no red team, as when the red team did not run below a gate
        claim = uncertainty.Premise(id="mid", statement="mid follows.", value=None)
        key = uncertainty.binding(claim, (uncertainty.paper_premise(test_uncertainty.PAPER),),
                                  red, quant)
        uncertainty.save_reading(folder / "readings.json", key, uncertainty.Reading(
            local=uncertainty.Local(value=0.6, worst=None), assessed=(), red_team="r@1",
            quantifier="q@1", calls=0, without_attack=0.6, attacked=False))
        kept = test_uncertainty.run(folder, test_uncertainty.register(),
                                    **{**options, uncertainty.WANT.key: False})
        mid = kept["mid"].checks["claim_uncertainty"]
        expect("a saved reading with no attack that passes the cutoff is reused, and flags",
               isinstance(mid, Flagged) and math.isclose(mid.measured, 0.6)
               and "mid" not in red.attacked, repr(mid))
        higher = {**options, uncertainty.WANT.key: False, "uncertainty_cutoff": 0.7}
        stale = test_uncertainty.run(folder, test_uncertainty.register(), **higher)
        mid = stale["mid"].checks["claim_uncertainty"]
        expect("with a higher cutoff and no model calls, a reading with no attack is Skipped",
               isinstance(mid, Skipped) and "no longer passes" in mid.because, repr(mid))
        with test_uncertainty.fake_replies({"other": [gap] * 10}, []):
            rerun = test_uncertainty.run(folder, test_uncertainty.register(),
                                         **{**higher, uncertainty.WANT.key: True})
        mid = rerun["mid"].checks["claim_uncertainty"]
        expect("with a higher cutoff and model calls, the red team runs for that claim",
               isinstance(mid, Passed) and math.isclose(mid.measured, 0.6)
               and "mid" in red.attacked, repr(mid))


def test_judge() -> None:
    """The judge asks the breakage prompt with the paper alone, and saves its reasoning."""
    paper = (uncertainty.paper_premise(test_uncertainty.PAPER),)
    alone = quantifier.break_prompt(CLAIM, paper, None)
    with_attack = quantifier.break_prompt(CLAIM, paper, ATTACK)
    expect("with no attack, the breakage prompt is the prompt of an attack without its result",
           with_attack.replace(" Also take this result as true:\n- the 70B model on the same "
                               "data: retrieval lowers accuracy by 2 points", "") == alone
           and CLAIM.statement in alone and test_uncertainty.PAPER not in alone, alone)
    asked: list[str] = []
    prefixes: list[str | None] = []
    real = models.ask_and_read

    def ask_and_read(prompt, model, read, timeout=300, **kw):
        asked.append(prompt)
        prefixes.append(kw.get("shared_prefix_for_cache"))
        return read("Some reasons.\nreasoning: Only 13B and 7B are measured.\nbreakage: 0.35")
    models.ask_and_read = ask_and_read
    kept = uncertainty.REASONS_FILE
    try:
        with tempfile.TemporaryDirectory() as tmp:
            uncertainty.REASONS_FILE = Path(tmp) / "reasons.jsonl"
            got = quantifier.JudgeQuantifier(model=M, cutoff=0.59).breakage_without_attack(
                CLAIM, paper)
            reasons = [json.loads(line)
                       for line in uncertainty.REASONS_FILE.read_text().splitlines()]
    finally:
        models.ask_and_read = real
        uncertainty.REASONS_FILE = kept
    expect("the judge asks that prompt once, after the paper prefix, and reads the breakage",
           got == 0.35 and asked == [alone]
           and prefixes == [shared_paper_prefix.shared_paper_prefix_for_model_calls(
               test_uncertainty.PAPER)], f"{got} {asked}")
    expect("the reasoning with no attack is saved with no attack",
           reasons == [{"claim": CLAIM.statement, "breakage": 0.35,
                        "reasoning": "Only 13B and 7B are measured.", "attack": None}],
           repr(reasons))


def test_coherence() -> None:
    """The coherence quantifier: the chat with no result, and the command's empty attack."""
    alone = quantifier.coherence_messages(CLAIM, PREMISES, None)
    expect("with no attack, the chat holds the premises, then the claim question",
           alone == quantifier.coherence_messages(CLAIM, PREMISES, ATTACK)[:4]
           + quantifier.coherence_messages(CLAIM, PREMISES, ATTACK)[-1:], repr(alone))
    sent: list[list[dict]] = []
    real = models.next_token_logprobs

    def next_token_logprobs(messages, model, top):
        sent.append(messages)
        return {"True": math.log(0.75), "False": math.log(0.25)}
    models.next_token_logprobs = next_token_logprobs
    try:
        got = quantifier.CoherenceQuantifier(command=None,
                                             judge=quantifier.JudgeQuantifier(M, 0.59),
                                             model=M).breakage_without_attack(CLAIM, PREMISES)
    finally:
        models.next_token_logprobs = real
    expect("the model gets that chat, and the breakage is P(False) of True and False",
           math.isclose(got, 0.25) and sent == [alone], f"{got} {sent}")

    echo = ("import json, sys; r = json.load(sys.stdin); "
            "print(json.dumps({'breakage': 0.4 if r['attack'] == [] else 0.9}))")
    command = quantifier.CoherenceQuantifier(
        command=f"{shlex.quote(sys.executable)} -c {shlex.quote(echo)}",
        judge=quantifier.JudgeQuantifier(M, 0.59))
    got = command.breakage_without_attack(CLAIM, PREMISES)
    expect("the command gets a request with an empty attack list", got == 0.4, repr(got))
    expect("the request with no attack holds the claim and the premises",
           quantifier.request(CLAIM, PREMISES, None)
           == {**quantifier.request(CLAIM, PREMISES, ATTACK), "attack": []})
    try:
        quantifier.CoherenceQuantifier(command=None, judge=quantifier.JudgeQuantifier(M, 0.59)
                                       ).breakage_without_attack(CLAIM, PREMISES)
        expect("with no command and no model, the breakage with no attack is refused", False,
               "no error raised")
    except ValueError:
        expect("with no command and no model, the breakage with no attack is refused", True)


def main() -> int:
    """Run every group of cases and report the failures."""
    for group in (test_read, test_check, test_judge, test_coherence):
        print(group.__name__)
        group()
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll cases of the breakage with no attack passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
