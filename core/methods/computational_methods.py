"""Computational methods: the functions in the method files of a project.

Each method runs in its own process.

A method file is a Python file in a methods folder (`.verifier/methods/` in a watched
project). Each function with `@method("name")` is one method. The Verifier has no other
method. A method reads the data of a claim and gives a Computed, so the method, not the
Verifier, knows how the project stores its data. A method that crashes gives only its own
claim MethodFailed.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from core.registry.run_options import RegistryError
from schema import inputs
from schema.errors import VerifierError
from schema.primitives import MethodName, is_real_number, read_only, record
from utils import structure

if TYPE_CHECKING:     # the registry of core/registry/checks.py holds methods of this module
    from core.registry import checks

METHODS_DIR = "methods"
RUNNER = Path(__file__).with_name("method_runner.py")
METHOD_SECONDS = 300                    # a method must take seconds; this stops a hung one


type MethodFunction = Callable[[str], Computed]


@record
class ComputationalMethodEntry:
    """A computational method: how recompute turns a claim's data into one number.

    Methods come only from the method files of a project (core/methods/computational_methods.py),
    so the method, not the Verifier, knows how the project stores its data. It is called
    with the claim's `path` (a glob, which may match several files), resolved against the
    claim register's base folder, and returns a Computed. A method that raises gives its
    claim MethodFailed; the run goes on.
    """
    name: MethodName
    function: MethodFunction


@record
class Computed:
    """Holds the result of a computational method and refuses values of the wrong type."""
    value: float                        # the recomputed number; NaN when there was no data
    n: int                              # how many observations it rests on
    interval: tuple[float, float] | None = None     # a confidence interval, if it gives one
    # how the run that produced the data was set up, as the data records it; compared
    # with the claim's `setup` when the claim states one
    setup: Mapping[str, inputs.Scalar] | None = None

    def __post_init__(self) -> None:
        """Check every field's type; make the setup read-only."""
        if not is_real_number(self.value):
            raise TypeError(f"Computed.value must be a number, got {self.value!r}")
        if not isinstance(self.n, int) or isinstance(self.n, bool):
            raise TypeError(f"Computed.n must be an integer, got {self.n!r}")
        if self.interval is not None and not (
                isinstance(self.interval, tuple) and len(self.interval) == 2
                and all(is_real_number(x) for x in self.interval)):
            raise TypeError(f"Computed.interval must be (lo, hi), got {self.interval!r}")
        if self.setup is not None:
            if not isinstance(self.setup, Mapping) or not all(
                    isinstance(k, str) and isinstance(v, inputs.SCALAR_TYPES)
                    for k, v in self.setup.items()):
                raise TypeError(f"Computed.setup must map names to plain values, "
                                f"got {self.setup!r}")
            object.__setattr__(self, "setup", read_only(self.setup))


class MethodFileError(VerifierError):
    """A methods folder, or a method file in it, that cannot be loaded."""


class MethodError(VerifierError):
    """A method's process failed; the message is the last line it wrote to stderr."""


class SubprocessComputationalMethod:
    """One computational method, run in its own process on each call.

    It is the function of a ComputationalMethodEntry: it takes the path of a claim and
    gives a Computed, which checks the types the process sends back.
    """

    def __init__(self, python: str, file: str, method_name: str) -> None:
        """Run the method `method_name` of the method file `file` with the Python `python`."""
        self.python = python
        self.file = file
        self.method_name = method_name

    def __call__(self, path: str) -> Computed:
        """Run the method on `path`.

        Raises:
            MethodError: the process failed; the message is the last line of its stderr, with
                the count of the lines before it.
        """
        out = subprocess.run([self.python, "-I", str(RUNNER), self.file, self.method_name,
                              path], capture_output=True, text=True, timeout=METHOD_SECONDS)
        if out.returncode != 0:
            err = out.stderr.strip().splitlines() or ["no output"]
            cut = f"[... {len(err) - 1} earlier lines cut] " if len(err) > 1 else ""
            raise MethodError(cut + err[-1])
        fields = json.loads(out.stdout)
        fields["interval"] = fields["interval"] and tuple(fields["interval"])
        return Computed(**fields)


class MethodFolderLoader:
    """Adds the computational methods of a methods folder to a copy of a check registry."""

    def __init__(self, python: str = sys.executable) -> None:
        """Load methods that run with the Python `python`."""
        self.python = python

    def registry_with_methods(self, base: checks.CheckRegistry,
                              folder: Path | None) -> checks.CheckRegistry:
        """A copy of `base` with the methods of the method files in `folder` added.

        Args:
            base: the registry to copy.
            folder: the methods folder. None, or a folder that does not exist, adds nothing.

        Raises:
            MethodFileError: a method file is not valid, or two methods have one name.
        """
        out = base.copy_for_one_run()
        if folder is None:
            return out
        for path in structure.python_files_in_folder(folder):
            try:
                for name in self.method_names_in_file(path):
                    out.add_computational_method(ComputationalMethodEntry(
                        name=name,
                        function=SubprocessComputationalMethod(self.python, str(path), name)))
            except (SyntaxError, RegistryError) as e:
                raise MethodFileError(f"{path}: {e}") from e
        return out

    @staticmethod
    def method_names_in_file(path: Path) -> list[str]:
        """The names in the @method("name") decorators of a file, read without running it.

        Raises:
            MethodFileError: the file has no @method, or a name that is not a plain string.
        """
        calls = [d for node in ast.walk(ast.parse(path.read_text(), str(path)))
                 if isinstance(node, ast.FunctionDef) for d in node.decorator_list
                 if isinstance(d, ast.Call)
                 and getattr(d.func, "id", getattr(d.func, "attr", "")) == "method"]
        if not calls:
            raise MethodFileError(f"{path}: defines no @method")
        names = [c.args[0].value for c in calls
                 if len(c.args) == 1 and isinstance(c.args[0], ast.Constant)
                 and isinstance(c.args[0].value, str)]
        if len(names) != len(calls):
            raise MethodFileError(f'{path}: name each method with a plain string, '
                                  '@method("name")')
        return names
