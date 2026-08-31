"""The TUI gateway must leave a native stack behind when it dies on a fatal signal.

``_log_signal`` only covers signals Python hands to a handler (SIGTERM/SIGHUP).
A *fatal* signal — SIGSEGV from a C extension — kills the interpreter outright,
so without faulthandler the TUI can only report ``child exit signal=SIGSEGV``
with nothing to point at. ``gateway/run.py`` has enabled faulthandler since
#70344; ``tui_gateway/entry.py`` never did, so every crash on the TUI path was
forensically silent.

The subprocess tests are the ones that matter: they drive real signals through a
real interpreter and assert the dump lands in the crash log the TUI already
collects.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap

import pytest


def _run_child(hermes_home, body: str) -> subprocess.CompletedProcess:
    """Import tui_gateway.entry in a fresh interpreter, then run `body`."""
    script = textwrap.dedent(
        """
        import faulthandler, os, signal, threading, time
        import tui_gateway.entry as entry
        """
    ) + textwrap.dedent(body)

    return subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "HERMES_HOME": str(hermes_home)},
        capture_output=True,
        text=True,
        timeout=180,
    )


def _emitted(result: subprocess.CompletedProcess, marker: str) -> bool:
    """Did the child reach `marker`?

    entry.py owns stdout for the JSON-RPC channel and redirects ordinary
    writes to stderr, so a marker can legitimately land on either stream.
    """
    return marker in result.stdout or marker in result.stderr


def _crash_log_text(hermes_home) -> str:
    log = hermes_home / "logs" / "tui_gateway_crash.log"
    assert log.exists(), "no crash log written — faulthandler was not wired up"
    return log.read_text(encoding="utf-8", errors="replace")


def test_fatal_signal_writes_native_stack_to_crash_log(tmp_path):
    """A SIGSEGV must leave an all-thread dump in tui_gateway_crash.log."""
    result = _run_child(
        tmp_path,
        """
        # A second thread proves all_threads=True: the dump must show a frame
        # from a thread other than the one taking the signal, which is what
        # makes these dumps useful for cross-thread teardown races.
        threading.Thread(
            target=lambda: time.sleep(30), daemon=True, name="Bystander"
        ).start()
        time.sleep(0.2)
        faulthandler._sigsegv()
        """,
    )

    # -11 on POSIX; 139 when a shell layer translates it.
    assert result.returncode in (-11, 139), (
        f"expected a fatal SIGSEGV, got {result.returncode}: {result.stderr[-2000:]}"
    )

    text = _crash_log_text(tmp_path)
    assert "Fatal Python error: Segmentation fault" in text
    assert "Bystander" in text or text.count("Thread 0x") >= 2, (
        "dump does not cover non-faulting threads — all_threads was not set"
    )


@pytest.mark.skipif(not hasattr(signal, "SIGUSR2"), reason="POSIX-only signal")
def test_sigusr2_dumps_threads_without_killing_the_gateway(tmp_path):
    """``kill -USR2 <pid>`` must dump and keep serving.

    SIGUSR2's default disposition is "terminate", so registering the dump with
    ``chain=True`` would make the diagnostic kill the session it was meant to
    inspect — the same trap #84539 fixes for the messaging gateway.
    """
    result = _run_child(
        tmp_path,
        """
        os.kill(os.getpid(), signal.SIGUSR2)
        time.sleep(0.4)
        print("SURVIVED")
        """,
    )

    assert result.returncode == 0, (
        f"SIGUSR2 killed the process: rc={result.returncode} {result.stderr[-2000:]}"
    )
    assert _emitted(result, "SURVIVED")
    assert "Current thread" in _crash_log_text(tmp_path)


def test_crash_log_handle_is_retained_and_open(tmp_path):
    """The module-global file handle must outlive ``_enable_faulthandler()``.

    faulthandler keeps writing to this fd for the process lifetime; a local
    handle would be garbage-collected and the next fatal signal would dump
    into a closed fd.
    """
    result = _run_child(
        tmp_path,
        """
        assert faulthandler.is_enabled(), "faulthandler not enabled on import"
        handle = entry._FAULTHANDLER_FILE
        assert handle is not None, "no crash-log handle retained"
        assert not handle.closed, "crash-log handle was closed"
        print("OK")
        """,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert _emitted(result, "OK")


def test_unwritable_crash_log_does_not_break_startup(tmp_path):
    """A gateway that cannot open its crash log must still start and serve.

    Forensics are best-effort; losing them must never cost the user a session.
    """
    blocked = tmp_path / "logs"
    blocked.write_text("not a directory", encoding="utf-8")

    result = _run_child(
        tmp_path,
        """
        # Import above already ran _enable_faulthandler() against the blocked
        # path. Reaching this line at all is the contract.
        print("STARTED")
        """,
    )

    assert result.returncode == 0, (
        f"import died on an unwritable crash log: {result.stderr[-2000:]}"
    )
    assert _emitted(result, "STARTED")
