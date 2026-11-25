"""The times the Verifier writes into its files, all in UTC."""
from __future__ import annotations

import datetime


def utc_now_iso() -> str:
    """The current time, ISO 8601 in UTC, to the second: for a record in a file."""
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def utc_now_for_file_name() -> str:
    """The current time in UTC as text that can be part of a file name, such as 20261113T010203Z."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
