"""One watched project: its folder, its state folder, and what a run of it needs.

A run of a watched project is the same analysis as the static verifier (core/analysis/),
with three differences: each tracked document gets the paper checks on its own, the claim
register lives in the state folder while its paths are relative to the project folder,
and the registry holds the built-in checks, the methods and plug-in checks the agent
saved in the state folder, and whatever the loaded extensions add. The run options are those of the
registry, at their watched values, changed by the options.json of the state folder.
"""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

from core.analysis import analysis_request, check_result_cache, findings_analyzer
from core.claim_steps import (
    claim_reordering,
    duplicates,
    full_paper_linker,
    labeller,
)
from core.methods import computational_methods, method_writer
from core.project import agent_session
from core.registry import extensions, plugin_checks, run_options

# by its name, because the method WatchedProject.check_registry hides the module in the class
from core.registry.checks import CheckRegistry
from core.report_model import report_header
from schema import findings, inputs
from schema.claim import claim_register_parser
from schema.errors import VerifierError
from utils import checksums, claim_files, extract, file_storage, structure

# The errors of a project whose inputs are wrong: a method file, a plug-in check, the claim
# register or a run option. A person fixes the project; no run can go on before that.
PROJECT_INPUT_ERRORS = (computational_methods.MethodFileError, plugin_checks.PluginCheckError,
                        inputs.ClaimRegisterError, run_options.OptionsError)


class MethodEnvironmentError(VerifierError):
    """The project has a uv.lock, and the environment it pins was not built."""


class WatchedProject:
    """One project folder and its state folder, and what a run of the project needs."""
    # The claim register, which the claim files of the auditing agent build.
    REGISTER_FILE: ClassVar[str] = claim_files.REGISTER
    # The run options of a watched project, in its state folder: {key: value}. Every
    # command of the project reads it, so the daemon, the commands of the agent and the
    # commands of a person use the same options, whatever their environment holds.
    OPTIONS_FILE: ClassVar[str] = "options.json"
    # The locked environment of the project, which computational methods run in
    # (runtime/daemon.py builds it).
    METHOD_ENV: ClassVar[Path] = Path("runtime") / "venv"
    # A document is a .tex file that starts a LaTeX document: \documentclass outside a comment.
    DOCUMENT_START: ClassVar[re.Pattern[str]] = re.compile(r"^[^%\n]*\\documentclass\b",
                                                           re.MULTILINE)

    def __init__(self, root: Path, state_dir: Path,
                 extension_set: extensions.ExtensionSet) -> None:
        """Hold the project folder `root`, its state folder, and the extensions to load."""
        self.root = root
        self.state_dir = state_dir
        self.extension_set = extension_set

    def claim_with_id(self, check_registry: CheckRegistry,
                      claim_id: str) -> tuple[inputs.Claim, dict[str, inputs.Claim]]:
        """One readable claim by id, and every readable claim by id.

        Raises:
            ClaimRegisterError: no readable claim has this id.
        """
        by_id = {c.id: c for c in self.claim_register(check_registry).claims}
        if claim_id not in by_id:
            raise inputs.ClaimRegisterError(f"no readable claim with id {claim_id!r}")
        return by_id[claim_id], by_id

    @classmethod
    def load(cls, root: Path, state_dir: Path) -> WatchedProject:
        """The watched project in `root`, with the extensions saved in `state_dir`."""
        return cls(root, state_dir, extensions.ExtensionSet.of_project(state_dir))

    def check_registry(self) -> CheckRegistry:
        """The built-in checks, the methods and plug-in checks of the state folder, and more.

        The loaded extensions add their checks last.
        """
        python = self.state_dir / self.METHOD_ENV / "bin" / "python"
        reg = plugin_checks.PluginCheckFolderLoader().registry_with_plugin_checks(
            extensions.builtin_check_registry(), self.state_dir / plugin_checks.CHECKS_DIR)
        reg = computational_methods.MethodFolderLoader(
            str(python) if python.exists() else sys.executable).registry_with_methods(
            reg, self.state_dir / computational_methods.METHODS_DIR)
        # the settings of the agent's session, its claim steps done by code, and the model
        # calls, which only a watched project has
        for module in (agent_session, duplicates, labeller, claim_reordering,
                       full_paper_linker, method_writer):
            reg.add_run_options_of_module(module)
        return self.extension_set.add_checks_to_registry(reg, self.root, self.state_dir)

    def saved_run_option_values(self, check_registry: CheckRegistry
                                ) -> dict[str, object]:
        """The values in options.json, by key; none when the file does not exist.

        The values are read with the catalog of the final registry, so the options of the
        plug-in checks, the extensions, the duplicate check and the method writer are known.

        Raises:
            OptionsError: the file is not a JSON object, names no declared option, or
                holds a value that is not of the kind of the option or not one of its
                choices.
        """
        path = self.state_dir / self.OPTIONS_FILE
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text())
        except ValueError as e:
            raise run_options.OptionsError(f"{path} is not valid JSON: {e}") from None
        if not isinstance(raw, dict):
            raise run_options.OptionsError(f"{path} must be a JSON object of run options")
        catalog = check_registry.run_option_catalog
        for key, value in raw.items():
            if key not in catalog.options:
                raise run_options.OptionsError(f"{path}: {key!r} names no run option; "
                                               f"known are {sorted(catalog.options)}")
            try:
                catalog.options[key].value_in_options({key: value})
            except (TypeError, ValueError) as e:
                # a wrong type, or a value that is not one of the choices of the option
                raise run_options.OptionsError(f"{path}: {e}") from None
        return raw

    def save_run_option_values(self, values: Mapping[str, object]) -> None:
        """Replace options.json with `values`, by key. The caller checks them first."""
        file_storage.JsonFile(self.state_dir / self.OPTIONS_FILE, indent=2,
                              sort_keys=True).write(dict(values))

    def run_options(self, check_registry: CheckRegistry,
                    saved_values: Mapping[str, object] | None = None) -> dict[str, object]:
        """The run options of the project, which test-check also uses.

        Each declared option has its watched value, changed by its saved value. The saved
        values are `saved_values` when given, else those in options.json. A relative path
        option, from either, is placed in the state folder.

        Raises:
            OptionsError: options.json is not an options file, names no declared option,
                or holds a value that is not of the kind of the option.
        """
        values = (self.saved_run_option_values(check_registry) if saved_values is None
                  else dict(saved_values))
        return check_registry.run_option_catalog.values_for_watched_project(self.state_dir,
                                                                            values)

    def method_environment_row(self) -> report_header.ReportHeaderRow:
        """The row of the report that says what the computational methods ran in.

        Raises:
            MethodEnvironmentError: the project has a uv.lock, and its environment is not
                built.
        """
        if not (self.root / "uv.lock").exists():
            return report_header.ReportHeaderRow(
                name=report_header.METHODS_RAN_IN,
                text="the standard library only (no uv.lock)")
        if not (self.state_dir / self.METHOD_ENV / "bin" / "python").exists():
            raise MethodEnvironmentError(
                "uv.lock did not build, so the methods cannot use the project's libraries; "
                f"`verifier alerts {self.state_dir.parent}` gives uv's output")
        digest = checksums.sha256_of_file(str(self.root / "uv.lock"))[:12]
        return report_header.ReportHeaderRow(
            name=report_header.METHODS_RAN_IN,
            text=f"the environment uv.lock pins (sha256 {digest})")

    def claim_register(self, check_registry: CheckRegistry) -> inputs.ClaimRegister:
        """The claim register of the project, or an empty one if none is saved yet."""
        path = self.state_dir / self.REGISTER_FILE
        if not path.exists():
            return inputs.ClaimRegister(claims=(), unreadable=(), base=str(self.root))
        return self.claim_register_parser(check_registry).parse_file(str(path), str(self.root))

    @staticmethod
    def claim_register_parser(check_registry: CheckRegistry
                              ) -> claim_register_parser.ClaimRegisterParser:
        """The parser of a claim register, for the methods and the claim keys of the registry."""
        return claim_register_parser.ClaimRegisterParser(check_registry.computational_methods,
                                                         check_registry.claim_field_types())

    def full_paper_text(self) -> str | None:
        """The full text of the project's paper, for the stages that read the whole paper.

        With one document, it is the text of that document, as the paper checks read it.
        With several, it is their texts in path order. With none, as in a project of
        Markdown notes, it is the text of every note file in path order. When the text joins
        two or more files, each part starts with a line that names its file. None when the
        project has no document and no selected note file (no notes are selected before
        `verifier start`).
        """
        documents = self.documents()
        if documents:
            parts = [(rel, extract.load(files).text) for rel, files in sorted(documents.items())]
        elif not (self.state_dir / claim_files.SCOPE).exists():
            return None
        else:
            notes = structure.note_files(str(self.root),
                                         within=claim_files.read_scope(self.state_dir))
            parts = [(rel, self._note_text(self.root / rel)) for rel in notes]
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0][1]
        return "\n\n".join(f"FILE: {rel}\n{text}" for rel, text in parts)

    @staticmethod
    def _note_text(path: Path) -> str:
        """The readable text of a note: through its reader for LaTeX or Markdown, else as is."""
        try:
            extract.paper_format(str(path))
        except extract.UnknownPaperFormat:
            return path.read_text()
        return extract.load([str(path)]).text

    def documents(self) -> dict[str, list[str]]:
        """Each document of the project (see documents_in_folder)."""
        return self.documents_in_folder(self.root)

    @classmethod
    def documents_in_folder(cls, root: Path) -> dict[str, list[str]]:
        r"""Each document in a folder: its path relative to the folder, and its file.

        A document is a tracked .tex file with \documentclass. Each is checked on its own,
        so a main paper and a standalone supplement are never read as one paper. Only
        tracked files count, so `.verifier/` and linked gitignored folders are left out.
        """
        out = {}
        for rel in structure.note_files(str(root)):
            if not rel.endswith(".tex"):
                continue
            text = structure.read_tex(root / rel)
            if text is not None and cls.DOCUMENT_START.search(text):
                out[rel] = [str(root / rel)]
        return out

    def analyse(self, check_registry: CheckRegistry | None = None,
                use_cache: bool = True,
                claim_checks_not_run_because: str | None = None) -> findings.Findings:
        """Run the paper checks on each document, and check every claim of the claim register.

        Args:
            check_registry: the checks to run; those of `check_registry()` when None.
            use_cache: give the stored result of a claim check when nothing it reads
                changed (core/analysis/check_result_cache.py).
            claim_checks_not_run_because: when given, run the paper checks only, and give
                this reason why no claim was checked.

        Raises:
            MethodFileError, PluginCheckError: a method file or a plug-in check cannot be
                loaded.
            ClaimRegisterError: the claim register file is not a claim register.
            MethodEnvironmentError: the project has a uv.lock, and its environment is not
                built.
        """
        methods_ran_in = self.method_environment_row().text
        reg = check_registry or self.check_registry()
        path = self.state_dir / self.REGISTER_FILE
        # a run that checks no claim uses no stored result; with the cache, its empty list
        # of used results would make the cache remove the results that later runs need
        cache = (check_result_cache.CheckResultCache(self.state_dir, methods_ran_in)
                 if use_cache and claim_checks_not_run_because is None else None)
        return findings_analyzer.FindingsAnalyzer(reg, cache).analyse(
            analysis_request.AnalysisRequest(
                documents={key: tuple(files) for key, files in self.documents().items()},
                claim_register_path=str(path) if path.exists() else None,
                claim_register_base_folder=str(self.root), run_options=self.run_options(reg),
                full_paper_text=self.full_paper_text(),
                paper_only=claim_checks_not_run_because is not None,
                claim_checks_not_run_because=claim_checks_not_run_because))
