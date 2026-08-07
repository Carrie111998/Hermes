"""Regression tests for issue #78608.

``SUDO_PASSWORD`` is honoured by the *foreground* terminal path
(``BaseEnvironment._prepare_command`` -> ``_transform_sudo_command``), but the
*background local* path used to hand the raw command straight to
``process_registry.spawn_local()`` — which spawns with ``stdin=DEVNULL`` — so a
background ``sudo`` invocation could never receive its password and failed with
"sudo: a terminal is required to read the password".

The fix:
  1. The background local branch now calls ``_transform_sudo_command`` and
     forwards the resulting ``sudo_stdin`` to ``spawn_local``.
  2. ``spawn_local`` gained an optional ``stdin_data`` parameter that, when set,
     spawns with ``stdin=PIPE`` (Popen path) / writes to the pty handle (PTY
     path) so the password reaches ``sudo -S``.

These tests exercise the *real* ``spawn_local`` subprocess machinery (not a
re-computation of the fix) plus a wiring guard that proves the terminal_tool
background branch actually performs the transform + hand-off.
"""

import os
import shlex
import sys

import pytest

from tools.process_registry import process_registry

_IS_WINDOWS = sys.platform.startswith("win")
_HAS_PTYPROCESS = True
try:  # pragma: no cover - import probe
    import ptyprocess  # noqa: F401
except Exception:  # pragma: no cover
    _HAS_PTYPROCESS = False


def _wait(session, timeout=12.0):
    """Block until the background process finishes (or timeout)."""
    session._completion_event.wait(timeout=timeout)
    return session.exited


def _cleanup(session):
    if not getattr(session, "exited", False):
        try:
            process_registry.kill_process(session.id, source="test_cleanup")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# spawn_local — Popen path: stdin_data must reach the real subprocess
# ---------------------------------------------------------------------------


def test_spawn_local_pipes_stdin_data_to_subprocess(tmp_path):
    """A background local process must receive spawn-time stdin.

    ``cat`` copies its stdin to the redirect target and exits on EOF; if
    ``stdin_data`` is never piped in, the capture file is never written.
    """
    outfile = tmp_path / "stdin_capture.txt"
    session = process_registry.spawn_local(
        command=f"cat > {shlex.quote(str(outfile))}",
        cwd=str(tmp_path),
        task_id="t-78608-pipe",
        stdin_data="hermes_secret_pw\n",
        use_pty=False,
    )
    try:
        assert _wait(session), "background `cat` did not finish in time"
        assert outfile.exists(), (
            "stdin_data was never piped to the subprocess — spawn_local is "
            "still spawning with stdin=DEVNULL (#78608)"
        )
        assert outfile.read_text() == "hermes_secret_pw\n", (
            "piped stdin content mismatch — password did not arrive intact"
        )
    finally:
        _cleanup(session)


def test_spawn_local_without_stdin_data_is_unchanged(tmp_path):
    """No sudo / no stdin_data -> spawn_local keeps stdin=DEVNULL and the
    common (non-sudo) background command runs exactly as before."""
    session = process_registry.spawn_local(
        command="echo nomarker78608",
        cwd=str(tmp_path),
        task_id="t-78608-nostdin",
        use_pty=False,
    )
    try:
        assert _wait(session), "background `echo` did not finish in time"
        assert session.exit_code == 0, (
            f"plain background command failed (rc={session.exit_code}) — "
            "the default stdin=DEVNULL path regressed"
        )
        assert "nomarker78608" in (session.output_buffer or ""), (
            "background output was not captured for the no-stdin case"
        )
    finally:
        _cleanup(session)


# ---------------------------------------------------------------------------
# spawn_local — PTY path: stdin_data must reach the pty handle
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    _IS_WINDOWS or not _HAS_PTYPROCESS,
    reason="PTY stdin write tested only on POSIX with ptyprocess installed",
)
def test_spawn_local_pty_writes_stdin_data(tmp_path):
    """In PTY mode the spawn-time stdin must be written to the pty handle so an
    interactive ``sudo -S`` under a pty also receives its password."""
    outfile = tmp_path / "pty_capture.txt"
    session = process_registry.spawn_local(
        command=f"read line; printf '%s' \"$line\" > {shlex.quote(str(outfile))}",
        cwd=str(tmp_path),
        task_id="t-78608-pty",
        stdin_data="ptysecret\n",
        use_pty=True,
    )
    try:
        # PTY path must actually have been taken.
        assert getattr(session, "_pty", None) is not None, (
            "spawn_local fell back to Popen — PTY path was not exercised"
        )
        assert _wait(session, timeout=15), "PTY `read` did not finish in time"
        assert outfile.exists(), (
            "stdin_data was never written to the pty handle — the PTY spawn "
            "path does not feed spawn-time stdin (#78608)"
        )
        assert outfile.read_text() == "ptysecret"
    finally:
        _cleanup(session)


# ---------------------------------------------------------------------------
# terminal_tool — background local branch must transform + hand off password
# ---------------------------------------------------------------------------


def _bg_sudo_base_config(tmp_path):
    return {
        "env_type": "local",
        "docker_image": "",
        "singularity_image": "",
        "modal_image": "",
        "daytona_image": "",
        "cwd": str(tmp_path),
        "timeout": 30,
    }


def _bg_harness(monkeypatch, tmp_path, captured):
    """Patch terminal_tool enough to reach the background local branch and
    record what ``spawn_local`` is called with."""
    import tools.terminal_tool as terminal_tool_module
    from tools import process_registry as process_registry_module
    from types import SimpleNamespace

    config = _bg_sudo_base_config(tmp_path)

    def fake_spawn_local(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id="proc_78608_wire",
            pid=4242,
            notify_on_complete=False,
            watcher_platform="",
            watcher_chat_id="",
            watcher_user_id="",
            watcher_user_name="",
            watcher_thread_id="",
            watcher_message_id="",
            watcher_interval=0,
        )

    monkeypatch.setattr(terminal_tool_module, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_tool_module,
        "_check_all_guards",
        lambda *_a, **_k: {"approved": True},
    )
    monkeypatch.setattr(
        process_registry_module.process_registry, "spawn_local", fake_spawn_local
    )
    monkeypatch.setitem(
        terminal_tool_module._active_environments, "default", SimpleNamespace(env={})
    )
    monkeypatch.setitem(terminal_tool_module._last_activity, "default", 0.0)
    return terminal_tool_module


def test_background_local_applies_sudo_transform_and_pipes_password(
    monkeypatch, tmp_path
):
    """The background local branch must rewrite ``sudo`` -> ``sudo -S`` and pass
    the password through as ``stdin_data`` to ``spawn_local`` (#78608)."""
    import tools.terminal_tool as terminal_tool_module

    monkeypatch.setenv("SUDO_PASSWORD", "bgpw-test")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    terminal_tool_module._reset_cached_sudo_passwords()

    captured = {}
    tt = _bg_harness(monkeypatch, tmp_path, captured)
    try:
        tt.terminal_tool(command="sudo id", background=True)
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)
        terminal_tool_module._reset_cached_sudo_passwords()

    assert "sudo -S" in captured.get("command", ""), (
        "background local command was not sudo-transformed — the branch is "
        "still handing the raw command to spawn_local (#78608)"
    )
    assert captured.get("stdin_data") == "bgpw-test\n", (
        "sudo password was not forwarded as stdin_data — spawn_local received "
        f"stdin_data={captured.get('stdin_data')!r}"
    )


def test_background_local_without_sudo_passes_no_stdin_data(monkeypatch, tmp_path):
    """A non-sudo background command must NOT inject stdin_data (no spurious
    password piping / no behaviour change for the common case)."""
    import tools.terminal_tool as terminal_tool_module

    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    terminal_tool_module._reset_cached_sudo_passwords()

    captured = {}
    tt = _bg_harness(monkeypatch, tmp_path, captured)
    try:
        tt.terminal_tool(
            command="echo plainbg78608", background=True, notify_on_complete=True
        )
    finally:
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)
        terminal_tool_module._reset_cached_sudo_passwords()

    assert captured.get("stdin_data") in (None, ""), (
        "non-sudo background command must not carry stdin_data"
    )
    assert "sudo -S" not in captured.get("command", ""), (
        "non-sudo command should not be rewritten"
    )
