"""The result classes of the schema, found from their base classes, for the tests.

A test compares a list kept elsewhere, such as the broken results of score_gold.py, with
these, so a new result class cannot be left out of that list.
"""
from schema import provenance, recompute
from schema.primitives import RecomputeResult


def every_subclass(cls: type) -> list[type]:
    """Every subclass of `cls` at any depth that its module still holds under its name.

    `record` makes a new class for its slots, and the class it replaced can stay in
    `__subclasses__` until it is garbage-collected, so only the class of the module counts.
    """
    found: list[type] = []
    for sub in cls.__subclasses__():
        for c in (sub, *every_subclass(sub)):
            module = __import__(c.__module__, fromlist=["_"])
            if getattr(module, c.__qualname__, None) is c and c not in found:
                found.append(c)
    return found


def broken_provenance_classes() -> list[type]:
    """The provenance results that make a claim impossible to verify."""
    return [c for c in every_subclass(provenance.ProvenanceResult)
            if c.category is provenance.ProvenanceCategory.BROKEN]


def broken_recompute_classes() -> list[type]:
    """The recompute results that make a claim impossible to verify."""
    assert recompute.Matches in every_subclass(RecomputeResult)
    return [c for c in every_subclass(RecomputeResult) if c.is_broken]
