"""M3 hermes gateway restart helpers — lockfile cleanup + detached launch.

Spec: profiles/sentinel/workspace/gateway-restart-cluster-2026-04-30.md.

The gateway restart subcommand's manual-restart fallback path (no
systemd / launchd) was holding the operator's terminal in foreground
and not cleaning up surviving lockfiles between stop and start. M3 adds
two helpers to fix both: cleanup_gateway_state_files removes the
orphaned PID + lock files for the current profile, and
launch_gateway_detached spawns ``hermes gateway run`` in a way that
survives the parent shell closing (CREATE_NEW_PROCESS_GROUP +
DETACHED_PROCESS on Windows, start_new_session on POSIX).

These are unit tests for both helpers in isolation. The end-to-end
restart-subcommand path is covered by the existing manual_gateway_*
fixtures elsewhere; the helpers here are the new additions.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import gateway as gw


@pytest.fixture
def isolated_hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a sandbox so cleanup operates on test files."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


class TestCleanupGatewayStateFiles:
    def test_removes_pid_and_lock_files(self, isolated_hermes_home):
        # Seed the surviving artefacts a non-graceful shutdown would leave.
        pid_path = isolated_hermes_home / "gateway.pid"
        lock_path = isolated_hermes_home / "gateway.lock"
        pid_path.write_text('{"pid": 99999}')
        lock_path.write_text("{}")

        removed = gw.cleanup_gateway_state_files()

        assert "gateway.pid" in removed
        assert "gateway.lock" in removed
        assert not pid_path.exists()
        assert not lock_path.exists()

    def test_removes_per_platform_scope_locks(self, isolated_hermes_home):
        # Per-platform session-claim files survive WMI Terminate too.
        # Cleanup globs them so we don't have to enumerate scope names.
        locks_dir = isolated_hermes_home / "gateway-locks"
        locks_dir.mkdir()
        wa_lock = locks_dir / "whatsapp-session-abcdef0123456789.lock"
        tg_lock = locks_dir / "telegram-bot-token-abcdef0123456789.lock"
        wa_lock.write_text("{}")
        tg_lock.write_text("{}")

        removed = gw.cleanup_gateway_state_files()

        assert wa_lock.name in removed
        assert tg_lock.name in removed
        assert not wa_lock.exists()
        assert not tg_lock.exists()

    def test_returns_empty_when_no_state_files(self, isolated_hermes_home):
        # Clean slate — nothing to remove. Must not raise.
        removed = gw.cleanup_gateway_state_files()
        assert removed == []

    def test_swallows_permission_error(self, isolated_hermes_home, monkeypatch):
        # If a single file fails to unlink (file in use, permission denied),
        # the cleanup must continue with the remaining candidates rather
        # than aborting partway. Simulate by patching Path.unlink to raise
        # OSError for the pid file but succeed for the lock file.
        pid_path = isolated_hermes_home / "gateway.pid"
        lock_path = isolated_hermes_home / "gateway.lock"
        pid_path.write_text('{"pid": 99999}')
        lock_path.write_text("{}")

        original_unlink = Path.unlink
        def selective_unlink(self, *args, **kwargs):
            if self.name == "gateway.pid":
                raise PermissionError("locked by another process")
            return original_unlink(self, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", selective_unlink)

        removed = gw.cleanup_gateway_state_files()

        assert "gateway.lock" in removed  # the cleanup continued
        assert "gateway.pid" not in removed  # this one failed
        assert not lock_path.exists()  # lock did get removed


class TestLaunchGatewayDetached:
    def test_invokes_subprocess_popen_with_detach_flags(self, monkeypatch):
        """Smoke test: the helper must call subprocess.Popen with the
        ``hermes gateway run`` argv plus platform-appropriate detach
        flags. We don't care about the actual process spawn here — the
        new gateway end-to-end is covered by the run-gateway integration
        tests."""
        captured: dict = {}

        class FakeProc:
            pid = 12345

        def fake_popen(cmd, *, stdin, stdout, stderr, creationflags=0,
                       start_new_session=False, close_fds):
            captured["cmd"] = cmd
            captured["creationflags"] = creationflags
            captured["start_new_session"] = start_new_session
            captured["close_fds"] = close_fds
            return FakeProc()

        monkeypatch.setattr(gw.subprocess, "Popen", fake_popen)
        # Pin sys.argv[0] to a recognisable string so the helper picks it
        # up as the cli_entry. Use a fake path that exists.
        fake_hermes = Path(__file__).parent / "hermes_fake_entry.py"
        fake_hermes.write_text("# fake")
        try:
            monkeypatch.setattr(gw.sys, "argv", [str(fake_hermes)])
            pid = gw.launch_gateway_detached()
        finally:
            fake_hermes.unlink(missing_ok=True)

        assert pid == 12345
        assert captured["cmd"][0] == str(fake_hermes)
        assert captured["cmd"][1:] == ["gateway", "run"]
        assert captured["close_fds"] is True
        if sys.platform == "win32":
            # Both flags must be set: CREATE_NEW_PROCESS_GROUP=0x200,
            # DETACHED_PROCESS=0x8
            assert captured["creationflags"] & 0x200 == 0x200
            assert captured["creationflags"] & 0x008 == 0x008
            assert captured["start_new_session"] is False
        else:
            assert captured["creationflags"] == 0
            assert captured["start_new_session"] is True

    def test_appends_extra_args(self, monkeypatch):
        """Caller-supplied extras (e.g. --replace) must trail the
        canonical ``gateway run`` tokens so argparse's positional
        consumption stays correct."""
        captured: dict = {}

        class FakeProc:
            pid = 1

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return FakeProc()

        monkeypatch.setattr(gw.subprocess, "Popen", fake_popen)
        gw.launch_gateway_detached(extra_args=["--replace", "-v"])
        assert captured["cmd"][-3:] == ["gateway", "run", "--replace"] or (
            captured["cmd"][-2:] == ["--replace", "-v"]
        )
        assert "--replace" in captured["cmd"]
        assert "-v" in captured["cmd"]

    def test_returns_none_on_launch_failure(self, monkeypatch):
        """Spawn errors must surface as None so the caller's restart
        flow can sys.exit(1) cleanly rather than swallowing silently."""
        def fake_popen(*args, **kwargs):
            raise OSError("fork failed")
        monkeypatch.setattr(gw.subprocess, "Popen", fake_popen)
        pid = gw.launch_gateway_detached()
        assert pid is None
