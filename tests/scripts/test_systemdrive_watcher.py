"""Tests for the runner-agnostic %SystemDrive% watcher.

The watcher exists because the never-landed runner-embedded probe
(9a6df34e25) could only see spawns the parallel runner itself made, and the
writer it was built for reproduced from a plain SEQUENTIAL pytest run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.systemdrive_watcher import JUNK_NAME, build_parser, write_record, ProcessRing, cwd_matches, describe_pid


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


def test_describe_pid_captures_ancestry_fields():
    entry = describe_pid(os.getpid())
    assert entry["pid"] == os.getpid()
    assert entry["name"]
    assert isinstance(entry["ppid"], int)
    assert isinstance(entry["cmdline"], list)


def test_describe_pid_of_a_dead_process_is_kept_with_an_error():
    """A PID we saw appear and could not read is still evidence.

    Dropping it would hide the very short-lived processes this ring buffer
    exists to catch.
    """
    entry = describe_pid(999999)
    assert entry["pid"] == 999999
    assert "error" in entry


def test_describe_pid_partial_failure_keeps_the_other_fields(monkeypatch):
    """cwd is AccessDenied for many Windows processes.

    Fields are captured individually so losing cwd does not also lose the
    cmdline, which is the actual identifying field. A single try/except
    around the whole block would drop both -- so this test INDUCES the cwd
    failure rather than hoping for one.
    """
    import psutil

    def boom(self):
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil.Process, "cwd", boom)
    entry = describe_pid(os.getpid())
    assert entry["cmdline"], "cmdline must survive a cwd failure"
    assert entry["errors"]["cwd"] == "AccessDenied"


def test_first_sample_is_a_baseline_not_a_flood():
    """Priming must not record the whole process table as 'creations'.

    ~1000 processes are live on this box; recording them all would bury the
    handful that actually started inside the watch window.
    """
    ring = ProcessRing(capacity=10000)
    assert ring.sample() == 0
    assert len(ring) == 0


def test_sample_records_a_newly_spawned_process():
    ring = ProcessRing(capacity=10000)
    ring.sample()  # prime
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        ring.sample()
        pids = [e["pid"] for e in ring.dump()]
        assert child.pid in pids
    finally:
        child.kill()
        child.wait()


def test_ring_is_bounded(tmp_path):
    ring = ProcessRing(capacity=3)
    ring._primed = True
    ring._entries.extend({"pid": n} for n in range(100))
    assert len(ring) == 3
    assert [e["pid"] for e in ring.dump()] == [97, 98, 99]


def test_cwd_matches_is_true_for_the_watched_root(tmp_path: Path):
    assert cwd_matches({"cwd": str(tmp_path)}, tmp_path.resolve())


def test_cwd_matches_is_false_without_a_cwd(tmp_path: Path):
    """Best-effort: a process that exited before enrichment has no cwd."""
    assert not cwd_matches({"pid": 1, "error": "NoSuchProcess"}, tmp_path.resolve())


def test_failed_enrichment_leaves_the_creation_retryable(monkeypatch):
    """Fix for the ordering bug: `_known` must advance only AFTER entries land.

    If `_known` were advanced before enrichment, a pid whose enrichment
    raised would be marked "known" forever and never recorded -- the ring
    silently losing the very creation it exists to capture.
    """
    import scripts.systemdrive_watcher as w

    ring = w.ProcessRing(capacity=5000)
    ring.sample()  # prime
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(6)"])
    try:
        monkeypatch.setattr(
            w, "describe_pid",
            lambda pid: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert ring.sample() == 0            # swallowed, not raised
        assert ring._sample_errors == 1
        monkeypatch.undo()                   # enrichment works again
        ring.sample()
        assert any(e["pid"] == child.pid for e in ring.dump()), \
            "child lost after a failed enrichment - _known advanced too early"
    finally:
        child.kill()
        child.wait()


def test_sample_never_raises(monkeypatch):
    """A diagnostic must never take down the run it observes."""
    import scripts.systemdrive_watcher as w

    ring = w.ProcessRing(capacity=10)
    ring.sample()
    monkeypatch.setattr(
        w.psutil, "pids",
        lambda: (_ for _ in ()).throw(RuntimeError("pids exploded")),
    )
    assert ring.sample() == 0
    assert ring._sample_errors >= 1
