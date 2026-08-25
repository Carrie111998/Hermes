"""Tests for gateway.pre_mortem — UNCLEANLY-death forensics (Windows gateway).

The gateway has been dying UNCLEANLY every few hours on this Windows 11
install (gateway-exit-diag.log shows 22 such events since July 30), and the
lifecycle ledger's ``gateway.previous_unclean_exit`` records prove the
death is real but cannot identify the killer — ``TerminateProcess`` and
``SIGKILL`` bypass every Python handler.  :mod:`gateway.pre_mortem` is the
defensive instrument that captures the cause: a SIGTERM/SIGBREAK/SIGINT
handler that writes a JSONL record before re-raising the signal, plus a
parent-PID watcher thread that records re-parenting events.

These tests lock in the contracts:

* install is idempotent — a second call is a no-op (the module-level
  ``_INSTALLED`` guard).
* simulate_terminate writes a well-formed JSONL record with the keys
  the on-call triage script greps for.
* the path resolution follows ``HERMES_HOME`` env var with the
  ``hermes_constants.get_hermes_home()`` fallback.
* the parent-watcher thread is daemon=True so a forgotten stop can't
  block process exit.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------


def _import_module():
    """Lazy import — keeps pytest collection fast and avoids the import
    side effects of loading all of gateway/ at collection time."""
    from gateway import pre_mortem
    # Reset the idempotency guard so each test starts fresh.  This is a
    # *test-only* mutation; production never relies on resetting it.
    pre_mortem._INSTALLED = False
    pre_mortem._ORIGINAL_HANDLERS.clear()
    return pre_mortem


# ---------------------------------------------------------------------------
# JSONL record shape
# ---------------------------------------------------------------------------


def test_simulate_terminate_writes_expected_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The synthesized record must contain every key the triage script greps for."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pre_mortem = _import_module()

    pre_mortem.simulate_terminate(reason="unit_test")

    records = _read_diag(tmp_path)
    assert len(records) == 1, "simulate_terminate should write exactly one record"
    rec = records[0]

    # Mandatory keys the on-call triage script reads
    assert rec["tag"] == "pre_mortem.simulated"
    assert rec["reason"] == "unit_test"
    assert rec["pid"] == os.getpid()
    assert rec["platform"] == sys.platform
    assert isinstance(rec["ts"], str) and rec["ts"]


def test_simulate_terminate_includes_process_context_when_psutil_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """psutil-derived context (ppid + current_user) shows up when the lib is importable."""
    pytest.importorskip("psutil")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pre_mortem = _import_module()
    pre_mortem.simulate_terminate(reason="ctx_check")

    rec = _read_diag(tmp_path)[0]
    assert "ppid" in rec, "ppid missing — psutil ran but didn't populate"
    assert rec["ppid"] > 0
    # current_user is set best-effort; psutil on every supported OS can
    # resolve it for our own process.
    assert "current_user" in rec


def test_simulate_terminate_writes_to_hermes_home_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file path is <HERMES_HOME>/logs/gateway-exit-diag.log, not cwd."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pre_mortem = _import_module()
    pre_mortem.simulate_terminate(reason="path_check")

    expected = tmp_path / "logs" / "gateway-exit-diag.log"
    assert expected.exists(), f"expected {expected}"
    # Nothing leaked into the HERMES_HOME root itself (or its siblings).
    assert list((tmp_path).glob("gateway-exit-diag.log*")) == []


def test_simulate_terminate_appends_to_existing_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-existing exit-diag log (from the lifecycle_ledger or the CLI) is appended, not overwritten."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pre_mortem = _import_module()

    log = tmp_path / "logs" / "gateway-exit-diag.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps({"ts": "2026-08-09T22:00:00+00:00", "tag": "gateway.start", "pid": 1, "platform": "win32"}) + "\n",
        encoding="utf-8",
    )

    pre_mortem.simulate_terminate(reason="append_check")

    records = _read_diag(tmp_path)
    tags = [r["tag"] for r in records]
    assert tags == ["gateway.start", "pre_mortem.simulated"], tags


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_install_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two install calls in the same process record exactly one ``installed`` line."""
    pre_mortem = _import_module()
    # Re-point HERMES_HOME so the install can write a real log.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("HERMES_HOME", td)
        assert pre_mortem.install_pre_mortem_handlers(interval_s=0.5) is True
        assert pre_mortem.install_pre_mortem_handlers(interval_s=0.5) is False
        assert pre_mortem.is_installed() is True

        records = _read_diag(Path(td))
        install_records = [r for r in records if r["tag"] == "pre_mortem.installed"]
        assert len(install_records) == 1, "second install must not write another installed record"

        # Restore original SIGTERM handler so the test process is not
        # left wired for kill — pytest cleanup would be sad otherwise.
        # Skip on POSIX where SIGTERM was already the default; restore is
        # only safe when we actually replaced the handler.
        if signal.SIGTERM in pre_mortem._ORIGINAL_HANDLERS:
            signal.signal(signal.SIGTERM, pre_mortem._ORIGINAL_HANDLERS[signal.SIGTERM])
        if signal.SIGINT in pre_mortem._ORIGINAL_HANDLERS:
            signal.signal(signal.SIGINT, pre_mortem._ORIGINAL_HANDLERS[signal.SIGINT])


# ---------------------------------------------------------------------------
# Parent watcher thread
# ---------------------------------------------------------------------------


def test_parent_watcher_thread_is_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """A forgotten watcher must NOT block interpreter shutdown."""
    pytest.importorskip("psutil")
    pre_mortem = _import_module()
    # Reach into the thread class — we don't need to run start_gateway to
    # instantiate one, the contract is just that it's daemon=True.
    t = pre_mortem._ParentWatcher(interval_s=0.1)
    assert t.daemon is True, "watcher must be daemon so process exit is never blocked"
    assert t.name == "gateway-pre-mortem-parent-watcher"


def test_parent_watcher_records_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """First sample establishes the baseline — even if no change ever happens, we get a record."""
    pytest.importorskip("psutil")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("HERMES_HOME", td)
        pre_mortem = _import_module()
        watcher = pre_mortem._ParentWatcher(interval_s=0.1)
        watcher.start()
        try:
            # First sample is synchronous inside run(); give the thread
            # a brief moment to write.
            time.sleep(0.5)
        finally:
            watcher.stop()
            watcher.join(timeout=2.0)

        records = _read_diag(Path(td))
        baseline = [r for r in records if r["tag"] == "pre_mortem.parent_baseline"]
        assert len(baseline) == 1, f"expected exactly one baseline, got {len(baseline)}"
        assert baseline[0]["ppid"] > 0


# ---------------------------------------------------------------------------
# Signal handler — re-raise must terminate, not re-enter
# ---------------------------------------------------------------------------


# The handler is only exercisable in a child process: a correct one kills
# the interpreter it runs in, so it can't be called from inside pytest.
_SIGNAL_CHILD = """\
import os, signal, sys
sys.path.insert(0, {root!r})
os.environ["HERMES_HOME"] = {home!r}
from gateway import pre_mortem
# Huge interval so the watcher thread never samples during the test.
pre_mortem.install_pre_mortem_handlers(interval_s=99999.0)
signal.raise_signal(getattr(signal, {signame!r}))
# Reaching this line means the handler returned instead of terminating.
sys.exit(99)
"""


@pytest.mark.parametrize(
    "signame",
    ["SIGTERM", "SIGINT"] + (["SIGBREAK"] if sys.platform == "win32" else []),
)
def test_signal_handler_terminates_without_re_entering(
    signame: str, tmp_path: Path
) -> None:
    """Re-raising must kill us, not recurse back into the handler.

    A handler is not reset on entry, so ``signal.raise_signal(signum)``
    from inside one re-delivers to *itself*.  Before the disarm was added,
    a single signal re-entered until the interpreter hit its recursion
    limit: the child died with ``RecursionError`` and wrote 495 duplicate
    ``pre_mortem.signal`` records, flooding the exact diagnostic log this
    module exists to keep readable — and destroying the by-signal death
    this module's docstring says it deliberately preserves.

    Exactly one record and a signal-terminated exit are the contract.
    """
    import subprocess

    root = str(Path(__file__).resolve().parents[2])
    code = _SIGNAL_CHILD.format(root=root, home=str(tmp_path), signame=signame)
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )

    assert proc.returncode != 99, "handler returned — the process survived the signal"
    assert proc.returncode != 0, f"expected death by signal, got clean exit\n{proc.stderr}"
    assert "RecursionError" not in proc.stderr, (
        f"handler re-entered itself:\n{proc.stderr[-2000:]}"
    )

    records = _read_diag(tmp_path)
    sig_records = [r for r in records if r["tag"] == "pre_mortem.signal"]
    assert len(sig_records) == 1, (
        f"expected exactly 1 pre_mortem.signal record, got {len(sig_records)} "
        "— more than one means the handler re-entered"
    )
    assert sig_records[0]["signal_name"] == signame


def test_signal_handler_embeds_stacks_without_breaking_jsonl(tmp_path: Path) -> None:
    """Stacks must land *inside* the record, keeping the log valid JSONL.

    Two failure modes are pinned here at once.  The stacks have to be
    captured at all — the gateway runs under ``pythonw.exe`` with no
    console, so the previous stderr-bound ``faulthandler.dump_traceback()``
    lost them entirely.  And they have to be captured as a *field*: this
    log is strict JSONL shared with the CLI's ``_exit_diag`` writer, so a
    raw multi-line traceback appended beside the records breaks every
    reader of the file, including :func:`_read_diag` below.
    """
    import subprocess

    root = str(Path(__file__).resolve().parents[2])
    code = _SIGNAL_CHILD.format(root=root, home=str(tmp_path), signame="SIGTERM")
    subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)

    # _read_diag json.loads() every line — it raises if the log is corrupt.
    records = _read_diag(tmp_path)
    sig = [r for r in records if r["tag"] == "pre_mortem.signal"][0]

    stacks = sig.get("stacks")
    assert isinstance(stacks, dict) and stacks, f"no stacks captured: {sig!r}"
    # The main thread is always present; frames are lists of source lines.
    joined = "\n".join(line for frames in stacks.values() for line in frames)
    assert "line" in joined and ".py" in joined, f"stacks look empty: {stacks!r}"


def test_concurrent_records_do_not_tear_the_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two writers must never split a record across lines.

    This module has two concurrent writers by design: the parent-watcher
    thread and the main thread's signal handler.  A single ``os.write`` to
    an O_APPEND fd is only indivisible up to a modest size, so once records
    carry stacks the two interleave and a record lands half on one line and
    half on the next — which corrupts the log for every reader, including
    :func:`_read_diag`.  Measured before the lock: 17 of 150 runs.

    Sized to the real payload (stack captures), not a toy string.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pre_mortem = _import_module()

    payload = {f"frame_{i}": "x" * 120 for i in range(12)}
    errors: list[BaseException] = []

    def hammer(n: int) -> None:
        try:
            for i in range(40):
                pre_mortem._record(f"pre_mortem.stress_{n}", seq=i, **payload)
        except BaseException as exc:  # pragma: no cover - defensive
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"_record raised under concurrency: {errors!r}"

    # Every line must be valid JSON — _read_diag raises otherwise.
    records = _read_diag(tmp_path)
    assert len(records) == 6 * 40, (
        f"expected 240 intact records, got {len(records)} — records were lost "
        "or merged"
    )


def test_capture_stacks_stays_bounded() -> None:
    """A pre-mortem record must stay small enough to append atomically.

    Unbounded stacks are what pushed records past the size a single write
    keeps indivisible.  The caps also keep the diag log skimmable — a log
    nobody can read is not evidence.
    """
    pre_mortem = _import_module()
    stacks = pre_mortem._capture_stacks()

    assert stacks, "expected at least this thread's stack"
    assert len(stacks) <= pre_mortem._MAX_STACK_THREADS + 1  # +1 for the marker
    for label, frames in stacks.items():
        if label == "_truncated_threads":
            continue
        assert len(frames) <= pre_mortem._MAX_STACK_FRAMES + 1, (
            f"{label} kept {len(frames)} frames"
        )
        for line in frames:
            assert len(line) <= pre_mortem._MAX_STACK_LINE_CHARS

    encoded = len(json.dumps({"stacks": stacks}, default=str))
    assert encoded < 60_000, f"stacks serialize to {encoded} bytes — too large"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_diag(home: Path) -> list[dict]:
    path = home / "logs" / "gateway-exit-diag.log"
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out