"""The base class of every command of `vcheck` and `verifier`, also of the extensions."""
from __future__ import annotations

import abc
import argparse
from typing import ClassVar


class CLICommand(abc.ABC):
    """One command of `vcheck` or `verifier`.

    The program sets two arguments before it calls `execute`. For a `vcheck` command and
    for a `verifier` command of an extension, `args.root` is the folder that is checked
    and `args.state` is the state folder. For a built-in `verifier` command, `args.root`
    is the working directory.
    """
    HELP: ClassVar[str | None] = None       # the line of the command in the program's help

    @abc.abstractmethod
    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        """Add the arguments of the command to its parser."""

    @abc.abstractmethod
    def execute(self, args: argparse.Namespace) -> int:
        """Run the command, and give its exit code."""
