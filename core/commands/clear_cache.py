"""`vcheck clear-cache`: remove every stored result of the claim checks."""
from __future__ import annotations

import argparse
from pathlib import Path

from core import cli_command
from core.analysis import check_result_cache


class VcheckClearCacheCommand(cli_command.CLICommand):
    """Runs `vcheck clear-cache` for the auditing agent and a person."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck clear-cache`."""
        return None

    def execute(self, args: argparse.Namespace) -> int:
        """Remove every stored result, so the next `vcheck run` runs every check again."""
        state_dir: Path = args.state
        check_result_cache.CheckResultCache.clear(state_dir)
        print(f"removed the stored check results in {state_dir / check_result_cache.CACHE_DIR}")
        return 0
