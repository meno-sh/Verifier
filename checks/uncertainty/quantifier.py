"""Quantifiers for the check claim_uncertainty (Part 3).

A quantifier scores one attack against one claim, with the claim's premises taken
as true. Then it turns the scores of all attacks into the uncertainty of that one
step, and it scores the breakage with no attack, from the premises alone. The judge
asks a model for each number in a separate request: believability without the claim,
breakage with it. The coherence quantifier takes believability from the judge and
breakage from the coherence metric: either from a model's next-token log
probabilities, or from an external command that runs the metric on a GPU (see
CoherenceQuantifier). utils/models.py gives the form of a model name.
"""
from __future__ import annotations

import hashlib
import json
import math
import queue
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from typing import ClassVar

from checks import interface
from checks.uncertainty import uncertainty
from checks.uncertainty.red_team import prompt
from utils import alerts, models, spending, text

# Haiku 5.5: it gives the scores with no reasoning unless asked; DeepSeek V4.1 Flash reasoned
# up to its 131,072-token limit in 4 of 115 score calls on the PR 144 eval
MODEL = interface.RunOption(key="judge_model", kind=str, flag="--judge-model",
                           default=models.preset.HAIKU,
                           help="the model the judge asks, as a model name "
                                "(utils/models.py); the coherence quantifier also takes "
                                "believability from it")
COMMAND = interface.RunOption(key="coherence_command", kind=str, flag="--coherence-command",
                             help="the command that runs the coherence metric on a GPU")
COHERENCE_MODEL = interface.RunOption(
    key="coherence_model", kind=str, flag="--coherence-model",
    help="the model that gives the coherence metric's breakage from the log probabilities "
         "of the first token of its answer, as a model name (utils/models.py)")
RUN_OPTIONS = (MODEL, COMMAND, COHERENCE_MODEL)
# the spending purpose of the judge and coherence model calls; under the claude CLI it
# picks the Claude model they get, so the version names it too (models.label)
PURPOSE = "quantifier"


@interface.options_rule(check=uncertainty.CHECK)
def models_are_reachable(options: Mapping[str, object]) -> None:
    """Refuse a judge or coherence model that cannot be called, before any model is called.

    Both quantifiers here ask the judge model; another quantifier asks neither model.

    Raises:
        ValueError: the check is asked for with a quantifier of this module, and one of
            its models is not a model name or its API key is not set.
    """
    chosen = uncertainty.QUANTIFIER.value_in_options(options)
    if uncertainty.WANT.value_in_options(options) and chosen in (JudgeQuantifier.name,
                                                                  CoherenceQuantifier.name):
        models.check_key(MODEL.value_in_options(options))
        coherence_model = COHERENCE_MODEL.value_in_options(options)
        if coherence_model is not None:
            models.check_key(coherence_model)


@interface.options_rule(check=uncertainty.CHECK)
def coherence_needs_one_source(options: Mapping[str, object]) -> None:
    """Refuse the coherence quantifier unless exactly one source of breakage is set.

    The source is the command or the model.

    Raises:
        ValueError: the check is asked for with the coherence quantifier, and both the
            command and the model are set, or neither is.
    """
    if (uncertainty.QUANTIFIER.value_in_options(options) == CoherenceQuantifier.name
            and uncertainty.WANT.value_in_options(options)
            and ((COMMAND.value_in_options(options) is None)
                 == (COHERENCE_MODEL.value_in_options(options) is None))):
        raise ValueError(f"the quantifier {CoherenceQuantifier.name} needs exactly one of "
                         f"{COMMAND.key} ({COMMAND.flag}) and {COHERENCE_MODEL.key} "
                         f"({COHERENCE_MODEL.flag})")
TIMEOUT = 300               # seconds the coherence command may take for one attack
ATTEMPTS = models.ATTEMPTS  # runs of the coherence command in all, for an error or a timeout

# the last line of each judge prompt
ENDING = 'End your answer with exactly this line:\n{name}: <number>'
# From the judge of the coherence experiments (project-coherence, src/claim_stress/rematch.py),
# with the two questions changed in review. Believability never sees the claim.
# The system part after the shared paper prefix (utils/shared_paper_prefix.py).
AFTER_PAPER_SYSTEM = 'Take the results that the paper reports as true.'

BELIEVE_PROMPT = (
    'You check hypothetical follow-up results against the paper in the instructions.\n'
    '\n'
    'Hypothetical follow-up result (nobody ran this experiment):\n'
    '{attack}\n'
    '\n'
    'How likely is it for the hypothetical result to occur, given the paper,'
    ' and assuming we run the hypothetical experiment? Give a number from 0 to 1.\n'
    '1 = certain to occur; 0 = impossible given the paper.\n'
    '\n'
    + ENDING.format(name="believability")
)
# With no attack, only the paper counts.
BREAK_PROMPT = (
    'Take the results of the paper in the instructions as true.{results}\n'
    '\n'
    'Claim: {claim}\n'
    '\n'
    'Read the scope of the claim carefully: the settings, data, conditions and quantities'
    ' that it states. A result that only goes against a more general version of the claim,'
    ' for example in settings or at scales that the claim does not cover, must not affect'
    ' your answer.\n'
    '\n'
    'How strongly do these results together show that the claim, within its stated scope,'
    ' is false? Give a number from 0 to 1.\n'
    '1 = the claim is certainly false; 0 = the claim stands.\n'
    '\n'
    'Before the final line, write one line that starts with "reasoning:". In at most 3'
    ' sentences, give the reason for your number. A reader must understand the reasoning'
    ' alone, without this prompt or any other reasoning: name the claim and the results'
    ' that you use.\n'
    + ENDING.format(name="breakage")
)
_NUMBER = r"([01](?:\.\d+)?|\.\d+)"

# The questions of the coherence metric (project-coherence, src/claim_stress/attack.py and
# scoring.py, put in a chat by build_messages_for_question in src/generation.py): each
# premise and each hypothetical result is a question and its answer, then the claim.
PREMISE_QUESTION = "What is an established result of the study?"
HYPOTHETICAL_QUESTION = "A follow-up experiment: {design}. What is its result?"
CLAIM_QUESTION = ("Claim: {claim}\nIs the claim true, given all the results above? "
                  "Answer True or False.")
# Asked once more when "True" or "False" is not among the first tokens of the answer.
STRICT_QUESTION = ("Claim: {claim}\nIs the claim true, given all the results above? "
                   "Answer with one word: True or False.")
TOP_TOKENS = 20             # first tokens read




def worst_above_cutoff(assessed: uncertainty.Assessed, cutoff: float) -> uncertainty.Local:
    """The largest breakage among the attacks with a believability above `cutoff`.

    Returns:
        That breakage and its attack, or 0 and no attack when no attack is above `cutoff`.
    """
    believable = [(a, s) for a, s in assessed if s.believability > cutoff]
    if not believable:
        return uncertainty.Local(value=0.0, worst=None)
    attack, scores = max(believable, key=lambda pair: pair[1].breakage)
    return uncertainty.Local(value=scores.breakage, worst=attack)


class JudgeQuantifier:
    """A model scores each attack, one request for each number (BELIEVE_PROMPT, BREAK_PROMPT)."""
    name = "judge"
    template = False
    # change it when the prompts change
    RUBRIC: ClassVar[str] = "6-paper-or-notes-prefix-scope-reasoned"

    def __init__(self, model: str, cutoff: float) -> None:
        """Use `model` as the judge; an attack counts when its believability is above `cutoff`."""
        self.model = model
        self.cutoff = cutoff
        # the model and the cutoff are part of every reading's binding
        self.version = f"{self.RUBRIC}/{models.label(model, PURPOSE)}/cutoff-{cutoff}"

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> JudgeQuantifier:
        """The judge with the model the run options name."""
        return cls(model=MODEL.value_in_options(options),
                   cutoff=uncertainty.CUTOFF.value_in_options(options))

    def assess(self, claim: uncertainty.Premise, premises: tuple[uncertainty.Premise, ...],
               attack: uncertainty.Attack) -> uncertainty.Assessment:
        """Score one attack against `claim`, with `premises` taken as true.

        Returns:
            believability: how well the attack agrees with the premises, from 0 to 1.
                This request does not show the claim.
            breakage: how much the premises and the attack together go against the
                claim, from 0 to 1.

        Raises:
            models.ProviderError: the model gave no answer after its retries.
            models.BadReply: two replies were not in the prompt's form.
        """
        return both(lambda: self.believability(premises, attack),
                    lambda: self.reasoned_breakage(claim, premises, attack))

    def breakage_without_attack(self, claim: uncertainty.Premise,
                                premises: tuple[uncertainty.Premise, ...]) -> float:
        """How much `premises` alone go against `claim`, from 0 to 1, with BREAK_PROMPT.

        Raises:
            models.ProviderError: the model gave no answer after its retries.
            models.BadReply: two replies were not in the prompt's form.
        """
        return self.reasoned_breakage(claim, premises, None)

    def believability(self, premises: tuple[uncertainty.Premise, ...],
                      attack: uncertainty.Attack) -> float:
        """How well `attack` agrees with `premises`, from 0 to 1; the claim is not shown."""
        return self.number(believe_prompt(premises, attack), "believability", premises)

    def reasoned_breakage(self, claim: uncertainty.Premise,
                          premises: tuple[uncertainty.Premise, ...],
                          attack: uncertainty.Attack | None) -> float:
        """The breakage, with its reasoning recorded in uncertainty.REASONS_FILE."""
        said: dict[str, str] = {}

        def read(reply: str) -> float:
            value = read_number(reply, "breakage")
            found = re.search(r"reasoning\W*:\s*(.+?)\s*\n[^\n]*breakage", reply,
                              re.IGNORECASE | re.DOTALL)
            if found is None:
                raise models.BadReply("the reply has no 'reasoning:' line: "
                                      f"{text.last_part(reply, 300)!r}")
            said["reasoning"] = re.sub(r"[*_`]", "", found.group(1)).strip()
            return value

        with spending.purpose(PURPOSE):
            value = models.ask_and_read(
                break_prompt(claim, premises, attack), self.model, read, system=AFTER_PAPER_SYSTEM,
                shared_prefix_for_cache=uncertainty.shared_paper_prefix_of_premises(premises))
        uncertainty.record_reason(claim, attack, value, said["reasoning"])
        return value

    def number(self, prompt: str, name: str,
               premises: tuple[uncertainty.Premise, ...]) -> float:
        """The `name` number in the model's reply to `prompt`, after the paper of `premises`.

        A reply that is not in the prompt's form is asked again once, with a reminder.

        Raises:
            models.ProviderError: the model gave no answer after its retries.
            models.BadReply: two replies were not in the prompt's form.
        """
        with spending.purpose(PURPOSE):
            return models.ask_and_read(
                prompt, self.model, lambda reply: read_number(reply, name),
                system=AFTER_PAPER_SYSTEM,
                shared_prefix_for_cache=uncertainty.shared_paper_prefix_of_premises(premises))

    def uncertainty(self, assessed: uncertainty.Assessed) -> uncertainty.Local:
        """The uncertainty of the step: the worst attack with a believability above the cutoff."""
        return worst_above_cutoff(assessed, self.cutoff)


class CoherenceQuantifier:
    """The coherence metric gives breakage; the judge gives believability.

    The coherence experiments found no coherence score of believability that works on
    real attacks, so believability comes from the judge. The metric reads a model's
    next-token log probabilities. It has two sources, and exactly one is set:

    - a model (`coherence_model`): the quantifier sends the metric's questions
      (coherence_messages) and computes breakage from the first token of the answer;
    - a command (`coherence_command`) that runs the metric on a GPU. The verifier and the
      command exchange JSON: the command reads `request(...)` on stdin and writes
      {"breakage": k} on stdout.
    """
    name = "coherence"
    template = False
    # change it when the metric or its model changes
    METRIC: ClassVar[str] = "6-cutoff-of-the-judge"

    def __init__(self, command: str | None, judge: JudgeQuantifier, *,
                 model: str | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        """Use `command` or `model` for breakage, and `judge` for believability.

        Each of `command` and `model` is None until the run options name it. `sleep`
        waits between two runs of the command; tests give one that does not wait.
        """
        self.command = command
        self.sleep = sleep
        self.model = model
        self.judge = judge
        # the command is part of every reading's binding as a checksum, so a token in
        # the command text is never written to a readings file
        digest = None if command is None else hashlib.sha256(command.encode()).hexdigest()[:12]
        label = None if model is None else models.label(model, PURPOSE)
        self.version = f"{self.METRIC}/{digest}/{label}/{judge.version}"

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> CoherenceQuantifier:
        """The quantifier with the command or model and the judge model the options name."""
        return cls(command=COMMAND.value_in_options(options),
                   judge=JudgeQuantifier.from_options(options),
                   model=COHERENCE_MODEL.value_in_options(options))

    def assess(self, claim: uncertainty.Premise, premises: tuple[uncertainty.Premise, ...],
               attack: uncertainty.Attack) -> uncertainty.Assessment:
        """Score one attack: believability from the judge, breakage from the coherence metric.

        Raises:
            ValueError: not exactly one of the command and the model is set, or the command
                wrote anything but the breakage.
            models.ProviderError: the judge gave no believability; the model gave no usable
                log probabilities; or the last of ATTEMPTS runs of the command did not
                finish in TIMEOUT seconds or exited with an error (for example, the
                scoring server is not reachable).
        """
        self.check_source()
        return both(lambda: self.judge.believability(premises, attack),
                    lambda: self.breakage(claim, premises, attack))

    def breakage_without_attack(self, claim: uncertainty.Premise,
                                premises: tuple[uncertainty.Premise, ...]) -> float:
        """How much `premises` alone go against `claim`, from the coherence metric.

        The model gets the metric's chat with no hypothetical result. The command gets a
        request with an empty attack list.

        Raises:
            ValueError: not exactly one of the command and the model is set, or the command
                wrote anything but the breakage.
            models.ProviderError: as for `assess`.
        """
        self.check_source()
        return self.breakage(claim, premises, None)

    def check_source(self) -> None:
        """Raise ValueError unless exactly one of the command and the model is set."""
        if (self.command is None) == (self.model is None):
            raise ValueError(f"the quantifier {self.name} needs exactly one of {COMMAND.key} "
                             f"({COMMAND.flag}) and {COHERENCE_MODEL.key} "
                             f"({COHERENCE_MODEL.flag})")

    def breakage(self, claim: uncertainty.Premise, premises: tuple[uncertainty.Premise, ...],
                 attack: uncertainty.Attack | None) -> float:
        """The breakage of one attack, or of no attack, from the model or else the command.

        A run of the command that exits with an error or takes more than TIMEOUT seconds
        is run again, up to ATTEMPTS runs in all, with the waits of utils/models.py.
        Each failed run before the last is a `warn` in the alert log.
        """
        if self.model is not None:
            return self.model_breakage(claim, premises, attack)
        sent = json.dumps(request(claim, premises, attack))
        for attempt in range(1, ATTEMPTS + 1):
            try:
                return breakage(self.run_command(sent))
            except models.ProviderError as e:
                if attempt == ATTEMPTS:
                    raise models.ProviderError(f"{e} (run {attempt} of {ATTEMPTS})") from e
                wait = min(models.FIRST_WAIT * 2 ** (attempt - 1), models.MAX_WAIT)
                alerts.notify("warn", "coherence.command_retry", str(e), error=e,
                              attempt=attempt, of=ATTEMPTS, wait_s=wait)
                self.sleep(wait)
        raise AssertionError("unreachable: the loop returns or raises")

    def run_command(self, sent: str) -> str:
        """Run the coherence command once with `sent` on stdin, and return its stdout.

        Raises:
            models.ProviderError: the command did not finish in TIMEOUT seconds, or it
                exited with an error.
        """
        try:
            done = subprocess.run(shlex.split(self.command), input=sent, capture_output=True,
                                  text=True, timeout=TIMEOUT, check=False)
        except subprocess.TimeoutExpired as e:
            raise models.ProviderError(
                f"the coherence command did not finish in {TIMEOUT} s") from e
        if done.returncode != 0:
            # the last 300 characters of each stream, so the error stays readable
            raise models.ProviderError(
                f"the coherence command exited with {done.returncode}; stdout: "
                f"{text.last_part(done.stdout.strip(), 300)!r}; "
                f"stderr: {text.last_part(done.stderr.strip(), 300)!r}")
        return done.stdout

    def model_breakage(self, claim: uncertainty.Premise,
                       premises: tuple[uncertainty.Premise, ...],
                       attack: uncertainty.Attack | None) -> float:
        """The breakage of one attack, or of no attack, from the first-token log probabilities.

        The model gets the metric's chat. If "True" or "False" is not among the first
        tokens of its answer, it gets the chat again, with STRICT_QUESTION. If one answer
        is still missing, the breakage is its upper bound (breakage_bound), and that is a
        `warn` in the alert log.

        Raises:
            models.ProviderError: the model gave no usable log probabilities, or neither
                answer is among the first tokens of either reply.
        """
        asked = []
        for question in (CLAIM_QUESTION, STRICT_QUESTION):
            with spending.purpose(PURPOSE):
                top = models.next_token_logprobs(
                    coherence_messages(claim, premises, attack, question), self.model,
                    TOP_TOKENS)
            if "True" in top and "False" in top:
                return true_false_breakage(top)
            asked.append(top)
        for top in reversed(asked):
            bound = breakage_bound(top)
            if bound is not None:
                alerts.notify("warn", "coherence.bound",
                              f"{self.model}: one of True and False is not among the "
                              f"{TOP_TOKENS} first tokens, so the breakage is its upper bound",
                              model=self.model, claim=claim.id, breakage=bound,
                              tokens=sorted(top, key=top.get, reverse=True)[:5],
                              tokens_not_listed=max(0, len(top) - 5))
                return bound
        raise models.ProviderError(
            f"{self.model}: neither True nor False is among the {TOP_TOKENS} first tokens, "
            f"after the strict question too (top 5: "
            f"{sorted(asked[-1], key=asked[-1].get, reverse=True)[:5]})")

    def uncertainty(self, assessed: uncertainty.Assessed) -> uncertainty.Local:
        """The uncertainty of the step: the worst attack with a believability above the cutoff.

        The cutoff is the cutoff of the judge (`--uncertainty-cutoff`), as for the judge
        quantifier, so the two quantifiers count the same attacks.
        """
        return worst_above_cutoff(assessed, self.judge.cutoff)


def coherence_messages(claim: uncertainty.Premise, premises: tuple[uncertainty.Premise, ...],
                       attack: uncertainty.Attack | None, question: str = CLAIM_QUESTION
                       ) -> list[dict[str, str]]:
    """The chat whose answer's first token gives the coherence metric's breakage.

    With CLAIM_QUESTION it is the scoring server's chat: a user question and an assistant
    answer for each premise and each hypothetical result, then the claim question from
    the user. With no attack, the chat has no hypothetical result. The server puts it in
    its model's own chat template; a chat-completions URL does the same. `question` has a
    {claim} field.
    """
    pairs = ([(PREMISE_QUESTION, _text(p)) for p in premises]
             + [(HYPOTHETICAL_QUESTION.format(design=h.design), h.result)
                for h in _hypotheticals(attack)])
    messages = [{"role": role, "content": text} for question, answer in pairs
                for role, text in (("user", question), ("assistant", answer))]
    return messages + [{"role": "user", "content": question.format(claim=_text(claim))}]


def true_false_breakage(top: Mapping[str, float]) -> float:
    """P("False") / (P("True") + P("False")), from first-token log probabilities.

    Raises:
        KeyError: "True" or "False" is not among the tokens.
    """
    logodds = top["False"] - top["True"]
    if logodds >= 0:
        return 1 / (1 + math.exp(-logodds))
    return math.exp(logodds) / (1 + math.exp(logodds))


def breakage_bound(top: Mapping[str, float]) -> float | None:
    """The largest breakage possible when exactly one of "True" and "False" is in `top`.

    The missing answer has at most the probability that the listed tokens leave over:
    1 minus their sum. With "False" missing, the bound is that rest / (P("True") + rest).
    With "True" missing, P("True") can be near 0, so the bound is 1.

    Returns:
        The bound, or None when both answers or neither are in `top`.
    """
    if ("True" in top) == ("False" in top):
        return None
    if "True" not in top:
        return 1.0
    rest = max(0.0, 1.0 - sum(math.exp(v) for v in top.values()))
    true = math.exp(top["True"])
    return rest / (true + rest)


def _text(p: uncertainty.Premise) -> str:
    """A claim's statement, and its value with every digit kept, when it has one."""
    if p.value is None:
        return p.statement
    return f"{p.statement} (value: {prompt.value_text(p.value)})"


def _premises(premises: tuple[uncertainty.Premise, ...]) -> str:
    return "\n".join(f"- {_text(p)}" for p in premises)


def _hypotheticals(attack: uncertainty.Attack | None) -> tuple[uncertainty.Hypothetical, ...]:
    """The attack's hypothetical results; none for no attack."""
    return () if attack is None else attack.hypotheticals


def _attack(attack: uncertainty.Attack | None) -> str:
    return "\n".join(f"- {h.design}: {h.result}" for h in _hypotheticals(attack))


def both(believability: Callable[[], float], breakage_: Callable[[], float]
         ) -> uncertainty.Assessment:
    """The two scores of one attack, asked at the same time, because neither needs the other.

    The first error is raised at once. The other request is not waited for: it runs in a
    daemon thread, so it does not hold up the error, Ctrl-C or the exit.
    """
    done: queue.Queue[tuple[str, float | None, BaseException | None]] = queue.Queue()

    def score(name: str, ask: Callable[[], float]) -> None:
        """Put the score, or the error that `ask` raised, on `done`."""
        try:
            done.put((name, ask(), None))
        except BaseException as e:  # noqa: BLE001 - raised again by the caller's thread
            done.put((name, None, e))

    for name, ask in (("believability", believability), ("breakage", breakage_)):
        threading.Thread(target=score, args=(name, ask), daemon=True).start()
    scores = {}
    while len(scores) < 2:
        name, value, error = done.get()
        if error is not None:
            raise error
        scores[name] = value
    return uncertainty.Assessment(**scores)


def believe_prompt(premises: tuple[uncertainty.Premise, ...], attack: uncertainty.Attack) -> str:
    """The judge's believability prompt; the paper is in the system part."""
    return BELIEVE_PROMPT.format(attack=_attack(attack))


def break_prompt(claim: uncertainty.Premise, premises: tuple[uncertainty.Premise, ...],
                 attack: uncertainty.Attack | None) -> str:
    """The judge's breakage prompt; the paper is in the system part."""
    results = "" if attack is None else " Also take this result as true:\n" + _attack(attack)
    return BREAK_PROMPT.format(results=results, claim=_text(claim))


def read_number(reply: str, name: str) -> float:
    """The number on the final `name:` line of a judge's reply, read strictly.

    Text before the line is allowed, and repeated `name:` lines with the same value are
    accepted. Markdown emphasis anywhere in a line and a full stop at its end are ignored,
    because models often write "**breakage:** 0.8" or "breakage: 0.8.".

    Raises:
        models.BadReply: the reply does not end with the line, its `name:` lines
            disagree, or the number is above 1.
    """
    plain = "\n".join(re.sub(r"[.]\s*$", "", re.sub(r"[*_`]", "", line).strip())
                      for line in reply.splitlines()).strip()
    line = re.compile(rf"^{name}:\s*{_NUMBER}$", re.IGNORECASE | re.MULTILINE)
    found = {float(v) for v in line.findall(plain)}
    last = plain.splitlines()[-1] if plain else ""
    if len(found) != 1 or not line.fullmatch(last):
        raise models.BadReply(f"the reply does not end with one consistent '{name}:' line: "
                         f"{text.last_part(reply, 300)!r}")
    value = found.pop()
    if value > 1:
        raise models.BadReply(f"the {name} must be from 0 to 1, got {value}")
    return value


def breakage(stdout: str) -> float:
    """The breakage an external quantifier wrote on the last line of `stdout`, read strictly.

    Lines before the last are allowed, because a command can log to stdout.

    Raises:
        ValueError: the last line is not a JSON object with exactly the breakage, from 0 to 1.
    """
    lines = stdout.strip().splitlines()
    try:
        reply = json.loads(lines[-1] if lines else "")
    except json.JSONDecodeError as e:
        raise ValueError("the coherence command did not write JSON: "
                         f"{text.first_part(stdout, 200)!r}") from e
    if not isinstance(reply, dict) or set(reply) != {"breakage"}:
        raise ValueError("the coherence command must write exactly {\"breakage\": k}, "
                         f"got {reply!r}")
    value = reply["breakage"]
    uncertainty.check_unit_number(value, "the coherence command's breakage")
    return float(value)


def request(claim: uncertainty.Premise, premises: tuple[uncertainty.Premise, ...],
            attack: uncertainty.Attack | None) -> dict:
    """The JSON that an external quantifier gets for one attack; an empty list for no attack."""
    def premise(p: uncertainty.Premise) -> dict:
        return {"id": p.id, "statement": p.statement, "value": p.value}
    return {"claim": premise(claim), "premises": [premise(p) for p in premises],
            "attack": [{"design": h.design, "result": h.result} for h in _hypotheticals(attack)]}
