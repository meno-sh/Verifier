"""Find the files whose words fit a query: the text search of the source short list.

The source short list (core/methods/source_index.py) uses it for a claim whose numbers point to
no file. Each document is the words of one file: its path, its column names or JSON
keys, and the first lines of a log file.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, Self

WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    """The lower-case words of `text`; "_", "/", "." and "-" separate words."""
    return WORD.findall(text.lower())


def bm25_weight(count: int, document_frequency: int, documents: int, length: int,
                average_length: float, k1: float, b: float) -> float:
    """The BM25 weight of a word in one document.

    Args:
        count: how often the word is in the document.
        document_frequency: how many documents hold the word.
        documents: how many documents there are.
        length: how many words the document has.
        average_length: the mean length of the documents.
        k1: how fast the weight stops growing with `count`.
        b: how much a long document counts less.
    """
    idf = math.log(1 + (documents - document_frequency + 0.5) / (document_frequency + 0.5))
    norm = k1 * (1 - b + b * length / average_length)
    return idf * count * (k1 + 1) / (count + norm)


class TextSearch(Protocol):
    """A search over the files of a project. Each implementation is made the same way."""

    @classmethod
    def for_project(cls, root: Path, documents: Mapping[str, str]) -> Self:
        """The search over the project in `root`.

        `documents` gives the words of each file, by its path; an implementation can
        read the files itself instead.
        """
        ...

    def search(self, query: str, limit: int) -> list[str]:
        """The paths of the files that fit `query` best, best first, at most `limit`."""
        ...


class Bm25TextSearch:
    """BM25 ranking over one document for each file."""

    K1 = 1.5        # how fast the score of a word stops growing with its count
    B = 0.75        # how much a long document counts less

    @classmethod
    def for_project(cls, root: Path, documents: Mapping[str, str]) -> Self:
        """The search over `documents`. BM25 needs no more of the project."""
        return cls(documents)

    def __init__(self, documents: Mapping[str, str]) -> None:
        """Index the documents, keyed by the path of their file."""
        self.counts = {path: Counter(words(text)) for path, text in documents.items()}
        self.lengths = {path: sum(c.values()) for path, c in self.counts.items()}
        self.average_length = (sum(self.lengths.values()) / len(self.lengths)
                               if self.lengths else 0.0)
        self.document_frequency: Counter[str] = Counter()
        for c in self.counts.values():
            self.document_frequency.update(c.keys())

    def search(self, query: str, limit: int) -> list[str]:
        """The paths of the files that fit `query` best, best first, at most `limit`.

        A file that holds no word of the query is not given.
        """
        n = len(self.counts)
        terms = set(words(query))
        scores: dict[str, float] = {}
        for path, c in self.counts.items():
            score = 0.0
            for t in terms & c.keys():
                score += bm25_weight(c[t], self.document_frequency[t], n, self.lengths[path],
                                     self.average_length, self.K1, self.B)
            if score > 0:
                scores[path] = score
        return sorted(scores, key=lambda p: (-scores[p], p))[:limit]
