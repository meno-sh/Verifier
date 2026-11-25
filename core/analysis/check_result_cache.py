"""The stored results of the claim checks and the methods, by all that each one reads.

A claim check gives its stored result when its code, claims, options, gate results and
files are the same as before (CheckResultCache.fingerprint). A method gives its stored
Computed when its file, environment, source and data files are the same
(CheckResultCache.recompute_fingerprint). A check that reads the whole register is never
stored. The results are in `.verifier/cache/check_results/`, outside git, one JSON file
each; the cache keeps the results that one of the last 3 runs used.
"""
from __future__ import annotations

import glob
import inspect
import math
import os
import shutil
import time
from pathlib import Path

from core.analysis import findings_json
from core.methods import computational_methods
from core.registry import checks
from schema import inputs
from schema.errors import VerifierError
from schema.primitives import CheckResult, RecomputeResult
from schema.provenance import ProvenanceResult
from utils import checksums, file_storage, text, version

CACHE_DIR = Path("cache") / "check_results"         # in the state folder
RUNS_KEPT = 3
# the folders of the Verifier whose code can change a result of a check
CODE_FOLDERS = ("schema", "utils", "core", "checks", "review", "diagnosis")


class CheckCacheError(VerifierError):
    """A file of the result cache cannot be read. Delete `.verifier/cache/` to fix it."""


def code_fingerprint() -> str:
    """The SHA-256 of the code of the Verifier that can change a check result.

    It covers each Python file of CODE_FOLDERS and the uv.lock of the Verifier, which
    pins the versions of its libraries.
    """
    paths = sorted(str(p) for folder in CODE_FOLDERS
                   for p in (version.VERIFIER_HOME / folder).rglob("*.py"))
    lock = version.VERIFIER_HOME / "uv.lock"
    return checksums.sha256_of_files((*paths, *([str(lock)] if lock.exists() else [])))




def _holds_a_non_finite_number(value: object) -> bool:
    """Whether a JSON value holds NaN or an infinity, which JSON cannot write."""
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_holds_a_non_finite_number(v) for v in value.values())
    if isinstance(value, list):
        return any(_holds_a_non_finite_number(v) for v in value)
    return False


class CheckResultCache:
    """Stores and gives the results of the claim checks of one state folder."""

    def __init__(self, state_dir: Path, methods_ran_in: str) -> None:
        """The cache of `state_dir`. The code fingerprint is computed once, here.

        `methods_ran_in` says what the methods run in, as the report says it: for a project
        with a uv.lock, its checksum.
        """
        self.folder = state_dir / CACHE_DIR
        self.methods_ran_in = methods_ran_in
        self.results = file_storage.JsonFileFolder(self.folder / "results")
        self.runs = file_storage.JsonFileFolder(self.folder / "runs")
        self.code = code_fingerprint()
        self.used: set[str] = set()
        # the SHA-256 of each file read in this run, by its path, size and change time
        self.checksums: dict[tuple[str, int, int], str] = {}
        self.lock = file_storage.FileLock(self.folder / "cache.lock")
        self.codec = findings_json.FindingsJsonCodec()
        self.fixed = findings_json.FindingsJsonCodec(fixed_form=True)

    def file_state(self, path: str) -> str:
        """What the cache knows of one file: its SHA-256, "folder", or "missing".

        Each file is read once in a run, unless its size or change time changes.
        """
        if os.path.isdir(path):
            return "folder"
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            return "missing"
        key = (path, stat.st_size, stat.st_mtime_ns)
        if key not in self.checksums:
            self.checksums[key] = checksums.sha256_of_file(path)
        return self.checksums[key]

    def python_folder_state(self, path: str) -> dict[str, str]:
        """The state of each Python file in the folder of `path`, which a module can import.

        A method file or a plug-in check can import a helper beside it, so a change to the
        helper must also give a new fingerprint.
        """
        folder = Path(path).parent
        return {str(p): self.file_state(str(p)) for p in sorted(folder.glob("*.py"))}

    def fingerprint(self, check: checks.ClaimCheck, claim: inputs.Claim,
                    context: checks.ClaimContext, provenance_result: ProvenanceResult,
                    recompute_result: RecomputeResult) -> str | None:
        """The SHA-256 of all that `check` reads for `claim`, or None.

        `context` is the restricted context of the check (CheckInputScope). The source
        file of the check is part of it, also for a plug-in check outside the Verifier.
        It is None when what the check reads holds NaN or an infinity, which JSON cannot
        hold; such a result is not stored.
        """
        scope = check.input_scope
        source = inspect.getsourcefile(type(check))
        data = {"code": self.code, "check": check.name,
                "check_files": self.python_folder_state(source) if source else None,
                "claims": self.fixed.encode([context.register[i] for i in scope.claim_ids(claim)
                                             if i in context.register]),
                "options": self.fixed.encode(context.options),
                "dependencies": self.fixed.encode(context.dependencies),
                "gate": self.fixed.encode([provenance_result, recompute_result]),
                "full_paper_text": (
                    None if not scope.reads_full_paper_text or context.full_paper_text is None
                    else checksums.sha256_of_json(context.full_paper_text)),
                "files": {p: self.file_state(p)
                          for p in scope.files_read(claim, context.options)}}
        return self._checksum(data)

    @staticmethod
    def _checksum(data: object) -> str | None:
        """The SHA-256 of a fingerprint, or None when it holds NaN or an infinity."""
        return None if _holds_a_non_finite_number(data) else checksums.sha256_of_json(data)

    def stored_result(self, fingerprint: str, check: checks.ClaimCheck) -> CheckResult | None:
        """The stored result of this fingerprint, or None when there is none.

        Raises:
            CheckCacheError: the stored file cannot be read.
        """
        name = f"{fingerprint}.json"
        self.used.add(name)
        try:
            data = self.results.read(name)
        except FileNotFoundError:           # none stored, or another run just removed it
            return None
        try:
            return self.codec.decode_check_result(data, check.problem_kinds)
        except ValueError as e:
            raise CheckCacheError(f"{self.folder / 'results' / name} cannot be read ({e}); "
                                  f"delete {self.folder.parent} and run again") from None

    def store_result(self, fingerprint: str, result: CheckResult) -> None:
        """Store a result of FindingsJsonCodec.STORED_RESULTS. A crash is never stored."""
        assert type(result) in findings_json.FindingsJsonCodec.STORED_RESULTS, \
            f"not a result that the cache stores: {result!r}"
        name = f"{fingerprint}.json"
        self.used.add(name)
        self.results.write(name, self.codec.encode(result))

    def finish_run(self) -> None:
        """Record the results this run used, and remove those none of the last 3 runs used.

        The lock makes two runs that end at the same time do this one after the other.
        """
        with self.lock.held():
            self._finish_run()

    def _finish_run(self) -> None:
        """The work of finish_run. The caller holds the lock."""
        # the name sorts by time, and two runs in one second get two files
        self.runs.write(f"{time.time_ns():020d}-{os.getpid()}.json", sorted(self.used))
        names = self.runs.names()
        for old in names[:-RUNS_KEPT]:
            self.runs.remove(old)
        kept: set[str] = set()
        for run in names[-RUNS_KEPT:]:
            used = self.runs.read(run)
            if not isinstance(used, list):
                raise CheckCacheError(f"{self.folder / 'runs' / run} is not a list; "
                                      f"delete {self.folder.parent} and run again")
            kept.update(used)
        for name in self.results.names():
            if name not in kept:
                self.results.remove(name)

    def recompute_fingerprint(self, method: computational_methods.ComputationalMethodEntry,
                              source: inputs.ArtifactSource,
                              provenance_result: ProvenanceResult) -> str | None:
        """The SHA-256 of all that one call of a computational method reads, or None.

        It is None for a method that does not run from a method file, which cannot be
        stored by its file, and when what it reads holds NaN or an infinity.
        """
        function = method.function
        if not isinstance(function, computational_methods.SubprocessComputationalMethod):
            return None
        data = {"kind": "recompute", "code": self.code, "methods_ran_in": self.methods_ran_in,
                "method": method.name, "method_files": self.python_folder_state(function.file),
                "python": function.python, "source": self.fixed.encode(source),
                "provenance": self.fixed.encode(provenance_result),
                # the content of every matched file: a pin can cover only some of them
                "files": {p: self.file_state(p) for p in sorted(glob.glob(source.path))}}
        return self._checksum(data)

    def stored_computed(self, fingerprint: str) -> computational_methods.Computed | None:
        """What the method gave for this fingerprint, or None when nothing is stored.

        Raises:
            CheckCacheError: the stored file cannot be read.
        """
        name = f"{fingerprint}.json"
        self.used.add(name)
        try:
            data = self.results.read(name)
        except FileNotFoundError:           # none stored, or another run just removed it
            return None
        try:
            if not isinstance(data, dict) or data.get("type") != "Computed":
                raise ValueError(f"not a stored Computed: {text.first_part(repr(data), 200)}")
            interval = data["interval"]
            return computational_methods.Computed(
                value=data["value"], n=data["n"],
                interval=None if interval is None else (interval[0], interval[1]),
                setup=data["setup"])
        except (KeyError, TypeError, ValueError) as e:
            raise CheckCacheError(f"{self.folder / 'results' / name} cannot be read ({e}); "
                                  f"delete {self.folder.parent} and run again") from None

    def store_computed(self, fingerprint: str, computed: computational_methods.Computed) -> None:
        """Store what a method gave. A NaN, which means no data, is not stored."""
        assert not math.isnan(computed.value), "a NaN is never stored"
        name = f"{fingerprint}.json"
        self.used.add(name)
        self.results.write(name, {
            "type": "Computed", "value": computed.value, "n": computed.n,
            "interval": None if computed.interval is None else list(computed.interval),
            "setup": None if computed.setup is None else dict(computed.setup)})

    @staticmethod
    def clear(state_dir: Path) -> None:
        """Remove every stored result of the state folder `state_dir`."""
        folder = state_dir / CACHE_DIR
        if folder.exists():
            shutil.rmtree(folder)
