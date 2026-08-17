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


import threading
import time

from scripts.systemdrive_watcher import Watcher, watch_polling


def _rows(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


# These two tests exercise on_hit()'s real (unmocked) `psutil.pids()` +
# `describe_pid()` walk of the live process table, by design (see on_hit's
# docstring: ancestry is the perishable part). describe_pid() is measured at
# ~204ms/process against ~968 live processes on this box, so an UNBOUNDED
# walk takes 60-200s -- well past the repo's global 30s pytest-timeout
# default (pyproject.toml addopts --timeout=30). Fix 2 put a time budget
# (`live_sweep_secs`) on that walk specifically so callers -- including these
# tests -- are not held hostage to the full table; passing a small budget
# here keeps the test fast while still exercising the real, unmocked sweep.
def test_on_hit_writes_the_sighting_before_returning(tmp_path: Path):
    """Requirement 4: JSONL at the MOMENT of the sighting, not at exit."""
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    root.mkdir()
    watcher = Watcher([root], log=log, live_sweep_secs=0.5)
    watcher.on_hit(root.resolve(), "test")
    assert log.exists()
    sightings = [r for r in _rows(log) if r["event"] == "SIGHTING"]
    assert len(sightings) == 1
    rec = sightings[0]
    assert rec["backend"] == "test"
    assert "watcher_has_systemdrive" in rec
    assert "ring_cwd_matches" in rec
    assert rec["live_sweep"] == "pending"
    live_sightings = [r for r in _rows(log) if r["event"] == "SIGHTING_LIVE"]
    assert len(live_sightings) == 1
    live_rec = live_sightings[0]
    assert "live_cwd_matches" in live_rec
    assert live_rec["live_process_count"] > 0
    assert live_rec["live_process_total"] > 0
    assert "live_sweep_truncated" in live_rec


def test_on_hit_reports_only_the_first_transition(tmp_path: Path):
    """Later ticks would re-report the same tree and bury the original."""
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    root.mkdir()
    watcher = Watcher([root], log=log, live_sweep_secs=0.5)
    watcher.on_hit(root.resolve(), "test")
    watcher.on_hit(root.resolve(), "test")
    assert len([r for r in _rows(log) if r["event"] == "SIGHTING"]) == 1
    assert watcher.sightings == 1


def test_sighting_record_is_durable_before_the_live_sweep(tmp_path: Path, monkeypatch):
    """The SIGHTING row must hit disk while the live sweep is still running.

    Not a row-ordering assertion: ordering alone passes even when the sweep is
    moved above the write, which is the exact regression this guards. The live
    sweep costs 60-200s on this box and the run can be killed at any moment,
    so the durable write must not wait for it.
    """
    import scripts.systemdrive_watcher as w

    root = tmp_path / "root"
    root.mkdir()
    log = tmp_path / "w.jsonl"

    # Make the sweep measurably slow, as it is in reality.
    real_describe_pid = w.describe_pid
    monkeypatch.setattr(
        w, "describe_pid",
        lambda pid: (time.sleep(0.02), real_describe_pid(pid))[1],
    )

    watcher = w.Watcher([root], log=log, live_sweep_secs=0.5)

    started = time.perf_counter()
    returned: dict = {}

    def run_hit():
        watcher.on_hit(root.resolve(), "test")
        returned["at"] = time.perf_counter() - started

    thread = threading.Thread(target=run_hit)
    thread.start()

    # Poll for the SIGHTING row appearing on disk WHILE on_hit is still running.
    durable_at = None
    while thread.is_alive() and time.perf_counter() - started < 20:
        if log.exists() and log.read_text(encoding="utf-8").strip():
            durable_at = time.perf_counter() - started
            break
        time.sleep(0.005)
    thread.join(timeout=25)

    assert durable_at is not None, "SIGHTING never became durable while on_hit ran"
    assert "at" in returned, "on_hit did not finish"
    assert durable_at < returned["at"], (
        f"SIGHTING became durable at {durable_at:.3f}s but on_hit only returned at "
        f"{returned['at']:.3f}s - the durable write is not ahead of the sweep"
    )

    rows = _rows(log)
    assert rows[0]["event"] == "SIGHTING"
    assert rows[0]["live_sweep"] == "pending"
    assert "ring_size" in rows[0]
    assert any(r["event"] == "SIGHTING_LIVE" for r in rows)


def test_preexisting_tree_is_recorded_and_blames_nobody(tmp_path: Path):
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    (root / JUNK_NAME).mkdir(parents=True)
    watcher = Watcher([root], log=log)
    watcher.record_preexisting()
    rows = [r for r in _rows(log) if r["event"] == "preexisting"]
    assert len(rows) == 1
    assert "cannot attribute" in rows[0]["note"]
    # A pre-existing tree must not later be reported as a fresh sighting.
    watcher.on_hit(root.resolve(), "test")
    assert not [r for r in _rows(log) if r["event"] == "SIGHTING"]


def test_watch_polling_detects_a_created_directory(tmp_path: Path):
    hits = []
    stop = threading.Event()
    thread = threading.Thread(
        target=watch_polling,
        args=(tmp_path.resolve(), lambda r, b: hits.append((r, b)), stop, 0.02),
        daemon=True,
    )
    thread.start()
    time.sleep(0.05)
    (tmp_path / JUNK_NAME).mkdir()
    thread.join(timeout=5)
    assert hits, "polling backend never fired"
    assert hits[0][1] == "polling"


def test_watch_polling_stops_on_the_stop_event(tmp_path: Path):
    stop = threading.Event()
    thread = threading.Thread(
        target=watch_polling,
        args=(tmp_path.resolve(), lambda r, b: None, stop, 0.02),
        daemon=True,
    )
    thread.start()
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_run_emits_the_negative_when_nothing_appears(tmp_path: Path):
    """A quiet armed run must be evidence, not silence."""
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    root.mkdir()
    watcher = Watcher([root], log=log, secs=0.3, poll_ms=20, sample_ms=20,
                      force_polling=True)
    assert watcher.run() == 0
    rows = _rows(log)
    assert rows[0]["event"] == "armed"
    assert "backend_by_root" in rows[0]
    done = [r for r in rows if r["event"] == "done"][0]
    assert done["sightings"] == 0
    assert "NEGATIVE" in done["note"]
    assert done["watched_secs"] >= 0


def test_run_stops_early_when_the_stop_file_appears(tmp_path: Path):
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    root.mkdir()
    stop_file = tmp_path / "stop"
    watcher = Watcher([root], log=log, secs=30, poll_ms=20, sample_ms=20,
                      stop_file=stop_file, force_polling=True)
    threading.Timer(0.2, stop_file.touch).start()
    started = time.monotonic()
    watcher.run()
    assert time.monotonic() - started < 10, "stop-file did not shut the watcher down"
    assert [r for r in _rows(log) if r["event"] == "done"]
