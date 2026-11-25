"""The worst-case scenario of a flagged claim: the gap that its worst attack shows, in plain text.

The red team writes the worst attack as follow-up experiments with made-up results. The
report does not show these. A model distils them into a statement about a gap in the
claim, with no made-up numbers, in at most MAX_SENTENCES sentences of ASD-STE100. Each
text is saved in a file beside the readings, so a report made again calls no model.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path

from utils import file_storage, models, spending

VERSION = "1"                   # change it when the prompt or the rules of the reply change
MAX_SENTENCES = 4
MAX_WORDS = 25                  # ASD-STE100: the longest sentence of a description
PROMPT = (
    "You write one part of a verification report on a research paper.\n"
    "\n"
    "Claim from the paper:\n"
    "{claim}\n"
    "\n"
    "Verified results that the claim is inferred from:\n"
    "{premises}\n"
    "\n"
    "A red team wrote this worst case against the claim. Nobody did these follow-up "
    "experiments, and the results are made up:\n"
    "{attack}\n"
    "\n"
    "Write the gap in the claim that this worst case shows. Obey these rules:\n"
    "- State the gap as a fact about the claim and its support: what the claim assumes, "
    "or what the evidence does not show. Do not describe an experiment or its result.\n"
    "- Do not give the made-up numbers or details. If you must give a detail, start it "
    'with "for example" or "e.g.".\n'
    f"- Use no more than {MAX_SENTENCES} sentences.\n"
    "- Write in ASD Simplified Technical English (ASD-STE100): one idea for each "
    f"sentence, at most {MAX_WORDS} words for each sentence, the active voice, simple "
    "tenses, and common words.\n"
    "\n"
    "Reply with the text only."
)
# "e.g." and "i.e." do not end a sentence
_SENTENCE_END = re.compile(r"(?<!\be\.g\.)(?<!\bi\.e\.)(?<=[.!?])\s+")


def path_beside(readings: Path) -> Path:
    """The file of saved scenarios, beside the readings file `readings`."""
    return readings.with_name(readings.stem + "_scenarios.json")


def key(claim: str, premises: Sequence[str], hypotheticals: Sequence[tuple[str, str]],
        model: str) -> str:
    """The checksum of everything a scenario depends on."""
    return hashlib.sha256(json.dumps(
        [VERSION, model, claim, list(premises), [list(h) for h in hypotheticals]]).encode()
    ).hexdigest()


def saved(path: Path, name: str) -> str | None:
    """The scenario saved under `name`, or None.

    Raises:
        ValueError: the file is not a scenarios file as save writes it.
    """
    return _read(path).get(name)


def distil(claim: str, premises: Sequence[str], hypotheticals: Sequence[tuple[str, str]],
           model: str) -> str:
    """Ask `model` for the gap that the worst attack shows, and check the reply's form.

    Raises:
        models.ProviderError: the model gave no answer after its retries.
        models.BadReply: two replies broke the rules of read_reply.
    """
    prompt = PROMPT.format(claim=claim, premises="\n".join(f"- {p}" for p in premises),
                           attack="\n".join(f"- {d}: {r}" for d, r in hypotheticals))
    with spending.purpose("scenario"):
        return models.ask_and_read(prompt, model, read_reply)


def read_reply(reply: str) -> str:
    """The reply as one paragraph, when it has 1 to MAX_SENTENCES sentences of MAX_WORDS words.

    Raises:
        models.BadReply: the reply is empty, or has too many sentences or too many words.
    """
    text = " ".join(reply.split())
    sentences = [s for s in _SENTENCE_END.split(text) if s]
    if not sentences:
        raise models.BadReply("the reply is empty")
    if len(sentences) > MAX_SENTENCES:
        raise models.BadReply(f"the reply has {len(sentences)} sentences; use no more than "
                              f"{MAX_SENTENCES}")
    long = [s for s in sentences if len(s.split()) > MAX_WORDS]
    if long:
        raise models.BadReply(f"this sentence has more than {MAX_WORDS} words: {long[0]!r}")
    return text


def save(path: Path, name: str, text: str) -> None:
    """Add one scenario to the file.

    A lock in the system's temporary folder makes two runs add their scenarios one after
    the other.
    """
    def add(raw: object | None) -> object:
        """The value of the file with the new scenario."""
        texts = {} if raw is None else _scenarios_from_json(path, raw)
        texts[name] = text
        return {"scenarios": texts}

    file_storage.JsonFile(
        path, indent=1, sort_keys=True,
        lock=file_storage.FileLock.in_temporary_folder("scenarios", path)).change(add)


def _read(path: Path) -> dict[str, str]:
    """Every saved scenario, by key; none when the file does not exist.

    Raises:
        ValueError: the file is not a scenarios file as save writes it.
    """
    if not path.exists():
        return {}
    return _scenarios_from_json(path, json.loads(path.read_text()))


def _scenarios_from_json(path: Path, data: object) -> dict[str, str]:
    """The scenarios in `data`, the JSON value of the file at `path`.

    Raises:
        ValueError: `data` is not the value of a scenarios file.
    """
    texts = data.get("scenarios") if isinstance(data, dict) else None
    if not isinstance(texts, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v for k, v in texts.items()):
        raise ValueError(f"{path} is not a scenarios file: it needs "
                         '{"scenarios": {key: text}}')
    return texts
