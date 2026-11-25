"""How PromptRedTeam reads the model's reply."""
from __future__ import annotations

from checks.uncertainty import uncertainty
from utils import alerts, models


def read(text: str, proposals: int, results: int) -> tuple[uncertainty.Attack, ...]:
    """The proposals in a reply.

    The rules: the text is one JSON value (models.read_json) of the schema in
    prompt.schema, with at least 1 proposal, each with 1 to `results` results, and
    each design and result non-empty text. The prompt asks for exactly `proposals`
    proposals, but models often give one more or one fewer. A longer reply gives its
    first `proposals` proposals, and only those are read. A shorter reply gives all
    of its proposals. A proposal that breaks a rule is left out, with a warn in the
    alert log, so long as at least one proposal keeps the rules: one bad proposal
    out of 8 should not cost the claim its score.

    Raises:
        models.BadReply: the text breaks a rule outside the proposals, or every proposal
            breaks one; the message says which.
    """
    raw = models.read_json(text)
    if not isinstance(raw, dict) or set(raw) != {"proposals"}:
        raise models.BadReply("the reply must be an object with only the key `proposals`")
    items = raw["proposals"]
    if not isinstance(items, list):
        raise models.BadReply("`proposals` must be a list")
    if not items:
        raise models.BadReply("`proposals` must have at least 1 item, not 0")
    attacks, broken = [], []
    for n, item in enumerate(items[:proposals], start=1):
        try:
            attacks.append(_attack(n, item, results))
        except models.BadReply as e:
            broken.append(e)
    if not attacks:
        raise broken[0]
    if broken:
        alerts.notify("warn", "red_team.proposal_left_out",
                      f"{len(broken)} of {len(broken) + len(attacks)} proposals left out: "
                      f"{broken[0]}")
    return tuple(attacks)


def _attack(n: int, item: object, results: int) -> uncertainty.Attack:
    """Proposal number `n` of the reply."""
    if not isinstance(item, dict) or set(item) != {"hypotheticals"}:
        raise models.BadReply(f"proposal {n} must be an object with only the key "
                              "`hypotheticals`")
    found = item["hypotheticals"]
    if not isinstance(found, list) or not 1 <= len(found) <= results:
        raise models.BadReply(f"`hypotheticals` of proposal {n} must be a list of 1 to "
                              f"{results} results")
    hypotheticals = []
    for m, h in enumerate(found, start=1):
        if (not isinstance(h, dict) or set(h) != {"design", "result"}
                or not all(isinstance(v, str) for v in h.values())):
            raise models.BadReply(f"result {m} of proposal {n} must be an object with "
                                  "exactly `design` and `result`, both text")
        try:
            hypotheticals.append(uncertainty.Hypothetical(design=h["design"], result=h["result"]))
        except ValueError as e:         # an empty design or result
            raise models.BadReply(f"result {m} of proposal {n}: {e}") from e
    return uncertainty.Attack(hypotheticals=tuple(hypotheticals))
