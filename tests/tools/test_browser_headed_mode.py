"""Tests for headed browser mode: config/env resolution, --headed injection,
the per-turn cleanup skip that keeps headed sessions alive between turns,
and the actionable-error / Xvfb-fallback path when headed is requested on a
host without a display server (issue #94827).

Salvaged from PR #24064 (fixes #11020 lead bug).
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _reset_headed_cache():
    """Reset the module-level headed-mode cache so tests start clean."""
    import tools.browser_tool as bt
    bt._cached_headed_mode = None
    bt._headed_mode_resolved = False


@pytest.fixture(autouse=True)
def _clean_headed_cache():
    _reset_headed_cache()
    yield
    _reset_headed_cache()


# ---------------------------------------------------------------------------
# _is_headed_mode resolution
# ---------------------------------------------------------------------------

class TestIsHeadedMode:
    def test_default_is_false(self):
        from tools.browser_tool import _is_headed_mode
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_BROWSER_HEADED", None)
            with patch("hermes_cli.config.read_raw_config", return_value={}):
                assert _is_headed_mode() is False

    def test_config_true(self):
        from tools.browser_tool import _is_headed_mode
        cfg = {"browser": {"headed": True}}
        with patch("hermes_cli.config.read_raw_config", return_value=cfg):
            assert _is_headed_mode() is True


    def test_caching(self):
        from tools.browser_tool import _is_headed_mode
        cfg = {"browser": {"headed": True}}
        with patch("hermes_cli.config.read_raw_config", return_value=cfg) as mock_read:
            assert _is_headed_mode() is True
            assert _is_headed_mode() is True
            assert mock_read.call_count == 1


# ---------------------------------------------------------------------------
# Per-turn cleanup skip (agent/chat_completion_helpers.cleanup_task_resources)
# ---------------------------------------------------------------------------

def _make_agent(verbose=False):
    return SimpleNamespace(verbose_logging=verbose)


class TestCleanupTaskResourcesHeadedSkip:
    def test_headless_still_cleans_browser(self):
        from agent.chat_completion_helpers import cleanup_task_resources
        with (
            patch("tools.browser_tool._is_headed_mode", return_value=False),
            patch("run_agent.cleanup_vm"),
            patch("run_agent.cleanup_browser") as mock_cb,
            patch(
                "agent.chat_completion_helpers.is_persistent_env",
                return_value=False,
            ),
        ):
            cleanup_task_resources(_make_agent(), "task-x")
            mock_cb.assert_called_once_with("task-x")


    def test_headed_does_not_skip_vm_cleanup(self):
        """Headed mode only affects the browser; VM teardown is untouched."""
        from agent.chat_completion_helpers import cleanup_task_resources
        with (
            patch("tools.browser_tool._is_headed_mode", return_value=True),
            patch("run_agent.cleanup_vm") as mock_vm,
            patch("run_agent.cleanup_browser"),
            patch(
                "agent.chat_completion_helpers.is_persistent_env",
                return_value=False,
            ),
        ):
            cleanup_task_resources(_make_agent(), "task-x")
            mock_vm.assert_called_once_with("task-x")


# ---------------------------------------------------------------------------
# --headed flag injection in local mode
# ---------------------------------------------------------------------------

class TestHeadedFlagInjection:
    def _run_and_capture(self, bt):
        """Run a snapshot command with Popen mocked; return captured argv."""
        captured_cmds = []
        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0

        def capture_popen(cmd, **kwargs):
            captured_cmds.append(cmd)
            return mock_proc

        mock_stdout = (
            '{"success": true, "data": {"snapshot": '
            '"- heading \\"Hi\\" [ref=e1]", "refs": {"e1": {}}}}'
        )
        with patch("subprocess.Popen", side_effect=capture_popen), \
             patch("tools.browser_tool._prepare_headed_env", return_value={}), \
             patch("os.open", return_value=99), \
             patch("os.close"), \
             patch("os.unlink"), \
             patch("os.makedirs"), \
             patch("builtins.open", MagicMock(return_value=MagicMock(
                 __enter__=MagicMock(return_value=MagicMock(
                     read=MagicMock(return_value=mock_stdout))),
                 __exit__=MagicMock(return_value=False),
             ))), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("tools.browser_tool._write_owner_pid"):
            bt._run_browser_command("task1", "snapshot", [], _engine_override="auto")
        return captured_cmds

    @patch("tools.browser_tool._get_session_info")
    @patch("tools.browser_tool._find_agent_browser", return_value="/usr/bin/agent-browser")
    @patch("tools.browser_tool._is_local_mode", return_value=True)
    @patch("tools.browser_tool._chromium_installed", return_value=True)
    @patch("tools.browser_tool._get_cloud_provider", return_value=None)
    @patch("tools.browser_tool._get_cdp_override", return_value="")
    @patch("tools.browser_tool._is_camofox_mode", return_value=False)
    def test_headed_flag_added_in_local_mode(
        self, _camofox, _cdp, _cloud, _chromium, _local, _find, _session
    ):
        import tools.browser_tool as bt
        bt._cached_headed_mode = True
        bt._headed_mode_resolved = True
        _session.return_value = {"session_name": "test-sess"}

        captured = self._run_and_capture(bt)
        assert len(captured) == 1
        assert "--headed" in captured[0]


    @patch("tools.browser_tool._get_session_info")
    @patch("tools.browser_tool._find_agent_browser", return_value="/usr/bin/agent-browser")
    @patch("tools.browser_tool._is_local_mode", return_value=True)
    @patch("tools.browser_tool._chromium_installed", return_value=True)
    @patch("tools.browser_tool._get_cloud_provider", return_value=None)
    @patch("tools.browser_tool._get_cdp_override", return_value="")
    @patch("tools.browser_tool._is_camofox_mode", return_value=False)
    def test_headed_flag_not_added_in_cloud_mode(
        self, _camofox, _cdp, _cloud, _chromium, _local, _find, _session
    ):
        """Cloud (CDP) sessions never get --headed — it's a local-only flag."""
        import tools.browser_tool as bt
        bt._cached_headed_mode = True
        bt._headed_mode_resolved = True
        _session.return_value = {
            "session_name": "test-sess",
            "cdp_url": "wss://example.invalid/cdp",
        }

        captured = self._run_and_capture(bt)
        assert len(captured) == 1
        assert "--headed" not in captured[0]
        assert "--cdp" in captured[0]


# ---------------------------------------------------------------------------
# Issue #94827: headed + no DISPLAY → actionable error + optional Xvfb fallback
# ---------------------------------------------------------------------------


def _missing_x_server_stderr() -> str:
    """Mimic what Chromium / agent-browser prints when no display is available."""
    return (
        "[ERROR:gpu_init.cc(523)] Passthrough is not supported, GL is disabled\n"
        "Missing X server or $DISPLAY\n"
        "The platform failed to initialize. Exiting.\n"
    )


def _strip_display_env(monkeypatch):
    """Make DISPLAY/WAYLAND_DISPLAY/XAUTHORITY look unset for the test process."""
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"):
        monkeypatch.delenv(var, raising=False)


class TestMissingDisplayDetection:
    """The fix surfaces a clear, actionable error instead of a raw Chromium crash."""

    def test_detects_missing_x_server_pattern(self):
        from tools.browser_tool import _is_missing_x_server_error

        assert _is_missing_x_server_error("Missing X server or $DISPLAY\n") is True
        assert (
            _is_missing_x_server_error(
                "the platform failed to initialize. exiting.\n"
            )
            is True
        )
        assert _is_missing_x_server_error("") is False
        assert _is_missing_x_server_error("Some unrelated error\n") is False

    def test_actionable_message_mentions_display(self):
        from tools.browser_tool import _format_missing_display_error

        msg = _format_missing_display_error(headed=True, platform_name="linux")
        # Operator must be told why this is happening and what to do.
        assert "DISPLAY" in msg or "WAYLAND_DISPLAY" in msg
        assert "Xvfb" in msg
        # We must not pretend the browser actually launched.
        assert "headless" in msg.lower() or "headed" in msg.lower()


class TestHeadedNoDisplayErrorSurfaced:
    """Headed + no display server → actionable error, not a raw crash."""

    def _patched_headed_no_display(self, monkeypatch):
        """Force headed=True, local mode, and Chromium crashing with the X error."""
        import tools.browser_tool as bt

        bt._cached_headed_mode = True
        bt._headed_mode_resolved = True
        _strip_display_env(monkeypatch)

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.wait.return_value = None

        stderr_text = _missing_x_server_stderr()
        stdout_text = ""

        def fake_popen(cmd, **kwargs):
            return mock_proc

        monkeypatch.setattr("subprocess.Popen", fake_popen)
        monkeypatch.setattr(
            "tools.browser_tool._find_agent_browser",
            lambda: "/usr/bin/agent-browser",
        )
        monkeypatch.setattr("tools.browser_tool._is_local_mode", lambda: True)
        monkeypatch.setattr("tools.browser_tool._chromium_installed", lambda: True)
        monkeypatch.setattr("tools.browser_tool._get_cloud_provider", lambda: None)
        monkeypatch.setattr("tools.browser_tool._get_cdp_override", lambda: "")
        monkeypatch.setattr("tools.browser_tool._is_camofox_mode", lambda: False)
        monkeypatch.setattr(
            "tools.browser_tool._get_session_info",
            lambda _task: {"session_name": "test-sess"},
        )
        # Disable the auto-Xvfb attempt so this test asserts the error path,
        # not the fallback.
        monkeypatch.setattr(
            "tools.browser_tool._try_start_xvfb", lambda: (False, None)
        )
        monkeypatch.setattr(
            "tools.browser_tool._read_command_output_files",
            lambda _o, _e: (stdout_text, stderr_text),
        )
        # Builtins.open is used to re-read stdout/stderr after success. Stub
        # so stderr contains the "Missing X server" pattern and stdout is empty.
        def fake_open(path, *args, **kwargs):
            text = _missing_x_server_stderr() if "_stderr_" in path else ""
            return MagicMock(
                __enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value=text))),
                __exit__=MagicMock(return_value=False),
            )

        monkeypatch.setattr("builtins.open", MagicMock(side_effect=fake_open))
        monkeypatch.setattr("tools.browser_tool.os.open", lambda *a, **k: 99)
        monkeypatch.setattr("tools.browser_tool.os.close", lambda *_a, **_k: None)
        monkeypatch.setattr("tools.browser_tool.os.unlink", lambda *_a, **_k: None)
        monkeypatch.setattr("tools.browser_tool.os.makedirs", lambda *_a, **_k: None)
        monkeypatch.setattr(
            "tools.browser_tool._write_owner_pid", lambda *_a, **_k: None
        )
        monkeypatch.setattr(
            "tools.interrupt.is_interrupted", lambda: False
        )
        return bt

    def test_headed_no_display_returns_actionable_error(self, monkeypatch):
        """Pre-fix: raw 'Missing X server' crash. Post-fix: actionable hint."""
        bt = self._patched_headed_no_display(monkeypatch)
        result = bt._run_browser_command(
            "task1", "open", ["http://example.com"], _engine_override="auto"
        )

        assert result.get("success") is False, result
        err = (result.get("error") or "").lower()
        # The raw browser crash must NOT be the only thing the user sees.
        assert "missing x server" not in err or "xvfb" in err or "display" in err
        # The actionable hint must be present.
        assert "display" in err or "wayland_display" in err
        # We must point the user toward a concrete fix.
        assert "xvfb" in err


class TestHeadedNoDisplayXvfbFallback:
    """Headed + no DISPLAY + Xvfb available → Xvfb is launched and DISPLAY is set."""

    def test_xvfb_attempt_invoked_when_headed_and_no_display(self, monkeypatch):
        import tools.browser_tool as bt

        bt._cached_headed_mode = True
        bt._headed_mode_resolved = True
        _strip_display_env(monkeypatch)

        calls = {"xvfb": 0, "display_set": None}

        def fake_try_start_xvfb():
            calls["xvfb"] += 1
            # Simulate Xvfb success: a fresh display number we can hand to the browser.
            calls["display_set"] = ":99"
            return (True, ":99")

        monkeypatch.setattr(
            "tools.browser_tool._try_start_xvfb", fake_try_start_xvfb
        )
        # Should never actually spawn — we're only asserting the gate.
        monkeypatch.setattr(
            "subprocess.Popen",
            MagicMock(side_effect=AssertionError("Popen should not be called")),
        )

        # The gate lives inside _run_browser_command, but for a focused unit
        # test we can also exercise the helper directly via _prepare_headed_env.
        env = bt._prepare_headed_env()
        assert env.get("DISPLAY") == ":99"
        assert calls["xvfb"] == 1

    def test_xvfb_attempt_skipped_when_display_already_set(self, monkeypatch):
        """No Xvfb spin-up when DISPLAY is already present — the user has it set up."""
        import tools.browser_tool as bt

        bt._cached_headed_mode = True
        bt._headed_mode_resolved = True
        monkeypatch.setenv("DISPLAY", ":0")
        calls = {"xvfb": 0}

        def fake_try_start_xvfb():
            calls["xvfb"] += 1
            return (True, ":99")

        monkeypatch.setattr(
            "tools.browser_tool._try_start_xvfb", fake_try_start_xvfb
        )
        env = bt._prepare_headed_env()
        # The helper must NOT touch DISPLAY when the user already has one set
        # (i.e. it returns no overrides, leaving the parent's $DISPLAY intact
        # in the subprocess env via ``browser_env``).
        assert env == {}
        assert calls["xvfb"] == 0


class TestTryStartXvfb:
    """Linux-only auto-fallback helper."""

    @pytest.mark.linux_only
    def test_returns_false_when_xvfb_missing(self, monkeypatch):
        from tools.browser_tool import _try_start_xvfb

        monkeypatch.setattr("shutil.which", lambda _name: None)
        ok, display = _try_start_xvfb()
        assert ok is False
        assert display is None

    @pytest.mark.linux_only
    def test_returns_display_when_xvfb_starts(self, monkeypatch):
        """When Xvfb is on PATH and keeps running, we get a display back."""
        import subprocess

        from tools.browser_tool import _try_start_xvfb

        # Force the helper to take the "Xvfb launched" branch without
        # actually invoking the binary: a proc whose wait() times out is
        # exactly the success path (Xvfb is still running and healthy).
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        mock_proc = MagicMock()
        mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd=None, timeout=2)
        monkeypatch.setattr("subprocess.Popen", lambda *a, **k: mock_proc)
        ok, display = _try_start_xvfb()
        assert ok is True
        assert display == ":99"
