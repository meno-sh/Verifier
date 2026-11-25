"""Model names: the known names, and the one rule that says when two names are the same.

The known names come from the Hugging Face Hub and OpenRouter, in model_names.txt
(refresh_model_names.py rebuilds it). Two names are the same model when their keys are
equal: case, the separators "-", "_" and " ", and an organisation prefix do not count.
"""
import os
import re

MODEL_NAMES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_names.txt")
# An organisation prefix, as in "Qwen/Qwen3-8B": one path part before the name.
ORGANISATION_PREFIX = re.compile(r"[\w.-]+/(?=[\w.:-]+$)")


def model_name_key(name: str) -> str:
    """A model name in the form names are compared in.

    The name is trimmed and in lower case, loses an organisation prefix, and has its parts
    joined by "-": "Qwen/Qwen3_8B" and "qwen3-8b" have the same key.
    """
    key = ORGANISATION_PREFIX.sub("", name.strip().lower())
    return re.sub(r"[-_ ]+", "-", key)


def same_model(a: str, b: str) -> bool:
    """True when two model names name the same model (see model_name_key)."""
    return model_name_key(a) == model_name_key(b)


def _load_known_names() -> dict[str, str]:
    """Every known model name, by its key."""
    with open(MODEL_NAMES_FILE, encoding="utf-8") as fh:
        names = [line.strip() for line in fh if line.strip() and not line.startswith("#")]
    return {model_name_key(n): n for n in names}


KNOWN_NAMES = _load_known_names()
