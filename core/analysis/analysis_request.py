"""What one analysis reads, and the documents it reads, loaded once."""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping

from schema.primitives import read_only, record
from utils import extract


@record
class AnalysisRequest:
    """What one analysis reads.

    `verifier static-check` and the real-time verifier both give one, so they run the same
    analysis.
    """
    # the documents to run the paper checks on, by the path that names each in the
    # findings; each is its files, LaTeX or Markdown, read as one paper in this order
    documents: Mapping[str, tuple[str, ...]] = dataclasses.field(
        default_factory=lambda: read_only({}))
    claim_register_path: str | None = None          # ignored with `paper_only`
    # run options for every paper check group and claim check, such as
    # {"want_model_review": True}; each reads its own
    run_options: Mapping[str, object] = dataclasses.field(
        default_factory=lambda: read_only({}))
    paper_only: bool = False                        # check the documents only
    switches_on: frozenset[str] | None = None       # every switch is on when None
    # the folder the paths of the register are relative to; its own folder when None
    claim_register_base_folder: str | None = None
    # the ids of the claims that get claim checks; every claim when None; the others
    # still go through the gate
    claims_with_claim_checks: frozenset[str] | None = None
    # the full text of the paper, for the claim checks that read the whole paper; when None,
    # the text of the one document, if there is exactly one (watched_project.full_paper_text)
    full_paper_text: str | None = None
    # why no claim is checked, for the report; only with `paper_only`
    claim_checks_not_run_because: str | None = None

    def __post_init__(self) -> None:
        """Refuse documents that are not a mapping of each document to its files.

        Raises:
            TypeError: `documents` is not a mapping, or the files of a document are one
                string, not a sequence of paths.
            ValueError: a reason why no claim is checked is empty, or comes without
                `paper_only`.
        """
        if self.claim_checks_not_run_because is not None:
            if not self.claim_checks_not_run_because.strip():
                raise ValueError("the reason why no claim is checked is empty")
            if not self.paper_only:
                raise ValueError("a reason why no claim is checked needs paper_only")
        if not isinstance(self.documents, Mapping):
            raise TypeError("an analysis takes a mapping from the path of each document "
                            "to its files")
        if any(isinstance(files, str) for files in self.documents.values()):
            raise TypeError("the files of a document are a sequence of paths, not one string")
        object.__setattr__(self, "documents", read_only(
            {key: tuple(files) for key, files in self.documents.items()}))
        object.__setattr__(self, "run_options", read_only(self.run_options))

    @property
    def uses_claim_register(self) -> bool:
        """True when the analysis reads a claim register."""
        return self.claim_register_path is not None and not self.paper_only

    def all_document_paths(self) -> tuple[str, ...]:
        """The files of every document, in order."""
        return tuple(path for files in self.documents.values() for path in files)


@record
class LoadedDocuments:
    """The documents of one analysis, each read once, by the path that names it."""
    papers: Mapping[str, extract.Paper]

    @classmethod
    def load(cls, documents: Mapping[str, tuple[str, ...]]) -> LoadedDocuments:
        """Read each document from its files."""
        return cls(papers=read_only({key: extract.load(files)
                                     for key, files in documents.items()}))

    def labels_of_other_documents(self, key: str) -> frozenset[str]:
        """Every label and anchor of the documents other than `key`.

        A supplement often refers to a table of the main paper, so such a reference is not
        dangling.
        """
        return frozenset().union(*(p.targets for k, p in self.papers.items() if k != key))
