"""Reading the claim register file into a ClaimRegister, one entry at a time.

Every entry is checked once, when it is read. An entry that is not valid becomes an
UnreadableClaim, which the checks do not see. An unknown key is an error, so a misspelt
key cannot switch a check off.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
from collections.abc import Collection, Mapping
from typing import ClassVar

from schema.claim.claim_entry_reader import ClaimEntryReader
from schema.claim.claim_links import ClaimLinks
from schema.inputs import (
    NUMBER,
    ArtifactSource,
    Claim,
    ClaimRegister,
    ClaimRegisterError,
    ClaimsSource,
    DocumentSource,
    FieldType,
    PrintedFloat,
    Source,
    UnreadableClaim,
)
from schema.primitives import MethodName, TraceReason, read_only


def record_field_names(cls: type) -> frozenset[str]:
    """The field names of a record, which are also its keys in the claim register."""
    return frozenset(f.name for f in dataclasses.fields(cls))


class ClaimRegisterParser:
    """Turns a claim register file, or its loaded JSON, into a ClaimRegister.

    The keys allowed at each level of the file come from the records of
    schema/inputs.py. The register holds only its claims. A claim also accepts the keys
    its checks declared, and the bookkeeping keys below. Any other key is an error.
    """
    REGISTER_KEYS: ClassVar[frozenset[str]] = frozenset({"claims"})
    # What utils/claim_files.py writes to keep the claim files in step across rounds: the
    # commit that added and last changed the entry, and the quotes of its equivalence
    # class. No check reads them, so a Claim does not hold them.
    BOOKKEEPING_KEYS: ClassVar[frozenset[str]] = frozenset({"created", "last_updated", "quotes"})
    CLAIM_KEYS: ClassVar[frozenset[str]] = ((record_field_names(Claim) - {"fields"})
                                            | BOOKKEEPING_KEYS)
    ARTIFACT_KEYS: ClassVar[frozenset[str]] = record_field_names(ArtifactSource) | {"kind"}
    DOCUMENT_KEYS: ClassVar[frozenset[str]] = record_field_names(DocumentSource) | {"kind"}
    CLAIMS_KEYS: ClassVar[frozenset[str]] = record_field_names(ClaimsSource) | {"kind"}

    def __init__(self, method_names: Collection[MethodName],
                 field_types: Mapping[str, FieldType]) -> None:
        """Build a parser for the methods and the claim keys of one check registry.

        Args:
            method_names: the computational methods a claim may name.
            field_types: the keys the registered claim checks read, each with its type.
        """
        self.method_names = method_names
        self.field_types = field_types

    def parse_file(self, path: str, base_folder: str | None = None) -> ClaimRegister:
        """Read and parse a claim register file, keeping every decimal as it was written.

        Args:
            path: the claim register file.
            base_folder: the folder relative paths are read from; the folder of the
                register if None.

        Raises:
            ClaimRegisterError: when the file cannot be read, is not valid JSON, or is
                not a claim register.
        """
        raw = self.read_raw_file(path)
        folder = base_folder if base_folder is not None else os.path.dirname(
            os.path.abspath(path))
        return self.parse_json(raw, folder)

    @staticmethod
    def read_raw_file(path: str) -> object:
        """The JSON of a claim register file, each decimal kept as written (0.120 stays 0.120).

        Every reader of the file reads it here.

        Raises:
            ClaimRegisterError: when the file cannot be read or is not valid JSON.
        """
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh, parse_float=PrintedFloat)
        except (OSError, UnicodeDecodeError) as e:
            raise ClaimRegisterError(f"cannot read the claim register: {e}") from None
        except json.JSONDecodeError as e:
            raise ClaimRegisterError(f"the claim register is not valid JSON: {e}") from None

    def parse_json(self, raw: object, base_folder: str) -> ClaimRegister:
        """Parse a loaded claim register JSON, one entry at a time.

        Args:
            raw: the claim register as json.load gives it.
            base_folder: the folder every relative path is resolved against.

        Returns:
            The claims that parsed, in file order, and every entry that did not. A
            repeated id keeps the first entry and marks the later ones unreadable. The
            register also keeps a copy of every entry as written.

        Raises:
            ClaimRegisterError: when the file is not an object with a `claims` list, or
                has an unknown top-level key.
        """
        if not isinstance(raw, dict):
            raise ClaimRegisterError(
                f"the claim register must be a JSON object, got {type(raw).__name__}")
        ClaimEntryReader(raw, "the claim register", base_folder).refuse_unknown_keys(
            self.REGISTER_KEYS)
        entries = raw.get("claims")
        if not isinstance(entries, list):
            raise ClaimRegisterError("the claim register must have a `claims` list")
        claims: list[Claim] = []
        unreadable: list[UnreadableClaim] = []
        seen: set[str] = set()
        for n, entry in enumerate(entries, start=1):
            given_id = entry.get("id") if isinstance(entry, dict) else None
            cid = given_id if isinstance(given_id, str) else None
            try:
                claim = self.parse_claim_entry(entry, base_folder)
            except ClaimRegisterError as e:
                unreadable.append(UnreadableClaim(entry=n, id=cid, reason=str(e)))
                continue
            if claim.id in seen:
                unreadable.append(UnreadableClaim(
                    entry=n, id=claim.id, reason=f"claim {claim.id!r} appears more than once"))
                continue
            seen.add(claim.id)
            claims.append(claim)
        kept, unreadable_with_links = ClaimLinks().keep_claims_with_valid_links(
            claims, unreadable, entries)
        return ClaimRegister(claims=kept, unreadable=unreadable_with_links, base=base_folder,
                             entries=tuple(copy.deepcopy(entries)))

    def parse_claim_entry(self, raw: object, base_folder: str) -> Claim:
        """Parse one claim register entry, with every path resolved against `base_folder`.

        Raises:
            ClaimRegisterError: naming the claim and the key that cannot be used.
        """
        if not isinstance(raw, dict):
            raise ClaimRegisterError(
                f"every claim must be a JSON object, got {type(raw).__name__}")
        cid = ClaimEntryReader(raw, "a claim", base_folder).required_value("id", str)
        entry = ClaimEntryReader(raw, f"claim {cid!r}", base_folder)
        entry.refuse_unknown_keys(self.CLAIM_KEYS | self.field_types.keys())
        source = self._parse_source(raw.get("source"), entry.label, base_folder)
        numbered = isinstance(source, ArtifactSource | DocumentSource)
        statement = entry.optional_value("statement", str)
        if statement is not None and not statement.strip():
            raise ClaimRegisterError(f"{entry.label}: `statement` must not be empty")
        if isinstance(source, ClaimsSource) and statement is None:
            raise ClaimRegisterError(f"{entry.label}: `statement` is required for a claim "
                                     "inferred from other claims")
        value = (entry.required_value("value", NUMBER) if numbered
                 else entry.optional_value("value", NUMBER))
        return Claim(
            id=cid,
            value=value,
            statement=statement,
            source=source,
            tol=entry.tolerance(),
            setup=entry.scalar_mapping("setup"),
            origin=entry.optional_path_relative_to_register("origin"),
            superseded_by=entry.optional_value("superseded_by", str),
            fields=read_only({key: found for key, field_type in self.field_types.items()
                              if (found := entry.field_declared_by_a_check(key, field_type))
                              is not None}),
        )

    def _parse_source(self, raw: object, claim_label: str, base_folder: str) -> Source | None:
        """Parse the `source` of a claim: an artifact, a document, or other claims."""
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ClaimRegisterError(
                f"{claim_label}: `source` must be an object, got {type(raw).__name__}")
        source = ClaimEntryReader(raw, f"{claim_label} source", base_folder)
        kind = source.required_value("kind", str)
        if kind == ArtifactSource.KIND:
            source.refuse_unknown_keys(self.ARTIFACT_KEYS)
            return ArtifactSource(
                path=source.path_relative_to_register("path"),
                method=source.method_name(self.method_names),
                sha256=source.checksums("sha256"),
                proposed_sha256=source.proposed_checksums(),
            )
        if kind == DocumentSource.KIND:
            source.refuse_unknown_keys(self.DOCUMENT_KEYS)
            return DocumentSource(
                path=source.path_relative_to_register("path"),
                must_contain=source.required_value("must_contain", str),
                why=source.enum_value("why", TraceReason),
            )
        if kind == ClaimsSource.KIND:
            source.refuse_unknown_keys(self.CLAIMS_KEYS)
            ids = source.text_list("claims")
            if not ids:
                raise ClaimRegisterError(f"{source.label}: `claims` must name at least one claim")
            if len(set(ids)) != len(ids):
                raise ClaimRegisterError(f"{source.label}: `claims` names a claim more than once")
            return ClaimsSource(claims=ids)
        raise ClaimRegisterError(f"{source.label}: `kind` must be '{ArtifactSource.KIND}', "
                                 f"'{DocumentSource.KIND}' or '{ClaimsSource.KIND}', got {kind!r}")
