"""The checksums ("pins") that the claim register keeps for the files of artifact claims.

A pin (`sha256`) is set when a claim is added; a later change to the file fails provenance.
A proposed checksum (`proposed_sha256`) waits for a person to confirm it: a file that
matches it passes provenance as ProvisionallyIntact, which counts as a caveat.

ArtifactPins reads the pins, for provenance and the review extension. ClaimRegisterPinEditor
proposes, confirms and withdraws them in the claim register file. Both find a pinned file
with one rule.
"""
from __future__ import annotations

import glob
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from schema import inputs, provenance
from schema.claim import claim_register_parser
from schema.primitives import record
from utils import checksums, claim_files, file_storage

# One entry of the claim register, as json.load gives it.
type RawJsonObject = dict[str, Any]


@record
class PinnedFile:
    """One file that an artifact source matches, and the checksum the register pins for it."""
    name: str                   # the key of the pin (claim_files.pin_key_of_file)
    path: str                   # the path the glob of the source matched
    pin: str                    # the pinned SHA-256


class ArtifactPins:
    """Compares the files of an artifact source with the checksums the register pins."""

    @staticmethod
    def pinned_files(source: inputs.ArtifactSource) -> dict[str, PinnedFile]:
        """Each pinned file that the path of the source matches, by the key of its pin."""
        return ArtifactPins.pinned_files_at(source.path, source.sha256)

    @staticmethod
    def pinned_files_at(path: str, sha256: str | Mapping[str, str] | None
                        ) -> dict[str, PinnedFile]:
        """Each pinned file that `path` matches, by the key of its pin.

        `sha256` is either one checksum for each pin key (claim_files.pin_key_of_file), or
        one checksum, which pins the first matching file in sorted order. A folder or a
        broken link is never a pinned file, also when its key is in the pins (commit
        16146f8 refused them in `pin`). The path and the pins are all it reads, so an
        entry with no method yet has pins too.
        """
        files = [p for p in sorted(glob.glob(path)) if os.path.isfile(p)]
        if isinstance(sha256, str):
            return {claim_files.pin_key_of_file(path, p):
                    PinnedFile(name=claim_files.pin_key_of_file(path, p), path=p, pin=sha256)
                    for p in files[:1]}
        pins = sha256 or {}
        keyed = {claim_files.pin_key_of_file(path, p): p for p in files}
        return {key: PinnedFile(name=key, path=p, pin=pins[key])
                for key, p in keyed.items() if key in pins}

    @staticmethod
    def pin_of_file(path: str, sha256: str | Mapping[str, str] | None,
                    file: Path) -> PinnedFile | None:
        """The pinned file `file`, if `path` pins it; else None."""
        target = os.path.normpath(file)
        return next((f for f in ArtifactPins.pinned_files_at(path, sha256).values()
                     if os.path.normpath(f.path) == target), None)

    @staticmethod
    def claims_pinning_file(claims: Iterable[inputs.Claim], root: Path,
                            file: str) -> list[tuple[inputs.Claim, PinnedFile]]:
        """Each claim that pins `file`, relative to `root`, with its pinned file."""
        out = []
        for c in claims:
            if isinstance(c.source, inputs.ArtifactSource):
                found = ArtifactPins.pin_of_file(c.source.path, c.source.sha256, root / file)
                if found is not None:
                    out.append((c, found))
        return out

    @staticmethod
    def current_checksum(root: Path, file: str) -> str:
        """The checksum that `file`, relative to `root`, has now."""
        return checksums.sha256_of_file(str(root / file))

    def compare_files_with_pins(self, source: inputs.ArtifactSource
                                ) -> provenance.Changed | provenance.Missing | tuple[str, ...]:
        """Compare each file of the source with the checksum pinned when the claim was added.

        Returns Missing for the first pinned file that the path no longer matches as a file,
        Changed for the first file that matches neither its pin nor its proposed checksum,
        else the names of the files that match only their proposed checksum.

        Honest limit: this shows nothing changed since the checksum was taken. It cannot
        show the original was produced honestly, so it defends against later editing,
        not against fabrication at the source.
        """
        pinned_files = self.pinned_files(source)
        if isinstance(source.sha256, str):
            gone = [] if pinned_files else [source.path]
        else:
            folder = claim_files.folder_before_first_wildcard(source.path)
            gone = [os.path.join(folder, key) for key in sorted(source.sha256 or {})
                    if key not in pinned_files]
        if gone:
            return provenance.Missing(path=gone[0])
        proposed = source.proposed_sha256 or {}
        provisional = []
        for pinned in pinned_files.values():
            got = checksums.sha256_of_file(pinned.path)
            if got == pinned.pin:
                continue
            if got != proposed.get(pinned.name):
                return provenance.Changed(file=pinned.name, expected_sha256=pinned.pin,
                                          actual_sha256=got)
            provisional.append(pinned.name)
        return tuple(provisional)


class ClaimRegisterPinEditor:
    """Proposes, confirms and withdraws a new checksum of a file on each claim that pins it.

    Each method reads the claim register file into a ClaimRegister, changes copies of its
    entries, parses them again into a new ClaimRegister, and writes that. It returns the
    ids of the claims it changed. Each one gives the same result when it runs a second
    time, and it writes the file only when a claim changes. So a review can apply its
    change again after a failure. Each holds the lock of the claim files
    (claim_files.lock), as the vcheck commands do.

    The entries are changed as written, not as parsed, so an entry with no method yet
    gets the new checksum too. The parser needs the check registry of the project, so a
    broken method file or plug-in check stops a pin change, as it stops a run.
    """

    def __init__(self, register: Path, root: Path,
                 parser: claim_register_parser.ClaimRegisterParser) -> None:
        """Edit the claim register `register`, whose paths are relative to `root`."""
        self.register = register
        self.root = root
        self.parser = parser

    def propose_checksum(self, file: str, checksum: str) -> list[str]:
        """Record `checksum` as the proposed checksum of `file` on every claim pinning it."""
        with claim_files.lock(self.register.parent).held():
            return self._propose_checksum(file, checksum)

    def _propose_checksum(self, file: str, checksum: str) -> list[str]:
        """The change itself. The caller holds the lock of the claim files."""
        entries = self._read().entry_copies()
        changed = []
        for entry, pinned in self._entries_pinning_file(entries, file):
            source = entry["source"]
            name = pinned.name
            already = ((source.get("proposed_sha256") or {}).get(name) == checksum
                       or pinned.pin == checksum)
            if already:
                continue
            source.setdefault("proposed_sha256", {})[name] = checksum
            changed.append(entry["id"])
        self._write(entries, changed)
        return changed

    def confirm_proposed_checksum(self, file: str, checksum: str) -> list[str]:
        """Move a proposed checksum into the pin, on every claim that proposed it."""
        with claim_files.lock(self.register.parent).held():
            return self._confirm_proposed_checksum(file, checksum)

    def _confirm_proposed_checksum(self, file: str, checksum: str) -> list[str]:
        """The change itself. The caller holds the lock of the claim files."""
        entries = self._read().entry_copies()
        changed = []
        for entry, pinned in self._entries_pinning_file(entries, file):
            source = entry["source"]
            name = pinned.name
            if (source.get("proposed_sha256") or {}).get(name) != checksum:
                continue
            if isinstance(source["sha256"], str):
                source["sha256"] = checksum
            else:
                source["sha256"][name] = checksum
            self._drop_proposal(source, name)
            changed.append(entry["id"])
        self._write(entries, changed)
        return changed

    def withdraw_proposed_checksum(self, file: str, checksum: str) -> list[str]:
        """Remove a proposed checksum from every claim that has it."""
        with claim_files.lock(self.register.parent).held():
            return self._withdraw_proposed_checksum(file, checksum)

    def _withdraw_proposed_checksum(self, file: str, checksum: str) -> list[str]:
        """The change itself. The caller holds the lock of the claim files."""
        entries = self._read().entry_copies()
        changed = []
        for entry, pinned in self._entries_pinning_file(entries, file):
            if (entry["source"].get("proposed_sha256") or {}).get(pinned.name) == checksum:
                self._drop_proposal(entry["source"], pinned.name)
                changed.append(entry["id"])
        self._write(entries, changed)
        return changed

    def _read(self) -> inputs.ClaimRegister:
        """The claim register, each decimal kept as it was printed (0.120 stays 0.120)."""
        return self.parser.parse_file(str(self.register), str(self.root))

    def _write(self, entries: list[object], changed: list[str]) -> None:
        """Write the register of the changed `entries`, and record the new pins of `changed`.

        Nothing is written when no claim changed.
        """
        if not changed:
            return
        register = self.parser.parse_json({"claims": entries}, str(self.root))
        file_storage.AtomicallyWrittenFile(self.register).write_text(
            claim_files.dump(register.to_json()))
        claim_files.record_pins(self.register.parent,
                                [e for e in entries
                                 if isinstance(e, dict) and e.get("id") in changed])

    def _entries_pinning_file(self, entries: list[object], file: str
                              ) -> list[tuple[RawJsonObject, PinnedFile]]:
        """The artifact entries, as written, that pin `file`, each with its pinned file."""
        out = []
        for entry in entries:
            if (isinstance(entry, dict) and isinstance(entry.get("source"), dict)
                    and entry["source"].get("kind") == inputs.ArtifactSource.KIND):
                source = entry["source"]
                pinned = ArtifactPins.pin_of_file(str(self.root / source["path"]),
                                                  source.get("sha256"), self.root / file)
                if pinned is not None:
                    out.append((entry, pinned))
        return out

    @staticmethod
    def _drop_proposal(source: RawJsonObject, pin_key: str) -> None:
        """Remove one file's proposal, and the key itself once it is empty."""
        proposals = source.get("proposed_sha256") or {}
        proposals.pop(pin_key, None)
        if not proposals:
            source.pop("proposed_sha256", None)
