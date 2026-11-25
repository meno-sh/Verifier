"""Run one computational method in another Python: `python -I method_runner.py FILE NAME PATH`.

Prints the method's Computed as JSON. Uses only the standard library (Python 3.7+).
A method file imports `Computed` and `method` from checks.interface. In this
process, the two classes below are that module. Keep the fields of Computed the same as
those of core.methods.computational_methods.Computed. `method` exists only here: a method file is
never imported in the Verifier's own process.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from collections.abc import Callable


class Computed:
    """Stand-in for core.methods.computational_methods.Computed: it only holds the fields."""

    def __init__(self, value: object, n: object, interval: object = None,
                 setup: object = None) -> None:
        """Store the fields to send back as JSON."""
        self.fields = {"value": value, "n": n, "interval": interval, "setup": setup}


def method(name: str) -> Callable[[Callable], Callable]:
    """The @method decorator of a method file: marks the function with its name."""
    def mark(function: Callable) -> Callable:
        function.verifier_method = name
        return function
    return mark


def plain(o: object) -> object:
    """A numpy number as a plain one; anything else is not a plain value."""
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"{o!r} is not a plain value")


def main(file: str, name: str, path: str) -> None:
    """Run the method called `name` in `file` on `path` and print its Computed as JSON."""
    interface = types.ModuleType("checks.interface")
    interface.Computed, interface.method = Computed, method
    sys.modules.update({"checks": types.ModuleType("checks"), "checks.interface": interface})
    stdout, sys.stdout = sys.stdout, sys.stderr      # the method's own prints go to stderr
    spec = importlib.util.spec_from_file_location("verifier_methods", file)
    module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)  # for dataclasses
    spec.loader.exec_module(module)
    got = next(f for f in vars(module).values()
               if getattr(f, "verifier_method", None) == name)(path)
    if not isinstance(got, Computed):
        sys.exit(f"returned {type(got).__name__}, not a Computed")
    stdout.write(json.dumps(got.fields, default=plain))


if __name__ == "__main__":
    main(*sys.argv[1:])
