"""An extension for the tests that gives fixed disputed flags (core/report_model/disputed_flags.py).

The disputes are read from `fixed_disputes.json` in the state directory: a list of
objects with the keys code, subject, detail, dispute_id and reason.
"""
import json
from pathlib import Path

from core.registry.extensions import Extension
from core.report_model.disputed_flags import DisputedFlag
from core.report_model.issues import FlagDispute

FILE = "fixed_disputes.json"
NAME = "fakes.fixed_disputes:FixedDisputesExtension"


class FixedDisputesExtension(Extension):
    """Gives the disputed flags of the state directory's file."""

    def disputed_flags(self, root: Path, state_dir: Path) -> list[DisputedFlag]:
        """The disputes in the state directory's file, or none when it does not exist."""
        path = state_dir / FILE
        if not path.exists():
            return []
        return [DisputedFlag(code=d["code"], subject=d["subject"], detail=d["detail"],
                             dispute=FlagDispute(dispute_id=d["dispute_id"], reason=d["reason"]))
                for d in json.loads(path.read_text())]
