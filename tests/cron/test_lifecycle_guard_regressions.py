from pathlib import Path
import sys

from cron.lifecycle_guard import (
    _contains_unsafe_gateway_action,
    contains_gateway_lifecycle_command_or_referenced_script,
)
from gateway.session_context import HERMES_CRON_SESSION_CONTEXTVAR
from tools import approval


def test_full_path_non_shell_binary_is_not_scanned(tmp_path: Path) -> None:
    binary = tmp_path / "python3"
    binary.write_bytes(b"\x00" * 4096)

    command = f'{binary} -c "print(1)"'

    assert not contains_gateway_lifecycle_command_or_referenced_script(command, cwd=str(tmp_path))
    assert not _contains_unsafe_gateway_action(
        command, cwd=str(tmp_path), depth=0, visited=set()
    )


def test_extensionless_text_script_without_shebang_is_scanned(tmp_path: Path) -> None:
    script = tmp_path / "restart-helper"
    script.write_text("hermes gateway restart\n")

    assert _contains_unsafe_gateway_action(
        str(script), cwd=".", depth=0, visited=set()
    )


def test_shell_script_reference_is_still_scanned(tmp_path: Path) -> None:
    script = tmp_path / "script.sh"
    script.write_text("#!/bin/sh\nhermes gateway restart\n")

    assert contains_gateway_lifecycle_command_or_referenced_script(
        f"./{script.name}", cwd=str(tmp_path)
    )


def test_actual_python_binary_is_not_scanned() -> None:
    command = f'{sys.executable} -c "print(1)"'

    assert not _contains_unsafe_gateway_action(
        command, cwd=".", depth=0, visited=set()
    )


def test_dot_source_and_source_script_references_are_scanned(tmp_path: Path) -> None:
    script = tmp_path / "evil.sh"
    script.write_text("#!/bin/sh\nhermes gateway restart\n")

    for prefix in (".", "source"):
        assert _contains_unsafe_gateway_action(
            f"{prefix} {script}", cwd=".", depth=0, visited=set()
        )


def test_bash_script_reference_is_scanned(tmp_path: Path) -> None:
    script = tmp_path / "evil.sh"
    script.write_text("#!/bin/sh\nhermes gateway restart\n")

    assert _contains_unsafe_gateway_action(
        f"bash {script}", cwd=".", depth=0, visited=set()
    )


def test_large_extensionless_shell_script_is_scanned(tmp_path: Path) -> None:
    script = tmp_path / "large-script"
    script.write_text("#!/bin/sh\n" + ("# " + "x" * 5000 + "\n") + "hermes gateway restart\n")

    assert _contains_unsafe_gateway_action(
        str(script), cwd=".", depth=0, visited=set()
    )


def test_embedded_null_path_does_not_raise() -> None:
    command = "./script\x00.sh"

    assert not _contains_unsafe_gateway_action(
        command, cwd="/tmp", depth=0, visited=set()
    )


def test_cron_session_contextvar_takes_precedence(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    token = HERMES_CRON_SESSION_CONTEXTVAR.set(True)
    try:
        assert approval._is_cron_session()
    finally:
        HERMES_CRON_SESSION_CONTEXTVAR.reset(token)


def test_cron_session_contextvar_reset_restores_false(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    token = HERMES_CRON_SESSION_CONTEXTVAR.set(True)
    try:
        assert approval._is_cron_session() is True
    finally:
        HERMES_CRON_SESSION_CONTEXTVAR.reset(token)

    assert approval._is_cron_session() is False


def test_cron_session_is_false_without_context_or_env(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)

    assert not approval._is_cron_session()


def test_cron_session_env_fallback(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")

    assert approval._is_cron_session()


def test_run_job_exception_releases_lock_and_resets_cron_flag(tmp_path, monkeypatch) -> None:
    """The real scheduler cleanup path releases its lock and ContextVar."""
    import threading
    from unittest.mock import MagicMock, patch

    import cron.scheduler as scheduler

    workdir = tmp_path / "cron-workdir"
    workdir.mkdir()
    job = {"id": "r3-test", "name": "cleanup", "prompt": "hi", "workdir": str(workdir)}

    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    assert HERMES_CRON_SESSION_CONTEXTVAR.get() is False

    # Check the writer lock is available before invoking run_job, then leave it
    # free for run_job to acquire.
    scheduler._terminal_cwd_lock.acquire_write()
    scheduler._terminal_cwd_lock.release_write()

    real_info = scheduler.logger.info

    def raise_on_workdir_log(message, *args, **kwargs):
        if isinstance(message, str) and "using workdir" in message:
            raise RuntimeError("r3 cleanup probe")
        return real_info(message, *args, **kwargs)

    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch.object(scheduler.logger, "info", side_effect=raise_on_workdir_log), \
         patch("hermes_state.SessionDB", return_value=MagicMock()):
        result = scheduler.run_job(job)

    assert result[0] is False
    assert HERMES_CRON_SESSION_CONTEXTVAR.get() is False

    # A leaked writer would block this acquisition indefinitely; the lock test
    # uses the same bounded thread pattern for this synchronization primitive.
    acquired = threading.Event()

    def acquire_and_release() -> None:
        scheduler._terminal_cwd_lock.acquire_write()
        try:
            acquired.set()
        finally:
            scheduler._terminal_cwd_lock.release_write()

    thread = threading.Thread(target=acquire_and_release, daemon=True)
    thread.start()
    assert acquired.wait(timeout=1), "writer lock was leaked by run_job"
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_run_job_handoff_propagates_cron_context_and_isolates_concurrent_thread(
    tmp_path, monkeypatch
) -> None:
    import threading
    from unittest.mock import MagicMock, patch

    import cron.scheduler as scheduler

    workdir = tmp_path / "cron-workdir"
    workdir.mkdir()
    job = {
        "id": "r2-handoff",
        "name": "handoff",
        "prompt": "probe",
        "workdir": str(workdir),
    }
    worker_probe = []
    concurrent_probe = []

    def probe_run_conversation(*args, **kwargs):
        worker_probe.append(HERMES_CRON_SESSION_CONTEXTVAR.get())

        def probe_concurrent_thread() -> None:
            concurrent_probe.append(HERMES_CRON_SESSION_CONTEXTVAR.get())

        thread = threading.Thread(target=probe_concurrent_thread)
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()
        raise RuntimeError("r2 handoff probe")

    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    assert HERMES_CRON_SESSION_CONTEXTVAR.get() is False
    scheduler._terminal_cwd_lock.acquire_write()
    scheduler._terminal_cwd_lock.release_write()

    agent = MagicMock()
    agent.run_conversation.side_effect = probe_run_conversation
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch(
             "hermes_cli.runtime_provider.resolve_runtime_provider",
             return_value={"provider": "test", "model": "test", "api_key": "key"},
         ), \
         patch("hermes_state.SessionDB", return_value=MagicMock()), \
         patch("run_agent.AIAgent", return_value=agent):
        result = scheduler.run_job(job)

    assert result[0] is False
    assert worker_probe == [True]
    assert concurrent_probe == [False]
    assert HERMES_CRON_SESSION_CONTEXTVAR.get() is False
