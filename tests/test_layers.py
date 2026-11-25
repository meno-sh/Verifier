#!/usr/bin/env python3
"""Assert the separation rules of the Verifier: which folder may import which.

From the bottom: `schema/`, then `utils/` (general helpers), then `core/`, then `checks/`
(one registered check in each subfolder). Above them are `runtime/` (the daemon, the git
hook and the agent), the review and diagnosis extensions, and the web page. A lower folder
never imports a higher one. A check reaches the core only through `checks/interface.py`, and never
imports another check. Only `checks/extension.py` names a registered check.

Returns 1 if any file breaks a rule.
"""
import ast
import sys
from collections.abc import Callable
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UPPER = ("runtime", "review", "diagnosis", "web")
INTERFACE = REPO / "checks" / "interface.py"       # the one way from a check to the core
EXTENSION = REPO / "checks" / "extension.py"       # adds every built-in check to a registry
# The registered checks: each folder in checks/ with a Python file in it.
CHECK_FOLDERS = frozenset(p.name for p in (REPO / "checks").iterdir()
                          if p.is_dir() and any(p.glob("*.py")))


def imported_modules(path: Path) -> list[str]:
    """The names of every module that one file imports, also `a.b` for `from a import b`."""
    names = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
            names += [f"{node.module}.{a.name}" for a in node.names]
    return names


def check_folder_of(module: str) -> str | None:
    """The registered check that a module name is in, such as "seeds", or None."""
    parts = module.split(".")
    return parts[1] if len(parts) > 1 and parts[0] == "checks" and parts[1] in CHECK_FOLDERS \
        else None


def top_folder(module: str) -> str:
    """The top folder of a module name, such as "core" for "core.gate"."""
    return module.split(".")[0]


def files_in(*folders: str) -> list[Path]:
    """Every Python file in the folders."""
    return [p for folder in folders for p in sorted((REPO / folder).rglob("*.py"))]


def own_check(path: Path) -> str | None:
    """The registered check that a file is in, or None."""
    parts = path.relative_to(REPO).parts
    return parts[1] if parts[0] == "checks" and len(parts) > 2 and parts[1] in CHECK_FOLDERS \
        else None


# Each rule: what it says, the files it covers, and the imports it refuses in one file.
type Refuses = Callable[[Path, str], bool]
RULES: tuple[tuple[str, list[Path], Refuses], ...] = (
    ("schema imports only schema",
     files_in("schema"),
     lambda path, name: top_folder(name) in ("utils", "checks", "core", *UPPER)),
    ("utils imports only schema and utils",
     files_in("utils"),
     lambda path, name: top_folder(name) in ("checks", "core", *UPPER)),
    ("core imports no check, no runtime and no extension",
     files_in("core"),
     lambda path, name: top_folder(name) in ("checks", *UPPER)),
    ("checks import no runtime and no extension",
     files_in("checks"),
     lambda path, name: top_folder(name) in UPPER),
    ("only checks/interface.py in checks/ imports the core",
     files_in("checks"),
     lambda path, name: top_folder(name) == "core" and path != INTERFACE),
    ("a check imports no other check",
     files_in("checks"),
     lambda path, name: check_folder_of(name) not in (None, own_check(path))
     and path != EXTENSION),
    ("schema, utils, core, runtime, the extensions and the web page name no registered check",
     files_in("schema", "utils", "core", "runtime", "review", "diagnosis", "web"),
     lambda path, name: check_folder_of(name) is not None),
)


def main() -> int:
    """Check every rule on every file it covers."""
    failed = 0
    for rule, paths, refuses in RULES:
        wrong = [f"{path.relative_to(REPO)} imports {name}"
                 for path in paths for name in imported_modules(path) if refuses(path, name)]
        if wrong:
            print(f"  FAIL  {rule}:\n        " + "\n        ".join(wrong))
            failed += 1
        else:
            print(f"  ok    {rule}")
    if failed:
        print(f"\n{failed} rule(s) broken.")
        return 1
    print("\nAll layer cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
