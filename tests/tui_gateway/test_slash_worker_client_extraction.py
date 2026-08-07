"""R1-S1 extraction tests: tui_gateway/slash_worker_client.py (epic #78647, target #78630).

Consensus test contract (R1-CONSENSUS.md §5, Pass B T1-T8): timeout contract,
closed-pipe drain + invalid-bytes safety, close idempotence ladder, re-export
identity, standalone import, side-effect non-replication, spawn env contract.
The golden-transcript pin (test_tui_gateway_server.py:454) is exercised by the
full server suite; it is not duplicated here.
"""

from __future__ import annotations

import atexit
import json
import queue
import sys
from unittest.mock import MagicMock, patch

import pytest

import tui_gateway.slash_worker_client as swc


# ── T4: re-export identity (the seam) ─────────────────────────────────────────


def test_server_reexports_slash_worker_identity():
    import tui_gateway.server as server

    assert server._SlashWorker is swc.SlashWorker
    assert server._SLASH_WORKER_TIMEOUT_S is swc._SLASH_WORKER_TIMEOUT_S
    assert server._SLASH_WORKER_TIMEOUT_S == max(5.0, swc._slash_timeout)


def test_legacy_private_name_still_importable_from_server():
    # install-time rebind pin: methods_tools.py handler bodies reference the
    # bare name _SlashWorker, rebound onto server globals at install.
    from tui_gateway.server import _SlashWorker

    assert _SlashWorker is swc.SlashWorker


def test_slash_worker_client_imports_no_server():
    # The module must be a pure dependency root (no server import at all).
    import subprocess
    import sys as _sys

    code = (
        "import sys; import tui_gateway.slash_worker_client; "
        "assert not any(m == 'tui_gateway.server' for m in sys.modules)"
    )
    out = subprocess.run(
        [_sys.executable, "-c", code], capture_output=True, text=True, cwd="."
    )
    assert out.returncode == 0, out.stderr
    assert "tui_gateway.server" not in out.stderr


# ── T5: standalone import (no side effects from server import chain) ──────────


def test_standalone_import_no_crash():
    import importlib

    # module is already imported by this test file; force a clean re-import
    # in a fresh subprocess to prove it stands alone.
    import subprocess

    code = "import tui_gateway.slash_worker_client as m; print(m.SlashWorker.__name__)"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd="."
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "SlashWorker"


# ── T6: side-effect non-replication ───────────────────────────────────────────


def test_import_does_not_replicate_server_side_effects():
    """Importing slash_worker_client must not install panic hooks, redirect
    stdout, start the reaper, or register atexit shutdown (server-only)."""
    import threading

    before_hook = sys.excepthook
    before_thread_hook = threading.excepthook
    before_atexit_count = atexit._ncallbacks
    before_stdout = sys.stdout

    import importlib

    importlib.reload(swc)

    assert sys.excepthook is before_hook
    assert threading.excepthook is before_thread_hook
    assert atexit._ncallbacks == before_atexit_count
    assert sys.stdout is before_stdout


# ── T7: spawn env contract ────────────────────────────────────────────────────


def test_init_spawn_env_contract():
    """start_new_session=True, windows_hide_flags() applied, HERMES_HOME override wins."""
    with patch("subprocess.Popen") as mock_popen:
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = MagicMock()
        mock_popen.return_value = proc

        with patch.object(swc, "hermes_subprocess_env", return_value={"BASE": "1"}), patch(
            "hermes_cli._subprocess_compat.windows_hide_flags", return_value=0x8000000
        ), patch("tools.environments.local.build_subprocess_env") as m_build:
            m_build.return_value = {"HERMES_HOME": "/p/home"}
            worker = swc.SlashWorker("key", "model", profile_home="/p/home")

        kwargs = mock_popen.call_args[1]
        assert kwargs["start_new_session"] is True
        assert kwargs["creationflags"] == 0x8000000
        assert kwargs["text"] is True
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["cwd"] is not None
        # argv: python -m tui_gateway.slash_worker --session-key key --model model
        argv = mock_popen.call_args[0][0]
        assert argv[1:4] == ["-m", "tui_gateway.slash_worker", "--session-key"]
        assert argv[4] == "key"
        assert "--model" in argv and argv[argv.index("--model") + 1] == "model"
        assert worker.proc is proc


def test_init_env_hermes_home_override_wins():
    """profile_home must land in env['HERMES_HOME'] via build_subprocess_env's extra."""
    with patch("subprocess.Popen") as mock_popen:
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = MagicMock()
        mock_popen.return_value = proc

        with patch.object(swc, "hermes_subprocess_env", return_value={"BASE": "1"}), patch(
            "tools.environments.local.build_subprocess_env"
        ) as m_build:
            m_build.return_value = {"HERMES_HOME": "/p/home", "OTHER": "x"}
            swc.SlashWorker("key", "model", profile_home="/p/home")

        call = m_build.call_args
        assert call.kwargs.get("extra") == {"HERMES_HOME": "/p/home"}
        assert call.kwargs.get("inherit_profile_home") is False
        assert call.kwargs.get("scrub_secrets") is False
        # base env is the hermes_subprocess_env result (passed positionally)
        assert call.args[0] == {"BASE": "1"}


# ── T1: timeout contract ──────────────────────────────────────────────────────


def _bare_worker():
    worker = object.__new__(swc.SlashWorker)
    worker._lock = MagicMock()
    worker._seq = 0
    worker.proc = MagicMock()
    worker.proc.poll.return_value = None
    worker.proc.stdin = MagicMock()
    worker.stdout_queue = queue.Queue()
    worker.stderr_tail = []
    return worker


def test_run_times_out_with_contract_message(monkeypatch):
    worker = _bare_worker()
    worker.stdout_queue.put({"id": 99})  # never matches rid 1 -> blocks -> timeout

    monkeypatch.setattr(swc, "_SLASH_WORKER_TIMEOUT_S", 0.01)

    with pytest.raises(RuntimeError, match="slash worker timed out"):
        worker.run("echo hi")
    assert worker._seq == 1
    worker.proc.stdin.write.assert_called_once_with(
        json.dumps({"id": 1, "command": "echo hi"}) + "\n"
    )


# ── T2: closed-pipe drain + invalid-bytes safety ──────────────────────────────


def test_run_raises_on_closed_pipe_with_stderr_tail(monkeypatch):
    worker = _bare_worker()
    worker.stderr_tail = ["line1", "line2"]
    worker.stdout_queue.put(None)  # EOF sentinel

    with pytest.raises(RuntimeError, match="slash worker closed pipe"):
        worker.run("cmd")


def test_drain_stdout_ignores_invalid_json_and_terminates(monkeypatch):
    worker = _bare_worker()
    # simulate lines that are NOT valid JSON (e.g. corrupted/GBK-garbled bytes
    # decoded lossily by errors="replace" in the child) — must not raise.
    worker.proc.stdout = iter(["not json at all\n", "{broken\n", '{"id": 1, "ok": true, "output": "fine"}\n'])
    swc.SlashWorker._drain_stdout(worker)
    items = []
    while True:
        item = worker.stdout_queue.get(timeout=1)
        if item is None:
            break
        items.append(item)
    assert items == [{"id": 1, "ok": True, "output": "fine"}]


def test_drain_stderr_keeps_tail_bounded():
    worker = _bare_worker()
    worker.proc.stderr = iter([f"err-{i}\n" for i in range(200)])
    swc.SlashWorker._drain_stderr(worker)
    assert len(worker.stderr_tail) == 80
    assert worker.stderr_tail[-1] == "err-199"
    assert worker.stderr_tail[0] == "err-120"


def test_drain_stderr_skips_blank_lines():
    worker = _bare_worker()
    worker.proc.stderr = iter(["\n", "   \n", "real\n"])
    swc.SlashWorker._drain_stderr(worker)
    # "\n" -> "" is falsy and skipped; "   " is truthy and kept (rstrip only
    # strips the newline, matching the verbatim implementation).
    assert worker.stderr_tail == ["   ", "real"]


# ── T3: close idempotence ladder (terminate → wait → kill → reap) ─────────────


def test_close_terminate_then_kill_ladder():
    worker = _bare_worker()
    proc = worker.proc
    proc.poll.side_effect = [None, None, None]  # running, still running after terminate
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = MagicMock(side_effect=[Exception("timeout"), None])
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()

    swc.SlashWorker.close(worker)

    proc.terminate.assert_called_once()
    assert proc.kill.call_count == 1
    assert proc.wait.call_count == 2  # terminate wait (raised) + kill wait (reap)
    assert worker._closed is True
    # idempotence: second close is a no-op
    proc.terminate.reset_mock()
    swc.SlashWorker.close(worker)
    proc.terminate.assert_not_called()


def test_close_handles_poll_raise_and_closes_streams():
    worker = _bare_worker()
    proc = worker.proc
    proc.poll.side_effect = Exception("poll boom")
    proc.kill = MagicMock()
    proc.wait = MagicMock(return_value=None)
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()

    swc.SlashWorker.close(worker)

    proc.kill.assert_called_once()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        stream.close.assert_called_once()
    assert worker._closed is True


def test_close_skips_when_already_closed():
    worker = _bare_worker()
    worker._closed = True
    swc.SlashWorker.close(worker)
    worker.proc.terminate.assert_not_called()
    worker.proc.kill.assert_not_called()


def test_run_raises_when_proc_exited():
    worker = _bare_worker()
    worker.proc.poll.return_value = 1
    with pytest.raises(RuntimeError, match="slash worker exited"):
        worker.run("cmd")


# ── run happy path ────────────────────────────────────────────────────────────


def test_run_returns_matching_output(monkeypatch):
    worker = _bare_worker()
    worker.stdout_queue.put({"id": 2, "ok": False, "error": "boom"})  # wrong rid -> skipped
    worker.stdout_queue.put({"id": 1, "ok": True, "output": "  result  \n"})

    out = worker.run("cmd")
    assert out == "  result"  # rstrip() strips trailing whitespace only


def test_run_raises_worker_error_payload():
    worker = _bare_worker()
    worker.stdout_queue.put({"id": 1, "ok": False, "error": "command exploded"})
    with pytest.raises(RuntimeError, match="command exploded"):
        worker.run("cmd")
