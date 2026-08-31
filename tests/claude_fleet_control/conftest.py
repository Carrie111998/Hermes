"""Shared fakes for the P6 fleet-controller tests.

Every fake process uses a NEGATIVE pid on purpose: if any test wiring ever
leaks into a live code path, a negative pid cannot address a real process.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claude_fleet_control.models import ProcessRecord, ProcessSnapshot

NOW = 1_800_000_000.0  # fixed epoch for pure-planner tests
USER = "BOX\\diego"


def rec(
    pid,
    ppid=None,
    name="node.exe",
    exe=None,
    cmdline=(),
    create_time=NOW - 7200.0,
    rss=10 * 1024 * 1024,
    username=USER,
    complete=True,
):
    return ProcessRecord(
        pid=pid, ppid=ppid, name=name, exe=exe,
        cmdline=tuple(cmdline), create_time=create_time, rss=rss,
        username=username, complete=complete,
    )


def cli_rec(pid, ppid=None, create_time=NOW - 7200.0, **kwargs):
    kwargs.setdefault("name", "claude.exe")
    kwargs.setdefault(
        "cmdline",
        ("claude.exe", "--output-format", "stream-json", "--resume",
         "00000000-0000-4000-8000-%012d" % abs(pid)),
    )
    return rec(pid, ppid=ppid, create_time=create_time, **kwargs)


def snapshot(records, taken_at=NOW, complete=True):
    return ProcessSnapshot(taken_at=taken_at, records=tuple(records), complete=complete)


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


@pytest.fixture
def fixed_now():
    return NOW
