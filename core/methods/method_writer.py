"""Write recomputation methods with a separate model call.

That model does not see the claimed values. The auditing agent only describes each quantity,
because the agent has read the notes and seen which claims fail. The writer gets the masked
passages of the notes that the claims come from, and the data fields, never a value. It does
not copy the authors' code, which would copy their bugs.

The needs go to the model in batches. Each batch holds the needs of a few data path
patterns, and only the data files and the passages of those needs, so each call stays small.
"""
from __future__ import annotations

import dataclasses
import decimal
import json
import math
import re
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.methods import computational_methods
from core.methods.computational_methods import Computed
from core.registry import extensions, run_options
from schema import inputs
from utils import extract, file_storage, models, spending, structure
from utils.text import last_part

MODULE_STEM = "written_methods_"          # each module is methods/written_methods_<n>.py
# The run options of `vcheck write-methods`; core/project/watched_project.py adds them to a
# watched run.
MODEL = run_options.RunOption(
    key="method_writer_model", kind=str, flag="--method-writer-model",
    default=models.preset.SONNET,
    help="the model `vcheck write-methods` asks, as a model name (utils/models.py)")
NEEDS_PER_BATCH = run_options.RunOption(
    key="method_writer_needs_per_batch", kind=int, flag="--method-writer-needs-per-batch",
    default=20, help="the most needs `vcheck write-methods` sends in one model call")
PROMPT_TOKENS_PER_BATCH = run_options.RunOption(
    key="method_writer_prompt_tokens_per_batch", kind=int,
    flag="--method-writer-prompt-tokens-per-batch", default=20_000,
    help="the most prompt tokens (characters / 4) of one `vcheck write-methods` call; "
         "a need that is larger alone goes alone")
RUN_OPTIONS = (MODEL, NEEDS_PER_BATCH, PROMPT_TOKENS_PER_BATCH)
CHARACTERS_PER_TOKEN = 4                # the estimate of the prompt tokens

type Ask = Callable[[str], str]

INSTRUCTIONS = """You write recomputation methods for a verifier. Each method recomputes one
quantity a research paper reports, from the authors' data files, so the verifier can compare it
with the paper. You are NOT told the paper's values, on purpose: write each method from your
best understanding of what the quantity means, using the paper's own definitions, never to
reach a number.

You get NEEDS (each: an id, the quantity to compute, the claim it supports, the data path, and
the numbers of its passages), the PASSAGES of the notes the claims come from and the DATA FILES
those paths point to (fields and sample records). In a claim, the value to recompute is written
{{VALUE}}; every other value to recompute, in a claim or a passage, is written *redacted*.

Write ONE Python module. For each need, one function:

    from checks.interface import Computed, method

    @method("<short_name>")
    def <short_name>(path):
        \"\"\"<one line: what it computes>.

        Definition: <the formula, citing the paper's definition when it gives one>.
        Assumptions: <which fields it reads (for example the model's answer and the correct
        answer), which records are kept, how seed files are combined>.
        \"\"\"
        ...
        return Computed(value=<float>, n=<number of records or files used>)

Rules:
  - `path` may be a glob spanning several files (e.g. one per seed); use glob.glob(path).
  - Files are JSON: a list of records, or an object holding `records` or `rows`.
  - Use only the standard library (json, glob, math, statistics). Compute from the raw
    outputs; never import or call the authors' own analysis code, which would copy its bugs.
  - Each method runs on every commit, so it must take seconds.
  - When a definition is ambiguous, choose the reading the paper's text supports and say so
    in Assumptions. Do not choose a reading because it might give a particular number.
  - Two needs computing the same quantity on different files can share one function.

Reply with JSON only:
{"module": "<the Python source>", "methods": [{"need": "<need id>", "method": "<short_name>"}, ...]}
"""

RETRY = """The methods of these needs could not be used. Write a NEW module with a method for each
of these needs only, in the same JSON form. The other methods of your previous module work and
stay. Do not use a method name of the previous module. You are not told any computed values.

PROBLEMS:
%s"""


@dataclasses.dataclass(frozen=True)
class MethodWriterNeed:
    """One number that names a data path but no method: what the method writer must recompute."""
    need_id: str                    # "<claim id>/n1"
    claim_id: str
    quantity: str                   # what to compute, in the agent's words
    path: str                       # the data path, relative to the project root
    value: str | None               # the claimed value as printed, or None; every prompt masks it
    sister_values: tuple[str, ...]  # the values of the claims with the same quote; masked too
    statement: str                  # the claim's text, anonymized (claim_files.claim_text)
    passages: tuple[str, ...]       # the texts of the notes the claim comes from


@dataclasses.dataclass
class MethodWriterReport:
    """What one `write_methods` gave: the method of each need that got one that works, and more."""
    needs: int
    batches: int = 0
    linked: int = 0
    methods: dict[str, str] = dataclasses.field(default_factory=dict)       # by need id
    unresolved: dict[str, str] = dataclasses.field(default_factory=dict)    # need id: problem
    versions: int = 0
    exchanges: list[dict[str, object]] = dataclasses.field(default_factory=list)


def masked_values(needs: list[MethodWriterNeed]) -> set[str]:
    """The values that every prompt hides: those of the needs and of their sister claims.

    Raises:
        ValueError: a value is not a number; the error names its claim.
    """
    values = set()
    for n in needs:
        for v in (n.value, *n.sister_values):
            if v is None:
                continue
            try:
                decimal.Decimal(inputs.PrintedFloat.plain(v))
            except decimal.InvalidOperation:
                raise ValueError(f"claim {n.claim_id}: the value {v!r} is not a number") from None
            values.add(v)
    return values


# a number in scientific notation, such as "1e-05", which extract.CLAIM_NUMBER refuses
SCIENTIFIC_NUMBER = re.compile(r"(?<![\w.])([+\-−]?\d+(?:\.\d+)?[eE][+\-−]?\d+)(?![\w.])")


def _mask(text: str, values: set[str]) -> str:
    """The text with every value to be recomputed hidden (extract.SISTER_VALUE_HIDDEN).

    A number is hidden when its size is the size of a value, in any print of it: "0.120"
    and "0.12", "1,300" and "1300", "0.00001" and "1e-05", "-0.5" and "0.5". `values`
    holds numbers only (masked_values checks them).
    """
    sizes = {abs(decimal.Decimal(inputs.PrintedFloat.plain(v))) for v in values}

    def hide(number: re.Match[str]) -> str:
        printed = number.group(1)
        size = abs(decimal.Decimal(inputs.PrintedFloat.plain(printed)))
        return extract.SISTER_VALUE_HIDDEN if size in sizes else printed
    return extract.CLAIM_NUMBER.sub(hide, SCIENTIFIC_NUMBER.sub(hide, text))


def _parse(text: str) -> tuple[str, dict[str, str]]:
    """The module source and the need -> method mapping from a reply; raises ValueError.

    The reply is read by models.read_json, whose BadReply is a ValueError.
    """
    data = models.read_json(text)
    if (not isinstance(data, dict) or not isinstance(data.get("module"), str)
            or not isinstance(data.get("methods"), list)):
        raise ValueError("the reply needs a `module` string and a `methods` list")
    wrong = [t for t in data["methods"] if not isinstance(t, dict)
             or not isinstance(t.get("need"), str) or not isinstance(t.get("method"), str)]
    if wrong:
        raise ValueError("each entry of `methods` needs a `need` string and a `method` string, "
                         f"not {last_part(repr(wrong[0]), 200)}")
    return data["module"], {t["need"]: t["method"] for t in data["methods"]}


class MethodWriter:
    """Asks a model for recomputation methods, tests them, and keeps the ones that work.

    The methods go to `folder`/methods/, one module for each model reply, and every reply's
    module also goes to `folder`/versions/. A module that does not load is removed. The
    methods of the other modules there stay, and their names are never used again.
    """

    def __init__(self, data_files: dict[str, Any], root: str, folder: Path, ask: Ask,
                 retries: int = 3, needs_per_batch: int = 20,
                 prompt_tokens_per_batch: int = 20_000) -> None:
        """Keep the settings of the calls.

        Args:
            data_files: the data files of the project, as structure.data_files gives them.
            root: the project root, which the data paths are relative to.
            folder: the state folder, which holds methods/ and versions/.
            ask: sends one prompt to the model and gives its reply.
            retries: the calls after the first one for the failed needs of a batch.
            needs_per_batch: the most needs of one batch.
            prompt_tokens_per_batch: the most prompt tokens of one batch.
        """
        assert needs_per_batch >= 1 and prompt_tokens_per_batch >= 1, \
            (needs_per_batch, prompt_tokens_per_batch)
        self.data_files = data_files
        self.root = root
        self.folder = folder
        self.ask = ask
        self.retries = retries
        self.needs_per_batch = needs_per_batch
        self.prompt_tokens_per_batch = prompt_tokens_per_batch
        # the parts of the prompt of each need, by need id, for one call of batches
        self._part_sizes: dict[str, tuple[int, dict[str, int], dict[str, int]]] = {}

    @property
    def methods_folder(self) -> Path:
        """The folder of the method modules."""
        return self.folder / computational_methods.METHODS_DIR

    def prompt_for(self, batch: list[MethodWriterNeed], masked: set[str],
                   taken: frozenset[str] = frozenset()) -> str:
        """The prompt for one batch: its needs, its masked passages and its data files.

        `masked` holds the values of every need, not only those of the batch, because a
        value of another batch can be the same number. The prompt tells the model not to
        use the method names in `taken`.
        """
        passages: list[str] = []
        shown = []
        for n in batch:
            numbers = []
            for text in n.passages:
                if text not in passages:
                    passages.append(text)
                numbers.append(passages.index(text) + 1)
            shown.append({"id": n.need_id, "quantity": _mask(n.quantity, masked),
                          "path": n.path, "claim": _mask(n.statement, masked),
                          "passages": numbers})
        wanted = {structure.path_pattern(n.path) for n in batch}
        groups = {p: g for p, g in self.data_files["groups"].items()
                  if p in wanted or any(structure.path_pattern(e) in wanted
                                        for e in g["examples"])}
        cut = self.data_files["not_listed"]
        more = f"\n({cut} more data file groups were not described)" if cut else ""
        avoid = (f"\n\nMETHOD NAMES ALREADY USED (choose others): {', '.join(sorted(taken))}"
                 if taken else "")
        return (INSTRUCTIONS + avoid
                + "\n\nNEEDS:\n" + json.dumps(shown, indent=1, ensure_ascii=False)
                + "\n\nDATA FILES:\n" + json.dumps(groups, ensure_ascii=False) + more
                + "\n\nPASSAGES:\n"
                + "\n\n".join(f"[{i}] {_mask(t, masked)}" for i, t in enumerate(passages, 1)))

    def batches(self, needs: list[MethodWriterNeed]) -> list[list[MethodWriterNeed]]:
        """The needs in batches, with the needs of one data path pattern in one batch.

        The groups of needs with one pattern go into a batch, whole, while the batch stays
        within `needs_per_batch` and `prompt_tokens_per_batch`. A group that is over a limit
        alone gets batches of its own, and a need that alone is over the token limit goes
        alone.
        """
        masked = masked_values(needs)
        self._part_sizes = {}               # the sizes depend on `masked`
        groups: dict[str, list[MethodWriterNeed]] = {}
        for n in sorted(needs, key=lambda n: n.need_id):
            groups.setdefault(structure.path_pattern(n.path), []).append(n)
        out: list[list[MethodWriterNeed]] = []
        for _pattern, group in sorted(groups.items()):
            if out and self._fits([*out[-1], *group], masked):
                out[-1].extend(group)
                continue
            out.append([])
            for n in group:
                if out[-1] and not self._fits([*out[-1], n], masked):
                    out.append([])
                out[-1].append(n)
        return out

    def _fits(self, batch: list[MethodWriterNeed], masked: set[str]) -> bool:
        """Whether `batch` is within the limit of needs and the limit of prompt tokens.

        The size is the sum of the parts of the prompt (_prompt_parts), with each data
        file group and each passage counted once, so no prompt is built.
        """
        if len(batch) > self.needs_per_batch:
            return False
        groups: dict[str, int] = {}
        passages: dict[str, int] = {}
        size = len(INSTRUCTIONS)
        for n in batch:
            need_size, need_groups, need_passages = self._prompt_parts(n, masked)
            size += need_size
            groups.update(need_groups)
            passages.update(need_passages)
        size += sum(groups.values()) + sum(passages.values())
        return size / CHARACTERS_PER_TOKEN <= self.prompt_tokens_per_batch

    def _prompt_parts(self, need: MethodWriterNeed, masked: set[str]
                      ) -> tuple[int, dict[str, int], dict[str, int]]:
        """The characters that one need adds to a prompt (prompt_for).

        Returns:
            (the size of its entry in NEEDS, the size of each data file group it needs by
            pattern, the size of each of its passages by text).
        """
        key = need.need_id
        if key not in self._part_sizes:
            entry = {"id": need.need_id, "quantity": _mask(need.quantity, masked),
                     "path": need.path, "claim": _mask(need.statement, masked),
                     "passages": list(range(len(need.passages)))}
            pattern = structure.path_pattern(need.path)
            groups = {p: len(json.dumps({p: g}, ensure_ascii=False))
                      for p, g in self.data_files["groups"].items()
                      if p == pattern or any(structure.path_pattern(e) == pattern
                                             for e in g["examples"])}
            self._part_sizes[key] = (len(json.dumps(entry, indent=1, ensure_ascii=False)),
                                     groups,
                                     {t: len(_mask(t, masked)) + 8 for t in need.passages})
        return self._part_sizes[key]

    def taken_method_names(self) -> frozenset[str]:
        """The names of the methods in the method folder.

        Raises:
            computational_methods.MethodFileError: a method file does not load, so its
                names cannot be avoided.
        """
        return frozenset(computational_methods.MethodFolderLoader().registry_with_methods(
            extensions.builtin_check_registry(), self.methods_folder).computational_methods)

    def next_module_name(self) -> str:
        """The name of a new module: written_methods_<one more than the highest number>.py."""
        numbers = [int(p.stem.removeprefix(MODULE_STEM))
                   for p in self.methods_folder.glob(f"{MODULE_STEM}*.py")
                   if p.stem.removeprefix(MODULE_STEM).isdigit()]
        return f"{MODULE_STEM}{max(numbers, default=0) + 1}.py"

    def test_module(self, module: str, mapping: dict[str, str], todo: list[MethodWriterNeed],
                    name: str) -> tuple[list[str], dict[str, str]]:
        """Write one module to the method folder, run its methods, and give its problems.

        No problem text contains a computed value.

        Returns:
            (problems with the whole module, problem by need id).
        """
        self.methods_folder.mkdir(parents=True, exist_ok=True)
        # `vcheck` can load the method folder at any time, so a method file is never half written
        file_storage.AtomicallyWrittenFile(self.methods_folder / name).write_text(module)
        try:
            reg = computational_methods.MethodFolderLoader().registry_with_methods(
                extensions.builtin_check_registry(), self.methods_folder)
        except computational_methods.MethodFileError as e:
            return [f"the module cannot be loaded: {last_part(str(e), 400)}"], {}
        per: dict[str, str] = {}
        for n in todo:
            method = mapping.get(n.need_id)
            if not method:
                per[n.need_id] = "no method named for it"
                continue
            if method not in reg.computational_methods:
                per[n.need_id] = f"no @method named {method!r} in the module"
                continue
            try:
                got = reg.computational_methods[method].function(str(Path(self.root) / n.path))
            except Exception:  # noqa: BLE001 - the error, without values, goes back to the agent
                tb = traceback.format_exc().strip().splitlines()
                per[n.need_id] = f"method {method} crashed: " + " | ".join(tb[-3:])
                continue
            if not isinstance(got, Computed):
                per[n.need_id] = f"method {method} did not return a Computed"
            elif got.n == 0 or (isinstance(got.value, float) and math.isnan(got.value)):
                per[n.need_id] = f"method {method} found no data in {n.path}"
        return [], per

    def write_methods(self, needs: list[MethodWriterNeed]) -> MethodWriterReport:
        """Ask for methods for every need, batch by batch, and keep the methods that work.

        A retry asks again only for the failed needs of its batch, and its reply is a new
        module. A module with no working method is removed, so a module that does not load
        never stays in the method folder.

        Raises:
            computational_methods.MethodFileError: a method file that was there before
                does not load, so its method names cannot be avoided.
        """
        report = MethodWriterReport(needs=len(needs))
        masked = masked_values(needs)
        versions = self.folder / "versions"
        for b, batch in enumerate(self.batches(needs), start=1):
            report.batches += 1
            todo, previous = batch, ""
            problems: list[str] = []
            for attempt in range(self.retries + 1):
                prompt = self.prompt_for(todo, masked, self.taken_method_names())
                if attempt:
                    prompt += ("\n\nYOUR PREVIOUS REPLY:\n" + previous + "\n\n"
                               + RETRY % "\n".join(f"- {p}" for p in problems))
                reply = self.ask(prompt)
                report.exchanges.append({"batch": b, "round": attempt, "problems": problems,
                                         "reply": reply})
                previous = reply
                name = self.next_module_name()
                try:
                    module, mapping = _parse(reply)
                except ValueError as e:
                    whole, per = [f"unreadable reply: {e}"], dict[str, str]()
                else:
                    versions.mkdir(parents=True, exist_ok=True)
                    (versions / f"b{b}_v{attempt + 1}_{name}").write_text(module)
                    report.versions += 1
                    whole, per = self.test_module(module, mapping, todo, name)
                    # a module with no working method goes, so its names are free again
                    if whole or len(per) == len(todo):
                        (self.methods_folder / name).unlink()
                    for n in todo:
                        if not whole and n.need_id not in per:
                            report.methods[n.need_id] = mapping[n.need_id]
                failed = [n for n in todo if whole or n.need_id in per]
                problems = whole + [f"need {k}: {v}" for k, v in per.items()]
                for n in failed:
                    report.unresolved[n.need_id] = whole[0] if whole else per[n.need_id]
                for n in todo:
                    if n not in failed:
                        report.unresolved.pop(n.need_id, None)
                todo = failed
                if not todo:
                    break
        report.linked = len(report.methods)
        return report


def ask(prompt: str, timeout: int = 900, model: str = models.preset.SONNET) -> str:
    """Send one prompt to the method-writer model and return its text.

    Raises:
        models.ProviderError: there is no key, or the call failed after its retries.
    """
    with spending.purpose("method_writer"):
        return models.ask(prompt, model, timeout)
