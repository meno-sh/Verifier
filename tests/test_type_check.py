#!/usr/bin/env python3
"""Assert that `mypy --strict` finds no error in the modules of the OOP refactor.

The modules are listed in pyproject.toml ([tool.mypy] files). Each step of the
refactor adds the modules it rewrites. The test runs mypy with the interpreter of the
project, so CI needs no step of its own for it. Returns 1 if mypy finds an error.
"""
import os
import subprocess
import sys

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    """Run mypy on the listed modules and print what it says."""
    done = subprocess.run([sys.executable, "-m", "mypy"], cwd=HOME, capture_output=True,
                          text=True)
    print(done.stdout + done.stderr, end="")
    if done.returncode != 0:
        print("\nFAILED: mypy --strict found errors in the refactored modules.")
        return 1
    print("\nmypy --strict found no error in the refactored modules.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
