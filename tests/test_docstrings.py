#!/usr/bin/env python3
"""Assert that every module's docstring is at most MAX_LINES lines, so it stays a summary.

Longer explanations belong in docs/ or in the docstrings of what they describe. Tests are
exempt, because their docstrings list the cases they cover. Returns 1 if any module is over.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_LINES = 10


def main() -> int:
    """Print every module whose docstring is too long, or that all are within the limit."""
    over = []
    for package in sorted(p.parent for p in ROOT.glob("*/__init__.py")):  # tests/ has none
        for path in sorted(package.rglob("*.py")):
            # stage2-ab holds bench scripts copied as-is from a local experiment; tests are exempt
            parts = path.relative_to(ROOT).parts
            if "examples" in parts or "stage2-ab" in parts or "tests" in parts:
                continue
            lines = len((ast.get_docstring(ast.parse(path.read_text())) or "").splitlines())
            if lines > MAX_LINES:
                over.append(f"{path.relative_to(ROOT)}: the module docstring is {lines} lines; "
                            f"the limit is {MAX_LINES}. Keep it a summary, and move the detail "
                            "into docs/ or into the docstrings of what it describes.")
    print("\n".join(over) or f"Every module docstring is at most {MAX_LINES} lines.")
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
