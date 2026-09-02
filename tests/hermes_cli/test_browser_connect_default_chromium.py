"""Default-Chromium detection and profile-dir resolution (hermes_cli.browser_connect).

These exercise the parsers with real command output shapes instead of
patching the detectors themselves, so a change in what macOS / xdg report is
caught here rather than in a user's browser session.
"""
from unittest.mock import mock_open, patch

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

    def test_velja_routes_to_chrome_when_installed(self):
        """Velja (a link router) as https handler resolves to its Chromium
        target. Chrome is installed on this host, so expect 'chrome'."""
        dump = _ls_dump(_handler("https", "com.sindresorhus.velja"))
        with self._run_with(dump):
            assert bc._detect_default_darwin() == "chrome"

    def test_velja_fails_closed_when_no_chromium_installed(self):
        """If no Chromium app exists, Velja's https handler must not guess."""
        dump = _ls_dump(_handler("https", "com.sindresorhus.velja"))
        velja_app_paths = {
            "com.google.chrome": "/Applications/Google Chrome.app",
            "org.chromium.chromium": "/Applications/Chromium.app",
            "com.brave.browser": "/Applications/Brave Browser.app",
            "com.microsoft.edgemac": "/Applications/Microsoft Edge.app",
        }
        with self._run_with(dump), \
             patch.object(bc.os.path, "isdir", side_effect=lambda p: False):
            assert bc._detect_default_darwin() is None

    def test_velja_prefers_its_configured_browser(self):
        """Velja's stored default browser wins over the installed-app fallback."""
        import io as _io
        import plistlib as _pl
        dump = _ls_dump(_handler("https", "com.sindresorhus.velja"))
        prefs_bytes = _pl.dumps({"defaultBrowser": "com.brave.browser"})
        with self._run_with(dump), \
             patch.object(bc.os.path, "expanduser", return_value="/fake/prefs.plist"), \
             patch("builtins.open", side_effect=lambda *a, **k: _io.BytesIO(prefs_bytes)):
            assert bc._detect_default_darwin() == "brave"

    def test_reader_failure_fails_closed(self):
        with patch.object(bc.subprocess, "run", side_effect=OSError("no defaults")):
            assert bc._detect_default_darwin() is None

    @pytest.mark.parametrize(
        "bundle,expected",
        [
            ("com.google.Chrome", "chrome"),
            ("com.brave.Browser", "brave"),
            ("com.brave.Browser.origin", "brave-origin"),
            ("com.microsoft.edgemac", "edge"),
            ("org.chromium.Chromium", "chromium"),
            ("com.brave.Browser.origin.beta", bc.UNSUPPORTED_CHANNEL),
            ("com.brave.Browser.origin.nightly", bc.UNSUPPORTED_CHANNEL),
        ],
    )
    def test_bundle_map(self, bundle, expected):
        with self._run_with(_ls_dump(_handler("https", bundle))):
            assert bc._detect_default_darwin() == expected


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
            ("brave-origin.desktop", "brave-origin"),
            ("brave-origin-beta.desktop", bc.UNSUPPORTED_CHANNEL),
            ("brave-origin-nightly.desktop", bc.UNSUPPORTED_CHANNEL),
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
