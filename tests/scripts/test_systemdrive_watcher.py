"""Tests for the runner-agnostic %SystemDrive% watcher.

The watcher exists because the never-landed runner-embedded probe
(9a6df34e25) could only see spawns the parallel runner itself made, and the
writer it was built for reproduced from a plain SEQUENTIAL pytest run.
"""

from __future__ import annotations

import ctypes
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


def test_sighting_record_is_durable_before_the_live_sweep(monkeypatch, tmp_path):
    """The SIGHTING row must be durable BEFORE the live sweep begins.

    Deterministic by construction: both marks are recorded on on_hit's OWN
    thread, so their order is fixed by the code path rather than by the
    scheduler. An earlier version polled the log from a second thread and was
    measured letting the regression through ~1 run in 8 under CPU contention.
    """
    import scripts.systemdrive_watcher as w

    root = tmp_path / "root"
    root.mkdir()
    log = tmp_path / "w.jsonl"
    marks = {}

    real_describe = w.describe_pid
    def slow_describe(pid):
        # First call marks the moment the live sweep actually begins.
        marks.setdefault("sweep_started", time.perf_counter())
        time.sleep(0.005)
        return real_describe(pid)
    monkeypatch.setattr(w, "describe_pid", slow_describe)

    real_write = w.write_record
    def spy_write(log_path, event, **fields):
        record = real_write(log_path, event, **fields)
        if event == "SIGHTING":
            marks["sighting_durable"] = time.perf_counter()
            # Prove it is genuinely ON DISK at this instant, not merely that
            # the function was entered.
            marks["sighting_bytes"] = log_path.stat().st_size
        return record
    monkeypatch.setattr(w, "write_record", spy_write)

    w.Watcher([root], log=log, live_sweep_secs=0.3).on_hit(root.resolve(), "test")

    assert "sighting_durable" in marks, "SIGHTING record was never written"
    assert "sweep_started" in marks, "live sweep never ran - test proves nothing"
    assert marks["sighting_bytes"] > 0, "SIGHTING was not flushed to disk before the sweep"
    assert marks["sighting_durable"] < marks["sweep_started"], (
        "the SIGHTING record was not durable before the live sweep began"
    )

    rows = _rows(log)
    assert rows[0]["event"] == "SIGHTING"
    assert rows[0]["live_sweep"] == "pending"
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
    # A substring check alone is vacuous here: the UNKNOWN note is literally
    # "UNKNOWN - a clean NEGATIVE cannot be claimed: ..." and CONTAINS
    # "NEGATIVE". `startswith` plus an explicit absence of "UNKNOWN" is what
    # actually discriminates the two outcomes -- see
    # test_run_emits_unknown_when_a_blocker_is_present below for the paired
    # falsifier that proves this test can fail.
    assert done["note"].startswith("NEGATIVE")
    assert "UNKNOWN" not in done["note"]
    assert done["watched_secs"] >= 0


def test_run_emits_unknown_when_a_blocker_is_present(tmp_path: Path):
    """Companion to test_run_emits_the_negative_when_nothing_appears.

    Proves the OTHER direction: a genuine blocker (here, a %SystemDrive%
    tree already on disk at arm time, which record_preexisting() latches so
    it can NEVER produce a SIGHTING) must demote `done` to UNKNOWN rather
    than let `sightings == 0` alone earn a clean NEGATIVE.

    Without this test, the suite has only ever exercised the "everything
    clean" path through `done`'s note -- a watcher permanently stuck at
    UNKNOWN (e.g. blockers forced non-empty unconditionally) would be just
    as useless as one that always claims NEGATIVE, and nothing here would
    catch it. This test is that catch: it fails against exactly that
    mutation (see the task's falsification requirement).
    """
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    (root / JUNK_NAME).mkdir(parents=True)
    watcher = Watcher([root], log=log, secs=0.3, poll_ms=20, sample_ms=20,
                       force_polling=True)
    assert watcher.run() == 0
    rows = _rows(log)
    done = [r for r in rows if r["event"] == "done"][0]
    assert done["sightings"] == 0
    # The note DOES contain the substring "NEGATIVE" ("a clean NEGATIVE
    # cannot be claimed") -- that is exactly the trap the sibling test's old
    # `assert "NEGATIVE" in done["note"]` fell into. `startswith` is what
    # actually tells the two outcomes apart.
    assert done["note"].startswith("UNKNOWN")
    # The structured fields, not just the prose note, must show WHY.
    assert done["systemdrive_present_on_disk"] == [str(root.resolve())]
    assert done["preexisting_roots"] == [str(root.resolve())]


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


import struct

import pytest

from scripts.systemdrive_watcher import (
    FILE_ACTION_ADDED,
    _HandleOwner,
    _open_directory_handle,
    parse_notifications,
    watch_readdirchanges,
)


def _notification(action: int, name: str, next_offset: int = 0) -> bytes:
    encoded = name.encode("utf-16-le")
    return struct.pack("<III", next_offset, action, len(encoded)) + encoded


def test_parse_notifications_reads_a_single_entry():
    buf = _notification(FILE_ACTION_ADDED, JUNK_NAME)
    assert list(parse_notifications(buf, len(buf))) == [(FILE_ACTION_ADDED, JUNK_NAME)]


def test_parse_notifications_walks_the_chain():
    """FileNameLength is in BYTES, not characters - the classic mistake."""
    alpha_len = len(_notification(FILE_ACTION_ADDED, "alpha"))
    first = _notification(FILE_ACTION_ADDED, "alpha", next_offset=alpha_len)
    second = _notification(FILE_ACTION_ADDED, JUNK_NAME)
    buf = first + second
    assert list(parse_notifications(buf, len(buf))) == [
        (FILE_ACTION_ADDED, "alpha"),
        (FILE_ACTION_ADDED, JUNK_NAME),
    ]


def test_parse_notifications_stops_at_the_declared_length():
    """Regression for review finding 7: the original version of this test
    built one notification with next_offset=0 plus trailing zero bytes. The
    next_off == 0 terminator ends the walk on its own -- that test would
    have passed even with the nbytes bound deleted entirely.

    This version proves the nbytes bound itself does the work: the buffer is
    the module's REUSED 64KB scratch area, so `stale` here stands in for a
    previous call's leftover notification. The header declares a name far
    longer than the bytes ReadDirectoryChangesW actually returned this call
    (`nbytes`); without the bound, parse_notifications would slice into
    `stale` and decode it as if it were part of the real notification.
    """
    stale = _notification(FILE_ACTION_ADDED, "PREVIOUS-CALL-LEFTOVER-DATA")
    honest_header = struct.pack("<III", 0, FILE_ACTION_ADDED, 4000)
    buf = honest_header + stale
    nbytes = 12  # only the header is within the declared valid region
    assert list(parse_notifications(buf, nbytes)) == []


@pytest.mark.skipif(sys.platform != "win32", reason="ReadDirectoryChangesW is Win32-only")
def test_readdirchanges_detects_a_created_directory(tmp_path: Path):
    """End-to-end proof the fast backend actually fires."""
    hits = []
    stop = threading.Event()
    log = tmp_path / "w.jsonl"
    owner = _HandleOwner(*_open_directory_handle(tmp_path.resolve()))
    thread = threading.Thread(
        target=watch_readdirchanges,
        args=(tmp_path.resolve(), lambda r, b: hits.append((r, b)), stop, owner, log),
        daemon=True,
    )
    thread.start()
    time.sleep(0.3)  # let the watch arm
    (tmp_path / JUNK_NAME).mkdir()
    thread.join(timeout=10)
    assert hits, "ReadDirectoryChangesW backend never fired"
    assert hits[0][1] == "readdirectorychanges"


@pytest.mark.skipif(sys.platform != "win32", reason="Win32-only")
def test_readdirchanges_ignores_unrelated_directories(tmp_path: Path):
    """Regression for review finding 8: the original version of this test
    called `stop.set()` with no filesystem event and no CancelIoEx. A
    `threading.Event` cannot interrupt a thread blocked inside
    ReadDirectoryChangesW (verified: the thread was still alive 4s after
    stop.set()), so that version leaked a permanently-blocked thread and
    handle for the rest of the pytest process.

    This version releases the watch for real via `owner.cancel()` (the only
    thing that actually unblocks the kernel call) and proves the thread
    actually exits -- which exercises real cancellation, previously
    untested.
    """
    hits = []
    stop = threading.Event()
    log = tmp_path / "w.jsonl"
    owner = _HandleOwner(*_open_directory_handle(tmp_path.resolve()))
    thread = threading.Thread(
        target=watch_readdirchanges,
        args=(tmp_path.resolve(), lambda r, b: hits.append((r, b)), stop, owner, log),
        daemon=True,
    )
    thread.start()
    time.sleep(0.3)
    (tmp_path / "unrelated").mkdir()
    time.sleep(0.5)
    stop.set()
    owner.cancel()  # actually releases the thread blocked in the kernel call
    thread.join(timeout=5)
    assert not thread.is_alive(), "watch thread survived cancellation"
    assert not hits
    # A clean, stop-driven cancellation must not be logged as an error --
    # nothing at all should have been written to the log.
    assert not log.exists() or not [
        r for r in _rows(log) if r["event"] == "watch_thread_error"
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="Win32-only")
def test_watcher_records_the_backend_it_actually_got(tmp_path: Path):
    """A run must never claim a fast watch it did not get."""
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    root.mkdir()
    watcher = Watcher([root], log=log, secs=0.3, sample_ms=50)
    watcher.run()
    armed = [r for r in _rows(log) if r["event"] == "armed"][0]
    assert armed["backend_by_root"][str(root.resolve())] == "readdirectorychanges"


def test_backend_downgrade_is_recorded_and_armed_agrees(tmp_path: Path, monkeypatch):
    """Coverage for the CRITICAL fix (finding 1/1b).

    Before the fix, `_start_backends` validated a root with a throwaway
    probe-then-close, recorded the fast backend, and only THEN let the
    watch thread open its own, second handle outside any try/finally. If
    that second open failed (TOCTOU), the thread died silently, no
    `backend_downgrade` was recorded, and `armed` still claimed the fast
    backend for a root that was never actually watched -- a fabricated
    clean negative.

    This test forces `_open_directory_handle` to fail and asserts BOTH
    halves agree: a `backend_downgrade` record is written AND
    `armed.backend_by_root` reports "polling" for that root.

    HONEST LIMIT -- do not cite this test as what catches the original bug.
    It passes against the PRE-FIX code too. Its monkeypatch fails EVERY
    call, which the old throwaway probe caught just as correctly as the
    current single open does. The real bug was TOCTOU-shaped -- first open
    succeeds, second fails -- and that shape is no longer expressible,
    because there is now exactly ONE `_open_directory_handle` call site and
    the handle it returns is the one the watch thread uses.

    So the invariant is guaranteed by ARCHITECTURE, not by this test. What
    this test actually defends is the downgrade-and-agree behaviour: that a
    root which cannot get the fast backend is recorded as downgraded and is
    honestly reported as "polling" rather than being claimed as fast. If
    someone ever reintroduces a second open, THIS TEST WILL NOT CATCH IT --
    verify the single call site instead:

        grep -n "_open_directory_handle(" scripts/systemdrive_watcher.py

    Expect exactly TWO lines: the `def` itself, and one call inside
    `_start_backends` feeding `_HandleOwner`. A third line means a second
    open has crept back in and the lying-`armed`-record bug is live again.
    """
    import scripts.systemdrive_watcher as w

    def boom(root):
        raise OSError(5, f"Access is denied: {root}")

    monkeypatch.setattr(w, "_open_directory_handle", boom)
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    root.mkdir()
    watcher = w.Watcher([root], log=log, secs=0.2, poll_ms=20, sample_ms=20)
    watcher.run()

    rows = _rows(log)
    downgrades = [r for r in rows if r["event"] == "backend_downgrade"]
    assert len(downgrades) == 1
    assert downgrades[0]["root"] == str(root.resolve())
    armed = [r for r in rows if r["event"] == "armed"][0]
    assert armed["backend_by_root"][str(root.resolve())] == "polling", (
        "the armed claim and the backend_downgrade record must agree"
    )


class _FakeKernel32:
    """Minimal stand-in for kernel32 so watch_readdirchanges's response
    handling can be unit tested without a real filesystem watch or real
    Win32 handle churn.

    ``responses`` is a list of ``(ok, returned_bytes, last_error_or_None)``
    consumed one per ReadDirectoryChangesW call.
    """

    def __init__(self, responses):
        self._responses = list(responses)

    def ReadDirectoryChangesW(self, handle, buf, size, watch_subtree, filt,
                               returned_ref, overlapped, completion):
        ok, value, err = self._responses.pop(0)
        returned_ref._obj.value = value
        if err is not None:
            ctypes.set_last_error(err)
        return 1 if ok else 0

    def CloseHandle(self, handle):
        return 1

    def CancelIoEx(self, handle, overlapped):
        return 1


def test_watch_readdirchanges_records_buffer_overflow_and_continues(tmp_path: Path):
    """Coverage for finding 3: ReadDirectoryChangesW returning TRUE with
    returned==0 is the documented overflow signal -- events were DROPPED.
    Left alone that is indistinguishable from "nothing happened yet". It
    must be recorded, and the watch must keep running rather than treat it
    as fatal.
    """
    fake = _FakeKernel32([
        (True, 0, None),   # overflow
        (True, 0, None),   # overflow again
        (False, 0, None),  # then the loop notices stop is set (below) and exits cleanly
    ])
    owner = _HandleOwner(fake, 12345)
    stop = threading.Event()
    log = tmp_path / "w.jsonl"
    hits = []
    calls = {"n": 0}
    real_read = fake.ReadDirectoryChangesW

    def counting_read(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            stop.set()  # simulate run()'s shutdown having already fired
        return real_read(*args, **kwargs)

    fake.ReadDirectoryChangesW = counting_read

    watch_readdirchanges(tmp_path.resolve(), lambda r, b: hits.append((r, b)), stop, owner, log)

    rows = _rows(log)
    overflow_rows = [r for r in rows if r["event"] == "watch_buffer_overflow"]
    assert len(overflow_rows) == 2
    assert not hits, "an overflow must never be mistaken for a hit"
    assert not [r for r in rows if r["event"] == "watch_thread_error"], (
        "a clean, stop-driven exit must not be logged as an abnormal failure"
    )


def test_watch_readdirchanges_records_an_unexplained_failure(tmp_path: Path):
    """Coverage for finding 6: ok=False WITHOUT stop being set is a genuine
    mid-run failure, not a clean CancelIoEx-driven shutdown, and the two
    must not be treated identically.
    """
    fake = _FakeKernel32([(False, 0, 6)])  # ERROR_INVALID_HANDLE; stop never set
    owner = _HandleOwner(fake, 12345)
    stop = threading.Event()
    log = tmp_path / "w.jsonl"

    watch_readdirchanges(tmp_path.resolve(), lambda r, b: None, stop, owner, log)

    errors = [r for r in _rows(log) if r["event"] == "watch_thread_error"]
    assert len(errors) == 1
    assert errors[0]["root"] == str(tmp_path.resolve())
    assert errors[0]["backend"] == "readdirectorychanges"


def test_run_drives_a_real_sighting_end_to_end(tmp_path: Path):
    """CRITICAL 3: prove the WIRING, not just the pieces in isolation.

    Every other test here exercises a backend against a bare lambda, or
    calls on_hit() directly. Nothing drove run() itself -- through a real
    backend thread -- to a real SIGHTING. That gap let a disconnected
    `on_hit` (swap it for `lambda r, b: None` in both thread-arg tuples
    inside `_start_backends`) pass the entire suite: 34/34 green with
    detection completely unplugged from recording. See the task brief's
    CRITICAL 3 falsification instructions -- this test was proven to FAIL
    against that exact mutation before being kept.

    Asserts the three things a hunter actually depends on:
    1. a SIGHTING record is produced by run() itself (not by calling on_hit
       directly);
    2. the CRITICAL 1 ring snapshot artifact it names is really on disk with
       readable content;
    3. `done` does not claim a clean NEGATIVE over a real sighting.
    """
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    root.mkdir()
    stop_file = tmp_path / "stop"
    watcher = Watcher(
        [root], log=log, secs=30, poll_ms=20, sample_ms=20,
        stop_file=stop_file, force_polling=True, live_sweep_secs=0.5,
    )
    thread = threading.Thread(target=watcher.run, daemon=True)
    thread.start()
    time.sleep(0.2)  # let the polling backend arm
    (root / JUNK_NAME).mkdir()

    # Wait for the real SIGHTING rather than guessing a sleep duration, then
    # ask the watcher to stop through its normal path so run() returns
    # quickly instead of riding out the 30s `secs` budget.
    deadline = time.monotonic() + 5
    sighted = False
    while time.monotonic() < deadline:
        if log.exists() and any(r["event"] == "SIGHTING" for r in _rows(log)):
            sighted = True
            break
        time.sleep(0.02)
    stop_file.touch()
    thread.join(timeout=15)

    assert not thread.is_alive(), "run() did not return after the stop signal"
    assert sighted, "run() never drove a real SIGHTING -- detection is disconnected from recording"

    rows = _rows(log)
    sightings = [r for r in rows if r["event"] == "SIGHTING"]
    assert len(sightings) == 1
    snapshot_path = Path(sightings[0]["snapshot_file"])
    assert snapshot_path.exists(), "CRITICAL 1's ring snapshot artifact must exist on disk"
    snapshot_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert "ring" in snapshot_data

    done = [r for r in rows if r["event"] == "done"][0]
    assert "NEGATIVE" not in done["note"], "a real sighting must never be reported as a clean negative"


def test_force_polling_is_reflected_in_the_armed_record(tmp_path: Path):
    log = tmp_path / "w.jsonl"
    root = tmp_path / "root"
    root.mkdir()
    watcher = Watcher([root], log=log, secs=0.3, sample_ms=50, force_polling=True)
    watcher.run()
    armed = [r for r in _rows(log) if r["event"] == "armed"][0]
    assert armed["backend_by_root"][str(root.resolve())] == "polling"
