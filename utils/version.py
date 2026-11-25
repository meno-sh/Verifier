"""The version of the Verifier code, so the output of a run says which version made it."""
import functools
import subprocess
from pathlib import Path

VERIFIER_HOME = Path(__file__).resolve().parent.parent      # the folder holding the verifier's code


@functools.cache
def verifier_short_commit() -> str:
    """The short commit of the Verifier code, or "unknown" outside a git checkout."""
    try:
        out = subprocess.run(["git", "-C", str(VERIFIER_HOME), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
    except OSError:
        out = None
    if out is not None and out.returncode == 0:
        return out.stdout.strip()
    return "unknown"
