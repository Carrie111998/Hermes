"""Tests for /update gateway slash command.

Tests both the _handle_update_command handler (spawns update process) and
the _send_update_notification startup hook (sends results after restart).
"""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/update", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890", thread_id=None,
                profile=None):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
        thread_id=thread_id,
        profile=profile,
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    """Create a bare GatewayRunner without calling __init__."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._active_profile_name = MagicMock(return_value="work")
    runner._voice_mode = {}
    runner._update_prompt_pending = {}
    runner._schedule_update_notification_watch = MagicMock()
    return runner


# ---------------------------------------------------------------------------
# _handle_update_command
# ---------------------------------------------------------------------------


class TestHandleUpdateCommand:
    """Tests for GatewayRunner._handle_update_command."""

    @pytest.mark.asyncio
    async def test_no_git_directory(self, tmp_path):
        """Returns an error when .git does not exist."""
        runner = _make_runner()
        event = _make_event()
        # Point _hermes_home to tmp_path and project_root to a dir without .git
        fake_root = tmp_path / "project"
        fake_root.mkdir()
        with patch("gateway.run._hermes_home", tmp_path), \
             patch("gateway.run.Path") as MockPath:
            # Path(__file__).parent.parent.resolve() -> fake_root
            MockPath.return_value = MagicMock()
            MockPath.__truediv__ = Path.__truediv__
            # Easier: just patch the __file__ resolution in the method
            pass

        # Simpler approach — mock at method level using a wrapper
        runner = _make_runner()

        with patch("gateway.run._hermes_home", tmp_path):
            # The handler does Path(__file__).parent.parent.resolve()
            # We need to make project_root / '.git' not exist.
            # Since Path(__file__) resolves to the real gateway/run.py,
            # project_root will be the real hermes-agent dir (which HAS .git).
            # Patch Path to control this.
            original_path = Path

            class FakePath(type(Path())):
                pass

            # Actually, simplest: just patch the specific file attr.
            # The _handle_update_command handler lives in gateway/slash_commands.py
            # (extracted from run.py in the god-file decomposition); it resolves
            # project_root via Path(__file__).parent.parent, so fake that file.
            fake_file = str(fake_root / "gateway" / "slash_commands.py")
            (fake_root / "gateway").mkdir(parents=True)
            (fake_root / "gateway" / "slash_commands.py").touch()

            with patch("gateway.slash_commands.__file__", fake_file):
                result = await runner._handle_update_command(event)

        assert "Not a git repository" in result


    @pytest.mark.asyncio
    async def test_resolve_hermes_bin_fallback(self):
        """_resolve_hermes_bin falls back to sys.executable argv when which fails."""
        import sys
        from gateway.run import _resolve_hermes_bin

        fake_spec = MagicMock()
        with patch("shutil.which", return_value=None), \
             patch("importlib.util.find_spec", return_value=fake_spec):
            result = _resolve_hermes_bin()

        assert result == [sys.executable, "-m", "hermes_cli.main"]


    @pytest.mark.asyncio
    @pytest.mark.linux_only
    async def test_writes_pending_marker(self, tmp_path):
        """Writes .update_pending.json with correct platform and chat info."""
        runner = _make_runner()
        event = _make_event(
            platform=Platform.TELEGRAM,
            chat_id="99999",
            profile="work",
        )
        event.message_id = "m-update"

        fake_root = tmp_path / "project"
        fake_root.mkdir()
        (fake_root / ".git").mkdir()
        (fake_root / "gateway").mkdir()
        (fake_root / "gateway" / "run.py").touch()
        fake_file = str(fake_root / "gateway" / "run.py")
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        mock_popen = MagicMock()
        with patch("gateway.run._hermes_home", hermes_home), \
             patch("gateway.run.__file__", fake_file), \
             patch("hermes_cli.runtime_launch.resolve_project_python", return_value="/project/python"), \
             patch("shutil.which", side_effect=lambda x: "/usr/bin/hermes" if x == "hermes" else "/usr/bin/setsid"), \
             patch("subprocess.Popen", mock_popen):
            result = await runner._handle_update_command(event)

        pending_path = hermes_home / ".update_pending.json"
        assert pending_path.exists()
        data = json.loads(pending_path.read_text())
        assert data["platform"] == "telegram"
        assert data["chat_id"] == "99999"
        assert data["chat_type"] == "dm"
        assert data["message_id"] == "m-update"
        assert data["origin_profile"] == "work"
        assert data["profile_home"].endswith("/profiles/work")
        assert data["control_home"] == str(hermes_home.resolve())
        assert data["install_id"]
        assert data["correlation_id"]
        assert "timestamp" in data
        assert not (hermes_home / ".update_exit_code").exists()
        launch_argv = mock_popen.call_args.args[0]
        launch_kwargs = mock_popen.call_args.kwargs
        assert launch_argv[0] == "/usr/bin/setsid"
        assert launch_argv[1] == "/project/python"
        assert "start_new_session" not in launch_kwargs
        assert launch_kwargs["env"]["HERMES_UPDATE_ORIGIN_PROFILE"] == "work"
        assert launch_kwargs["env"]["HERMES_UPDATE_ORIGIN_HOME"] == data["profile_home"]
        assert launch_kwargs["env"]["HERMES_UPDATE_CORRELATION_ID"] == data["correlation_id"]
        assert launch_kwargs["env"]["HERMES_UPDATE_OUTPUT_PATH"] == str(
            (hermes_home / ".update_output.txt").resolve()
        )

    @pytest.mark.asyncio
    @pytest.mark.windows_only
    async def test_windows_launcher_correlates_breakaway_proof(self, tmp_path):
        runner = _make_runner()
        fake_root = tmp_path / "project"
        (fake_root / ".git").mkdir(parents=True)
        (fake_root / "gateway").mkdir()
        fake_file = str(fake_root / "gateway" / "run.py")
        Path(fake_file).touch()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        mock_popen = MagicMock()

        with patch("gateway.run._hermes_home", hermes_home), patch(
            "gateway.run.__file__", fake_file
        ), patch(
            "hermes_cli.runtime_launch.resolve_project_python",
            return_value=r"C:\project\venv\Scripts\python.exe",
        ), patch(
            "hermes_cli._subprocess_compat.windows_detach_popen_kwargs",
            return_value={"creationflags": 0x01000200},
        ), patch(
            "subprocess.Popen", mock_popen
        ):
            await runner._handle_update_command(_make_event())

        launch_env = mock_popen.call_args.kwargs["env"]
        correlation_id = launch_env["HERMES_UPDATE_CORRELATION_ID"]
        assert launch_env["HERMES_UPDATE_WINDOWS_DETACHED"] == correlation_id
        assert mock_popen.call_args.args[0][0] == r"C:\project\venv\Scripts\python.exe"
        assert mock_popen.call_args.args[0][-2:] == ["update", "--gateway"]
        assert "start_new_session" not in mock_popen.call_args.kwargs
        assert mock_popen.call_args.kwargs["creationflags"] == 0x01000200

    @pytest.mark.asyncio
    @pytest.mark.parametrize("profile", ["../escape", "work/name", "tmp"])
    async def test_invalid_origin_profile_is_refused_before_launch(
        self, tmp_path, profile
    ):
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        with patch("gateway.run._hermes_home", hermes_home), patch(
            "subprocess.Popen"
        ) as popen:
            result = await runner._handle_update_command(
                _make_event(profile=profile)
            )

        assert "invalid profile" in result.lower()
        assert not (hermes_home / ".update_pending.json").exists()
        popen.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failing_preparation",
        ["resolve_project_python", "detached_python_env", "popen"],
    )
    async def test_preparation_failure_publishes_no_pending_state(
        self,
        tmp_path,
        failing_preparation,
    ):
        """Fallible launch preparation is owned before pending publication."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        resolver = (
            patch(
                "hermes_cli.runtime_launch.resolve_project_python",
                side_effect=RuntimeError("resolver failed"),
            )
            if failing_preparation == "resolve_project_python"
            else patch(
                "hermes_cli.runtime_launch.resolve_project_python",
                return_value="/project/python",
            )
        )
        environment = (
            patch(
                "hermes_cli.runtime_launch.detached_python_env",
                side_effect=RuntimeError("environment failed"),
            )
            if failing_preparation == "detached_python_env"
            else patch(
                "hermes_cli.runtime_launch.detached_python_env",
                return_value={},
            )
        )

        with patch("gateway.run._hermes_home", hermes_home), resolver, environment, \
             patch("subprocess.Popen") as popen:
            if failing_preparation == "popen":
                popen.side_effect = OSError("spawn failed")
            result = await runner._handle_update_command(_make_event())

        assert "failed" in result.lower()
        if failing_preparation == "popen":
            popen.assert_called_once()
        else:
            popen.assert_not_called()
        assert not (hermes_home / ".update_pending.json").exists()
        assert not (hermes_home / ".update_pending.tmp").exists()
        assert not (hermes_home / ".update_output.txt").exists()
        assert not (hermes_home / ".update_exit_code").exists()


    @pytest.mark.asyncio
    @pytest.mark.linux_only
    async def test_fallback_when_no_setsid(self, tmp_path):
        """Falls back to start_new_session=True when setsid is not available."""
        runner = _make_runner()
        event = _make_event()

        fake_root = tmp_path / "project"
        fake_root.mkdir()
        (fake_root / ".git").mkdir()
        (fake_root / "gateway").mkdir()
        (fake_root / "gateway" / "run.py").touch()
        fake_file = str(fake_root / "gateway" / "run.py")
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        mock_popen = MagicMock()

        def which_no_setsid(x):
            if x == "hermes":
                return "/usr/bin/hermes"
            if x == "setsid":
                return None
            return None

        with patch("gateway.run._hermes_home", hermes_home), \
             patch("gateway.run.__file__", fake_file), \
             patch("hermes_cli.runtime_launch.resolve_project_python", return_value="/project/python"), \
             patch("shutil.which", side_effect=which_no_setsid), \
             patch("subprocess.Popen", mock_popen):
            result = await runner._handle_update_command(event)

        # The fallback uses Python's single detach mechanism, without setsid.
        call_args = mock_popen.call_args[0][0]
        assert call_args[0] == "/project/python"
        assert call_args[1] == "-c"
        assert call_args[-2:] == ["update", "--gateway"]
        assert "rc != 75" in call_args[2]
        assert ".update_exit_code" in call_args[2]
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("start_new_session") is True
        assert call_kwargs["env"]["PYTHONUNBUFFERED"] == "1"
        assert "Starting Hermes update" in result

    @pytest.mark.asyncio
    async def test_duplicate_pending_update_is_inert(self, tmp_path):
        runner = _make_runner()
        event = _make_event()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        original = {"correlation_id": "already-running"}
        (hermes_home / ".update_pending.json").write_text(json.dumps(original))

        with patch("gateway.run._hermes_home", hermes_home), \
             patch("subprocess.Popen") as mock_popen:
            result = await runner._handle_update_command(event)

        assert "already in progress" in result
        assert json.loads((hermes_home / ".update_pending.json").read_text()) == original
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_completed_claimed_action_keeps_slot_until_delivery(self, tmp_path):
        """A terminal-but-undelivered action cannot be overwritten."""

        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        original = {"correlation_id": "claimed-result"}
        claimed = hermes_home / ".update_pending.claimed.json"
        claimed.write_text(json.dumps(original), encoding="utf-8")
        (hermes_home / ".update_exit_code.claimed-result").write_text(
            "0", encoding="utf-8"
        )

        with patch("gateway.run._hermes_home", hermes_home), patch(
            "hermes_cli.runtime_launch.resolve_project_python",
            return_value="/project/python",
        ), patch("subprocess.Popen") as popen:
            result = await runner._handle_update_command(_make_event())

        assert "already in progress" in result
        assert json.loads(claimed.read_text(encoding="utf-8")) == original
        popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_command_boundary_marks_preflight_exit_but_not_handoff(self, tmp_path):
        """The portable boundary covers early exits without faking handoff success."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        mock_popen = MagicMock()

        with patch("gateway.run._hermes_home", hermes_home), \
             patch("hermes_cli.runtime_launch.resolve_project_python", return_value="/project/python"), \
             patch("shutil.which", return_value=None), \
             patch("subprocess.Popen", mock_popen):
            await runner._handle_update_command(_make_event())

        boundary = mock_popen.call_args.args[0][2]
        correlation_id = mock_popen.call_args.kwargs["env"][
            "HERMES_UPDATE_CORRELATION_ID"
        ]
        marker = hermes_home / f".update_exit_code.{correlation_id}"
        boundary_env = {
            "HERMES_HOME": str(hermes_home),
            "HERMES_UPDATE_CORRELATION_ID": correlation_id,
        }
        with patch.dict("os.environ", boundary_env), \
             patch("hermes_cli.main.main", side_effect=SystemExit(2)):
            with pytest.raises(SystemExit) as preflight:
                exec(compile(boundary, "<update-boundary>", "exec"), {})
        assert preflight.value.code == 2
        assert marker.read_text() == "2"

        marker.write_text("9")
        with patch.dict("os.environ", boundary_env), \
             patch("hermes_cli.main.main", side_effect=SystemExit(3)):
            with pytest.raises(SystemExit):
                exec(compile(boundary, "<update-boundary>", "exec"), {})
        assert marker.read_text() == "9"

        marker.unlink()
        with patch.dict("os.environ", boundary_env), \
             patch("hermes_cli.main.main", side_effect=SystemExit(75)):
            with pytest.raises(SystemExit) as handoff:
                exec(compile(boundary, "<update-boundary>", "exec"), {})
        assert handoff.value.code == 75
        assert not marker.exists()


# ---------------------------------------------------------------------------
# Platform allowlist gate
# ---------------------------------------------------------------------------


class TestUpdateCommandPlatformGate:
    """Tests for the platform-allowlist gate at the top of
    ``_handle_update_command``.  Built-in messaging platforms are listed in
    ``_UPDATE_ALLOWED_PLATFORMS``; plugin-migrated platforms (discord,
    mattermost, teams, …) are NOT in the frozenset and rely on the
    registry's ``allow_update_command=True`` fallback.  Programmatic
    interfaces (ACP, API server, webhooks) must be blocked.
    """


    @pytest.mark.asyncio
    async def test_allows_plugin_platform_via_registry_fallback(self, monkeypatch):
        """A plugin-migrated platform (DISCORD) is no longer in
        ``_UPDATE_ALLOWED_PLATFORMS`` but must still pass the gate via
        the registry's ``allow_update_command=True`` flag.

        This test is the empirical guarantee that removing DISCORD from
        the hardcoded frozenset does not regress the /update command for
        Discord users.
        """
        from gateway.run import GatewayRunner

        # Precondition: DISCORD is NOT in the hardcoded set anymore.
        assert Platform.DISCORD not in GatewayRunner._UPDATE_ALLOWED_PLATFORMS

        # Make sure the plugin registry is populated so the fallback fires.
        from hermes_cli.plugins import PluginManager
        PluginManager().discover_and_load(force=True)
        from gateway.platform_registry import platform_registry
        discord_entry = platform_registry.get("discord")
        assert discord_entry is not None
        assert discord_entry.allow_update_command is True

        runner = _make_runner()
        event = _make_event(platform=Platform.DISCORD)
        monkeypatch.setenv("HERMES_MANAGED", "")

        with patch("subprocess.Popen"):
            result = await runner._handle_update_command(event)

        # The gate must NOT have rejected us — anything other than the
        # ``platform_not_messaging`` rejection string is acceptable here.
        # Later steps may legitimately return success ("Starting Hermes
        # update…") or fail for environment reasons.
        assert "only available from messaging platforms" not in result


    @pytest.mark.asyncio
    async def test_allows_homeassistant_via_registry_fallback(self, monkeypatch):
        """Same as DISCORD/MATTERMOST: HOMEASSISTANT is now plugin-migrated
        (PR #40709) and not in the hardcoded frozenset; the registry must
        keep /update working via ``allow_update_command=True``.
        """
        from gateway.run import GatewayRunner

        assert Platform.HOMEASSISTANT not in GatewayRunner._UPDATE_ALLOWED_PLATFORMS

        from hermes_cli.plugins import PluginManager
        PluginManager().discover_and_load(force=True)
        from gateway.platform_registry import platform_registry
        ha_entry = platform_registry.get("homeassistant")
        assert ha_entry is not None
        assert ha_entry.allow_update_command is True

        runner = _make_runner()
        event = _make_event(platform=Platform.HOMEASSISTANT)
        monkeypatch.setenv("HERMES_MANAGED", "")

        with patch("subprocess.Popen"):
            result = await runner._handle_update_command(event)

        assert "only available from messaging platforms" not in result


# ---------------------------------------------------------------------------
# _send_update_notification
# ---------------------------------------------------------------------------


class TestSendUpdateNotification:
    """Tests for GatewayRunner._send_update_notification."""


    @pytest.mark.asyncio
    async def test_defers_notification_while_update_still_running(self, tmp_path):
        """Returns False and keeps marker files when the update has not exited yet."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path = hermes_home / ".update_pending.json"
        pending_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "67890", "user_id": "12345",
        }))
        (hermes_home / ".update_output.txt").write_text("still running")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        assert result is False
        mock_adapter.send.assert_not_called()
        assert pending_path.exists()

    @pytest.mark.asyncio
    async def test_missing_correlated_receipt_reports_terminal_marker(self, tmp_path):
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        pending = {
            "platform": "telegram",
            "chat_id": "67890",
            "session_key": "session-current",
            "correlation_id": "corr-current",
            "origin_profile": "work",
            "profile_home": "/profiles/work",
            "install_id": "install-1",
        }
        (hermes_home / ".update_pending.json").write_text(json.dumps(pending))
        marker = hermes_home / ".update_exit_code.corr-current"
        marker.write_text("2")
        os.utime(marker, (1, 1))
        adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: adapter}

        with patch("gateway.run._hermes_home", hermes_home), \
             patch("hermes_cli.update_receipt.read_receipt_for_correlation", return_value=None):
            delivered = await runner._send_update_notification()

        assert delivered is True
        sent = adapter.send.call_args.args[1]
        assert "terminal marker 2, correlated receipt missing" in sent
        assert "corr-current" in sent
        assert not (hermes_home / ".update_pending.json").exists()

    @pytest.mark.asyncio
    async def test_stale_other_correlation_marker_cannot_finish_current_action(
        self, tmp_path
    ):
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        pending = {
            "platform": "telegram",
            "chat_id": "67890",
            "correlation_id": "current-action",
        }
        pending_path = hermes_home / ".update_pending.json"
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        (hermes_home / ".update_exit_code.old-action").write_text(
            "0", encoding="utf-8"
        )
        (hermes_home / ".update_exit_code").write_text("0", encoding="utf-8")
        adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            delivered = await runner._send_update_notification()

        assert delivered is False
        adapter.send.assert_not_awaited()
        assert pending_path.exists()
        assert (hermes_home / ".update_exit_code.old-action").exists()

    @pytest.mark.asyncio
    async def test_recovers_from_claimed_pending_file(self, tmp_path):
        """A claimed pending file from a crashed notifier is still deliverable."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        claimed_path = hermes_home / ".update_pending.claimed.json"
        claimed_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "67890", "user_id": "12345",
        }))
        (hermes_home / ".update_output.txt").write_text("done")
        (hermes_home / ".update_exit_code").write_text("0")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        assert result is True
        mock_adapter.send.assert_called_once()
        assert not claimed_path.exists()

    @pytest.mark.asyncio
    async def test_sends_notification_with_output(self, tmp_path):
        """Sends update output to the correct platform and chat."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        # Write pending marker
        pending = {
            "platform": "telegram",
            "chat_id": "67890",
            "user_id": "12345",
            "timestamp": "2026-03-04T21:00:00",
        }
        (hermes_home / ".update_pending.json").write_text(json.dumps(pending))
        (hermes_home / ".update_output.txt").write_text(
            "→ Found 3 new commit(s)\n✓ Code updated!\n✓ Update complete!"
        )
        (hermes_home / ".update_exit_code").write_text("0")

        # Mock the adapter
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            await runner._send_update_notification()

        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        assert call_args[0][0] == "67890"  # chat_id
        assert "Update complete" in call_args[0][1] or "update finished" in call_args[0][1].lower()


    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure_mode", ["raises", "returns_false"])
    async def test_send_failure_preserves_markers_for_retry(
        self,
        tmp_path,
        failure_mode,
    ):
        """A transport failure cannot consume the only durable result."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "111", "user_id": "222",
        }))
        output_path.write_text("✓ Done")
        exit_code_path.write_text("0")

        # Adapters report transport failure either by raising or by returning
        # the shared SendResult(success=False) contract.
        mock_adapter = AsyncMock()
        if failure_mode == "raises":
            mock_adapter.send.side_effect = RuntimeError("network error")
        else:
            mock_adapter.send.return_value = SimpleNamespace(success=False)
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            first = await runner._send_update_notification()

        assert first is False
        assert pending_path.exists()
        assert output_path.exists()
        assert exit_code_path.exists()
        assert not (hermes_home / ".update_pending.claimed.json").exists()

        mock_adapter.send.side_effect = None
        mock_adapter.send.return_value = SimpleNamespace(success=True)
        with patch("gateway.run._hermes_home", hermes_home):
            second = await runner._send_update_notification()

        assert second is True
        assert mock_adapter.send.await_count == 2
        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()

    @pytest.mark.asyncio
    async def test_secondary_profile_uses_its_own_adapter(self, tmp_path):
        """Multiplex completion is delivered by the originating bot token."""
        runner = _make_runner()
        runner._active_profile_name = MagicMock(return_value="default")
        primary = AsyncMock()
        secondary = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: primary}
        runner._profile_adapters = {
            "work": {Platform.TELEGRAM: secondary},
        }
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        pending_path = hermes_home / ".update_pending.json"
        pending_path.write_text(json.dumps({
            "platform": "telegram",
            "chat_id": "111",
            "origin_profile": "work",
        }))
        (hermes_home / ".update_exit_code").write_text("0")

        with patch("gateway.run._hermes_home", hermes_home):
            delivered = await runner._send_update_notification()

        assert delivered is True
        secondary.send.assert_awaited_once()
        primary.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_secondary_adapter_never_falls_back_to_primary(self, tmp_path):
        runner = _make_runner()
        runner._active_profile_name = MagicMock(return_value="default")
        primary = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: primary}
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        pending_path = hermes_home / ".update_pending.json"
        pending_path.write_text(json.dumps({
            "platform": "telegram",
            "chat_id": "111",
            "origin_profile": "work",
        }))
        (hermes_home / ".update_exit_code").write_text("0")

        with patch("gateway.run._hermes_home", hermes_home):
            delivered = await runner._send_update_notification()

        assert delivered is False
        primary.send.assert_not_awaited()
        assert pending_path.exists()

    @pytest.mark.asyncio
    async def test_notification_claim_is_exclusive_across_runners(self, tmp_path):
        """Only the process holding the OS claim may send and clean markers."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        pending_path = hermes_home / ".update_pending.json"
        pending_path.write_text(json.dumps({
            "platform": "telegram",
            "chat_id": "111",
        }))
        (hermes_home / ".update_output.txt").write_text("done")
        (hermes_home / ".update_exit_code").write_text("0")

        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingAdapter:
            def __init__(self):
                self.calls = 0

            async def send(self, *args, **kwargs):
                self.calls += 1
                started.set()
                await release.wait()
                return SimpleNamespace(success=True)

        adapter = BlockingAdapter()
        first_runner = _make_runner()
        second_runner = _make_runner()
        first_runner.adapters = {Platform.TELEGRAM: adapter}
        second_runner.adapters = {Platform.TELEGRAM: adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            first_task = asyncio.create_task(
                first_runner._send_update_notification()
            )
            await asyncio.wait_for(started.wait(), timeout=1.0)
            second_result = await second_runner._send_update_notification()
            assert second_result is False
            assert pending_path.exists() or (
                hermes_home / ".update_pending.claimed.json"
            ).exists()
            release.set()
            assert await first_task is True

        assert adapter.calls == 1
        assert not pending_path.exists()


    @pytest.mark.asyncio
    async def test_no_adapter_for_platform_preserves_markers(self, tmp_path):
        """A finished update whose platform is offline keeps its markers.

        When the target platform's adapter has not reconnected yet, dropping
        the completion markers would silently lose the notification. Instead the
        call defers (returns False) and leaves every marker on disk so a later
        retry can deliver once the platform is back.
        """
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending = {"platform": "discord", "chat_id": "111", "user_id": "222"}
        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(json.dumps(pending))
        output_path.write_text("Done")
        exit_code_path.write_text("0")

        # Only telegram adapter available, but pending says discord
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        # No send (wrong platform offline) and the result is deferred.
        assert result is False
        mock_adapter.send.assert_not_called()
        # Markers are preserved for a later retry — NOT cleaned up.
        assert pending_path.exists()
        assert output_path.exists()
        assert exit_code_path.exists()
        # The marker stays in its canonical pending location (claim restored).
        assert not (hermes_home / ".update_pending.claimed.json").exists()

    @pytest.mark.asyncio
    async def test_deferred_notification_delivers_after_reconnect(self, tmp_path):
        """A deferred completion is delivered once the platform reconnects.

        Regression for the late-reconnect /update bug: the update finishes while
        the target platform is offline, the markers survive the deferral, and
        the next call (after the adapter is registered) delivers the result and
        cleans up — exactly once.
        """
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending = {"platform": "discord", "chat_id": "111", "user_id": "222"}
        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(json.dumps(pending))
        output_path.write_text("✓ Update complete!")
        exit_code_path.write_text("0")

        # First pass: target platform (discord) is still offline → defer.
        with patch("gateway.run._hermes_home", hermes_home):
            first = await runner._send_update_notification()

        assert first is False
        assert pending_path.exists()

        # Platform reconnects: the reconnect watcher adds the adapter back.
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.DISCORD: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            second = await runner._send_update_notification()

        assert second is True
        mock_adapter.send.assert_called_once()
        sent_text = mock_adapter.send.call_args[0][1]
        assert "Update complete" in sent_text
        # Now everything is cleaned up — no duplicate deliveries possible.
        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()
        assert not (hermes_home / ".update_pending.claimed.json").exists()

    @pytest.mark.asyncio
    async def test_completion_notification_tolerates_invalid_utf8_output(self, tmp_path):
        """Completion-only update notifications must not crash on bad bytes."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending = {"platform": "discord", "chat_id": "111", "user_id": "222"}
        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(json.dumps(pending))
        output_path.write_bytes(b"ok before\ninvalid byte: \x96\ncontinued after\n")
        exit_code_path.write_text("0")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.DISCORD: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            delivered = await runner._send_update_notification()

        assert delivered is True
        mock_adapter.send.assert_called_once()
        sent_text = mock_adapter.send.call_args[0][1]
        assert "ok before" in sent_text
        assert "invalid byte" in sent_text
        assert "continued after" in sent_text
        assert "Hermes update finished" in sent_text
        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()


# ---------------------------------------------------------------------------
# /update in help and known_commands
# ---------------------------------------------------------------------------


class TestUpdateInHelp:
    """Verify /update appears in help text and known commands set."""


    def test_update_is_known_command(self):
        """The /update command is in the help text (proxy for _known_commands)."""
        # _known_commands is local to _handle_message, so we verify by
        # checking the help output includes it.
        from gateway.run import GatewayRunner
        import inspect
        source = inspect.getsource(GatewayRunner._handle_message)
        assert '"update"' in source

class TestWatchUpdateProgress:
    @pytest.mark.asyncio
    async def test_timeout_is_soft_and_does_not_publish_terminal_marker(self, tmp_path):
        """An independent rollout remains authoritative after the UI timeout."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path = hermes_home / ".update_pending.json"
        pending_path.write_text(json.dumps({
            "platform": "telegram",
            "chat_id": "67890",
            "user_id": "12345",
        }))
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            watch = asyncio.create_task(runner._watch_update_progress(
                poll_interval=0.01,
                stream_interval=0.01,
                timeout=0.03,
            ))
            await asyncio.sleep(0.08)
            assert not watch.done()
            assert pending_path.exists()
            assert not (hermes_home / ".update_exit_code").exists()
            watch.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watch

    @pytest.mark.asyncio
    async def test_timeout_only_fails_exact_dead_worker_with_no_update_lock(
        self, tmp_path
    ):
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        correlation_id = "dead-worker"
        pending_path = hermes_home / ".update_pending.json"
        pending_path.write_text(
            json.dumps(
                {
                    "platform": "telegram",
                    "chat_id": "67890",
                    "correlation_id": correlation_id,
                    "launch_state": "spawned",
                    "launcher_pid": 999_999_999,
                    "launcher_started_at": 1.0,
                }
            ),
            encoding="utf-8",
        )
        adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: adapter}

        with patch("gateway.run._hermes_home", hermes_home), patch(
            "hermes_cli.update_lock.read_live_update", return_value=None
        ), patch.object(
            type(runner), "_update_process_identity_state", return_value=False
        ):
            assert runner._update_worker_definitively_gone(
                json.loads(pending_path.read_text(encoding="utf-8"))
            ) is True
            marker = runner._update_status_path(hermes_home, {
                "correlation_id": correlation_id,
            })
            assert marker is not None
            assert runner._publish_update_status(marker, 1) is True
            assert marker.read_text(encoding="utf-8") == "1"

    def test_orphan_probe_fails_closed_for_live_or_uncertain_owner(self):
        runner = _make_runner()
        pending = {
            "launch_state": "spawned",
            "launcher_pid": 123,
            "launcher_started_at": 1.0,
        }

        with patch(
            "hermes_cli.update_lock.read_live_update",
            return_value=SimpleNamespace(pid=456),
        ), patch.object(
            type(runner), "_update_process_identity_state", return_value=False
        ):
            assert runner._update_worker_definitively_gone(pending) is False

        with patch(
            "hermes_cli.update_lock.read_live_update", return_value=None
        ), patch.object(
            type(runner), "_update_process_identity_state", return_value=None
        ):
            assert runner._update_worker_definitively_gone(pending) is False

    @pytest.mark.asyncio
    async def test_invalid_utf8_update_output_does_not_crash_watcher(self, tmp_path):
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        (hermes_home / ".update_pending.json").write_text(json.dumps({
            "platform": "telegram",
            "chat_id": "67890",
            "user_id": "12345",
        }))
        (hermes_home / ".update_output.txt").write_bytes(
            b"ok before\n\xe2\x9c invalid-continuation: \x96\ncontinued after\n"
        )
        (hermes_home / ".update_exit_code").write_text("0")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            await runner._watch_update_progress(poll_interval=0.01, stream_interval=0.01, timeout=1.0)

        sent = "\n".join(call.args[1] for call in mock_adapter.send.call_args_list)
        assert "ok before" in sent
        assert "continued after" in sent
        assert "Hermes update finished" in sent
        assert not (hermes_home / ".update_pending.json").exists()
