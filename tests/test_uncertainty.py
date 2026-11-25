#!/usr/bin/env python3
"""Run the two checks on claims inferred from other claims: acyclic_support and claim_uncertainty.

The claims rest on the fixture's verified claims (`fixtures/workdir/`). The red team and
the quantifier are fakes, so every uncertainty is known in advance: the fake quantifier
gives each step the local uncertainty in LOCAL. The cases cover compounding over a chain
and over shared dependencies, saved readings and when they go stale, the limit of score calls, the
cutoff, provider errors, parts that return the wrong thing, and cycles. The full paper text
(PAPER) is the one premise the red team and the quantifier read. The worst case
of a flagged claim is distilled once, saved, and read again; a reply that breaks the rules
of the distilled text is refused.
Returns 1 if any case fails, so it can gate a commit.
"""
import contextlib
import io
import json
import math
import os
import shlex
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checks.uncertainty import quantifier, red_team, scenario, uncertainty  # noqa: E402
from core.analysis import analysis_request, findings_analyzer  # noqa: E402
from core.methods import computational_methods  # noqa: E402
from core.project import watched_project  # noqa: E402
from core.registry import extensions, run_options  # noqa: E402
from runtime import cli as runtime_cli  # noqa: E402
from runtime.project import project_preparation  # noqa: E402
from runtime.project.paths import StatePaths  # noqa: E402
from schema import findings  # noqa: E402
from schema.primitives import Flagged, Off, Passed, Skipped  # noqa: E402
from utils import alerts, models, shared_paper_prefix  # noqa: E402

BUILT_IN_CHECKS = extensions.builtin_check_registry()   # the registry of the built-in checks

HERE = os.path.dirname(os.path.abspath(__file__))
WORKDIR = os.path.join(HERE, "fixtures", "workdir")
REG = computational_methods.MethodFolderLoader().registry_with_methods(
    BUILT_IN_CHECKS, Path(WORKDIR) / "methods")
FAILED: list[str] = []
M = "https://openrouter.ai/api/v1/chat/completions#m"     # a model name; no call is made

# The local uncertainty the fake quantifier gives each derived claim's own step.
LOCAL = {"mid": 0.2, "top": 0.3, "mid2": 0.1, "top2": 0.0, "on-caveat": 0.5, "on-traced": 0.5,
         "on-broken": 0.5, "above-caveat": 0.5, "cyc-a": 0.5, "cyc-b": 0.5, "after-cyc": 0.5}
OPTIONS_FILE = watched_project.WatchedProject.OPTIONS_FILE
# The full paper, which claim uncertainty reads as the one premise of every claim.
PAPER = "We add retrieval to a 7B and a 13B model. Retrieval adds 4.1 points at 13B."


def analyse_project(root: Path, state: Path) -> object:
    """The findings of the watched project in `root`, with the extensions of the environment."""
    return watched_project.WatchedProject.load(root, state).analyse()

def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


class FakeRedTeam:
    """One attack per claim, after asking for a score `asks` times.

    It asks for the score of that attack, or with `distinct`, of a new attack each time.
    """
    name, version, template = "fake-red-team", "1", False

    def __init__(self, asks: int = 1, catch: bool = True, distinct: bool = False) -> None:
        self.asks, self.catch, self.distinct, self.feedback = asks, catch, distinct, 0

    def attack(self, claim, premises, score):
        hit = uncertainty.Attack(hypotheticals=(uncertainty.Hypothetical(
            design=f"a follow-up to {claim.id}", result=f"{claim.id} does not hold there"),))
        for k in range(self.asks):
            tried = hit if not self.distinct else uncertainty.Attack(hypotheticals=(
                uncertainty.Hypothetical(design=f"try {k} on {claim.id}", result="it fails"),))
            try:
                score(tried)
                self.feedback += 1
            except uncertainty.EvalLimitReached:
                if not self.catch:
                    raise
                break
        return (hit,)


class FakeQuantifier:
    """Believable attacks whose breakage is the claim's LOCAL value.

    The breakage with no attack is the claim's value in `without` (0 when it is not there).
    """
    name, version, template = "fake-quantifier", "1", False

    def __init__(self, fail: str | None = None, without: dict[str, float] | None = None) -> None:
        self.fail, self.assessed = fail, 0
        self.without, self.asked_without = without or {}, 0

    def breakage_without_attack(self, claim, premises):
        self.asked_without += 1
        if self.fail == "long paper":
            raise shared_paper_prefix.PaperTooLongForModelContext("the paper has about "
                                                                  "300,000 tokens")
        return self.without.get(claim.id, 0.0)

    def assess(self, claim, premises, attack):
        self.assessed += 1
        if self.fail == "provider":
            raise models.ProviderError("the provider answered HTTP 429")
        if self.fail == "type":
            return 0.5
        return uncertainty.Assessment(believability=0.9, breakage=LOCAL[claim.id])

    def uncertainty(self, assessed):
        worst = max(assessed, key=lambda pair: pair[1].breakage, default=None)
        return uncertainty.Local(value=worst[1].breakage if worst else 0.0,
                                 worst=worst[0] if worst else None)


def register(statements: dict[str, str] | None = None) -> list[dict]:
    """The fixture's claims the derived claims rest on, with statements, and the derived claims."""
    statements = statements or {}
    base = {c["id"]: c for c in json.loads(Path(WORKDIR, "claims.json").read_text())["claims"]}
    leaves = []
    for cid in ("good", "prior", "no-source", "traced", "disagrees"):
        leaves.append({**base[cid], "statement": statements.get(cid, f"{cid} holds.")})

    def derived(cid: str, *deps: str) -> dict:
        return {"id": cid, "statement": statements.get(cid, f"{cid} follows."),
                "source": {"kind": "claims", "claims": list(deps)}}
    return [*leaves, derived("mid", "good", "prior"), derived("top", "mid"),
            derived("mid2", "good"), derived("top2", "mid", "mid2"),
            derived("on-caveat", "no-source"), derived("on-traced", "traced"),
            derived("on-broken", "disagrees"), derived("above-caveat", "on-caveat"),
            derived("cyc-a", "cyc-b"), derived("cyc-b", "cyc-a"), derived("after-cyc", "cyc-a")]


def run(folder: Path, claims: list[dict], paper: str = PAPER,
        **options: object) -> dict[str, findings.ClaimFindings]:
    """Check a register written into `folder`, with paths read from the fixture's folder.

    `paper` is the full paper text of the run, which claim uncertainty reads.
    """
    path = folder / "claims.json"
    path.write_text(json.dumps({"claims": claims}))
    tree = findings_analyzer.FindingsAnalyzer(REG).analyse(analysis_request.AnalysisRequest(
        claim_register_path=str(path), claim_register_base_folder=WORKDIR, run_options=options,
        full_paper_text=paper))
    return {c.claim.id: c for c in tree.claims}


def measured(found: dict[str, findings.ClaimFindings], cid: str) -> float | None:
    """The uncertainty the check measured for a claim, or None."""
    result = found[cid].checks["claim_uncertainty"]
    return result.measured if isinstance(result, Passed | Flagged) else None


def parts(red_team: FakeRedTeam, quantifier: FakeQuantifier, folder: Path, **more: object) -> dict:
    """The run options that ask for the check with these fakes.

    The cutoff is 1 unless `more` names another, so the check never flags a claim.
    Propagation is on unless `more` turns it off, because most cases test compounding;
    test_local_steps covers the default.
    """
    uncertainty.RED_TEAMS["fake"] = lambda options: red_team
    uncertainty.QUANTIFIERS["fake"] = lambda options: quantifier
    return {uncertainty.WANT.key: True, uncertainty.READINGS.key: str(folder / "readings.json"),
            uncertainty.RED_TEAM.key: "fake", uncertainty.QUANTIFIER.key: "fake",
            uncertainty.PROPAGATE.key: True,
            uncertainty.MAX_EVALS.key: 3, uncertainty.THRESHOLD.key: 1.0, **more}


def test_propagate() -> None:
    """Compounding over a chain and over several dependencies, with the largest one."""
    expect("TY's three levels: 0.2 then 0.3 compound to 0.44",
           math.isclose(uncertainty.propagate(0.3, [uncertainty.propagate(0.2, [])]), 0.44))
    expect("two dependencies use the largest uncertainty, not the sum or the product",
           math.isclose(uncertainty.propagate(0.0, [0.2, 0.1]), 0.2))
    expect("dependencies whose doubts add past 1 do not give uncertainty 1",
           math.isclose(uncertainty.propagate(0.0, [0.6, 0.7]), 0.7))
    expect("no dependency: the local uncertainty",
           math.isclose(uncertainty.propagate(0.3, []), 0.3))
    try:
        uncertainty.propagate(1.5, [])
        expect("a local uncertainty above 1 is refused", False, "no error raised")
    except ValueError:
        expect("a local uncertainty above 1 is refused", True)


def test_check() -> None:
    """The check end to end on a register, with fake parts."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        off = run(folder, register(), **{uncertainty.WANT.key: False})
        expect("turned off, and no part named: Off on every derived claim",
               all(isinstance(off[c].checks["claim_uncertainty"], Off) for c in LOCAL))
        expect("the two new checks give no result on a claim that is not derived",
               not {"claim_uncertainty", "acyclic_support"} & set(off["good"].checks))
        expect("checks that need a recomputed number give no result on a derived claim",
               not {"baseline", "interval", "measurement"} & set(off["mid"].checks))

        red, quant = FakeRedTeam(), FakeQuantifier()
        found = run(folder, register(), **parts(red, quant, folder))
        got = {c: measured(found, c) for c in ("mid", "top", "mid2", "top2")}
        expect("a step resting on verified claims has its local uncertainty",
               math.isclose(got["mid"], 0.2) and math.isclose(got["mid2"], 0.1), repr(got))
        expect("the top of the chain compounds both steps to 0.44",
               math.isclose(got["top"], 0.44), repr(got))
        expect("shared dependencies compound with the largest of them",
               math.isclose(got["top2"], 0.2), repr(got))
        for cid, fragment in [("on-caveat", "no-source is not verified"),
                              ("on-traced", "traced is not verified"),
                              ("on-broken", "disagrees failed its gate"),
                              ("above-caveat", "on-caveat has no known uncertainty"),
                              ("after-cyc", "cyc-a has no known uncertainty: cyc-b is "
                               "checked after this claim"),
                              ("cyc-a", "cyc-b is checked after this claim, because they are "
                               "on a cycle")]:
            result = found[cid].checks["claim_uncertainty"]
            expect(f"{cid} is Skipped, saying why",
                   isinstance(result, Skipped) and fragment in result.because, repr(result))
        expect("the score the red team asked for is kept, so each attack is scored once",
               quant.assessed == red.feedback, f"{quant.assessed} {red.feedback}")

        saved = json.loads((folder / "readings.json").read_text())["readings"]
        expect("one reading is saved for each step that ran", len(saved) == 4, repr(len(saved)))
        calls = quant.assessed
        again = run(folder, register(),
                    **{**parts(red, quant, folder), uncertainty.WANT.key: False})
        expect("saved readings are reused with no model call, and give the same numbers",
               quant.assessed == calls and measured(again, "top") == got["top"])

        reworded = run(folder, register({"good": "good holds, worded again."}),
                       **{**parts(red, quant, folder), uncertainty.WANT.key: False})
        expect("a changed register premise does not make a reading stale, because the full "
               "paper is the one premise",
               quant.assessed == calls and measured(reworded, "mid") == got["mid"],
               repr(reworded["mid"].checks["claim_uncertainty"]))
        stale = run(folder, register(), paper=PAPER + " Retrieval adds 2 points at 7B.",
                    **{**parts(red, quant, folder), uncertainty.WANT.key: False})
        expect("a changed paper text makes every reading stale",
               all(isinstance(stale[c].checks["claim_uncertainty"], Off)
                   for c in ("mid", "top", "mid2", "top2")))
        stale = run(folder, register({"mid": "mid follows, worded again."}),
                    **{**parts(red, quant, folder), uncertainty.WANT.key: False})
        expect("a changed claim statement makes its own reading stale",
               isinstance(stale["mid"].checks["claim_uncertainty"], Off))
        top = stale["top"].checks["claim_uncertainty"]
        expect("a claim above a stale reading has no known uncertainty",
               isinstance(top, Skipped) and "mid has no known uncertainty" in top.because,
               repr(top))
        try:
            run(folder, register(), paper=None, **parts(red, quant, folder))
            expect("a run with no paper text is refused by the check", False,
                   "no error raised")
        except ValueError as e:
            expect("a run with no paper text is refused by the check",
                   "has no paper text" in str(e), str(e))

        gap = "The claim assumes that the seeds are independent. The evidence does not show this."
        asked: list[str] = []
        with fake_replies({"other": [gap]}, asked):
            flagged = run(folder, register(), **parts(red, quant, folder,
                                                       uncertainty_cutoff=0.4))
        result = flagged["top"].checks["claim_uncertainty"]
        expect("above the threshold: Flagged, with the uncertainty and the worst case",
               isinstance(result, Flagged) and math.isclose(result.measured, 0.44)
               and "a follow-up to top" in result.problems[0].detail, repr(result))
        expect("the report calls the worst attack a hypothetical case to rule out",
               "worst case to rule out, an illustration of a hypothetical scenario"
               in result.problems[0].detail
               and "without necessarily empirical support" in result.problems[0].detail,
               repr(result))
        facts = dict(result.problems[0].facts)
        expect("the problem gives the uncertainty and the distilled worst case, one line each",
               list(facts) == [uncertainty.UNCERTAINTY_LABEL, uncertainty.WORST_CASE_LABEL]
               and facts[uncertainty.UNCERTAINTY_LABEL].startswith("0.44 (cutoff 0.4")
               and "this step alone" not in facts[uncertainty.UNCERTAINTY_LABEL]
               and facts[uncertainty.WORST_CASE_LABEL] == gap, repr(facts))
        expect("the model that distils is asked once, with the made-up result in the prompt",
               len(asked) == 1 and "a follow-up to top" in asked[0], repr(asked))
        saved = scenario.path_beside(folder / "readings.json")
        again = run(folder, register(), **parts(red, quant, folder, uncertainty_cutoff=0.4,
                                                want_claim_uncertainty=False))
        expect("the distilled text is saved, and read again with no model call",
               dict(again["top"].checks["claim_uncertainty"].problems[0].facts)
               [uncertainty.WORST_CASE_LABEL] == gap, repr(sorted(folder.iterdir())))
        expect("at or below the threshold: Passed",
               isinstance(flagged["mid"].checks["claim_uncertainty"], Passed))


def test_local_steps() -> None:
    """The default: each claim's own step, with its premises taken as true."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        red, quant = FakeRedTeam(), FakeQuantifier()
        found = run(folder, register(), **parts(red, quant, folder,
                                                propagate_claim_uncertainty=False))
        got = {c: measured(found, c) for c in LOCAL}
        expect("each claim has the uncertainty of its own step, with no compounding",
               all(got[c] is not None and math.isclose(got[c], LOCAL[c])
                   for c in ("mid", "top", "mid2", "top2")), repr(got))
        expect("a premise with no source, a traced premise, or a failed gate does not stop "
               "the check",
               all(got[c] is not None and math.isclose(got[c], LOCAL[c])
                   for c in ("on-caveat", "on-traced", "on-broken", "above-caveat")),
               repr(got))
        expect("the option is off when it is not given",
               uncertainty.PROPAGATE.default is False)


def test_parts() -> None:
    """The limit of score calls, provider errors (Skipped), and parts that return the wrong
    thing."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        run(folder, register(), **parts(FakeRedTeam(), FakeQuantifier(), folder,
                                        max_uncertainty_evals_per_claim=2))
        saved = json.loads((folder / "readings.json").read_text())["readings"].values()
        expect("a reading counts one score call for its one attack",
               all(r["calls"] == 1 for r in saved), repr([r["calls"] for r in saved]))

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        alerts.configure(folder / "alerts.jsonl", "test", folder)
        for name, red, limit in [
                ("a red team that does not stop at its limit",
                 FakeRedTeam(asks=5, catch=False, distinct=True), 1),
                ("a red team that uses up the limit, so its attack cannot be scored",
                 FakeRedTeam(asks=5, distinct=True), 2)]:
            found = run(folder, register(), **parts(red, FakeQuantifier(), folder,
                                                    max_uncertainty_evals_per_claim=limit))
            result = found["mid"].checks["claim_uncertainty"]
            expect(f"{name}: the claim is Skipped with the reason, and the run goes on",
                   isinstance(result, Skipped)
                   and f"limit of {limit} score calls" in result.because,
                   repr(result))
        warned = [r for r in alerts.read(folder / "alerts.jsonl")
                  if r["key"] == "uncertainty.limit_reached"]
        expect("each claim over its limit is a warn in the alert log, and nothing is saved",
               warned and all(r["level"] == "warn" for r in warned)
               and not (folder / "readings.json").exists(), repr(warned))

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        alerts.configure(folder / "alerts.jsonl", "test", folder)
        found = run(folder, register(), **parts(FakeRedTeam(), FakeQuantifier("provider"), folder))
        result = found["mid"].checks["claim_uncertainty"]
        expect("a provider error that outlasts its retries makes the claim Skipped with the "
               "error, and the run goes on",
               isinstance(result, Skipped) and "a model failed after its retries" in result.because
               and "HTTP 429" in result.because, repr(result))
        failed = [r for r in alerts.read(folder / "alerts.jsonl")
                  if r["key"] == "uncertainty.model_failed"]
        expect("each claim whose model failed is an error in the alert log, and nothing is saved",
               failed and all(r["level"] == "error" for r in failed)
               and not (folder / "readings.json").exists(), repr(failed))

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        alerts.configure(folder / "alerts.jsonl", "test", folder)
        found = run(folder, register(),
                    **parts(FakeRedTeam(), FakeQuantifier("long paper"), folder))
        result = found["mid"].checks["claim_uncertainty"]
        expect("a paper too long for the model makes the claim Skipped with the reason, and "
               "the run goes on", isinstance(result, Skipped) and "tokens" in result.because,
               repr(result))

    for name, options, error in [
            ("a quantifier that returns no Assessment",
             lambda f: parts(FakeRedTeam(), FakeQuantifier("type"), f), TypeError),
            ("a limit of 0",
             lambda f: parts(FakeRedTeam(), FakeQuantifier(), f,
                             max_uncertainty_evals_per_claim=0),
             run_options.OptionsError),
            ("a cutoff above 1",
             lambda f: parts(FakeRedTeam(), FakeQuantifier(), f, uncertainty_cutoff=1.5),
             run_options.OptionsError),
            ("a limit that is not a whole number",
             lambda f: parts(FakeRedTeam(), FakeQuantifier(), f,
                             max_uncertainty_evals_per_claim="3"),
             run_options.OptionsError),
            ("an empty readings file name",
             lambda f: {**parts(FakeRedTeam(), FakeQuantifier(), f), uncertainty.READINGS.key: " "},
             run_options.OptionsError),
            ("the coherence quantifier with no command",
             lambda f: {**parts(FakeRedTeam(), FakeQuantifier(), f),
                        uncertainty.QUANTIFIER.key: "coherence"},
             run_options.OptionsError),
            ("asking for the check with no readings file",
             lambda f: {**parts(FakeRedTeam(), FakeQuantifier(), f),
                        uncertainty.READINGS.key: None},
             run_options.OptionsError),
            ("a part name nobody registered",
             lambda f: {**parts(FakeRedTeam(), FakeQuantifier(), f), uncertainty.RED_TEAM.key: "x"},
             run_options.OptionsError)]:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                run(Path(tmp), register(), **options(Path(tmp)))
                expect(f"{name} stops the run", False, "no error raised")
            except error:
                expect(f"{name} stops the run", True)

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "readings.json").write_text('{"readings": {"x": {"local": 2}}}')
        try:
            run(folder, register(), **parts(FakeRedTeam(), FakeQuantifier(), folder))
            expect("a broken readings file stops the run", False, "no error raised")
        except ValueError:
            expect("a broken readings file stops the run", True)


def test_cycles() -> None:
    """acyclic_support flags each claim on a cycle and names it, and nothing else."""
    def derived(cid: str, *deps: str) -> dict:
        return {"id": cid, "statement": f"{cid}.", "source": {"kind": "claims",
                                                              "claims": list(deps)}}
    claims = [derived("self", "self"), derived("x", "y"), derived("y", "z"), derived("z", "x"),
              derived("w", "x"), {"id": "leaf", "value": 1, "statement": "leaf."},
              derived("fine", "leaf")]
    with tempfile.TemporaryDirectory() as tmp:
        found = run(Path(tmp), claims, **{uncertainty.WANT.key: False})
    want = {"self": "self -> self", "x": "x -> y -> z -> x", "y": "y -> z -> x -> y",
            "z": "z -> x -> y -> z"}
    for cid in ("self", "x", "y", "z", "w", "fine"):
        result = found[cid].checks["acyclic_support"]
        if cid in want:
            ok = isinstance(result, Flagged) and want[cid] in result.problems[0].detail
        else:
            ok = isinstance(result, Passed)
        expect(f"acyclic_support on {cid}: "
               + ("flags the cycle" if cid in want else "passes"), ok, repr(result))


@contextlib.contextmanager
def no_key():
    """Run the block with no OPENROUTER_API_KEY, then restore it."""
    saved = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["OPENROUTER_API_KEY"] = saved


@contextlib.contextmanager
def fake_replies(replies: dict[str, list[str]], asked: list[str],
                 sent: list[dict] | None = None):
    """Run the block with models.ask answering from `replies` and recording each prompt.

    The judge asks its two prompts at the same time, so the replies are kept per prompt:
    "believe" for the judge's believability prompt and "other" for every other prompt,
    each given in turn. `sent`, when given, gets the keyword arguments of each call.
    """
    real = models.ask
    left = {kind: iter(texts) for kind, texts in replies.items()}

    def ask(prompt: str, model: str, timeout: int = 300, **options: object) -> str:
        asked.append(prompt)
        if sent is not None:
            sent.append(options)
        return next(left["believe" if prompt.startswith("You check hypothetical") else "other"])
    models.ask = ask
    try:
        yield
    finally:
        models.ask = real


def test_red_team() -> None:
    """The red team is registered and versioned by its model; a model call fails loudly."""
    built = uncertainty.RED_TEAMS["prompt"]({"red_team_model": M})
    expect("the prompt red team is registered, and built with the model the options name",
           isinstance(built, red_team.PromptRedTeam) and built.model == M)
    expect("the red team's model is part of its version, so of every reading's binding",
           red_team.PromptRedTeam(model=M + "a").version
           != red_team.PromptRedTeam(model=M + "b").version)
    key = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        models.ask("hello", "https://openrouter.ai/api/v1/chat/completions#any/model")
        expect("a model call with no key is a provider error", False, "no error raised")
    except models.ProviderError:
        expect("a model call with no key is a provider error", True)
    finally:
        if key is not None:
            os.environ["OPENROUTER_API_KEY"] = key
    for name, payload in [("an error in place of choices", {"error": {"message": "busy"}}),
                          ("no choices", {"choices": []}),
                          ("a reply cut at the token limit", {"choices": [
                              {"finish_reason": "length", "message": {"content": "half"}}]}),
                          ("a reply with no text", {"choices": [
                              {"finish_reason": "stop", "message": {"content": " "}}]}),
                          ("a reply that is not an object", ["x"])]:
        try:
            models.reply_text(payload, "any/model")
            expect(f"{name} is a provider error", False, "no error raised")
        except models.ProviderError:
            expect(f"{name} is a provider error", True)
    expect("a whole reply gives its text", models.reply_text(
        {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}, "m") == "ok")


class CountingJudge(quantifier.JudgeQuantifier):
    """A judge with no model: believability is the number of results over 4."""

    def believability(self, premises: tuple[uncertainty.Premise, ...],
                      attack: uncertainty.Attack) -> float:
        return len(attack.hypotheticals) / 4


def coherence_command(script: str) -> quantifier.CoherenceQuantifier:
    """The coherence quantifier with a command that runs `script` in this Python."""
    return quantifier.CoherenceQuantifier(
        command=f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
        judge=CountingJudge(model=M, cutoff=0.59))


def test_coherence_command(claim: uncertainty.Premise, attack: uncertainty.Attack) -> None:
    """The coherence quantifier sends the request on stdin, reads the breakage strictly, and
    takes believability from the judge."""
    echo = ("import json, sys; r = json.load(sys.stdin); "
            "print(json.dumps({'breakage': len(r['attack']) / 8}))")
    got = coherence_command(echo).assess(claim, (claim,), attack)
    expect("the command gets the request, and the judge gives believability",
           got == uncertainty.Assessment(believability=0.5, breakage=0.25), repr(got))
    for name, script, error in (
            ("a command that exits with an error is a provider error",
             "import sys; print('server down', file=sys.stderr); sys.exit(7)",
             models.ProviderError),
            ("text that is not JSON is refused", "print('0.2')", ValueError),
            ("believability from the command is refused",
             "print('{\"believability\": 0.5, \"breakage\": 0.2}')", ValueError),
            ("a breakage that is not a number is refused",
             "print('{\"breakage\": true}')", ValueError),
            ("a breakage above 1 is refused", "print('{\"breakage\": 1.5}')", ValueError)):
        try:
            coherence_command(script).assess(claim, (claim,), attack)
            expect(name, False, "no error raised")
        except error as e:
            expect(name, True)
            if error is models.ProviderError:
                expect("the provider error shows the command's stderr",
                       "server down" in str(e), str(e))
    try:
        quantifier.CoherenceQuantifier(command=None, judge=CountingJudge(model=M, cutoff=0.59)
                                       ).assess(
            claim, (claim,), attack)
        expect("the coherence quantifier with no command is refused", False, "no error raised")
    except ValueError:
        expect("the coherence quantifier with no command is refused", True)


def test_judge(claim: uncertainty.Premise, attack: uncertainty.Attack) -> None:
    """The judge's prompts hold their inputs, the believability prompt hides the claim, the
    paper goes in the shared prefix, and each reply is read strictly."""
    paper = (uncertainty.paper_premise(PAPER),)
    believe = quantifier.believe_prompt(paper, attack)
    brk = quantifier.break_prompt(claim, paper, attack)
    expect("the believability prompt holds each result, not the claim and not the paper",
           believe.count("the 70B model: no gain") == 2 and "Retrieval helps." not in believe
           and "Claim:" not in believe and PAPER not in believe, believe)
    expect("the breakage prompt holds the claim and each result, not the paper",
           brk.count("Retrieval helps.") == 1 and brk.count("the 70B model: no gain") == 2
           and PAPER not in brk, brk)
    expect("the breakage prompt asks for a reasoning line before the breakage line",
           'starts with "reasoning:"' in brk and brk.endswith("breakage: <number>"), brk)
    got = quantifier.read_number("Some reasons.\n\nbreakage: 0.1", "breakage")
    expect("an explanation before the line is allowed", got == 0.1, repr(got))
    got = quantifier.read_number("**believability: 0.5**\nbelievability: 0.5.", "believability")
    expect("a repeated identical line, with emphasis or a full stop, is accepted", got == 0.5,
           repr(got))
    for name, reply in (("lines that disagree are refused", "breakage: 0.8\nbreakage: 0.1"),
                        ("text after the line is refused", "breakage: 0.8\nDone."),
                        ("a number above 1 is refused", "breakage: 1.5"),
                        ("the other number's line is refused", "believability: 0.5"),
                        ("an empty reply is refused", "")):
        try:
            quantifier.read_number(reply, "breakage")
            expect(name, False, "no error raised")
        except ValueError:
            expect(name, True)
    for name, reply, value in (("emphasis around the name only is ignored",
                                "**breakage:** 0.8", 0.8),
                               ("emphasis around the number only is ignored",
                                "breakage: **0.8**", 0.8),
                               ("the same value written two ways is one value",
                                "breakage: 0.50\nbreakage: 0.5", 0.5)):
        expect(name, quantifier.read_number(reply, "breakage") == value)
    with no_key():
        try:
            quantifier.JudgeQuantifier(
                model="https://openrouter.ai/api/v1/chat/completions#any/model",
                cutoff=0.59).assess(claim, paper, attack)
            expect("the judge without a key is a provider error", False, "no error raised")
        except models.ProviderError:
            expect("the judge without a key is a provider error", True)
    try:
        quantifier.JudgeQuantifier(model=M, cutoff=0.59).believability((claim,), attack)
        expect("the judge refuses premises that are not the one paper premise", False,
               "no error raised")
    except ValueError as e:
        expect("the judge refuses premises that are not the one paper premise",
               "the full paper must be the one premise" in str(e), str(e))
    asked: list[str] = []
    sent: list[dict] = []
    reason = "The 70B result shows no gain, so retrieval does not help there."
    kept = uncertainty.REASONS_FILE
    with (tempfile.TemporaryDirectory() as tmp,
          fake_replies({"believe": ["believability: 0.7"],
                        "other": ["breakage: 0.9",
                                  f"reasoning: {reason}\nbreakage: 0.9"]}, asked, sent)):
        # the refused reply is a warn in the alert log
        alerts.configure(Path(tmp) / "alerts.jsonl", "test", Path(tmp))
        uncertainty.REASONS_FILE = Path(tmp) / "reasons.jsonl"
        try:
            got = quantifier.JudgeQuantifier(model=M, cutoff=0.59).assess(claim, paper, attack)
            reasons = [json.loads(line)
                       for line in uncertainty.REASONS_FILE.read_text().splitlines()]
        finally:
            uncertainty.REASONS_FILE = kept
    again = [p for p in asked if "breakage: 0.9" in p]
    expect("the judge asks b and k separately, and asks a reply with no reasoning line "
           "again with a reminder",
           got == uncertainty.Assessment(believability=0.7, breakage=0.9) and len(asked) == 3
           and len(again) == 1 and "YOUR LAST REPLY BROKE A RULE" in again[0]
           and "reasoning:" in again[0], repr(got))
    expect("the reasoning of the breakage is saved with its claim and attack",
           reasons == [{"claim": claim.statement, "breakage": 0.9, "reasoning": reason,
                        "attack": [[h.design, h.result] for h in attack.hypotheticals]}],
           repr(reasons))
    prefix = shared_paper_prefix.shared_paper_prefix_for_model_calls(PAPER)
    expect("each judge call sends the paper as the shared prefix, and the fixed system part",
           len(sent) == 3 and all(o["shared_prefix_for_cache"] == prefix
                                  and o["system"] == quantifier.AFTER_PAPER_SYSTEM
                                  for o in sent), repr(sent))


def test_quantifiers() -> None:
    """The judge and coherence quantifiers: registered, built from the options, and the gate."""
    judge = uncertainty.QUANTIFIERS["judge"]({"judge_model": M})
    coherence = uncertainty.QUANTIFIERS["coherence"]({"coherence_command": "score-it",
                                                      "judge_model": M})
    expect("the judge and coherence quantifiers are registered, and built from the options",
           isinstance(judge, quantifier.JudgeQuantifier) and judge.model == M
           and judge.cutoff == uncertainty.CUTOFF.default
           and isinstance(coherence, quantifier.CoherenceQuantifier)
           and coherence.command == "score-it" and coherence.judge.model == M)
    a = quantifier.JudgeQuantifier(model=M + "a", cutoff=0.59)
    b = quantifier.JudgeQuantifier(model=M + "b", cutoff=0.59)
    expect("the judge's model and cutoff and the coherence command are part of each version",
           a.version != b.version
           and a.version != quantifier.JudgeQuantifier(model=M + "a", cutoff=0.7).version
           and quantifier.CoherenceQuantifier(command="a", judge=a).version
           != quantifier.CoherenceQuantifier(command="b", judge=a).version
           and quantifier.CoherenceQuantifier(command="a", judge=a).version
           != quantifier.CoherenceQuantifier(command="a", judge=b).version)
    expect("the coherence command text is not in the version, which readings save",
           "TOKEN" not in quantifier.CoherenceQuantifier(command="run TOKEN=x", judge=a).version)
    expect("a model name gives its URL and model",
           models.endpoint("http://gpu:8000/v1/chat/completions#Qwen/Qwen3-32B")
           == ("http://gpu:8000/v1/chat/completions", "Qwen/Qwen3-32B"))
    for name in ("http://gpu:8000/v1/chat/completions", "anthropic/claude-sonnet-5.5"):
        try:
            models.endpoint(name)
            expect(f"a model name that is not URL#MODEL is refused: {name}", False,
                   "no error raised")
        except ValueError:
            expect(f"a model name that is not URL#MODEL is refused: {name}", True)
    try:
        quantifier.models_are_reachable({uncertainty.WANT.key: True,
                                             "judge_model": "http://gpu:8000/v1/chat/completions"})
        expect("the options rule refuses that name before the run", False, "no error raised")
    except ValueError:
        expect("the options rule refuses that name before the run", True)
    one = uncertainty.Hypothetical(design="the 70B model", result="no gain")
    weak, strong, unlikely = (uncertainty.Attack(hypotheticals=(one,) * n) for n in (1, 2, 3))

    def scored(believability: float, breakage: float) -> uncertainty.Assessment:
        return uncertainty.Assessment(believability=believability, breakage=breakage)
    coherence = quantifier.CoherenceQuantifier(
        command="true", judge=quantifier.JudgeQuantifier(model=M, cutoff=0.59))
    local = coherence.uncertainty(((weak, scored(0.9, 0.2)), (strong, scored(0.55, 0.9)),
                                   (unlikely, scored(0.1, 1.0))))
    expect("coherence counts only an attack above the cutoff of the judge, as the judge does",
           local.value == 0.2 and local.worst == weak, repr(local))
    assessed = ((weak, scored(0.9, 0.2)), (strong, scored(0.59, 0.7)),
                (unlikely, scored(0.1, 1.0)))
    local = quantifier.JudgeQuantifier(model=M, cutoff=0.59).uncertainty(assessed)
    expect("the judge counts only an attack whose believability is above the cutoff",
           local.value == 0.2 and local.worst == weak, repr(local))
    local = quantifier.JudgeQuantifier(model=M, cutoff=0.5).uncertainty(assessed)
    expect("with a lower cutoff, the worst attack above it sets the uncertainty",
           local.value == 0.7 and local.worst == strong, repr(local))
    expect("no attack above the cutoff gives 0 and no worst attack",
           quantifier.worst_above_cutoff(((unlikely, scored(0.1, 1.0)),), 0.59)
           == uncertainty.Local(value=0.0, worst=None))
    claim = uncertainty.Premise(id="h", statement="Retrieval helps.", value=None)
    test_judge(claim, strong)
    test_coherence_command(claim, strong)
    sent = quantifier.request(claim, (claim,), strong)
    expect("the request to an external quantifier holds the claim, premises and every result",
           sent["claim"]["id"] == "h" and len(sent["premises"]) == 1 and len(sent["attack"]) == 2,
           repr(sent))


def test_watched_options() -> None:
    """The run options of a watched project and of verify, set from the environment or flags."""
    class TemplateQuantifier(FakeQuantifier):
        """A quantifier that is not written yet."""
        template = True
    uncertainty.QUANTIFIERS["template-test"] = lambda options: TemplateQuantifier()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state = root / ".verifier"
        state.mkdir()
        for name in ("runs", "logs"):
            (root / name).symlink_to(Path(WORKDIR) / name)
        (state / "methods").symlink_to(Path(WORKDIR) / "methods")
        (state / "claim_register.json").write_text(json.dumps({"claims": register()}))
        # the paper of the project, which claim uncertainty reads in full
        (root / "paper.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\n" + PAPER + "\n\\end{document}\n")
        with no_key():
            try:
                analyse_project(root, state)
                refused = ""
            except run_options.OptionsError as e:
                refused = str(e)
        expect("a watched project runs claim_uncertainty by default with the default cutoff, "
               "so it needs the key of its models",
               "OPENROUTER_API_KEY is not set" in refused and "uncertainty_cutoff" not in refused,
               refused)
        def options(**values: object) -> None:
            """Save the project's run options, as `verifier start --option` does."""
            (state / OPTIONS_FILE).write_text(json.dumps(values))

        options(want_claim_uncertainty=False)
        tree = analyse_project(root, state)
        result = {c.claim.id: c for c in tree.claims}["top"].checks["claim_uncertainty"]
        expect("a watched project with the check turned off does not run claim_uncertainty",
               isinstance(result, Off), repr(result))

        # from here on, the check flags no claim: a cutoff of 1 is never passed
        options(uncertainty_cutoff=1, want_claim_uncertainty=True, quantifier="template-test")
        tree = analyse_project(root, state)
        result = {c.claim.id: c for c in tree.claims}["top"].checks["claim_uncertainty"]
        expect("asked for with the template parts, it gives Skipped and calls no part",
               isinstance(result, Skipped) and "template" in result.because, repr(result))

        uncertainty.RED_TEAMS["default-test"] = lambda options: FakeRedTeam()
        uncertainty.QUANTIFIERS["default-test"] = lambda options: FakeQuantifier()
        options(uncertainty_cutoff=1, red_team="default-test", quantifier="default-test",
                want_claim_uncertainty=True, propagate_claim_uncertainty=True)
        tree = analyse_project(root, state)
        tested = watched_project.WatchedProject.load(state.parent, state).run_options(
            BUILT_IN_CHECKS)
        found = {c.claim.id: c for c in tree.claims}
        expect("a watched project runs claim_uncertainty on every commit, and saves readings",
               math.isclose(measured(found, "top"), 0.44)
               and (state / "readings" / "claim_uncertainty.json").exists())
        expect("test-check gets the same options as a watched run, whatever the environment",
               tested[uncertainty.RED_TEAM.key] == "default-test"
               and tested[uncertainty.WANT.key] is True
               and tested[uncertainty.READINGS.key] == str(state / "readings"
                                                       / "claim_uncertainty.json"), repr(tested))
        options(uncertainty_cutoff=1, claim_uncertainty_readings="elsewhere/r.json")
        placed = watched_project.WatchedProject.load(state.parent, state).run_options(
            BUILT_IN_CHECKS)[uncertainty.READINGS.key]
        expect("a relative readings path in the options is in the state directory",
               placed == str(state / "elsewhere" / "r.json"), placed)
        options(uncertainty_cutoff=1, claim_uncertainty_readings="")
        try:
            analyse_project(root, state)
            expect("an empty readings path in the options is refused", False, "no error raised")
        except run_options.OptionsError:
            expect("an empty readings path in the options is refused", True)
        broken = state / "readings" / "claim_uncertainty.json"
        saved = broken.read_text()
        broken.write_text("{not json")
        options(uncertainty_cutoff=1, quantifier="template-test")
        try:
            tree = analyse_project(root, state)
            result = {c.claim.id: c for c in tree.claims}["top"].checks["claim_uncertainty"]
            expect("a broken readings file stops the run when the check is on",
                   False, "no error raised")
        except ValueError:
            expect("a broken readings file stops the run when the check is on",
                   True)
        broken.unlink()
        options(uncertainty_cutoff=1, want_claim_uncertainty=False)
        tree = analyse_project(root, state)
        result = {c.claim.id: c for c in tree.claims}["top"].checks["claim_uncertainty"]
        expect("with the check off and no readings file, it is Off", isinstance(result, Off),
               repr(result))
        broken.write_text(saved)
        options(uncertainty_cutoff=1, want_claim_uncertainty=True, quantifier="template-test")
        project = watched_project.WatchedProject.load(root, state)
        tree = findings_analyzer.FindingsAnalyzer(project.check_registry()).analyse(
            analysis_request.AnalysisRequest(
                claim_register_path=str(state / "claim_register.json"),
                claim_register_base_folder=str(root),
                run_options=project.run_options(BUILT_IN_CHECKS),
                full_paper_text=project.full_paper_text(),
                claims_with_claim_checks=frozenset({"good", "prior", "mid"})))
        checked = {c.claim.id: c.checks["claim_uncertainty"] for c in tree.claims
                   if "claim_uncertainty" in c.checks}
        expect("with `only`, claims outside it get no claim check (test-check runs one claim)",
               "mid" in checked and not isinstance(checked["mid"], Off)
               and isinstance(checked.get("top"), Off),
               repr({k: type(v).__name__ for k, v in checked.items()}))
        for values in ({"no_such_thing": 1}, {"max_uncertainty_evals_per_claim": "ten"}):
            options(**values)
            try:
                project = watched_project.WatchedProject.load(state.parent, state)
                project.run_options(BUILT_IN_CHECKS)
                expect(f"the options {values} are refused", False, "no error raised")
            except run_options.OptionsError:
                expect(f"the options {values} are refused", True)
        (state / OPTIONS_FILE).unlink()

        argv = sys.argv
        # static-check has no computational method, so the premises here are traced to a document
        traced = json.loads(Path(WORKDIR, "claims.json").read_text())["claims"]
        traced_source = next(c["source"] for c in traced if c["id"] == "traced")
        without_artifacts = [{**c, "source": traced_source}
                             if c.get("source", {}).get("kind") == "artifact" else c
                             for c in register()]
        (root / "claims.json").write_text(json.dumps({"claims": without_artifacts}))
        sys.argv = ["verifier", "static-check", "--paper",
                    os.path.join(HERE, "fixtures", "broken_paper.tex"),
                    "--claims", str(root / "claims.json"), "--json",
                    "--claim-uncertainty", "--uncertainty-readings", str(root / "r.json"),
                    "--red-team", "default-test", "--quantifier", "default-test",
                    "--uncertainty-cutoff", "0.4", "--propagate-claim-uncertainty"]
        out = io.StringIO()
        try:
            # a flagged claim's worst case is distilled by a model, here a fake one
            with contextlib.redirect_stdout(out), fake_replies(
                    {"other": ["The claim assumes one fixed seed."]}, []):
                runtime_cli.main(sys.argv[1:])
        finally:
            sys.argv = argv
        claims = {c["claim"]["id"]: c for c in json.loads(out.getvalue())["findings"]["claims"]}
        result = claims["top"]["checks"]["claim_uncertainty"]
        expect("static-check accepts the --claim-uncertainty flags; with propagation it skips "
               "the check, because with no computational method no premise is verified",
               result["type"] == "Skipped" and "is not verified" in result["because"],
               repr(result))

        sys.argv = ["verifier", "static-check", "--paper",
                    os.path.join(HERE, "fixtures", "broken_paper.tex"),
                    "--claims", str(root / "claims.json"), "--claim-uncertainty"]
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = runtime_cli.main(sys.argv[1:])
        finally:
            sys.argv = argv
        expect("static-check --claim-uncertainty with no readings file exits 1 with a message",
               code == 1 and "claim_uncertainty_readings" in err.getvalue(), err.getvalue())

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "readings.json"
        one = uncertainty.Reading(local=uncertainty.Local(value=0.1, worst=None), assessed=(),
                                  red_team="r@1", quantifier="q@1", calls=0,
                                  without_attack=0.0, attacked=True)
        two = uncertainty.Reading(local=uncertainty.Local(value=0.2, worst=None), assessed=(),
                                  red_team="r@1", quantifier="q@1", calls=0,
                                  without_attack=0.2, attacked=False)
        uncertainty.save_reading(path, "a", one)
        first = uncertainty.load_readings(path)
        uncertainty.save_reading(path, "b", two)
        expect("a saved reading is read back after the file was read before",
               set(uncertainty.load_readings(path)) == {"a", "b"} and set(first) == {"a"})
        expect("an options rule of a check that is switched off does not run",
               _rule_skipped_when_off())
        expect("saving leaves no temporary or lock file next to the readings",
               sorted(p.name for p in Path(tmp).iterdir()) == ["readings.json"])
        mask = os.umask(0)
        os.umask(mask)
        expect("the readings file gets the usual mode for a new file",
               path.stat().st_mode & 0o777 == 0o666 & ~mask, oct(path.stat().st_mode))


def _rule_skipped_when_off() -> bool:
    """A limit of 0 is refused only while claim_uncertainty is on."""
    bad = {uncertainty.MAX_EVALS.key: 0}
    BUILT_IN_CHECKS.refuse_options_that_do_not_fit(bad, frozenset({uncertainty.CHECK}))
    try:
        BUILT_IN_CHECKS.refuse_options_that_do_not_fit(bad)
    except ValueError:
        return True
    return False

def test_on_by_default() -> None:
    """The check is on by default, so a run with an inferred claim needs the keys of its models.

    The cutoff has a default, so a run needs no cutoff.
    """
    readings = {uncertainty.READINGS.key: "r.json"}
    uncertainty.options_fit(readings)
    expect("on by default, the check accepts a run with no cutoff, and uses the default 0.59",
           uncertainty.CUTOFF.default == 0.59
           and uncertainty.CUTOFF.value_in_options(readings) == 0.59)
    uncertainty.options_fit({**readings, uncertainty.WANT.key: False})
    uncertainty.options_fit({**readings, uncertainty.CUTOFF.key: 0.5})
    expect("turned off, or with a cutoff, the check's options pass", True)
    plain = [c for c in register() if c.get("source", {}).get("kind") != "claims"]
    with tempfile.TemporaryDirectory() as tmp:
        found = run(Path(tmp), plain)
    expect("with no inferred claim in the register, no cutoff or key is needed",
           found and not any("claim_uncertainty" in c.checks for c in found.values()),
           repr(sorted(found)))
    saved = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            uncertainty.RED_TEAMS["fake"] = lambda options: FakeRedTeam()
            try:
                run(Path(tmp), register(), **{**readings, uncertainty.THRESHOLD.key: 0.5,
                                              uncertainty.RED_TEAM.key: "fake"})
                refused = ""
            except run_options.OptionsError as e:
                refused = str(e)
        expect("the judge with no OPENROUTER_API_KEY is refused before any check runs",
               "OPENROUTER_API_KEY is not set" in refused, refused)
    finally:
        if saved is not None:
            os.environ["OPENROUTER_API_KEY"] = saved
    with tempfile.TemporaryDirectory() as tmp:
        paths = StatePaths(root=Path(tmp))
        project = watched_project.WatchedProject.load(paths.root, paths.state)

        def save_given(given: list[str]) -> str | None:
            """Check the --option values like `verifier start`, and save them when they fit."""
            values, problem = project_preparation.run_options_given_at_start(project, given)
            if values is not None:
                project.save_run_option_values(values)
            return problem

        with no_key():
            problem = project_preparation.options_problem(project)
        expect("verifier start names a missing model key before the first commit",
               problem is not None and "OPENROUTER_API_KEY is not set" in problem, repr(problem))
        expect("--option saves a run option in .verifier/options.json",
               save_given(["want_claim_uncertainty=0"]) is None
               and json.loads((paths.state / OPTIONS_FILE).read_text())
               == {"want_claim_uncertainty": False})
        # the agent's command models need a key; no model is called here
        saved = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = saved or "test-key"
        try:
            problem = project_preparation.options_problem(project)
        finally:
            if saved is None:
                del os.environ["OPENROUTER_API_KEY"]
        expect("verifier start passes with the check turned off", problem is None, repr(problem))
        problem = save_given(["uncertainty_cutoff=0.5", "quantifier=judge"])
        expect("a start saves each --option, and keeps the other saved ones",
               problem is None and json.loads((paths.state / OPTIONS_FILE).read_text())
               == {"want_claim_uncertainty": False, "uncertainty_cutoff": 0.5,
                   "quantifier": "judge"}, repr(problem))
        for given, said in ((["no_such_thing=1"], "names no run option"),
                            (["max_uncertainty_evals_per_claim=ten"], "is not int"),
                            (["quantifier"], "KEY=VALUE")):
            problem = save_given(given)
            expect(f"--option {given[0]} is refused", problem is not None and said in problem,
                   repr(problem))
        (paths.state / OPTIONS_FILE).write_text(json.dumps({"claim_reordering_effort": "bogus"}))
        try:
            project.saved_run_option_values(project.check_registry())
            refused = ""
        except run_options.OptionsError as e:
            refused = str(e)
        expect("a value of options.json that is not a choice of the option is an OptionsError",
               OPTIONS_FILE in refused, refused)


def test_scenario() -> None:
    """The distilled text has 1 to 4 sentences of at most 25 words; it is saved by key."""
    ok = "The claim assumes X, e.g. one fixed seed. The evidence does not show Y."
    expect("scenario: two sentences pass, and \"e.g.\" does not end a sentence",
           scenario.read_reply(f"  {ok}\n") == ok)
    for name, reply in (("an empty reply", "  "),
                        ("five sentences", "A is so. B is so. C is so. D is so. E is so."),
                        ("a sentence of 26 words", " ".join(["word"] * 26) + ".")):
        try:
            scenario.read_reply(reply)
            expect(f"scenario: {name} is refused", False)
        except models.BadReply:
            expect(f"scenario: {name} is refused", True)
    with tempfile.TemporaryDirectory() as tmp:
        path = scenario.path_beside(Path(tmp) / "readings.json")
        first = scenario.key("claim", ["premise"], [("design", "result")], "m")
        expect("scenario: the key changes with the attack",
               first != scenario.key("claim", ["premise"], [("design", "other")], "m"))
        expect("scenario: nothing is saved at first", scenario.saved(path, first) is None)
        scenario.save(path, first, ok)
        expect("scenario: a saved text is read again", scenario.saved(path, first) == ok)
        path.write_text('{"scenarios": {"k": ""}}')
        try:
            scenario.saved(path, first)
            expect("scenario: a file with an empty text is refused", False)
        except ValueError:
            expect("scenario: a file with an empty text is refused", True)


def main() -> int:
    """Run every group of cases and report the failures."""
    for group in (test_propagate, test_check, test_local_steps, test_parts, test_cycles,
                  test_red_team, test_quantifiers, test_watched_options, test_on_by_default,
                  test_scenario):
        print(group.__name__)
        group()
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll uncertainty and support cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
