"""Computational methods for the fixture working directory.

Its runs write `results.json`, a {"records": [...]} file with one record per question
(`condition`, `post`, `prior`, `y`), and `setup.json` beside it, the run's own record
of how it was set up. These methods know that layout; the verifier does not.
"""
import glob
import json
import math
import os

from checks.interface import Computed, method


def _records(path: str) -> list[dict]:
    """Every record in the files matching `path`."""
    return [r for p in sorted(glob.glob(path)) for r in json.load(open(p))["records"]]


def _setup(path: str) -> dict | None:
    """The run's setup record beside the first matching file, or None if it has none."""
    files = sorted(glob.glob(path))
    record = os.path.join(os.path.dirname(files[0]), "setup.json") if files else ""
    return json.load(open(record)) if os.path.exists(record) else None


def _mean(values: list[float], path: str) -> Computed:
    """The mean of `values`, NaN when there are none, with the run's setup."""
    return Computed(value=sum(values) / len(values) if values else math.nan, n=len(values),
                    setup=_setup(path))


@method("trained_brier_post")
def trained_brier_post(path: str) -> Computed:
    """Mean Brier score of the trained model's posterior."""
    rows = [r for r in _records(path) if r["condition"] == "trained"]
    return _mean([(r["post"] - r["y"]) ** 2 for r in rows], path)


@method("trained_brier_pre")
def trained_brier_pre(path: str) -> Computed:
    """Mean Brier score of the trained model's prior."""
    rows = [r for r in _records(path) if r["condition"] == "trained"]
    return _mean([(r["prior"] - r["y"]) ** 2 for r in rows], path)


@method("mean_post")
def mean_post(path: str) -> Computed:
    """Mean posterior over every record."""
    return _mean([r["post"] for r in _records(path)], path)
