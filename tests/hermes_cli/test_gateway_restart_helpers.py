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

import json
import os
import sys
from pathlib import Path

import pytest

from tests._home_isolation import redirect_home
from gateway import status as gw_status
from hermes_cli import gateway as gw


@pytest.fixture
def isolated_hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a sandbox so cleanup operates on test files.

    Also overrides ``HERMES_GATEWAY_LOCK_DIR``: scope locks are
    MACHINE-LOCAL (``gateway.status._get_lock_dir()``), so without this
    the cleanup under test would glob — and delete from — this box's real
    ``~/.local/state/hermes/gateway-locks``.  The sandbox lock dir is
    deliberately NOT ``HERMES_HOME/gateway-locks``: that path is what the
    cleanup used to glob, and nothing has ever written there.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "machine-locks"))
    return tmp_path


@pytest.fixture
def scope_lock_dir(isolated_hermes_home):
    """The sandboxed machine-local scope-lock directory, created."""
    lock_dir = gw_status._get_lock_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


def _dead_pid() -> int:
    """A PID that is provably not alive right now."""
    for candidate in range(999_999, 999_899, -1):
        if not gw_status._pid_exists(candidate):
            return candidate
    pytest.fail("could not find a dead PID to seed the test with")


def _write_claim(path: Path, pid: int) -> None:
    """Write a scope-lock record in acquire_scoped_lock's on-disk shape."""
    path.write_text(
        json.dumps({
            "pid": pid,
            "start_time": gw_status._get_process_start_time(pid),
            "scope": "whatsapp-session",
            "argv": ["hermes", "gateway", "run"],
        }),
        encoding="utf-8",
    )


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

    def test_removes_stale_per_platform_scope_locks(self, scope_lock_dir):
        # Per-platform session-claim files survive WMI Terminate too.
        # Cleanup globs them so we don't have to enumerate scope names.
        # They live in the machine-local lock dir — the same one
        # acquire_scoped_lock() writes to — not under HERMES_HOME.
        dead = _dead_pid()
        wa_lock = scope_lock_dir / "whatsapp-session-abcdef0123456789.lock"
        tg_lock = scope_lock_dir / "telegram-bot-token-abcdef0123456789.lock"
        _write_claim(wa_lock, dead)
        _write_claim(tg_lock, dead)

        removed = gw.cleanup_gateway_state_files()

        assert wa_lock.name in removed
        assert tg_lock.name in removed
        assert not wa_lock.exists()
        assert not tg_lock.exists()

    def test_preserves_scope_lock_held_by_a_live_process(self, scope_lock_dir):
        # THE regression this guard exists for. Scope locks are written and
        # CLOSED (acquire_scoped_lock -> _write_json_file), so unlike
        # gateway.lock the OS will happily delete a live claim. Deleting a
        # live whatsapp-session claim can force a QR re-pair, and the dir is
        # machine-local, so a restart in one profile must not evict another
        # profile's running gateway. Decide staleness by pid liveness.
        live_lock = scope_lock_dir / "whatsapp-session-0123456789abcdef.lock"
        _write_claim(live_lock, os.getpid())

        removed = gw.cleanup_gateway_state_files()

        assert live_lock.name not in removed
        assert live_lock.exists()

    def test_preserves_scope_lock_with_an_unusable_record(self, scope_lock_dir):
        # Deliberately more conservative than acquire_scoped_lock(), which
        # treats a malformed record as stale and takes it over in-process.
        # There is nothing to win by racing that from here, and guessing
        # wrong deletes a live claim — so leave anything we cannot read a
        # pid out of.
        empty = scope_lock_dir / "telegram-bot-token-1111111111111111.lock"
        garbage = scope_lock_dir / "telegram-bot-token-2222222222222222.lock"
        pidless = scope_lock_dir / "telegram-bot-token-3333333333333333.lock"
        empty.write_text("", encoding="utf-8")
        garbage.write_text("not json {{{", encoding="utf-8")
        pidless.write_text(json.dumps({"scope": "telegram-bot-token"}), encoding="utf-8")

        removed = gw.cleanup_gateway_state_files()

        assert removed == []
        assert empty.exists() and garbage.exists() and pidless.exists()

    def test_ignores_locks_dir_under_hermes_home(self, isolated_hermes_home):
        # HERMES_HOME/gateway-locks is the path cleanup used to glob. Nothing
        # writes there — _get_lock_dir() resolves $HERMES_GATEWAY_LOCK_DIR /
        # $XDG_STATE_HOME/hermes/gateway-locks / ~/.local/state/... — so a
        # file found there is not a session claim and is not ours to delete.
        decoy_dir = isolated_hermes_home / "gateway-locks"
        decoy_dir.mkdir()
        decoy = decoy_dir / "whatsapp-session-abcdef0123456789.lock"
        _write_claim(decoy, _dead_pid())

        removed = gw.cleanup_gateway_state_files()

        assert decoy.name not in removed
        assert decoy.exists()

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
    @pytest.fixture(autouse=True)
    def _isolate_detached_gateway_logs(self, tmp_path, monkeypatch):
        """Keep the detached-spawn log files out of the developer's ~/.claude.

        ``launch_gateway_detached`` opens its child's stdout/stderr at
        ``Path.home() / ".claude" / "logs" / "hermes-gateway.{out,err}.log"``
        (gateway.py ~:1733-1745) BEFORE it calls ``subprocess.Popen`` -- so
        mocking Popen, as every test in this class does, does not stop the
        open. An audit hook caught all three tests mkdir'ing the real
        ``~/.claude/logs`` and opening the two live gateway logs.

        Append mode ("ab") meant nothing was truncated and the mocked Popen
        wrote nothing, so no data was lost -- but the files are real forensic
        logs shared with gateway_watchdog._restart_gateway and
        laptop-start.ps1, and a test has no business holding handles on them.

        ``redirect_home`` works here (unlike the shell-based tilde test in
        tests/tools/test_file_tools_live.py) because this path is resolved by
        ``Path.home()`` inside THIS process.
        """
        redirect_home(monkeypatch, tmp_path)

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
