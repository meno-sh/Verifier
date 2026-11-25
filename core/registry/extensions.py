"""Extensions: packages that add to the verifier without the core naming them.

An extension is a subclass of Extension, which overrides the hooks it uses. ExtensionSet
calls each hook on every loaded extension. `verifier start --extensions` saves the names
of a watched project in `.verifier/extensions.json`. The built-in checks are an extension
too (BUILTIN_CHECKS), which every run loads first; a static run loads only that one.

In the real-time verifier, the `root` that a hook gets is the daemon's checkout of the
tracked branch (`.verifier/checkout`) at the checked commit. The checkout exists only
after the first round starts, so `verifier` calls no hook that takes `root` before then.
"""
from __future__ import annotations

import functools
import importlib
import json
from collections.abc import Callable, Iterable
from pathlib import Path

from core import cli_command
from core.analysis import claim_checker
from core.registry import checks
from core.report_model import report_header
from core.report_model.disputed_flags import DisputedFlag
from schema import findings
from schema.errors import VerifierError
from utils import file_storage

# How an extension describes one kind of event to the auditing agent.
type EventDescriber = Callable[[dict[str, object], Path], str]


EXTENSIONS_FILE = "extensions.json"
# The extensions that `verifier start` and `verifier init` save when --extensions is not
# given and nothing is saved yet. A state folder with no extensions.json loads none.
DEFAULT_EXTENSIONS = ("review.extension:ReviewExtension",
                      "diagnosis.extension:DiagnosisExtension")


# The extension of the built-in checks. Every run loads it first, before the plug-in checks,
# the method files and the saved extensions. It is never saved in extensions.json, so
# `--extensions ""` keeps it. The core names it only here, and imports no check.
BUILTIN_CHECKS = "checks.extension:BuiltinChecks"


class ExtensionsFileError(VerifierError):
    """extensions.json, or an extension name in it, cannot be used."""


def extension_names_from_argument(text: str) -> tuple[str, ...]:
    """The extension names of an --extensions value: comma-separated, and empty for none."""
    return tuple(n.strip() for n in text.split(",") if n.strip())


class Extension:
    """One extension: a subclass overrides the hooks it uses. Each hook does nothing here.

    The core calls each hook through ExtensionSet, on every loaded extension, in load order.
    """

    def extend(self, check_registry: checks.CheckRegistry, root: Path,
               state_dir: Path) -> None:
        """Add checks to the registry of one run."""
        return None

    def notices(self, tree: findings.Findings, root: Path,
                state_dir: Path) -> list[report_header.ReportHeaderRow]:
        """The rows for the top of the report."""
        return []

    def disputed_flags(self, root: Path, state_dir: Path) -> list[DisputedFlag]:
        """The flags that a person disputed, which the report strikes out."""
        return []

    def verifier_commands(self) -> dict[str, cli_command.CLICommand]:
        """The commands that the extension adds to `verifier`, by name."""
        return {}

    def vcheck_commands(self) -> dict[str, cli_command.CLICommand]:
        """The commands that the extension adds to `vcheck`, by name."""
        return {}

    def status_lines(self, root: Path, state_dir: Path) -> list[str]:
        """The lines for `verifier status`."""
        return []

    def after_round_warnings(self, root: Path, state_dir: Path) -> list[str]:
        """The warnings after a round of the daemon."""
        return []

    def event_describers(self) -> dict[str, EventDescriber]:
        """How the extension describes its events to the auditing agent, by event kind."""
        return {}

    def agent_instructions(self) -> str | None:
        """The section of the extension for AGENTS.md, or None."""
        return None

    def worker_instructions(self) -> str | None:
        """The section of the extension for WORKER.md, or None."""
        return None


class ExtensionSet:
    """The loaded extensions, in load order, and a call of each hook on all of them."""

    def __init__(self, extensions: Iterable[Extension]) -> None:
        """Hold the extensions, in the order given."""
        self.extensions = tuple(extensions)

    @classmethod
    def load_named(cls, names: Iterable[str]) -> ExtensionSet:
        """Import the named extension classes, and make one of each.

        Each name is `module:Class`, such as `review.extension:ReviewExtension`.

        Raises:
            ExtensionsFileError: a name is not of this form, or names no Extension subclass.
        """
        return cls(cls.extension_named(name) for name in names)

    @staticmethod
    def extension_named(name: str) -> Extension:
        """One extension, from its name `module:Class`.

        Raises:
            ExtensionsFileError: the name is not of this form, or names no Extension subclass.
        """
        module_name, colon, class_name = name.partition(":")
        if not colon or not module_name or not class_name:
            raise ExtensionsFileError(f"{name!r} is not an extension name of the form "
                                      "module:Class, such as review.extension:ReviewExtension")
        found = getattr(importlib.import_module(module_name), class_name, None)
        if not (isinstance(found, type) and issubclass(found, Extension)):
            raise ExtensionsFileError(f"{name!r} names no subclass of Extension")
        return found()

    @classmethod
    def of_project(cls, state_dir: Path) -> ExtensionSet:
        """The extensions saved in the state folder `state_dir`, loaded."""
        return cls.load_named(cls.saved_names(state_dir))

    @staticmethod
    def saved_names(state_dir: Path) -> tuple[str, ...]:
        """The extension names saved in `state_dir`, in load order; none with no file.

        Raises:
            ExtensionsFileError: the file is not a JSON object with a list of names.
        """
        path = state_dir / EXTENSIONS_FILE
        if not path.exists():
            return ()
        data = json.loads(path.read_text())
        if (not isinstance(data, dict) or set(data) != {"extensions"}
                or not isinstance(data["extensions"], list)
                or not all(isinstance(n, str) and n.strip() for n in data["extensions"])):
            raise ExtensionsFileError(f'{path} must be {{"extensions": [extension names]}}')
        return tuple(data["extensions"])

    @classmethod
    def saved_or_default_names(cls, state_dir: Path) -> tuple[str, ...]:
        """The extension names saved in `state_dir`, or DEFAULT_EXTENSIONS when none are saved.

        Raises:
            ExtensionsFileError: as saved_names raises it.
        """
        return cls.saved_names(state_dir) if cls.names_are_saved(state_dir) else DEFAULT_EXTENSIONS

    @staticmethod
    def names_are_saved(state_dir: Path) -> bool:
        """Whether `state_dir` has a list of extension names."""
        return (state_dir / EXTENSIONS_FILE).exists()

    @staticmethod
    def save_names(state_dir: Path, names: tuple[str, ...]) -> None:
        """Save the extension names in `state_dir`, in load order."""
        file_storage.JsonFile(state_dir / EXTENSIONS_FILE, indent=2).write(
            {"extensions": list(names)})

    def add_checks_to_registry(self, check_registry: checks.CheckRegistry, root: Path,
                               state_dir: Path) -> checks.CheckRegistry:
        """A copy of the registry with the checks of every extension added."""
        out = check_registry.copy_for_one_run()
        for extension in self.extensions:
            extension.extend(out, root, state_dir)
        return out

    def report_notices(self, tree: findings.Findings, root: Path,
                       state_dir: Path) -> list[report_header.ReportHeaderRow]:
        """The rows of every extension for the top of the report, in load order."""
        return [row for extension in self.extensions
                for row in extension.notices(tree, root, state_dir)]

    def disputed_flags(self, root: Path, state_dir: Path) -> list[DisputedFlag]:
        """The disputed flags of every extension, in load order."""
        return [d for extension in self.extensions
                for d in extension.disputed_flags(root, state_dir)]

    def verifier_commands(self) -> dict[str, cli_command.CLICommand]:
        """The commands of every extension for `verifier`."""
        return self._merged_commands([e.verifier_commands() for e in self.extensions])

    def vcheck_commands(self) -> dict[str, cli_command.CLICommand]:
        """The commands of every extension for `vcheck`."""
        return self._merged_commands([e.vcheck_commands() for e in self.extensions])

    @staticmethod
    def _merged_commands(command_sets: list[dict[str, cli_command.CLICommand]]
                         ) -> dict[str, cli_command.CLICommand]:
        """The commands of all extensions in one map.

        Raises:
            ValueError: two extensions define the same command.
        """
        out: dict[str, cli_command.CLICommand] = {}
        for commands in command_sets:
            for name, command in commands.items():
                if name in out:
                    raise ValueError(f"two extensions define the command {name!r}")
                out[name] = command
        return out

    def status_lines(self, root: Path, state_dir: Path) -> list[str]:
        """The lines of every extension for `verifier status`."""
        return [line for e in self.extensions for line in e.status_lines(root, state_dir)]

    def after_round_warnings(self, root: Path, state_dir: Path) -> list[str]:
        """The warnings of every extension after a round of the daemon."""
        return [line for e in self.extensions
                for line in e.after_round_warnings(root, state_dir)]

    def agent_instructions(self) -> list[str]:
        """The sections of every extension for AGENTS.md, the guide of the auditing agent."""
        return [text for e in self.extensions
                if (text := e.agent_instructions()) is not None]

    def worker_instructions(self) -> list[str]:
        """The sections of every extension for WORKER.md, the guide of the worker's agent."""
        return [text for e in self.extensions
                if (text := e.worker_instructions()) is not None]

    def event_describers(self) -> dict[str, EventDescriber]:
        """How every extension describes its events to the auditing agent, by event kind."""
        out: dict[str, EventDescriber] = {}
        for extension in self.extensions:
            out.update(extension.event_describers())
        return out


@functools.cache
def builtin_check_registry() -> checks.CheckRegistry:
    """The registry of the built-in checks. Each run takes a copy (copy_for_one_run).

    The built-in checks read no project folder, so the current folder is given as the root
    and the state folder.
    """
    out = checks.CheckRegistry()
    ExtensionSet.extension_named(BUILTIN_CHECKS).extend(out, Path.cwd(), Path.cwd())
    out.add_run_options_of_module(claim_checker)     # how many claims are checked at once
    return out
