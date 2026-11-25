"""The check claim_uncertainty, with its red teams, quantifiers and scenarios.

Importing the package registers the parts that claim_uncertainty can use, by name, in
uncertainty.RED_TEAMS and uncertainty.QUANTIFIERS. Each part is built from the run options.
"""
from checks.uncertainty import quantifier, red_team, uncertainty

uncertainty.RED_TEAMS[red_team.PromptRedTeam.name] = red_team.PromptRedTeam.from_options
for _quantifier in (quantifier.JudgeQuantifier, quantifier.CoherenceQuantifier):
    uncertainty.QUANTIFIERS[_quantifier.name] = _quantifier.from_options
