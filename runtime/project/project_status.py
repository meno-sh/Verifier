"""What `verifier status` and `verifier report` read about a watched project: report, spending."""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from core.report_output import report_store, saved_report
from runtime.agent import opencode
from runtime.project.paths import StatePaths
from schema.primitives import record
from utils import spending


@record
class LatestReport:
    """The latest saved report of a watched project, with its Markdown, markup and layout data."""
    saved: saved_report.SavedReport
    text: str | None                # the Markdown, or None when its file is gone
    typst: str | None               # the Typst markup, or None when its file is gone
    layout: Mapping[str, object] | None   # the layout data, or None for an older report

    def to_json(self) -> dict[str, object]:
        """The saved report, with its Markdown, Typst markup and layout data.

        They are under "text", "typst" and "layout".
        """
        return {**self.saved.to_json(), "text": self.text, "typst": self.typst,
                "layout": None if self.layout is None else dict(self.layout)}


def latest_report(paths: StatePaths) -> LatestReport | None:
    """The latest saved report of the project, or None before the first report."""
    store = report_store.ReportStore(paths.state)
    saved = store.load_saved_report("latest")
    if saved is None:
        return None
    return LatestReport(saved=saved, text=store.load_report_text("latest", ".md"),
                        typst=store.load_report_text("latest", ".typ"),
                        layout=store.load_layout_data("latest"))


@dataclasses.dataclass(frozen=True)
class Spending:
    """What the model calls of a watched project cost in dollars, in a span of time.

    The agent's calls come from OpenCode's database; the Verifier's own calls, such as the
    method writer and the uncertainty check, from the spending log (utils/spending.py).
    """
    agent: opencode.AgentSpending
    model_calls: Mapping[str, spending.Total]       # the Verifier's own calls, by purpose

    @property
    def cost(self) -> float:
        """The cost of every call, in dollars."""
        return self.agent.cost + sum(t.cost for t in self.model_calls.values())

    @property
    def unknown(self) -> int:
        """How many of the Verifier's own calls gave no cost."""
        return sum(t.unknown for t in self.model_calls.values())

    @classmethod
    def of_project(cls, paths: StatePaths, since: float = 0.0) -> Spending:
        """The calls made at or after `since` (epoch seconds).

        Raises:
            opencode.OpenCodeError: OpenCode's database is not in the layout it knows.
            spending.SpendingError: the spending log has a line that is not a record.
        """
        since_ms = int(since * 1000)
        calls = spending.totals(spending.read(spending.log_path(paths.alerts_log)), since_ms)
        return cls(agent=opencode.spending(paths.runtime, since_ms),
                   model_calls=dict(sorted(calls.items())))

    def to_json(self) -> dict[str, object]:
        """The total, the agent's calls, the Verifier's own calls, and the calls with no cost."""
        return {"cost": self.cost, "agent": dataclasses.asdict(self.agent),
                "model_calls": {name: dataclasses.asdict(t)
                                for name, t in self.model_calls.items()},
                "unknown": self.unknown}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Spending:
        """Read the JSON that `to_json` gave, as a round file keeps it."""
        return cls(agent=opencode.AgentSpending(**data["agent"]),
                   model_calls={name: spending.Total(**t)
                                for name, t in data["model_calls"].items()})

    def status_line(self) -> str:
        """One line on what the calls cost, per part."""
        parts = [f"agent ${self.agent.cost:.2f} in {self.agent.calls} calls"]
        parts += [f"{name} ${t.cost:.2f} in {t.calls} calls"
                  for name, t in self.model_calls.items()]
        unknown = f"; {self.unknown} calls gave no cost" if self.unknown else ""
        return f"spent: ${self.cost:.2f} ({', '.join(parts)}{unknown})"
