"""The text of the full paper that goes first in every model call that reads the paper.

Every stage that sends the paper to a model (the red team, the quantifier, the setup review,
the model review) gets it from shared_paper_prefix_for_model_calls(). The same paper always
gives the same bytes, so a provider can keep the prefix in its cache and give it to the next
call of any stage that uses the same model. utils/models.py puts the prefix first in the
request (`shared_prefix_for_cache`) and marks only the prefix for the cache.
"""
from __future__ import annotations

import re

FIRST_SENTENCE = "The full text of the research paper (LaTeX or Markdown notes) follows."
# The longest paper the prefix takes. A longer paper is refused, never cut.
MAX_PAPER_TOKENS = 256_000
# An upper estimate of the tokens: 18 LaTeX papers gave 1.7 to 2.6 characters a token in the
# Haiku 5.5 tokenizer, so 2 characters a token counts at least the real tokens of most papers.
CHARACTERS_PER_TOKEN = 2
# Lines that tell LaTeX how to typeset the paper, and say nothing a reader needs.
TYPESETTING_COMMANDS = re.compile(r"\\(usepackage|documentclass|bibliography\w*)\{[^}]*\}")


class PaperTooLongForModelContext(ValueError):
    """The paper has more tokens than MAX_PAPER_TOKENS, by the estimate of CHARACTERS_PER_TOKEN."""


def estimated_paper_tokens(paper_text: str) -> int:
    """The number of tokens of `paper_text`: its characters divided by CHARACTERS_PER_TOKEN."""
    return len(paper_text) // CHARACTERS_PER_TOKEN


def shared_paper_prefix_for_model_calls(paper_latex_text: str) -> str:
    r"""FIRST_SENTENCE, a blank line, and the paper without its typesetting commands.

    Args:
        paper_latex_text: all readable text of the paper, as utils/extract.py gives it
            in Paper.text (no comments; \input files inlined).

    Raises:
        ValueError: the paper text is empty.
        PaperTooLongForModelContext: the text without its typesetting commands has more
            than MAX_PAPER_TOKENS estimated tokens.
    """
    if not paper_latex_text.strip():
        raise ValueError("the paper text is empty, so there is no paper to send to a model")
    paper = TYPESETTING_COMMANDS.sub(" ", paper_latex_text)
    paper = re.sub(r"\n{3,}", "\n\n", paper)
    tokens = estimated_paper_tokens(paper)
    if tokens > MAX_PAPER_TOKENS:
        raise PaperTooLongForModelContext(
            f"the paper has about {tokens:,} tokens ({len(paper):,} characters at "
            f"{CHARACTERS_PER_TOKEN} characters a token), more than the limit of "
            f"{MAX_PAPER_TOKENS:,}; the Verifier sends the whole paper or nothing")
    return f"{FIRST_SENTENCE}\n\n{paper}"
