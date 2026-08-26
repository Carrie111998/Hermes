"""Default-Chromium detection and profile-dir resolution (hermes_cli.browser_connect).

These exercise the parsers with real command output shapes instead of
patching the detectors themselves, so a change in what macOS / xdg report is
caught here rather than in a user's browser session.
"""
import os
import socket
import subprocess
from unittest.mock import patch

import pytest

import hermes_cli.browser_connect as bc


def _ls_dump(*entries: str) -> str:
    return "(\n" + ",\n".join(entries) + "\n)\n"


def _handler(scheme: str, bundle: str) -> str:
    return (
        "    {\n"
        "        LSHandlerPreferredVersions =         {\n"
        '            LSHandlerRoleAll = "-";\n'
        "        };\n"
        f'        LSHandlerRoleAll = "{bundle}";\n'
        f"        LSHandlerURLScheme = {scheme};\n"
        "    }"
    )


def _content_type_handler(uti: str, bundle: str) -> str:
    return (
        "    {\n"
        f'        LSHandlerContentType = "{uti}";\n'
        f'        LSHandlerRoleViewer = "{bundle}";\n'
        "    }"
    )


class TestLaunchServicesHttpsHandler:
    def test_https_entry_wins_over_other_schemes(self):
        dump = _ls_dump(
            _handler("ftp", "com.google.chrome"),
            _handler("https", "com.apple.safari"),
        )
        assert bc._launchservices_https_handler(dump) == "com.apple.safari"

    def test_content_type_registration_is_not_an_https_handler(self):
        dump = _ls_dump(_content_type_handler("public.html", "com.google.chrome"))
        assert bc._launchservices_https_handler(dump) is None

    def test_no_entries_means_no_recorded_handler(self):
        assert bc._launchservices_https_handler("(\n)\n") is None
        assert bc._launchservices_https_handler("") is None

    def test_nested_dictionary_does_not_split_the_entry(self):
        dump = _ls_dump(_handler("https", "com.microsoft.edgemac"))
        assert bc._launchservices_https_handler(dump) == "com.microsoft.edgemac"


class TestDetectDefaultDarwin:
    def _run_with(self, dump: str):
        class _Proc:
            stdout = dump

        return patch.object(bc.subprocess, "run", return_value=_Proc())

    def test_chrome_as_https_handler(self):
        with self._run_with(_ls_dump(_handler("https", "com.google.chrome"))):
            assert bc._detect_default_darwin() == "chrome"

    def test_safari_default_with_chrome_installed_fails_closed(self):
        """The old fallback returned the first installed Chromium app; a
        non-Chromium default must resolve to None even when Chrome exists."""
        dump = _ls_dump(
            _handler("https", "com.apple.safari"),
            _handler("ftp", "com.google.chrome"),
        )
        with self._run_with(dump), \
             patch.object(bc, "chromium_executable", return_value="/Applications/Google Chrome.app/x"):
            assert bc._detect_default_darwin() is None

    def test_no_handler_recorded_fails_closed(self):
        with self._run_with("(\n)\n"), \
             patch.object(bc, "chromium_executable", return_value="/Applications/Google Chrome.app/x"):
            assert bc._detect_default_darwin() is None

    def test_firefox_default_fails_closed(self):
        with self._run_with(_ls_dump(_handler("https", "org.mozilla.firefox"))):
            assert bc._detect_default_darwin() is None

    def test_reader_failure_fails_closed(self):
        with patch.object(bc.subprocess, "run", side_effect=OSError("no defaults")):
            assert bc._detect_default_darwin() is None

    @pytest.mark.parametrize(
        "bundle,expected",
        [
            ("com.google.Chrome", "chrome"),
            ("com.brave.Browser", "brave"),
            ("com.microsoft.edgemac", "edge"),
            ("org.chromium.Chromium", "chromium"),
        ],
    )
    def test_bundle_map(self, bundle, expected):
        with self._run_with(_ls_dump(_handler("https", bundle))):
            assert bc._detect_default_darwin() == expected


class TestPreReleaseChannels:
    """Channel builds keep their own profile; the stable maps must neither
    swallow them (macOS/Linux ids extend the stable id) nor report them as
    'not a Chromium browser' (Windows ProgIds differ per channel)."""

    @pytest.mark.parametrize(
        "prog_id,expected",
        [
            ("ChromeHTML", "chrome"),
            ("ChromeHTML.ABCDEF", "chrome"),
            ("ChromeBHTML", None),
            ("ChromeDHTML.X", None),
            ("ChromeSSHTM", None),
            ("MSEdgeHTM", "edge"),
            ("MSEdgeBHTML", None),
            ("MSEdgeDHTML", None),
            ("BraveHTML", "brave"),
            ("FirefoxURL-308046B0AF4A39CB", None),
            (None, None),
        ],
    )
    def test_windows_progid_classification(self, prog_id, expected):
        assert bc._classify_windows_progid(prog_id) == expected

    @pytest.mark.parametrize(
        "prog_id,channel",
        [
            ("ChromeBHTML", "Google Chrome Beta"),
            ("ChromeDHTML", "Google Chrome Dev"),
            ("ChromeSSHTM.X", "Google Chrome Canary"),
            ("MSEdgeBHTML", "Microsoft Edge Beta"),
            ("MSEdgeDHTML", "Microsoft Edge Dev"),
            ("ChromeHTML", None),
            ("FirefoxURL", None),
        ],
    )
    def test_windows_channel_names(self, prog_id, channel):
        assert bc.unsupported_channel(prog_id) == channel

    @pytest.mark.parametrize(
        "bundle,expected,channel",
        [
            ("com.google.chrome", "chrome", None),
            ("com.google.chrome.canary", None, "Google Chrome Canary"),
            ("com.google.chrome.beta", None, "Google Chrome Beta"),
            ("com.microsoft.edgemac", "edge", None),
            ("com.microsoft.edgemac.beta", None, "Microsoft Edge Beta"),
            ("com.brave.browser", "brave", None),
            ("com.brave.browser.nightly", None, "Brave Nightly"),
        ],
    )
    def test_darwin_bundle_classification(self, bundle, expected, channel):
        assert bc._classify_darwin_bundle(bundle) == expected
        assert bc.unsupported_channel(bundle) == channel

    @pytest.mark.parametrize(
        "desktop,expected,channel",
        [
            ("google-chrome.desktop", "chrome", None),
            ("google-chrome-beta.desktop", None, "Google Chrome Beta"),
            ("google-chrome-unstable.desktop", None, "Google Chrome Dev"),
            ("microsoft-edge-dev.desktop", None, "Microsoft Edge Dev"),
            ("brave-browser-nightly.desktop", None, "Brave Nightly"),
            ("brave-browser.desktop", "brave", None),
        ],
    )
    def test_linux_desktop_classification(self, desktop, expected, channel):
        assert bc._classify_linux_desktop(desktop) == expected
        assert bc.unsupported_channel(desktop) == channel

    def test_identifier_is_exposed_for_diagnostics(self):
        class _Proc:
            stdout = "google-chrome-beta.desktop\n"

        with patch.object(bc.subprocess, "run", return_value=_Proc()):
            assert bc.default_browser_identifier("Linux") == "google-chrome-beta.desktop"
            assert bc.detect_default_chromium("Linux") is None


class TestDetectDefaultLinux:
    def _run_with(self, output: str):
        class _Proc:
            stdout = output

        return patch.object(bc.subprocess, "run", return_value=_Proc())

    @pytest.mark.parametrize(
        "desktop,expected",
        [
            ("google-chrome.desktop", "chrome"),
            ("com.google.Chrome.desktop", "chrome"),
            ("chromium_chromium.desktop", "chromium"),
            ("org.chromium.Chromium.desktop", "chromium"),
            ("brave-browser.desktop", "brave"),
            ("com.brave.Browser.desktop", "brave"),
            ("microsoft-edge.desktop", "edge"),
            ("com.microsoft.Edge.desktop", "edge"),
            ("firefox.desktop", None),
            ("org.mozilla.firefox.desktop", None),
            ("", None),
        ],
    )
    def test_xdg_desktop_names(self, desktop, expected):
        with self._run_with(desktop + "\n"):
            assert bc._detect_default_linux() == expected

    def test_missing_xdg_settings_fails_closed(self):
        with patch.object(bc.subprocess, "run", side_effect=FileNotFoundError("xdg-settings")):
            assert bc._detect_default_linux() is None


@pytest.mark.skipif(os.name == "nt", reason="SingletonLock symlink is the POSIX lock")
class TestProfileLockHolder:
    @staticmethod
    def _dead_pid() -> int:
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def test_no_lock_is_free(self, tmp_path):
        assert bc.profile_lock_holder(str(tmp_path)) is None

    def test_live_pid_on_this_host_is_in_use(self, tmp_path):
        os.symlink(f"{socket.gethostname()}-{os.getpid()}", tmp_path / "SingletonLock")
        assert bc.profile_lock_holder(str(tmp_path)) == f"pid {os.getpid()}"

    def test_stale_lock_from_dead_pid_is_free(self, tmp_path):
        os.symlink(f"{socket.gethostname()}-{self._dead_pid()}", tmp_path / "SingletonLock")
        assert bc.profile_lock_holder(str(tmp_path)) is None

    def test_lock_from_another_host_is_in_use(self, tmp_path):
        os.symlink("elsewhere-12345", tmp_path / "SingletonLock")
        holder = bc.profile_lock_holder(str(tmp_path))
        assert holder and "elsewhere" in holder

    def test_unparsable_lock_is_ignored(self, tmp_path):
        os.symlink("garbage", tmp_path / "SingletonLock")
        assert bc.profile_lock_holder(str(tmp_path)) is None


class TestChromiumMajorVersion:
    def _run_with(self, output: str):
        class _Proc:
            stdout = output

        return patch.object(bc.subprocess, "run", return_value=_Proc())

    @pytest.mark.skipif(os.name == "nt", reason="--version is not run on Windows")
    @pytest.mark.parametrize(
        "output,expected",
        [
            ("Google Chrome 152.0.7977.64 \n", 152),
            ("Chromium 151.0.7900.12 snap\n", 151),
            ("Brave Browser 141.1.85.100\n", 141),
            ("Microsoft Edge 150.0.3200.5\n", 150),
        ],
    )
    def test_parses_version_output(self, tmp_path, output, expected):
        exe = tmp_path / "chrome"
        exe.write_text("", encoding="utf-8")
        with self._run_with(output):
            assert bc.chromium_major_version(str(exe)) == expected

    def test_falls_back_to_versioned_directory(self, tmp_path):
        """Windows layout: …\\Application\\<version>\\ next to chrome.exe."""
        app = tmp_path / "Application"
        app.mkdir()
        (app / "chrome.exe").write_text("", encoding="utf-8")
        (app / "140.0.7300.10").mkdir()
        (app / "152.0.7977.64").mkdir()
        (app / "SetupMetrics").mkdir()
        with patch.object(bc.subprocess, "run", side_effect=OSError("cannot run")):
            assert bc.chromium_major_version(str(app / "chrome.exe")) == 152

    def test_unknown_when_nothing_matches(self, tmp_path):
        exe = tmp_path / "chrome"
        exe.write_text("", encoding="utf-8")
        with self._run_with("garbage"):
            assert bc.chromium_major_version(str(exe)) is None


class TestLinuxProfileDir:
    def _env(self, monkeypatch, home):
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    def test_native_path_when_nothing_exists(self, tmp_path, monkeypatch):
        self._env(monkeypatch, tmp_path)
        assert bc.real_profile_data_dir("chromium", "Linux") == str(tmp_path / ".config" / "chromium")

    def test_snap_chromium_profile_is_found(self, tmp_path, monkeypatch):
        self._env(monkeypatch, tmp_path)
        snap = tmp_path / "snap" / "chromium" / "common" / "chromium"
        snap.mkdir(parents=True)
        assert bc.real_profile_data_dir("chromium", "Linux") == str(snap)

    def test_flatpak_chrome_profile_is_found(self, tmp_path, monkeypatch):
        self._env(monkeypatch, tmp_path)
        flatpak = tmp_path / ".var" / "app" / "com.google.Chrome" / "config" / "google-chrome"
        flatpak.mkdir(parents=True)
        assert bc.real_profile_data_dir("chrome", "Linux") == str(flatpak)

    def test_native_profile_wins_when_present(self, tmp_path, monkeypatch):
        self._env(monkeypatch, tmp_path)
        native = tmp_path / ".config" / "BraveSoftware" / "Brave-Browser"
        native.mkdir(parents=True)
        (tmp_path / ".var" / "app" / "com.brave.Browser" / "config" / "BraveSoftware" / "Brave-Browser").mkdir(parents=True)
        assert bc.real_profile_data_dir("brave", "Linux") == str(native)

    def test_xdg_config_home_is_honoured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", "/home/t/.config")
        assert bc.real_profile_data_dir("edge", "Linux") == "/home/t/.config/microsoft-edge"
