"""Tests for the runner-agnostic %SystemDrive% watcher.

The watcher exists because the never-landed runner-embedded probe
(9a6df34e25) could only see spawns the parallel runner itself made, and the
writer it was built for reproduced from a plain SEQUENTIAL pytest run.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.systemdrive_watcher import JUNK_NAME, build_parser, write_record


def test_junk_name_is_the_literal_template():
    assert JUNK_NAME == "%SystemDrive%"


def test_write_record_appends_jsonl_immediately(tmp_path: Path):
    """The record must be on disk the moment it is written.

    Requirement 4 of the spec: this class of run gets killed, so anything
    buffered to exit is the one copy of the evidence that does not survive.
    """
    log = tmp_path / "w.jsonl"
    write_record(log, "armed", roots=["/x"])
    assert log.exists()
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["event"] == "armed"
    assert rows[0]["roots"] == ["/x"]
    assert "at" in rows[0] and "watcher_pid" in rows[0]


def test_write_record_appends_rather_than_truncates(tmp_path: Path):
    log = tmp_path / "w.jsonl"
    write_record(log, "armed")
    write_record(log, "done", sightings=0)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in rows] == ["armed", "done"]


def test_write_record_survives_an_unwritable_log(tmp_path: Path, capsys):
    """A diagnostic must never take down the run it observes."""
    log = tmp_path / "nope"
    log.mkdir()  # a directory where a file is expected
    record = write_record(log, "armed")
    assert record["event"] == "armed"  # returned anyway


def test_parser_has_real_help():
    """The watcher must not repeat run_tests_parallel.py's --help trap."""
    parser = build_parser()
    assert parser.format_help()


def test_parser_defaults_are_conservative():
    args = build_parser().parse_args([])
    assert args.ring > 0
    assert args.sample_ms > 0
    assert args.secs > 0
    assert args.stop_file is None
