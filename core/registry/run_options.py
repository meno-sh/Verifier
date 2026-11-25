"""The run options that checks and their parts read, and the rules that refuse bad values.

A module declares its own options in RUN_OPTIONS, and every entry point builds the
options from the check registry, so no entry point names a check. Each CheckRegistry
owns one RunOptionCatalog and copies it with itself.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection, Mapping
from pathlib import Path

from schema.errors import VerifierError
from schema.primitives import record

OPTION_KINDS = (bool, int, float, str)

type OptionsRule = Callable[[Mapping[str, object]], None]


class RegistryError(VerifierError):
    """A check, group, method or run option that cannot be registered as written.

    It is here, the lowest module that raises it, so the modules above can all raise it.
    """


class OptionsError(VerifierError):
    """A run option has the wrong type, or does not fit with the others."""


@record
class RunOption:
    """One run option: its key, type and defaults, and how a person sets it."""
    key: str
    kind: type                          # one of OPTION_KINDS
    help: str                           # one line, for the command line
    flag: str                           # the flag of `verifier static-check`, such as --red-team
    default: object = None              # the value when nobody sets it
    # the value in a watched project, when it is not `default`
    watched: object = None
    # the value is a path; in a watched project it is relative to the state directory
    in_state: bool = False
    # the values the option takes; empty for any value of its kind
    choices: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        """Refuse an unknown kind, a bad flag, or a default of the wrong type or not a choice."""
        if self.kind not in OPTION_KINDS:
            raise RegistryError(
                f"run option {self.key!r}: kind must be one of "
                f"{[k.__name__ for k in OPTION_KINDS]}, not {self.kind!r}")
        if not self.flag.startswith("--"):
            raise RegistryError(f"run option {self.key!r}: flag must start with --, "
                                f"not {self.flag!r}")
        if self.in_state and self.kind is not str:
            raise RegistryError(
                f"run option {self.key!r}: a path in the state directory is str")
        for value in (self.default, self.watched):
            if value is not None:
                self.value_in_options({self.key: value})

    def value_in_options(self, options: Mapping[str, object]) -> object:
        """The value of this option in `options`, or its default when absent or None.

        Raises:
            TypeError: the value is not of the kind of this option.
            ValueError: the option has choices, and the value is not one of them.
        """
        value = options.get(self.key)
        if value is None:
            return self.default
        if self.kind is float and isinstance(value, int) and not isinstance(value, bool):
            return float(value)
        if not isinstance(value, self.kind) or (self.kind is not bool and isinstance(value, bool)):
            raise TypeError(f"run option {self.key} must be {self.kind.__name__}, got {value!r}")
        if refused := self.refusal(value):
            raise ValueError(refused)
        return value

    def refusal(self, value: object) -> str | None:
        """Why `value` is not one of the option's choices, or None when it is (or any goes)."""
        if not self.choices or value in self.choices:
            return None
        return (f"{self.key} must be one of {', '.join(str(c) for c in self.choices)}, "
                f"not {value!r}")

    def parse_text_value(self, text: str) -> object:
        """The value of this option written as text, as on a command line or in the environment.

        Raises:
            ValueError: the text is not a value of the kind of this option.
        """
        if self.kind is bool:
            if text not in ("0", "1", "false", "true"):
                raise ValueError(f"run option {self.key}: write 0, 1, false or true, "
                                 f"not {text!r}")
            return text in ("1", "true")
        try:
            value = self.kind(text)
        except ValueError as e:
            raise ValueError(f"run option {self.key}: {text!r} is not "
                             f"{self.kind.__name__}") from e
        if refused := self.refusal(value):
            raise ValueError(f"run option {refused}")
        return value


@dataclasses.dataclass(frozen=True)
class OptionsRuleEntry:
    """A rule that refuses run options of one check that do not fit together."""
    check: str                          # the claim check or paper check group it belongs to
    function: OptionsRule


class RunOptionCatalog:
    """The run options that the registered checks declare, and the rules of these checks."""

    def __init__(self) -> None:
        """Create an empty catalog."""
        self.options: dict[str, RunOption] = {}
        self.rules: list[OptionsRuleEntry] = []

    def copy(self) -> RunOptionCatalog:
        """A catalog with the same options and rules, to which more can be added."""
        out = RunOptionCatalog()
        out.options, out.rules = dict(self.options), list(self.rules)
        return out

    def add_option(self, option: RunOption) -> None:
        """Add a run option. Refuses a key or a flag another option has."""
        if option.key in self.options:
            raise RegistryError(f"two run options have the key {option.key!r}")
        if any(o.flag == option.flag for o in self.options.values()):
            raise RegistryError(f"two run options have the flag {option.flag}")
        self.options[option.key] = option

    def add_options_rule(self, rule: OptionsRuleEntry) -> None:
        """Add the options rule of one check."""
        self.rules.append(rule)

    def values_for_watched_project(self, state_dir: Path,
                                   changed: Mapping[str, object] | None = None
                                   ) -> dict[str, object]:
        """Every run option at its value in a watched project with this state directory.

        Args:
            state_dir: the folder a path option with `in_state` is relative to.
            changed: values that replace the watched ones, such as those set in the
                environment; a relative path in them is also placed in `state_dir`.
        """
        changed = changed or {}
        out = {}
        for key, o in self.options.items():
            value = changed.get(key, o.default if o.watched is None else o.watched)
            if o.in_state and isinstance(value, str) and value.strip():
                value = str(state_dir / value)
            out[key] = value
        return out

    def refuse_options_that_do_not_fit(self, options: Mapping[str, object],
                                       registered_checks: Collection[str],
                                       switched_off: frozenset[str] = frozenset()) -> None:
        """Refuse options of the wrong type, or that the rule of a check that is on refuses.

        Args:
            options: the run options.
            registered_checks: the names of the claim checks and paper check groups of the
                registry; the rule of a check that is not registered does not run.
            switched_off: the checks that are off; their rules do not run.

        Raises:
            TypeError, ValueError: an option is wrong; the message says which.
        """
        for o in self.options.values():
            o.value_in_options(options)
        for rule in self.rules:
            if rule.check in registered_checks and rule.check not in switched_off:
                rule.function(options)


# The options rule decorator marks a function, and CheckRegistry.add_run_options_of_module
# adds every marked function of a module. Nothing is registered by importing alone.

MARK = "_verifier_registration"


def options_rule(check: str) -> Callable[[OptionsRule], OptionsRule]:
    """Mark a function that refuses run options of `check` that do not fit together.

    `check` names a claim check or a paper check group. The rule raises ValueError.
    The core calls it before any check runs, and only when `check` is on, so a wrong
    option stops the run before the paid model calls.
    """
    def mark(function: OptionsRule) -> OptionsRule:
        setattr(function, MARK, OptionsRuleEntry(check=check, function=function))
        return function
    return mark
