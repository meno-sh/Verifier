"""The rounds of the daemon: each batch of events sent to the agent, and what became of it.

Each round is saved as rounds/<n>.json in the state folder. The daemon writes them; the
daemon, `verifier status` and the diagnosis extension read them.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, ClassVar

from runtime.project import project_status
from runtime.project.paths import StatePaths
from schema.verdict import Verdict
from utils import file_storage


@dataclasses.dataclass
class Round:
    """One batch of events sent to the agent, and what became of it."""
    n: int
    events: list[dict[str, Any]]
    sent_at: float
    seen_busy: bool = False
    aborted: bool = False
    ended_at: float | None = None
    verdict: Verdict | None = None
    # the model call failed, or a claim step failed; the events go back in the inbox
    error: str | None = None
    spending: project_status.Spending | None = None   # what the round cost, once it ended
    # the model of the auditing agent in this round
    model: str | None = None
    session_id: str | None = None   # the OpenCode session the round was sent in
    continued: int = 0              # follow-ups sent after a turn cut by the output limit
    # the claim steps that code did before the agent was woken (Daemon.do_claim_steps), then
    # the link step after its round (Daemon.do_link_steps)
    claim_steps: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    prompted_at: float | None = None    # when the last follow-up was sent
    # when delivery began, before the claim steps; what the round cost counts from here
    begun_at: float | None = None
    # a claim step failed, so the agent was not woken and only the papers were checked
    # (Daemon.check_papers_only)
    papers_only: bool = False

    # The keys that an older round file does not have: `error` (before commit 0a42924),
    # `spending` (before commit ecae952), `model` (before commit 84d07f9), `session_id`
    # (added with core/project/agent_session.py), the four of the claim steps and follow-ups
    # (added with the claim steps of runtime/daemon.py), and `papers_only` (added with
    # Daemon.check_papers_only). Each reads as its default. Every other key is required.
    KEYS_ADDED_LATER: ClassVar[tuple[str, ...]] = (
        "error", "spending", "model", "session_id", "continued", "claim_steps", "prompted_at",
        "begun_at", "papers_only")

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Round:
        """Read a round file.

        Raises:
            ValueError: a key the round must have is missing, or a key is unknown.
        """
        names = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - names)
        missing = sorted(names - set(data) - set(cls.KEYS_ADDED_LATER))
        if unknown or missing:
            raise ValueError(f"round file of round {data.get('n')}: unknown keys {unknown}, "
                             f"missing keys {missing}")
        fields = {**{f.name: cls._default(f) for f in dataclasses.fields(cls)
                     if f.name in cls.KEYS_ADDED_LATER}, **data}
        if fields["verdict"] is not None:
            fields["verdict"] = Verdict(fields["verdict"])
        if fields["spending"] is not None:
            fields["spending"] = project_status.Spending.from_json(fields["spending"])
        return cls(**fields)

    @staticmethod
    def _default(f: dataclasses.Field[Any]) -> object:
        """The default value of a field, for a round file that does not have its key."""
        if f.default_factory is not dataclasses.MISSING:
            return f.default_factory()
        return None if f.default is dataclasses.MISSING else f.default

    def to_json(self) -> dict[str, Any]:
        """The round as its round file holds it."""
        return {**{f.name: getattr(self, f.name) for f in dataclasses.fields(self)},
                "verdict": None if self.verdict is None else self.verdict.value,
                "spending": None if self.spending is None else self.spending.to_json()}

    def ended_with_no_error(self) -> bool:
        """True when the round ended and its model call did not fail."""
        return self.ended_at is not None and not self.error

    @property
    def last_prompted_at(self) -> float:
        """When the agent was last sent a message: the last follow-up, or the round itself."""
        return self.prompted_at or self.sent_at

    @property
    def spending_since(self) -> float:
        """Where what the round cost counts from: when delivery began (sent_at in older rounds)."""
        return self.begun_at if self.begun_at is not None else self.sent_at


class RoundHistory:
    """Reads and writes the round files of one state folder."""

    def __init__(self, state_paths: StatePaths) -> None:
        """The rounds in the state folder of `state_paths`."""
        self.folder = state_paths.rounds
        self.round_folder = file_storage.JsonFileFolder(self.folder, indent=2)

    def round_files(self) -> list[Path]:
        """The round files, oldest first; none when the folder does not exist yet."""
        if not self.folder.exists():
            return []
        return sorted(self.folder.glob("*.json"), key=lambda p: int(p.stem))

    def rounds(self) -> list[Round]:
        """Every round, oldest first."""
        return [self._read(p) for p in self.round_files()]

    def last_rounds(self, count: int) -> list[Round]:
        """The last `count` rounds, oldest first; fewer when fewer exist. Reads only them."""
        assert count > 0, f"count must be positive, got {count}"
        return [self._read(p) for p in self.round_files()[-count:]]

    def last_round_with_agent_before(self, n: int) -> Round | None:
        """The last round before round `n` in which the agent was woken, or None.

        Papers-only rounds are skipped. It reads the round files from the newest back, and
        stops at the first such round.
        """
        for path in reversed(self.round_files()):
            if int(path.stem) >= n:
                continue
            r = self._read(path)
            if not r.papers_only:
                return r
        return None

    def last_round(self) -> Round | None:
        """The last round, or None before the first."""
        last = self.last_rounds(1)
        return last[0] if last else None

    def open_round(self) -> Round | None:
        """The last round if no daemon saw it end, else None."""
        last = self.last_round()
        return last if last is not None and last.ended_at is None else None

    def next_round_number(self) -> int:
        """The number of the next round."""
        return 1 + max((int(p.stem) for p in self.round_files()), default=0)

    def _read(self, path: Path) -> Round:
        """The round in one round file."""
        data = self.round_folder.read(path.name)
        assert isinstance(data, dict), f"{path} is not a JSON object"
        return Round.from_json(data)

    def save(self, r: Round) -> None:
        """Write a round to rounds/<n>.json."""
        self.round_folder.write(f"{r.n}.json", r.to_json())

    def model_of_last_clean_round_for_each_event_value(self, kind: str,
                                                       key: str) -> dict[str, str | None]:
        """The model of the last round that got an event and ended with no error.

        Each event of `kind` names a value at `key`, such as the id of a dispute. A failed
        round sends its events again, so the last round that got the event and ended with
        no error is the one that handled it.
        """
        out: dict[str, str | None] = {}
        for r in self.rounds():
            if not r.ended_with_no_error():
                continue
            for e in r.events:
                if e.get("kind") == kind:
                    out[e[key]] = r.model
        return out
