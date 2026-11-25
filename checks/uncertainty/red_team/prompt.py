"""The text that PromptRedTeam sends.

The full paper goes first, as the shared prefix that only the provider's cache mark covers.
The system part after it is the same for every claim in a run, and the rounds after the
first add one fixed paragraph on feedback. The user part
holds the claim and its premises, and from the second round the round and every
earlier proposal with its two scores.
"""
from __future__ import annotations

import json

from checks.uncertainty import uncertainty
from schema import inputs


def schema(proposals: int, results: int) -> dict:
    """The JSON schema of the reply, with the counts of one run.

    It is given to the model with no example, so the model is not anchored to one
    kind of proposal.
    """
    if proposals < 1 or results < 1:
        raise ValueError(f"proposals and results must be at least 1, got {proposals}, {results}")
    return {
        "type": "object",
        "required": ["proposals"],
        "additionalProperties": False,
        "properties": {"proposals": {
            "type": "array", "minItems": proposals, "maxItems": proposals,
            "items": {
                "type": "object",
                "required": ["hypotheticals"],
                "additionalProperties": False,
                "properties": {"hypotheticals": {
                    "type": "array", "minItems": 1, "maxItems": results,
                    "items": {
                        "type": "object",
                        "required": ["design", "result"],
                        "additionalProperties": False,
                        "properties": {"design": {"type": "string"},
                                       "result": {"type": "string"}},
                    }}},
            }}},
    }


# The part of the instructions that only the rounds after the first get.
FEEDBACK = """
This is not the first round. The user message also gives your proposals from the \
earlier rounds, each with two scores from 0 to 1 that the verifier measured:
- "believability": how well the proposal agrees with the paper. A proposal with \
a low believability is not believed, so it cannot count against the claim.
- "breakage": how much the paper and the proposal together go against the claim.
Use the scores. Find what made the best earlier proposals strong and what made the \
weak ones fail, then write new proposals that are stronger than all of them. Do not \
repeat an earlier proposal.
"""


def system_text(proposals: int, results: int, feedback: bool = False) -> str:
    """The static instructions, with the schema. Nothing in them is about one claim or paper.

    The paper goes before them, as the shared prefix (utils/shared_paper_prefix.py).

    `feedback` adds the paragraph on earlier proposals and their scores. Only the
    rounds after the first get it, so the first round says nothing about feedback.
    """
    return f"""\
You are the red team of a research verifier. A researcher states a claim in a paper. \
The full text of the paper (LaTeX or Markdown notes) is before these instructions. \
Take the results that the paper reports as true. Your task is to find the evidence that would most \
effectively show that the claim does not follow from what the paper reports.

Write exactly {proposals} proposals against the claim in the user message.

A proposal is a set of 1 to {results} hypothetical results. A hypothetical result is the \
outcome of a follow-up study that nobody has done. It has two parts:
- "design": the follow-up study, specific enough that someone could carry it out. It \
can be any kind of study: an experiment, a statistical reanalysis of existing data, a \
human study, an analysis of transcripts or model outputs, a causal analysis, a \
qualitative study, or any other kind. Choose the kind that would falsify the claim most \
effectively.
- "result": the outcome you suppose the study gives, stated as a research paper states \
a result, with numbers where the study gives numbers.

The rules:
1. The results of one proposal, together with the paper, must go against the claim. \
They can do this together even if no single result does.
2. No result may contradict the paper with high probability (for example 75% or more). \
A result that the paper makes unlikely is a weak proposal, because nobody will believe \
it. Do not restate a result of the paper as a result.
3. Make each proposal as strong as you can: a plausible result that, if it were true, \
would most decrease belief in the claim.
4. Make the proposals as different from each other as you can. Vary them along \
different axes: the kind of study, the population or data, the conditions, the \
measure, and the way the claim could fail.
{FEEDBACK if feedback else ""}
Think hard before you answer.

Reply with one JSON object and nothing else: no text before or after it. The object \
must follow this JSON schema:
{json.dumps(schema(proposals, results))}
"""




def user_text(claim: uncertainty.Premise, premises: tuple[uncertainty.Premise, ...],
              scored: uncertainty.Assessed = (), round_number: int = 1, rounds: int = 1) -> str:
    """The claim, each premise, and in a later round, the round and each scored proposal.

    Each premise is given with its value when it has one. The claim ids are not given:
    they are register names and carry no meaning for the model. The first round has no
    `scored`, and says nothing of rounds.

    Raises:
        ValueError: no premise, a round outside 1 to `rounds`, or `scored` given in
            the first round or missing in a later one.
    """
    if not premises:
        raise ValueError("the red team needs at least one premise")
    if not 1 <= round_number <= rounds:
        raise ValueError(f"round {round_number} is not in 1 to {rounds}")
    if (round_number > 1) != bool(scored):
        raise ValueError(f"round {round_number} got {len(scored)} earlier proposals; only "
                         "rounds after the first have them, and each such round has some")
    lines = ["The claim to attack (from the paper in the instructions):", _item(claim)]
    if scored:
        lines += ["", f"This is round {round_number} of {rounds}.", "",
                  "Your proposals from the earlier rounds, with their scores:"]
        for n, (attack, assessment) in enumerate(scored, start=1):
            lines.append(f"Proposal {n}: believability {assessment.believability:.2f}, "
                         f"breakage {assessment.breakage:.2f}")
            lines += [f"- design: {h.design}\n  result: {h.result}"
                      for h in attack.hypotheticals]
    return "\n".join(lines)


def _item(claim: uncertainty.Premise) -> str:
    """One claim's statement, and its value when it has one."""
    if claim.value is None:
        return claim.statement
    return f"{claim.statement} (reported value: {value_text(claim.value)})"


def value_text(value: float) -> str:
    """A value with every digit kept: 1234567, not 1.23457e+06 as `:g` gives.

    A decimal from the register keeps the text the paper printed (0.120 stays 0.120).
    """
    if isinstance(value, inputs.PrintedFloat):
        return value.text
    if value == int(value) and abs(value) < 1e16:
        return str(int(value))
    return repr(value)
