"""What a check is, what it reads, and the registry of the checks that a run uses.

A check is a subclass of Check with its declarations as class attributes: its name, title,
why, and the problem kinds it can report. A claim check gives one result for one claim and
reads a ClaimContext. A paper check group reads a PaperContext and gives one result for
each of its problem kinds. A CheckRegistry holds the checks, methods and run options of one
run; it refuses a problem kind that two checks claim. docs/SCHEMA.md section 6 describes it.
"""
from __future__ import annotations

import abc
import dataclasses
from collections.abc import Callable, Iterable, Mapping
from types import ModuleType
from typing import TYPE_CHECKING, ClassVar

from core.registry import run_options
from schema import findings, inputs, recompute
from schema.claim import claim_register_parser
from schema.primitives import (
    CheckResult,
    ClaimId,
    MethodName,
    Off,
    Problem,
    ProblemKind,
    Skipped,
    read_only,
    record,
)
from utils import extract

if TYPE_CHECKING:     # the methods module imports this one for its errors and the registry
    from core.methods import computational_methods


class Check(abc.ABC):
    """What every check declares, and the rules the registry applies to all of them.

    A subclass sets the attributes as class attributes. A check made at run time, such
    as the check of one review kind, sets them in its constructor.
    """
    name: str                               # also the switch that turns the check off
    title: str                              # shown next to its toggle
    why: str                                # what it looks for, one sentence
    problem_kinds: tuple[ProblemKind, ...]  # every problem kind it may report
    # False for code written by the agent: a crash of a claim check is a caveat
    trusted: ClassVar[bool] = True

    def refuse_malformed_declarations(self) -> None:
        """Refuse a missing declaration, or a problem kind that is not a ProblemKind.

        Raises:
            RegistryError: naming the check and what is wrong.
        """
        missing = [a for a in ("name", "title", "why", "problem_kinds") if not hasattr(self, a)]
        if missing:
            raise run_options.RegistryError(f"check {type(self).__name__} does not declare "
                                f"{', '.join(missing)}")
        if not self.problem_kinds:
            raise run_options.RegistryError(f"check {self.name!r} declares no problem kinds")
        wrong = [k for k in self.problem_kinds if not isinstance(k, ProblemKind)]
        if wrong:
            raise run_options.RegistryError(f"check {self.name!r}: {wrong!r} is not a ProblemKind")


# Which claims a claim check reads (CheckInputScope.claims).
OWN_CLAIM = "own-claim"                 # only its own claim
NAMED_CLAIM = "named-claim"             # its own claim, and the claim one of its keys names
PREMISES = "premises"                   # its own claim, and the claims it is inferred from
WHOLE_REGISTER = "whole-register"       # every claim; such a check is never cached
CLAIM_SCOPES = (OWN_CLAIM, NAMED_CLAIM, PREMISES, WHOLE_REGISTER)

# The other files a check reads for a claim, from the claim and the options in its scope.
type FilesRead = Callable[[inputs.Claim, Mapping[str, object]], tuple[str, ...]]


@record
class CheckInputScope:
    """What a claim check reads: which claims, which run options, and which files.

    A check sees only what its scope declares (restricted_context). So a check that
    reads more than it declares fails in the tests, and the result cache
    (core/analysis/check_result_cache.py) can store its result by what it reads.
    """
    claims: str = WHOLE_REGISTER            # one of CLAIM_SCOPES
    named_by: str | None = None             # with NAMED_CLAIM: the key that names the claim
    option_keys: tuple[str, ...] | None = ()        # None: every run option
    other_files_read: FilesRead | None = None       # None: no file
    reads_full_paper_text: bool = False             # ClaimContext.full_paper_text

    def __post_init__(self) -> None:
        """Refuse an unknown claim scope, or a key that names a claim with no NAMED_CLAIM.

        Raises:
            RegistryError: the scope is not valid.
        """
        if self.claims not in CLAIM_SCOPES:
            raise run_options.RegistryError(f"the claim scope must be one of {CLAIM_SCOPES}, "
                                            f"not {self.claims!r}")
        if (self.claims == NAMED_CLAIM) != (self.named_by is not None):
            raise run_options.RegistryError(
                f"give `named_by` with {NAMED_CLAIM!r}, and only with it")

    @property
    def can_be_cached(self) -> bool:
        """Whether a result can be stored by what the check reads: not for the whole register."""
        return self.claims != WHOLE_REGISTER

    def claim_ids(self, claim: inputs.Claim) -> tuple[ClaimId, ...]:
        """The ids of the claims the check reads for `claim`, its own first.

        For WHOLE_REGISTER this gives only the claim's own id; the context is not
        restricted then.
        """
        if self.claims == NAMED_CLAIM:
            assert self.named_by is not None        # __post_init__ refuses it
            named = claim.fields.get(self.named_by)
            return (claim.id, named) if isinstance(named, str) else (claim.id,)
        if self.claims == PREMISES:
            return (claim.id, *claim.ids_of_claims_it_is_inferred_from())
        return (claim.id,)

    def restricted_context(self, claim: inputs.Claim, context: ClaimContext) -> ClaimContext:
        """The context with only the claims, options and dependencies of this scope.

        A read of anything else raises KeyError, or gives None from `get`. The findings
        of the dependencies are kept only for PREMISES, and the document only for a scope
        that reads it.
        """
        if self.claims == WHOLE_REGISTER:
            return context
        ids = self.claim_ids(claim)
        options = (context.options if self.option_keys is None
                   else {k: v for k, v in context.options.items() if k in self.option_keys})
        return ClaimContext(
            register={i: context.register[i] for i in ids if i in context.register},
            dependencies=context.dependencies if self.claims == PREMISES else {},
            options=options,
            full_paper_text=context.full_paper_text if self.reads_full_paper_text else None)

    def files_read(self, claim: inputs.Claim, options: Mapping[str, object]) -> tuple[str, ...]:
        """The files the check reads for `claim`, besides the claim register.

        `options` are the options of the restricted context.
        """
        return () if self.other_files_read is None else self.other_files_read(claim, options)


class ClaimCheck(Check):
    """One check run on each claim, after the gate."""
    # what it reads; by default every claim and option, so its results are never cached
    input_scope: ClassVar[CheckInputScope] = CheckInputScope()
    # the claim keys it reads, each with its type; the claim register accepts them
    fields: Mapping[str, inputs.FieldType] = read_only({})
    needs: tuple[str, ...] = ()             # the keys it cannot run without
    needs_recompute: bool = False           # reads the number that recompute produced
    # runs only on a claim inferred from other claims, and reads their findings
    needs_dependencies: bool = False

    @abc.abstractmethod
    def check(self, claim: inputs.Claim, recomputed: recompute.Matches | None,
              context: ClaimContext) -> CheckResult:
        """The result of the check on one claim: Passed or Flagged."""

    def refuse_malformed_declarations(self) -> None:
        """Refuse an untyped key, a key the gate uses, an undeclared need, or both needs_*.

        Raises:
            RegistryError: naming the check and what is wrong.
        """
        super().refuse_malformed_declarations()
        untyped = sorted(k for k, t in self.fields.items() if not isinstance(t, inputs.FieldType))
        if untyped:
            raise run_options.RegistryError(
                f"claim check {self.name!r}: give each key a FieldType, "
                f"not {untyped}")
        gate_keys = claim_register_parser.ClaimRegisterParser.CLAIM_KEYS | {"fields"}
        clash = sorted(set(self.fields) & gate_keys)
        if clash:
            raise run_options.RegistryError(
                f"claim check {self.name!r} declares keys the claim register "
                f"already uses: {clash}")
        unknown = sorted(set(self.needs) - set(self.fields))
        if unknown:
            raise run_options.RegistryError(
                f"claim check {self.name!r} needs keys it does not declare: "
                f"{unknown}")
        if self.needs_recompute and self.needs_dependencies:
            raise run_options.RegistryError(
                f"claim check {self.name!r} needs both a recomputed number and "
                "dependencies, but a derived claim has no number")

    def reads_findings_of_claims_it_rests_on(self, options: Mapping[str, object]) -> bool:
        """Whether the check reads ClaimContext.dependencies in a run with these options.

        When no check that is on reads them, the claims are checked in parallel with no
        wait (core/analysis/claim_checker.py). By default, a check that needs dependencies
        reads their findings.
        """
        return self.needs_dependencies

    def applies_to_claim(self, claim: inputs.Claim) -> bool:
        """Whether this claim check runs on a claim at all.

        A check that reads a recomputed number does not apply to a claim inferred from
        other claims, which has none. A check that reads dependencies applies only to such
        a claim. A check that does not apply gives no result, so it is never a caveat.
        """
        derived = isinstance(claim.source, inputs.ClaimsSource)
        return not (self.needs_recompute and derived or self.needs_dependencies and not derived)


class PaperCheckGroup(Check):
    """A set of paper checks run together on one document, one check for each problem kind.

    The check of a kind coded "a-b" is named "a_b". The name of the group is the switch
    that turns all of its checks off.
    """
    # when on, its result (named after the group) must not be Skipped; see Findings.must_run
    must_run_if_requested: bool = False

    @abc.abstractmethod
    def run(self, context: PaperContext) -> PaperOutcome:
        """One result for each check of the group, and the facts it counted."""

    def results_when_switched_off(self) -> dict[str, CheckResult]:
        """One Off result for each check, for a group that is switched off."""
        return PaperCheckGroupResultBuilder(
            self.problem_kinds).results_when_switched_off()


class PaperCheckGroupResultBuilder:
    """Turns the problems a paper check group found into one result for each of its kinds."""

    def __init__(self, kinds: tuple[ProblemKind, ...]) -> None:
        """Build the results of a group that reports these kinds."""
        self.kinds = kinds

    @staticmethod
    def check_name_for_kind(kind: ProblemKind) -> str:
        """The name of the paper check that reports `kind`."""
        return kind.code.replace("-", "_")

    def results_from_problems(self, found: Iterable[Problem],
                              skipped: Mapping[ProblemKind, str] | None = None
                              ) -> dict[str, CheckResult]:
        """One result for each kind: Skipped with its reason, or Passed or Flagged.

        Raises:
            RegistryError: a problem has a kind that the group did not declare.
        """
        problems = tuple(found)
        undeclared = sorted({p.kind.code for p in problems if p.kind not in self.kinds})
        if undeclared:
            raise run_options.RegistryError(f"a paper check group reported undeclared kind(s) "
                                f"{', '.join(undeclared)}")
        skipped = skipped or {}
        return {self.check_name_for_kind(kind):
                (Skipped(because=skipped[kind]) if kind in skipped
                 else CheckResult.from_problems(p for p in problems if p.kind == kind))
                for kind in self.kinds}

    def results_when_switched_off(self) -> dict[str, CheckResult]:
        """One Off result for each kind, for a group that was switched off."""
        return {self.check_name_for_kind(kind): Off() for kind in self.kinds}


# What a check reads, and what a paper check group returns.

@record
class ClaimContext:
    """What a claim check reads besides its claim and the recomputed number."""
    register: inputs.ClaimsById         # every claim in the register, by id
    # the findings of each claim this claim is inferred from, already checked; a claim
    # checked after this one, because a cycle breaks here, is absent
    dependencies: Mapping[ClaimId, findings.ClaimFindings] = dataclasses.field(
        default_factory=lambda: read_only({}))
    # the run's options, as paper check groups get them; each check reads its own keys
    options: Mapping[str, object] = dataclasses.field(default_factory=lambda: read_only({}))
    # the full text of the paper (watched_project.full_paper_text); None when the run has
    # none, so a check that reads the paper must refuse None
    full_paper_text: str | None = None

    def __post_init__(self) -> None:
        """Make the mappings read-only."""
        object.__setattr__(self, "register", read_only(self.register))
        object.__setattr__(self, "dependencies", read_only(self.dependencies))
        object.__setattr__(self, "options", read_only(self.options))





@record
class PaperContext:
    """What a paper check group reads."""
    paper: extract.Paper                # the paper's structure, whatever its format
    options: Mapping[str, object]       # run options from the caller; each group reads its own
    # every label and anchor of the project's other documents, which a reference may name
    elsewhere: frozenset[str]


@record
class PaperOutcome:
    """What a paper check group returns: one result per check, and facts it counted.

    The document checker merges the outcomes of all groups into one PaperFindings, which
    names the same two maps `checks` and `facts`.
    """
    checks: Mapping[str, CheckResult]
    facts: Mapping[str, int]            # e.g. {"tables": 4, "numeric_sentences": 31}


# The registry of the checks, methods and run options of one run.

class CheckRegistry:
    """The checks, paper check groups, methods and run options one run uses, in order.

    A registry is a builder: the entry points, the method files, the plug-in checks and
    the extensions add to it.
    `copy_for_one_run` gives a copy for one run, so a run never changes the registry
    of built-in checks.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self.claim_checks: dict[str, ClaimCheck] = {}
        self.paper_check_groups: dict[str, PaperCheckGroup] = {}
        self.computational_methods: dict[
            MethodName, computational_methods.ComputationalMethodEntry] = {}
        self.run_option_catalog = run_options.RunOptionCatalog()
        self._owner_of_problem_kind: dict[str, tuple[ProblemKind, str]] = {}
        self._owner_of_claim_field: dict[str, tuple[inputs.FieldType, str]] = {}

    def copy_for_one_run(self) -> CheckRegistry:
        """A registry holding the same entries and run options, to which more can be added."""
        out = CheckRegistry()
        out.claim_checks = dict(self.claim_checks)
        out.paper_check_groups = dict(self.paper_check_groups)
        out.computational_methods = dict(self.computational_methods)
        out.run_option_catalog = self.run_option_catalog.copy()
        out._owner_of_problem_kind = dict(self._owner_of_problem_kind)
        out._owner_of_claim_field = dict(self._owner_of_claim_field)
        return out

    def _claim_problem_kinds(self, owner: str, kinds: Iterable[ProblemKind]) -> None:
        """Record the kinds a check reports; refuse a code another check already claims."""
        for kind in kinds:
            if not isinstance(kind, ProblemKind):
                raise run_options.RegistryError(f"{owner}: {kind!r} is not a ProblemKind")
            held = self._owner_of_problem_kind.get(kind.code)
            if held is not None and held[1] != owner:
                raise run_options.RegistryError(
                    f"{owner}: problem kind {kind.code!r} already belongs to "
                    f"{held[1]}")
            self._owner_of_problem_kind[kind.code] = (kind, owner)

    def _refuse_repeated_check_name(self, name: str) -> None:
        """Refuse a name that a claim check or a paper check group already has."""
        if name in self.claim_checks or name in self.paper_check_groups:
            raise run_options.RegistryError(f"two checks are named {name!r}")

    def add_claim_check(self, check: ClaimCheck) -> None:
        """Add a claim check.

        Refuses a malformed check, a repeated name, or a kind code another check owns.
        """
        check.refuse_malformed_declarations()
        self._refuse_repeated_check_name(check.name)
        for key, ftype in check.fields.items():
            held = self._owner_of_claim_field.get(key)
            if held is not None and held[0] is not ftype:
                raise run_options.RegistryError(
                    f"claim check {check.name!r} reads {key!r} as {ftype.value}, "
                    f"but {held[1]} reads it as {held[0].value}")
        self._claim_problem_kinds(check.name, check.problem_kinds)
        for key, ftype in check.fields.items():
            self._owner_of_claim_field.setdefault(key, (ftype, check.name))
        self.claim_checks[check.name] = check

    def add_paper_check_group(self, group: PaperCheckGroup) -> None:
        """Add a paper check group.

        Refuses a malformed group, a repeated name, or a kind code another check owns.
        """
        group.refuse_malformed_declarations()
        self._refuse_repeated_check_name(group.name)
        self._claim_problem_kinds(group.name, group.problem_kinds)
        self.paper_check_groups[group.name] = group

    def add_computational_method(
            self, method_entry: computational_methods.ComputationalMethodEntry) -> None:
        """Add a computational method. Refuses a repeated name."""
        if method_entry.name in self.computational_methods:
            raise run_options.RegistryError(
                f"two computational methods are named {method_entry.name!r}")
        self.computational_methods[method_entry.name] = method_entry

    def add_run_options_of_module(self, module: ModuleType, trusted: bool = True) -> None:
        """Add the RUN_OPTIONS of a module, and its options rules when it is trusted.

        A plug-in check file (`trusted=False`) adds no options rule, because a rule runs
        before any check and its crash would stop every run.
        """
        for value in vars(module).values():
            entry = getattr(value, run_options.MARK, None) if callable(value) else None
            if isinstance(entry, run_options.OptionsRuleEntry) and trusted:
                self.run_option_catalog.add_options_rule(entry)
        for option in getattr(module, "RUN_OPTIONS", ()):
            if not isinstance(option, run_options.RunOption):
                raise run_options.RegistryError(f"{module.__name__}.RUN_OPTIONS holds {option!r}, "
                                    "not a RunOption")
            self.run_option_catalog.add_option(option)

    def refuse_options_that_do_not_fit(self, options: Mapping[str, object],
                                       switched_off: frozenset[str] = frozenset()) -> None:
        """Refuse options of the wrong type, or that the rule of a check that is on refuses.

        Raises:
            TypeError, ValueError: an option is wrong; the message says which.
        """
        self.run_option_catalog.refuse_options_that_do_not_fit(
            options, self.names_that_can_be_switched_off(), switched_off)

    def claim_field_types(self) -> dict[str, inputs.FieldType]:
        """Every claim key a registered claim check reads, with its type."""
        return {key: ftype for key, (ftype, _owner) in self._owner_of_claim_field.items()}

    def claim_checks_that_read_field(self, key: str) -> list[str]:
        """The claim checks that read one claim key, in registration order."""
        return [name for name, c in self.claim_checks.items() if key in c.fields]

    def names_that_can_be_switched_off(self) -> tuple[str, ...]:
        """Everything that can be switched off by name: each paper group and claim check."""
        return (*self.paper_check_groups, *self.claim_checks)

    def checks_that_must_not_be_skipped(self, switched_off: frozenset[str]) -> frozenset[str]:
        """The checks that are on and must not be skipped (see Findings.must_run)."""
        return frozenset(g.name for g in self.paper_check_groups.values()
                         if g.must_run_if_requested and g.name not in switched_off)
