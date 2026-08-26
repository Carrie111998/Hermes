"""Tests for real-profile local browser resolution + routing."""
import os
import ntpath
from unittest.mock import Mock, patch

import pytest


class TestRealProfileResolvers:
    def test_data_dir_windows(self):
        import hermes_cli.browser_connect as bc
        with patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\T\AppData\Local"}, clear=False):
            got = bc.real_profile_data_dir("chrome", "Windows")
        # Use ntpath basename checks so this passes on Linux CI too.
        assert got.endswith(ntpath.join("Google", "Chrome", "User Data")) or got.endswith(
            "Google\\Chrome\\User Data"
        )

    def test_data_dir_linux_edge(self):
        import hermes_cli.browser_connect as bc
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/home/t/.config"}, clear=False):
            got = bc.real_profile_data_dir("edge", "Linux")
        assert got == "/home/t/.config/microsoft-edge"

    def test_data_dir_unknown_browser_is_none(self):
        import hermes_cli.browser_connect as bc
        assert bc.real_profile_data_dir("firefox", "Windows") is None

    def test_detect_default_windows_progid_maps(self):
        import hermes_cli.browser_connect as bc
        # Non-Windows host: _detect_default_windows short-circuits via winreg
        # ImportError → None. Assert the ProgId map itself is correct instead.
        m = dict(bc._WINDOWS_PROGID_MAP)
        assert m["chromehtml"] == "chrome"
        assert m["msedgehtm"] == "edge"
        assert m["bravehtml"] == "brave"

    def test_detect_default_non_chromium_is_none(self):
        import hermes_cli.browser_connect as bc
        with patch.object(bc, "_detect_default_linux", return_value=None):
            assert bc.detect_default_chromium("Linux") is None


class TestUseRealProfileConsent:
    """The consent flag is re-read per call: revocation must not wait for a
    process restart, and multiplexed profiles must not inherit each other's
    consent through a module-level cache."""

    def test_toggle_off_revokes_without_restart(self):
        import tools.browser_tool as bt
        reads = [
            {"browser": {"use_real_profile": True}},
            {"browser": {"use_real_profile": False}},
        ]
        with patch("hermes_cli.config.read_raw_config", side_effect=reads):
            assert bt._use_real_profile() is True
            assert bt._use_real_profile() is False

    def test_string_false_is_off(self):
        import tools.browser_tool as bt
        with patch("hermes_cli.config.read_raw_config",
                   return_value={"browser": {"use_real_profile": "false"}}):
            assert bt._use_real_profile() is False

    def test_missing_key_and_unreadable_config_are_off(self):
        import tools.browser_tool as bt
        with patch("hermes_cli.config.read_raw_config", return_value={"browser": {}}):
            assert bt._use_real_profile() is False
        with patch("hermes_cli.config.read_raw_config", side_effect=OSError("boom")):
            assert bt._use_real_profile() is False


class TestRealProfileLaunchArgs:
    def _reset(self):
        import tools.browser_tool as bt
        bt._use_real_profile_resolved = False
        bt._cached_use_real_profile = False
        bt._real_profile_args_cache = None

    def test_consent_off_is_noop(self):
        import tools.browser_tool as bt
        self._reset()
        with patch.object(bt, "_use_real_profile", return_value=False):
            args, err = bt._real_profile_launch_args()
        assert args == [] and err is None

    def test_non_chromium_default_fails_closed(self):
        import tools.browser_tool as bt
        self._reset()
        with patch.object(bt, "_use_real_profile", return_value=True), \
             patch("hermes_cli.browser_connect.detect_default_chromium", return_value=None), \
             patch("hermes_cli.browser_connect.default_browser_identifier", return_value="firefox.desktop"):
            args, err = bt._real_profile_launch_args()
        assert args == []
        assert err and "not a supported Chromium" in err

    def test_pre_release_channel_is_named_in_the_error(self):
        """Chrome Beta/Dev/Canary and Edge Beta/Dev are separate installs with
        their own profiles; say which one it is instead of 'not a Chromium'."""
        import tools.browser_tool as bt
        self._reset()
        with patch.object(bt, "_use_real_profile", return_value=True), \
             patch("hermes_cli.browser_connect.detect_default_chromium", return_value=None), \
             patch("hermes_cli.browser_connect.default_browser_identifier", return_value="ChromeBHTML.H1"):
            args, err = bt._real_profile_launch_args()
        assert args == []
        assert err and "Google Chrome Beta" in err and "stable channels" in err

    def test_chromium_default_injects_profile(self, tmp_path):
        import tools.browser_tool as bt
        self._reset()
        data_dir = tmp_path / "chrome-user-data"
        data_dir.mkdir()
        with patch.object(bt, "_use_real_profile", return_value=True), \
             patch("hermes_cli.browser_connect.detect_default_chromium", return_value="chrome"), \
             patch("hermes_cli.browser_connect.real_profile_data_dir", return_value=str(data_dir)), \
             patch("hermes_cli.browser_connect.chromium_executable", return_value="/usr/bin/google-chrome"), \
             patch("hermes_cli.browser_connect.chromium_major_version", return_value=None):
            args, err = bt._real_profile_launch_args()
        assert err is None
        assert "--profile" in args and str(data_dir) in args
        assert "--executable-path" in args and "/usr/bin/google-chrome" in args

    def test_missing_profile_dir_fails_closed(self, tmp_path):
        import tools.browser_tool as bt
        self._reset()
        with patch.object(bt, "_use_real_profile", return_value=True), \
             patch("hermes_cli.browser_connect.detect_default_chromium", return_value="chrome"), \
             patch("hermes_cli.browser_connect.real_profile_data_dir", return_value=str(tmp_path / "nope")):
            args, err = bt._real_profile_launch_args()
        assert args == []
        assert err and "profile directory was not found" in err

    def test_missing_executable_fails_closed(self, tmp_path):
        """No resolvable binary must not fall through to ``--profile`` alone —
        agent-browser would then open the real profile with its bundled
        Chromium (one-way profile migration)."""
        import tools.browser_tool as bt
        self._reset()
        data_dir = tmp_path / "chrome-user-data"
        data_dir.mkdir()
        with patch.object(bt, "_use_real_profile", return_value=True), \
             patch("hermes_cli.browser_connect.detect_default_chromium", return_value="chrome"), \
             patch("hermes_cli.browser_connect.real_profile_data_dir", return_value=str(data_dir)), \
             patch("hermes_cli.browser_connect.chromium_executable", return_value=None):
            args, err = bt._real_profile_launch_args()
        assert args == []
        assert err and "executable could not be located" in err

    def _chrome_ready(self, tmp_path, major):
        import tools.browser_tool as bt
        self._reset()
        data_dir = tmp_path / "chrome-user-data"
        data_dir.mkdir(exist_ok=True)
        version = Mock(return_value=major)
        ctx = [
            patch.object(bt, "_use_real_profile", return_value=True),
            patch("hermes_cli.browser_connect.detect_default_chromium", return_value="chrome"),
            patch("hermes_cli.browser_connect.real_profile_data_dir", return_value=str(data_dir)),
            patch("hermes_cli.browser_connect.chromium_executable", return_value="/usr/bin/google-chrome"),
            patch("hermes_cli.browser_connect.chromium_major_version", version),
        ]
        return bt, ctx, version

    def test_google_chrome_136_plus_fails_closed(self, tmp_path):
        """Branded Chrome refuses remote debugging on its default profile dir;
        agent-browser would hang waiting for DevToolsActivePort (reproduced
        by the reviewer on Chrome 152)."""
        from contextlib import ExitStack
        bt, ctx, _ = self._chrome_ready(tmp_path, 152)
        with ExitStack() as stack:
            for c in ctx:
                stack.enter_context(c)
            args, err = bt._real_profile_launch_args()
        assert args == []
        assert err and "Google Chrome 152" in err and "hang" in err

    def test_google_chrome_before_136_launches(self, tmp_path):
        from contextlib import ExitStack
        bt, ctx, _ = self._chrome_ready(tmp_path, 130)
        with ExitStack() as stack:
            for c in ctx:
                stack.enter_context(c)
            args, err = bt._real_profile_launch_args()
        assert err is None and "--profile" in args

    def test_unknown_chrome_version_is_not_blocked(self, tmp_path):
        from contextlib import ExitStack
        bt, ctx, _ = self._chrome_ready(tmp_path, None)
        with ExitStack() as stack:
            for c in ctx:
                stack.enter_context(c)
            args, err = bt._real_profile_launch_args()
        assert err is None and "--profile" in args

    def test_version_gate_only_applies_to_google_chrome(self, tmp_path):
        import tools.browser_tool as bt
        self._reset()
        data_dir = tmp_path / "brave-user-data"
        data_dir.mkdir()
        version = Mock(return_value=152)
        with patch.object(bt, "_use_real_profile", return_value=True), \
             patch("hermes_cli.browser_connect.detect_default_chromium", return_value="brave"), \
             patch("hermes_cli.browser_connect.real_profile_data_dir", return_value=str(data_dir)), \
             patch("hermes_cli.browser_connect.chromium_executable", return_value="/usr/bin/brave"), \
             patch("hermes_cli.browser_connect.chromium_major_version", version):
            args, err = bt._real_profile_launch_args()
        assert err is None and "--profile" in args
        version.assert_not_called()

    def test_profile_in_use_fails_closed(self, tmp_path):
        """A running browser on the profile would make Chromium hand off and
        exit; agent-browser then retries into a timeout. Say so up front."""
        import tools.browser_tool as bt
        self._reset()
        data_dir = tmp_path / "chrome-user-data"
        data_dir.mkdir()
        with patch.object(bt, "_use_real_profile", return_value=True), \
             patch("hermes_cli.browser_connect.detect_default_chromium", return_value="chrome"), \
             patch("hermes_cli.browser_connect.real_profile_data_dir", return_value=str(data_dir)), \
             patch("hermes_cli.browser_connect.profile_lock_holder", return_value="pid 4242"), \
             patch("hermes_cli.browser_connect.chromium_executable", return_value="/usr/bin/google-chrome"):
            args, err = bt._real_profile_launch_args()
        assert args == []
        assert err and "open in another Chromium process (pid 4242)" in err

    def test_resolution_is_cached_per_process(self, tmp_path):
        """Detection shells out to the OS; it must run once, not per command,
        and the launch flags must stay identical (agent-browser hashes them)."""
        import tools.browser_tool as bt
        from unittest.mock import Mock
        self._reset()
        data_dir = tmp_path / "chrome-user-data"
        data_dir.mkdir()
        detect = Mock(return_value="chrome")
        with patch.object(bt, "_use_real_profile", return_value=True), \
             patch("hermes_cli.browser_connect.detect_default_chromium", detect), \
             patch("hermes_cli.browser_connect.real_profile_data_dir", return_value=str(data_dir)), \
             patch("hermes_cli.browser_connect.chromium_executable", return_value="/usr/bin/google-chrome"), \
             patch("hermes_cli.browser_connect.chromium_major_version", return_value=None):
            first = bt._real_profile_launch_args()
            second = bt._real_profile_launch_args()
        assert first == second and first[1] is None
        assert detect.call_count == 1

    def test_cache_drops_when_consent_turns_off(self, tmp_path):
        import tools.browser_tool as bt
        self._reset()
        bt._real_profile_args_cache = (["--profile", "/x"], None)
        with patch.object(bt, "_use_real_profile", return_value=False):
            assert bt._real_profile_launch_args() == ([], None)
        assert bt._real_profile_args_cache is None


class TestRunBrowserCommandInjection:
    """_run_browser_command must inject the resolved flags on local launches,
    fail closed on resolver errors — except for ``close``, which has to reach
    an already-running daemon."""

    def _run(self, tmp_path, command, profile_result, chromium_installed=True):
        import json
        import os
        from unittest.mock import MagicMock, mock_open
        import tools.browser_tool as bt

        captured = {}
        proc = MagicMock()
        proc.returncode = 0
        proc.wait.return_value = 0

        def capture_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return proc

        fake_session = {"session_name": "test-session", "session_id": "id", "cdp_url": None}
        with patch("tools.browser_tool._find_agent_browser", return_value="/usr/bin/agent-browser"), \
             patch("tools.browser_tool._chromium_installed", return_value=chromium_installed), \
             patch("tools.browser_tool._get_session_info", return_value=fake_session), \
             patch("tools.browser_tool._socket_safe_tmpdir", return_value=str(tmp_path)), \
             patch("tools.browser_tool._discover_homebrew_node_dirs", return_value=[]), \
             patch("tools.browser_tool._real_profile_launch_args", return_value=profile_result), \
             patch("hermes_constants.Path.home", return_value=tmp_path), \
             patch("subprocess.Popen", side_effect=capture_popen), \
             patch("os.open", return_value=99), \
             patch("os.close"), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                                     "HERMES_HOME": str(tmp_path / "hh")}, clear=True), \
             patch("builtins.open", mock_open(read_data=json.dumps({"success": True}))):
            result = bt._run_browser_command("t", command, [])
        return result, captured.get("cmd")

    def test_profile_flags_land_in_argv(self, tmp_path):
        result, cmd = self._run(
            tmp_path, "open", (["--profile", "/p", "--executable-path", "/bin/chrome"], None)
        )
        assert result.get("success") is True
        assert cmd[1:7] == ["--session", "test-session", "--profile", "/p",
                            "--executable-path", "/bin/chrome"]

    def test_resolver_error_fails_closed_before_launch(self, tmp_path):
        result, cmd = self._run(tmp_path, "open", ([], "no chromium default"))
        assert result == {"success": False, "error": "no chromium default"}
        assert cmd is None

    def test_close_still_reaches_agent_browser_on_resolver_error(self, tmp_path):
        result, cmd = self._run(tmp_path, "close", ([], "no chromium default"))
        assert cmd is not None and cmd[1:5] == ["--session", "test-session", "--json", "close"]
        assert "--profile" not in cmd

    def test_real_browser_binary_skips_the_bundled_chromium_gate(self, tmp_path):
        """A consented launch runs the user's own browser; the bundled-Chromium
        check must neither block it nor trigger the ~170 MB auto-install."""
        import tools.browser_tool as bt
        autoinstall = Mock(return_value=False)
        with patch.object(bt, "_is_local_mode", return_value=True), \
             patch.object(bt, "_maybe_autoinstall_chromium", autoinstall):
            result, cmd = self._run(
                tmp_path, "open",
                (["--profile", "/p", "--executable-path", "/Applications/Google Chrome.app/x"], None),
                chromium_installed=False,
            )
        assert result.get("success") is True
        assert cmd is not None and "--executable-path" in cmd
        autoinstall.assert_not_called()

    def test_gate_still_applies_without_a_real_binary(self, tmp_path):
        import tools.browser_tool as bt
        with patch.object(bt, "_is_local_mode", return_value=True), \
             patch.object(bt, "_maybe_autoinstall_chromium", return_value=False), \
             patch.object(bt, "_running_in_docker", return_value=False):
            result, cmd = self._run(tmp_path, "open", ([], None), chromium_installed=False)
        assert result.get("success") is False
        assert "Chromium browser is missing" in result["error"]
        assert cmd is None

    def test_lightpanda_engine_conflicts_with_real_profile(self, tmp_path):
        """agent-browser rejects --profile for Lightpanda; surface a config
        conflict instead of letting the Chrome fallback drop the profile."""
        import tools.browser_tool as bt
        with patch.object(bt, "_get_browser_engine", return_value="lightpanda"):
            result, cmd = self._run(
                tmp_path, "open", (["--profile", "/p", "--executable-path", "/bin/chrome"], None)
            )
        assert result.get("success") is False
        assert "lightpanda" in result["error"]
        assert cmd is None


class TestLocalBrowserRouting:
    def _reset(self):
        import tools.browser_tool as bt
        bt._use_real_profile_resolved = False
        bt._cached_use_real_profile = False
        bt._real_profile_args_cache = None

    def test_local_browser_forces_sidecar_with_consent(self):
        import tools.browser_tool as bt
        self._reset()
        with patch.object(bt, "_get_cdp_override_raw", return_value=""), \
             patch.object(bt, "_is_camofox_mode", return_value=False), \
             patch.object(bt, "_get_cloud_provider", return_value=Mock()), \
             patch.object(bt, "_url_is_private", return_value=False), \
             patch.object(bt, "_use_real_profile", return_value=True):
            key = bt._navigation_session_key("t1", "https://example.com", local_browser=True)
        assert key == "t1::local"

    def test_local_browser_without_cloud_provider_keeps_bare_session(self):
        """Pure local backend: the bare session already is the (real-profile)
        local Chromium; a second ``::local`` session would fight it for the
        same user-data-dir."""
        import tools.browser_tool as bt
        self._reset()
        with patch.object(bt, "_get_cdp_override_raw", return_value=""), \
             patch.object(bt, "_is_camofox_mode", return_value=False), \
             patch.object(bt, "_get_cloud_provider", return_value=None), \
             patch.object(bt, "_use_real_profile", return_value=True):
            key = bt._navigation_session_key("t1", "https://example.com", local_browser=True)
        assert key == "t1"

    def test_local_browser_respects_private_url_opt_out(self):
        """auto_local_for_private_urls: false is the user's LAN routing
        decision; a model-supplied flag must not override it."""
        import tools.browser_tool as bt
        self._reset()
        with patch.object(bt, "_get_cdp_override_raw", return_value=""), \
             patch.object(bt, "_is_camofox_mode", return_value=False), \
             patch.object(bt, "_get_cloud_provider", return_value=Mock()), \
             patch.object(bt, "_url_is_private", return_value=True), \
             patch.object(bt, "_auto_local_for_private_urls", return_value=False), \
             patch.object(bt, "_use_real_profile", return_value=True):
            key = bt._navigation_session_key("t1", "http://192.168.1.1/admin", local_browser=True)
        assert key == "t1"

    def test_local_browser_private_url_follows_auto_local_when_allowed(self):
        import tools.browser_tool as bt
        self._reset()
        with patch.object(bt, "_get_cdp_override_raw", return_value=""), \
             patch.object(bt, "_is_camofox_mode", return_value=False), \
             patch.object(bt, "_get_cloud_provider", return_value=Mock()), \
             patch.object(bt, "_url_is_private", return_value=True), \
             patch.object(bt, "_auto_local_for_private_urls", return_value=True), \
             patch.object(bt, "_use_real_profile", return_value=True):
            key = bt._navigation_session_key("t1", "http://192.168.1.1/admin", local_browser=True)
        assert key == "t1::local"

    def test_local_browser_ignored_without_consent(self):
        import tools.browser_tool as bt
        self._reset()
        with patch.object(bt, "_get_cdp_override_raw", return_value=""), \
             patch.object(bt, "_is_camofox_mode", return_value=False), \
             patch.object(bt, "_use_real_profile", return_value=False), \
             patch.object(bt, "_get_cloud_provider", return_value=None):
            key = bt._navigation_session_key("t1", "https://example.com", local_browser=True)
        assert key == "t1"

    def test_cdp_override_still_wins_over_local_browser(self):
        import tools.browser_tool as bt
        self._reset()
        with patch.object(bt, "_get_cdp_override_raw", return_value="ws://x"), \
             patch.object(bt, "_use_real_profile", return_value=True):
            key = bt._navigation_session_key("t1", "https://example.com", local_browser=True)
        assert key == "t1"
