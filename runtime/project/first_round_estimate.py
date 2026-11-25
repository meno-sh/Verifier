"""The cost and time of a first round, estimated before it starts from `cost_rates.json`.

`verifier start` and `verifier init` show the estimate when the person selects the notes
(runtime/project/scope.py). The rates are rough guesses for the default models. The rate of the
claim uncertainty check is per claim, because the number of claims that rest on other
claims is not known before the round.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

RATES_FILE = Path(__file__).with_name("cost_rates.json")


@dataclasses.dataclass(frozen=True)
class Estimate:
    """The cost and time of a first round, low and high, in all and per part."""
    dollars: tuple[float, float]
    minutes: tuple[float, float]
    parts: dict[str, tuple[float, float]]       # the dollars of each part, low and high


def estimate(candidates: int, path: Path = RATES_FILE) -> Estimate:
    """The cost and time of a first round over `candidates`, from the rates in `path`.

    The fixed parts and the parts per candidate are the same in the low and the high
    estimate. The uncertainty check differs, because the number of claims that rest on
    other claims is not known before the round: `claims_per_candidate` gives it low and high.

    Raises:
        ValueError: `candidates` is negative, or the rates file is not in its form.
    """
    if candidates < 0:
        raise ValueError(f"the number of candidates cannot be negative, got {candidates}")
    parts = json.loads(path.read_text()).get("parts")
    linear = ("fixed_dollars", "dollars_per_candidate", "fixed_minutes", "minutes_per_candidate")
    per_claim = ("dollars_per_claim", "minutes_per_claim")
    if not isinstance(parts, dict) or set(parts) != {"agent", "grouping_and_tools",
                                                     "claim_uncertainty"}:
        raise ValueError(f"{path} must have the parts agent, grouping_and_tools and "
                         "claim_uncertainty")
    dollars: dict[str, tuple[float, float]] = {}
    minutes = [0.0, 0.0]
    for name, rate in parts.items():
        keys = per_claim + ("claims_per_candidate",) if name == "claim_uncertainty" else linear
        if not isinstance(rate, dict) or not all(k in rate for k in keys):
            raise ValueError(f"{path}: the part {name} needs {', '.join(keys)}")
        numbers = [rate[k] for k in keys if k != "claims_per_candidate"]
        if not all(isinstance(x, int | float) and not isinstance(x, bool) and x >= 0
                   for x in numbers):
            raise ValueError(f"{path}: the rates of {name} must be numbers, 0 or more")
        if name == "claim_uncertainty":
            ratio = rate["claims_per_candidate"]
            if not (isinstance(ratio, list) and len(ratio) == 2 and 0 <= ratio[0] <= ratio[1]):
                raise ValueError(f"{path}: claims_per_candidate must be [low, high]")
            claims = [candidates * r for r in ratio]
            dollars[name] = (claims[0] * rate["dollars_per_claim"],
                             claims[1] * rate["dollars_per_claim"])
            minutes = [m + c * rate["minutes_per_claim"] for m, c in zip(minutes, claims,
                                                                         strict=True)]
        else:
            d = rate["fixed_dollars"] + candidates * rate["dollars_per_candidate"]
            dollars[name] = (d, d)
            m = rate["fixed_minutes"] + candidates * rate["minutes_per_candidate"]
            minutes = [x + m for x in minutes]
    return Estimate(dollars=(sum(lo for lo, _ in dollars.values()),
                             sum(hi for _, hi in dollars.values())),
                    minutes=(minutes[0], minutes[1]), parts=dollars)
