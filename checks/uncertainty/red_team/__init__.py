"""Red teams for the check claim_uncertainty (Part 2).

A red team writes proposals against one claim. A proposal is an Attack: a set of one
or more hypothetical results. Together with the premises, the set must go against the
claim. The modules: `prompted` (the red team), `prompt` (the text it sends) and
`reply` (how it reads the answer). Only `prompted` is used outside this package.
"""
from checks.uncertainty.red_team.prompted import RUN_OPTIONS, PromptRedTeam, options_fit

__all__ = ["RUN_OPTIONS", "PromptRedTeam", "options_fit"]
