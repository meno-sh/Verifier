#!/usr/bin/env python3
"""Check the red team (checks/uncertainty/red_team/) and the model request it uses.

The cases cover its settings, options rule, prompt texts, strict reading of a reply,
its request, its rounds with score feedback, and a reading through claim_uncertainty.
The model call is a fake that gives fixed replies; the retries of models.ask_and_read
are tested in test_alerts.py. The request body is built with no network. Returns 1 if
any case fails, so it can gate a commit.
"""
import json
import math
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_uncertainty  # noqa: E402

from checks.uncertainty import red_team, uncertainty  # noqa: E402
from checks.uncertainty.red_team import prompt, prompted, reply  # noqa: E402
from core.analysis import analysis_request, findings_analyzer  # noqa: E402
from core.registry import extensions  # noqa: E402
from utils import (
    alerts,  # noqa: E402
    models,  # noqa: E402
)

# reply.read() writes a warn when it leaves out a broken proposal
_ALERTS = tempfile.mkdtemp()
alerts.configure(Path(_ALERTS) / "alerts.jsonl", "test", Path(_ALERTS))

BUILT_IN_CHECKS = extensions.builtin_check_registry()   # the registry of the built-in checks

FAILED: list[str] = []
M = "https://openrouter.ai/api/v1/chat/completions#m"     # a model name; no call is made


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def test_settings() -> None:
    """The red team is registered, built from the run options, and versioned by them."""
    built = uncertainty.RED_TEAMS["prompt"]({"red_team_model": M, "red_team_proposals": 4,
                                             "red_team_results": 2, "red_team_effort": "high",
                                             "red_team_timeout": 60, "red_team_iterations": 3})
    expect("the prompt red team is built with the settings the options give",
           isinstance(built, red_team.PromptRedTeam)
           and (built.model, built.proposals, built.results, built.effort, built.timeout,
                built.iterations) == (M, 4, 2, "high", 60, 3), repr(vars(built)))
    expect("its options are in the registry, so verify has a flag for each",
           {o.key for o in prompted.RUN_OPTIONS} <= set(BUILT_IN_CHECKS.run_option_catalog.options))
    base = red_team.PromptRedTeam(model=M).version
    for name, changed in [("model", {"model": M + "n"}), ("proposals", {"proposals": 9}),
                          ("results", {"results": 4}), ("effort", {"effort": "xhigh"}),
                          ("rounds", {"iterations": 3})]:
        expect(f"the {name} is part of the version",
               red_team.PromptRedTeam(**{"model": M, **changed}).version != base)
    expect("the timeout is not part of the version",
           red_team.PromptRedTeam(model=M, timeout=5).version == base)


def test_options_rule() -> None:
    """The options rule refuses settings that cannot work, before any model call."""
    for name, options in [("no proposals", {"red_team_proposals": 0}),
                          ("no results", {"red_team_results": 0}),
                          ("an unknown effort", {"red_team_effort": "huge"}),
                          ("no timeout", {"red_team_timeout": 0}),
                          ("no rounds", {"red_team_iterations": 0}),
                          ("rounds that need more score calls than the limit",
                           {"want_claim_uncertainty": True, "red_team_iterations": 10,
                            "red_team_proposals": 8})]:
        try:
            prompted.options_fit(options)
            expect(f"the options rule refuses {name}", False, "no error raised")
        except ValueError:
            expect(f"the options rule refuses {name}", True)
    prompted.options_fit({})
    expect("the defaults pass the options rule", True)
    prompted.options_fit({"want_claim_uncertainty": True, "red_team_iterations": 10,
                          "red_team_proposals": 8, "max_uncertainty_evals_per_claim": 80})
    expect("rounds that fit a raised limit pass the options rule", True)
    prompted.options_fit({"want_claim_uncertainty": True, "red_team": "fake",
                          "red_team_iterations": 10, "red_team_proposals": 8})
    expect("the limit on rounds applies only when this red team is chosen", True)
    prompted.options_fit({"want_claim_uncertainty": False, "red_team_iterations": 10,
                          "red_team_proposals": 8})
    expect("the limit on rounds applies only when the check is on", True)


CLAIM = uncertainty.Premise(id="h-register-name", statement="Retrieval helps on every task.",
                            value=None)
# the full paper, the one premise the check gives the red team (uncertainty.paper_premise)
PAPER = ("\\documentclass{article}\n\\begin{document}\n"
         "Retrieval adds 4 points on QA. Retrieval helps on code.\n\\end{document}\n")
PREMISES = (uncertainty.paper_premise(PAPER),)
PREFIX = uncertainty.shared_paper_prefix_of_premises(PREMISES)   # the paper, as models get it


def proposal(*pairs: tuple[str, str]) -> dict:
    """One proposal of a reply, as JSON values."""
    return {"hypotheticals": [{"design": d, "result": r} for d, r in pairs]}


def good_reply(n: int = 2) -> str:
    """A reply that keeps every rule: n proposals, the first with two results."""
    items = [proposal(("Run it on summarization, 3 seeds.", "Retrieval loses 2 points."),
                      ("Ask 30 annotators to rate the summaries.", "They prefer no retrieval."))]
    items += [proposal((f"Design {k}.", f"Result {k}.")) for k in range(2, n + 1)]
    return json.dumps({"proposals": items})


def second_reply() -> str:
    """A reply of 2 proposals that differ from those of good_reply, for a second round."""
    return json.dumps({"proposals": [proposal(("Second design A.", "Second result A.")),
                                     proposal(("Second design B.", "Second result B."))]})


class FakeAskAndRead:
    """models.ask_and_read with fixed replies: records each call and reads the next reply.

    The retries of ask_and_read are tested in test_alerts.py, so this fake reads once.
    """

    def __init__(self, *replies: str) -> None:
        self.replies, self.calls = list(replies), []

    def __call__(self, prompt: str, model: str, read, timeout: int = 300, *,
                 system: str | None = None, reasoning: str | None = None, **kw) -> object:
        self.calls.append({"prompt": prompt, "model": model, "timeout": timeout,
                           "system": system, "reasoning": reasoning, **kw})
        return read(self.replies.pop(0))


class RoundReplies(FakeAskAndRead):
    """FakeAskAndRead that gives `first` to a first round and `later` to every later round.

    It answers any number of claims, so a test does not depend on how many claims a
    fixture register red-teams.
    """

    def __init__(self, first: str, later: str) -> None:
        super().__init__()
        self.first, self.later = first, later

    def __call__(self, prompt: str, model: str, read, timeout: int = 300, *,
                 system: str | None = None, reasoning: str | None = None, **kw) -> object:
        self.replies = [self.later if "This is round " in prompt else self.first]
        return super().__call__(prompt, model, read, timeout, system=system,
                                reasoning=reasoning, **kw)


def test_prompt() -> None:
    """The texts hold what the model needs, and the system part is the same for every claim."""
    system = prompt.system_text(8, 3)
    expect("the system part holds the schema with the counts, and no example",
           json.dumps(prompt.schema(8, 3)) in system and "exactly 8 proposals" in system
           and "1 to 3" in system and "example reply" not in system.lower())
    expect("the system part names kinds of study other than an experiment",
           all(kind in system for kind in ("human study", "statistical reanalysis",
                                           "qualitative study", "causal analysis")))
    expect("the system part says nothing about one claim or the paper",
           CLAIM.statement not in system and prompt.system_text(8, 3) == system
           and "Retrieval adds 4 points" not in system)
    expect("the system part says the full paper is before it",
           "The full text of the paper (LaTeX or Markdown notes) is before these instructions."
           in system)
    user = prompt.user_text(CLAIM, PREMISES)
    expect("the user part holds the claim, and no premise, paper text or claim id",
           CLAIM.statement in user
           and user.startswith("The claim to attack (from the paper in the instructions):")
           and "Retrieval adds 4 points" not in user and "documentclass" not in user
           and "register-name" not in user and "paper\n" not in user, user)
    expect("a claim with no value gets no value", "reported value" not in user, user)
    valued = uncertainty.Premise(id="v-register-name", statement="Retrieval adds 4 points.",
                                 value=4.0)
    expect("a claim with a value is given with its value",
           "Retrieval adds 4 points. (reported value: 4)" in prompt.user_text(valued, PREMISES))
    expect("the first round's system part says nothing of feedback or rounds",
           "believability" not in system and "round" not in system.lower())
    later_system = prompt.system_text(8, 3, feedback=True)
    expect("a later round's system part adds what the two scores mean, and is the same "
           "for every claim",
           prompt.FEEDBACK in later_system and "believability" in later_system
           and "breakage" in later_system and later_system.startswith(system[:200])
           and later_system == prompt.system_text(8, 3, feedback=True))
    first = uncertainty.Attack(hypotheticals=(
        uncertainty.Hypothetical(design="Run it on summarization.", result="It loses 2 points."),
        uncertainty.Hypothetical(design="Ask 30 annotators.", result="They prefer no retrieval.")))
    one = ((first, uncertainty.Assessment(believability=0.9, breakage=0.7)),)
    later = prompt.user_text(CLAIM, PREMISES, one, round_number=2, rounds=3)
    expect("a later round's user part gives the round and each earlier proposal with its "
           "two scores",
           later.startswith(user) and "This is round 2 of 3." in later
           and "Proposal 1: believability 0.90, breakage 0.70" in later
           and all(h.design in later and h.result in later for h in first.hypotheticals), later)
    expect("the first round's user part says nothing of rounds or earlier proposals",
           "round" not in user.lower() and "earlier" not in user)
    for name, call in [("earlier proposals in the first round",
                        lambda: prompt.user_text(CLAIM, PREMISES, one)),
                       ("a later round with no earlier proposals",
                        lambda: prompt.user_text(CLAIM, PREMISES, (), 2, 2)),
                       ("a round past the last", lambda: prompt.user_text(CLAIM, PREMISES,
                                                                          one, 3, 2))]:
        try:
            call()
            expect(f"the user part refuses {name}", False, "no error raised")
        except ValueError:
            expect(f"the user part refuses {name}", True)
    for name, call in [("no premise", lambda: prompt.user_text(CLAIM, ())),
                       ("no proposals", lambda: prompt.schema(0, 3))]:
        try:
            call()
            expect(f"the prompt refuses {name}", False, "no error raised")
        except ValueError:
            expect(f"the prompt refuses {name}", True)


def test_read() -> None:
    """A reply that keeps the rules gives its proposals; each broken rule is named."""
    got = reply.read(good_reply(3), 3, 2)
    expect("a good reply gives N proposals, one of them with two results",
           len(got) == 3 and len(got[0].hypotheticals) == 2
           and got[1].hypotheticals[0].design == "Design 2.", repr(got))
    fenced = reply.read("```json\n" + good_reply(3) + "\n```", 3, 2)
    expect("one code fence around the whole reply is accepted", fenced == got)
    two = proposal(("a", "b"))
    for rule, text, part in [
            ("not JSON", "Here it is: " + good_reply(2), "not one JSON value"),
            ("text outside a fence", "Sure.\n```json\n" + good_reply(2) + "\n```",
             "not one JSON value"),
            ("not an object", json.dumps([two, two]), "only the key `proposals`"),
            ("another key", json.dumps({"proposals": [two, two], "note": "x"}),
             "only the key `proposals`"),
            ("proposals not a list", json.dumps({"proposals": "x"}), "must be a list"),
            ("no proposals", json.dumps({"proposals": []}), "at least 1 item, not 0"),
            ("a proposal with another key", json.dumps({"proposals": [{
                **two, "why": "x"}]}), "proposal 1 must be an object"),
            ("no results", json.dumps({"proposals": [{"hypotheticals": []}]}),
             "1 to 2 results"),
            ("too many results", json.dumps({"proposals": [proposal(
                ("a", "b"), ("c", "d"), ("e", "f"))]}), "1 to 2 results"),
            ("a result with another key", json.dumps({"proposals": [{"hypotheticals": [
                {"design": "a", "result": "b", "p": 1}]}]}), "exactly `design` and `result`"),
            ("a result that is not text", json.dumps({"proposals": [{"hypotheticals": [
                {"design": "a", "result": 3}]}]}), "both text"),
            ("an empty design", json.dumps({"proposals": [proposal((" ", "b"))]}),
             "result 1 of proposal 1: Hypothetical.design must be non-empty"),
            ("every proposal broken", json.dumps({"proposals": [{"hypotheticals": []}, {
                **two, "why": "x"}]}), "proposal 1 must be a list")]:
        try:
            reply.read(text, 2, 2)
            expect(f"read refuses {rule}", False, "no error raised")
        except models.BadReply as e:
            expect(f"read refuses {rule}, naming the rule", part in str(e), str(e))
    kept = reply.read(json.dumps({"proposals": [two, {**two, "why": "x"}, proposal(("c", "d"))]}),
                      3, 2)
    expect("a broken proposal is left out and the others are kept",
           [a.hypotheticals[0].design for a in kept] == ["a", "c"], repr(kept))
    warns = [json.loads(line) for line in
             (Path(_ALERTS) / "alerts.jsonl").read_text().splitlines()]
    expect("leaving out a proposal writes a red_team.proposal_left_out warn naming it",
           any(w["key"] == "red_team.proposal_left_out" and "1 of 3" in w["message"]
               and "proposal 2" in w["message"] for w in warns), repr(warns[-1:]))


def test_attack() -> None:
    """One request with the run's settings; the reader refuses a bad reply."""
    ask = FakeAskAndRead(good_reply(2))
    team = red_team.PromptRedTeam(model=M, proposals=2, results=2, effort="high", timeout=9,
                                  iterations=1, ask_and_read=ask)
    got = team.attack(CLAIM, PREMISES, lambda attack: None)
    expect("one round: a good reply gives the proposals, with one request",
           len(got) == 2 and len(ask.calls) == 1, repr(ask.calls))
    expect("the request has the model, the effort, the timeout, both parts and the paper",
           ask.calls[0]["model"] == M and ask.calls[0]["reasoning"] == "high"
           and ask.calls[0]["timeout"] == 9
           and ask.calls[0]["system"] == prompt.system_text(2, 2)
           and ask.calls[0]["prompt"] == prompt.user_text(CLAIM, PREMISES)
           and ask.calls[0]["shared_prefix_for_cache"] == PREFIX, repr(ask.calls))
    expect("the paper goes in the shared prefix only, not in the system or user part",
           "Retrieval adds 4 points on QA." in PREFIX
           and "Retrieval adds 4 points on QA." not in ask.calls[0]["system"]
           and "Retrieval adds 4 points on QA." not in ask.calls[0]["prompt"])
    try:
        red_team.PromptRedTeam(model=M, proposals=2, results=2, iterations=1,
                               ask_and_read=FakeAskAndRead(good_reply(2))).attack(
            CLAIM, (uncertainty.Premise(id="p-register-name", statement="Retrieval helps.",
                                        value=None),), lambda attack: None)
        expect("the red team refuses register premises in place of the paper", False,
               "no error raised")
    except ValueError as e:
        expect("the red team refuses register premises in place of the paper",
               "the full paper must be the one premise" in str(e), str(e))
    bad = FakeAskAndRead(json.dumps({"proposals": []}))
    try:
        red_team.PromptRedTeam(model=M, proposals=2, results=2, iterations=1,
                               ask_and_read=bad).attack(
            CLAIM, PREMISES, lambda attack: None)
        expect("the reader it gives ask_and_read refuses a bad reply", False, "no error raised")
    except models.BadReply as e:
        expect("the reader it gives ask_and_read refuses a bad reply, naming the rule",
               "at least 1 item, not 0" in str(e), str(e))


def test_rounds() -> None:
    """Each round after the first is shown the scores of every earlier proposal."""
    second = json.dumps({"proposals": [proposal(("Second design A.", "Second result A.")),
                                       proposal(("Second design B.", "Second result B."))]})
    ask = FakeAskAndRead(good_reply(2), second)
    scored = []

    def score(attack: uncertainty.Attack) -> uncertainty.Assessment:
        scored.append(attack)
        return uncertainty.Assessment(believability=0.8, breakage=0.1 * len(scored))
    team = red_team.PromptRedTeam(model=M, proposals=2, results=2, iterations=2,
                                  ask_and_read=ask)
    got = team.attack(CLAIM, PREMISES, score)
    first = reply.read(good_reply(2), 2, 2)
    expect("two rounds give the proposals of both rounds, in order",
           got == first + reply.read(second, 2, 2), repr(got))
    expect("only the first round's proposals are scored, each once",
           scored == list(first), repr(scored))
    expect("the second request shows the first round's proposals with their scores",
           ask.calls[1]["prompt"] == prompt.user_text(CLAIM, PREMISES, (
               (first[0], uncertainty.Assessment(believability=0.8, breakage=0.1)),
               (first[1], uncertainty.Assessment(believability=0.8, breakage=0.2))), 2, 2),
           ask.calls[1]["prompt"])
    expect("the first round's request says nothing of rounds",
           ask.calls[0]["prompt"] == prompt.user_text(CLAIM, PREMISES)
           and ask.calls[0]["system"] == prompt.system_text(2, 2), repr(ask.calls[0]))
    expect("the second round's system part adds the paragraph on feedback",
           ask.calls[1]["system"] == prompt.system_text(2, 2, feedback=True))
    expect("both rounds send the same paper as the shared prefix",
           [c["shared_prefix_for_cache"] for c in ask.calls] == [PREFIX, PREFIX])
    # the check's `score` gives only an Assessment (test_uncertainty.py, test_parts)


def test_reading() -> None:
    """Asked for, the written red team gives a claim a reading through claim_uncertainty."""
    ask = RoundReplies(good_reply(2), second_reply())
    team = red_team.PromptRedTeam(model=M, proposals=2, results=2, ask_and_read=ask)
    quantifier = test_uncertainty.FakeQuantifier()
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        options = {**test_uncertainty.parts(team, quantifier, folder),
                   uncertainty.MAX_EVALS.key: 4}
        # the run gives the check the full paper, which the red team gets as its one premise
        path = folder / "claims.json"
        path.write_text(json.dumps({"claims": test_uncertainty.register()}))
        tree = findings_analyzer.FindingsAnalyzer(test_uncertainty.REG).analyse(
            analysis_request.AnalysisRequest(
                claim_register_path=str(path),
                claim_register_base_folder=test_uncertainty.WORKDIR, run_options=options,
                full_paper_text=PAPER))
        found = {c.claim.id: c for c in tree.claims}
        expect("the step resting on verified claims gets its reading",
               math.isclose(test_uncertainty.measured(found, "mid"), 0.2), repr(found["mid"]))
        saved = json.loads((folder / "readings.json").read_text())["readings"]
        expect("each reading holds the proposals of both rounds, scored, every score "
               "call (2 for feedback, 2 final) and the red team's version",
               saved and all(len(r["assessed"]) == 4 and r["calls"] == 4
                             and r["red_team"] == f"prompt@{team.version}"
                             for r in saved.values()), repr(saved))
        expect("every request of the reading sends the run's paper as the shared prefix",
               ask.calls and all(c["shared_prefix_for_cache"] == PREFIX for c in ask.calls),
               repr([c.get("shared_prefix_for_cache") for c in ask.calls]))


def test_request_body() -> None:
    """The request has the reasoning, the cache breakpoint and no budget, as asked."""
    plain = models.request_body("q", "m")
    expect("with no reasoning: temperature 0 and one user message",
           plain == {"model": "m", "messages": [{"role": "user", "content": "q"}],
                     "temperature": 0}, repr(plain))
    body = models.request_body("q", "m", system="rules", reasoning="xhigh")
    expect("with reasoning: the effort, no temperature and no token budget",
           body["reasoning"] == {"effort": "xhigh"} and "temperature" not in body
           and "max_tokens" not in body and "max_tokens" not in body["reasoning"], repr(body))
    first = body["messages"][0]
    expect("the system part comes first, with a cache breakpoint at its end",
           first["role"] == "system"
           and first["content"][-1] == {"type": "text", "text": "rules",
                                        "cache_control": {"type": "ephemeral"}}
           and body["messages"][1] == {"role": "user", "content": "q"}, repr(body))


def main() -> int:
    """Run every group of cases and report the failures."""
    for group in (test_settings, test_options_rule, test_prompt, test_read, test_attack,
                  test_rounds, test_reading, test_request_body):
        print(group.__name__)
        group()
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll red team cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
