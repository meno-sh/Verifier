#!/usr/bin/env python3
"""Assert that a requested model review that gives no reading is a Skipped result, not a crash.

Covers:
  - a model failure after its retries is a Skipped result with the error;
  - a paper that is too long for the model is a Skipped result with the reason.

No model is called. Returns 1 if any case fails, so it can gate a commit.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checks.model_review import model_review  # noqa: E402
from schema.primitives import Skipped  # noqa: E402
from utils import models, shared_paper_prefix  # noqa: E402

FAILED: list[str] = []


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


class Context:
    """The parts of a PaperContext that the check reads."""

    def __init__(self, paper: str) -> None:
        self.options = {"want_model_review": True}
        self.paper = type("P", (), {"text": paper})()


def test_review_with_no_reading() -> None:
    """A model failure and a long paper each give a Skipped result."""
    def failing(*args: object, **kw: object) -> object:
        raise models.ProviderError("the provider timed out")
    real, models.ask_and_read = models.ask_and_read, failing
    try:
        result = model_review.ModelReviewChecks().run(Context("The paper.")).checks["model_review"]
    finally:
        models.ask_and_read = real
    expect("a model failure is a Skipped result with the error, not a crash",
           isinstance(result, Skipped) and "timed out" in result.because, repr(result))
    long_paper = "word " * (shared_paper_prefix.MAX_PAPER_TOKENS
                            * shared_paper_prefix.CHARACTERS_PER_TOKEN // 4)
    result = model_review.ModelReviewChecks().run(Context(long_paper)).checks["model_review"]
    expect("a paper too long for the model is a Skipped result with the reason, not a crash",
           isinstance(result, Skipped) and "tokens" in result.because, repr(result)[:300])


def main() -> int:
    """Run every case and report."""
    test_review_with_no_reading()
    print("all passed" if not FAILED else f"{len(FAILED)} FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
