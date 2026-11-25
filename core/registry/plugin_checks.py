"""Plug-in checks: claim checks written for one project, added to a check registry.

A plug-in check file is a Python file in a checks folder (`.verifier/checks/` in a watched
project). Each subclass of PluginCheck (below) in it is one check. It imports PluginCheck from
checks/interface.py.
It uses only the standard library. Plug-in checks are untrusted. A check that
crashes, or reports a kind it did not declare, gives its claim Skipped. A file that does
not load stops the run. A plug-in check has the name "plugin_check:<name>".
"""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType
from typing import ClassVar

from core.registry import checks
from core.registry.run_options import RegistryError
from schema.errors import VerifierError
from utils import structure

CHECKS_DIR = "checks"


# Goes before the name of each plug-in check, so a report shows where the check came from.
PLUGIN_CHECK_PREFIX = "plugin_check:"


class PluginCheck(checks.ClaimCheck):
    """A claim check that a project wrote, in `.verifier/checks/`.

    It is untrusted: if it crashes, its claim gets Skipped, and the run goes on. Its name
    in the report is its class attribute `name` after PLUGIN_CHECK_PREFIX.
    """
    trusted: ClassVar[bool] = False

    def __init__(self) -> None:
        """Name the check with PLUGIN_CHECK_PREFIX before the name its class declares."""
        self.name = PLUGIN_CHECK_PREFIX + type(self).name


class PluginCheckError(VerifierError):
    """A checks folder, or a plug-in check file in it, that cannot be loaded."""


class PluginCheckFolderLoader:
    """Adds the plug-in checks of a checks folder to a copy of a check registry."""

    def registry_with_plugin_checks(self, base: checks.CheckRegistry,
                                    folder: Path | None) -> checks.CheckRegistry:
        """A copy of `base` with the claim checks of the files in `folder` added.

        Args:
            base: the registry to copy.
            folder: the checks folder. None, or a folder that does not exist, adds nothing.

        Raises:
            PluginCheckError: a file does not load, defines no PluginCheck subclass, or a
                check clashes with another.
        """
        out = base.copy_for_one_run()
        if folder is None:
            return out
        for path in structure.python_files_in_folder(folder):
            module = self.import_check_file(path)
            found = self.plugin_check_classes_in_module(module)
            if not found:
                raise PluginCheckError(f"{path}: defines no subclass of PluginCheck")
            unnamed = [c.__name__ for c in found if not isinstance(getattr(c, "name", None), str)]
            if unnamed:
                raise PluginCheckError(f"{path}: the check {unnamed[0]} has no `name`. Give it "
                                       'a class attribute such as name = "my_check".')
            try:
                for check_class in found:
                    out.add_claim_check(check_class())
                out.add_run_options_of_module(module, trusted=False)
            except RegistryError as e:
                raise PluginCheckError(f"{path}: {e}") from e
        return out

    @staticmethod
    def plugin_check_classes_in_module(module: ModuleType) -> list[type[PluginCheck]]:
        """The concrete PluginCheck subclasses that a module defines, in their file order."""
        return [value for value in vars(module).values()
                if isinstance(value, type) and issubclass(value, PluginCheck)
                and value.__module__ == module.__name__ and not inspect.isabstract(value)]

    @staticmethod
    def import_check_file(path: Path) -> ModuleType:
        """Import one plug-in check file as a module named after it.

        Raises:
            PluginCheckError: the file cannot be imported, or raises when it runs.
        """
        # A name unique to the file, so two checks folders never share a module, and
        # registered while it runs, as dataclasses and pickling look modules up there.
        digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
        name = f"verifier_plugin_checks.{path.stem}_{digest}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise PluginCheckError(f"{path}: cannot be imported")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:  # noqa: BLE001 - reported with the file name, then raised
            del sys.modules[name]
            raise PluginCheckError(f"{path}: {type(e).__name__}: {e}") from e
        return module
