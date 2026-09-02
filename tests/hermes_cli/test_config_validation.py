"""Tests for config.yaml structure validation (validate_config_structure)."""

import io

from hermes_cli.config import (
    DEFAULT_CONFIG,
    _EXTRA_KNOWN_ROOT_KEYS,
    _KNOWN_ROOT_KEYS,
    validate_config_structure,
    ConfigIssue,
    print_config_warnings,
)


class _FakeStream:
    """A stream stand-in that reports a fixed TTY status and records output."""

    def __init__(self, tty: bool):
        self._tty = tty
        self.buf = io.StringIO()

    def isatty(self):
        return self._tty

    def write(self, s):
        self.buf.write(s)
        return len(s)


class TestPrintConfigWarnings:
    """print_config_warnings() must gate ANSI on the *stderr* stream.

    It writes to stderr, so piped/systemd/gateway stderr (non-TTY) or
    NO_COLOR must produce plain text — raw ESC sequences would leak as
    jumbled '?[33m' garbage into logs.
    """

    _BAD_CONFIG = {
        # custom_providers as a dict (not a list) -> error issue
        "custom_providers": {"name": "test", "base_url": "https://example.com/v1"},
    }

    def test_colored_when_stderr_is_tty(self, monkeypatch):
        fake = _FakeStream(tty=True)
        monkeypatch.setattr("sys.stderr", fake)

        print_config_warnings(self._BAD_CONFIG)

        out = fake.buf.getvalue()
        assert "Config issues detected" in out
        assert "\033[31m" in out  # red error marker
        assert "\033[33m" in out  # yellow section header

    def test_plain_when_stderr_piped(self, monkeypatch):
        fake = _FakeStream(tty=False)
        monkeypatch.setattr("sys.stderr", fake)

        print_config_warnings(self._BAD_CONFIG)

        out = fake.buf.getvalue()
        assert "Config issues detected" in out
        assert "hermes doctor" in out
        assert "\033[" not in out  # no raw ANSI escapes

    def test_plain_when_stderr_piped_even_if_stdout_is_tty(self, monkeypatch):
        # The decision must follow stderr, not the (colored) stdout.
        monkeypatch.setattr("sys.stdout", _FakeStream(tty=True))
        fake_err = _FakeStream(tty=False)
        monkeypatch.setattr("sys.stderr", fake_err)

        print_config_warnings(self._BAD_CONFIG)

        assert "Config issues detected" in fake_err.buf.getvalue()
        assert "\033[" not in fake_err.buf.getvalue()

    def test_no_color_env_disables_color_on_tty(self, monkeypatch):
        fake = _FakeStream(tty=True)
        monkeypatch.setattr("sys.stderr", fake)
        monkeypatch.setenv("NO_COLOR", "1")

        print_config_warnings(self._BAD_CONFIG)

        assert "Config issues detected" in fake.buf.getvalue()
        assert "\033[" not in fake.buf.getvalue()

    def test_healthy_config_is_silent(self, monkeypatch):
        fake = _FakeStream(tty=True)
        monkeypatch.setattr("sys.stderr", fake)

        print_config_warnings({})

        assert fake.buf.getvalue() == ""


class TestCustomProvidersValidation:
    """custom_providers must be a YAML list, not a dict."""

    def test_dict_instead_of_list(self):
        """The exact Discord user scenario — custom_providers as flat dict."""
        issues = validate_config_structure({
            "custom_providers": {
                "name": "Generativelanguage.googleapis.com",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key": "xxx",
                "model": "models/gemini-2.5-flash",
                "rate_limit_delay": 2.0,
                "fallback_model": {
                    "provider": "openrouter",
                    "model": "qwen/qwen3.6-plus:free",
                },
            },
            "fallback_providers": [],
        })
        errors = [i for i in issues if i.severity == "error"]
        assert any("dict" in i.message and "list" in i.message for i in errors), (
            "Should detect custom_providers as dict instead of list"
        )

    def test_dict_detects_misplaced_fields(self):
        """When custom_providers is a dict, detect fields that look misplaced."""
        issues = validate_config_structure({
            "custom_providers": {
                "name": "test",
                "base_url": "https://example.com",
                "api_key": "xxx",
            },
        })
        warnings = [i for i in issues if i.severity == "warning"]
        # Should flag base_url, api_key as looking like custom_providers entry fields
        misplaced = [i for i in warnings if "custom_providers entry fields" in i.message]
        assert len(misplaced) == 1


    def test_list_entry_not_dict(self):
        """Non-dict list entries should warn."""
        issues = validate_config_structure({
            "custom_providers": ["not-a-dict"],
            "model": {"provider": "custom"},
        })
        assert any("not a dict" in i.message for i in issues)




class TestMissingModelSection:
    """Warn when custom_providers exists but model section is missing."""


    def test_custom_providers_with_model(self):
        issues = validate_config_structure({
            "custom_providers": [
                {"name": "test", "base_url": "https://example.com/v1"},
            ],
            "model": {"provider": "custom", "default": "test-model"},
        })
        # Should not warn about missing model section
        assert not any("no 'model' section" in i.message for i in issues)


class TestConfigIssueDataclass:
    """ConfigIssue should be a proper dataclass."""

    def test_fields(self):
        issue = ConfigIssue(severity="error", message="test msg", hint="test hint")
        assert issue.severity == "error"
        assert issue.message == "test msg"
        assert issue.hint == "test hint"

    def test_equality(self):
        a = ConfigIssue("error", "msg", "hint")
        b = ConfigIssue("error", "msg", "hint")
        assert a == b


class TestVoiceSubmitModeValidation:
    def test_default_is_direct(self):
        assert DEFAULT_CONFIG["voice"]["submit_mode"] == "direct"

    def test_direct_and_draft_are_valid(self):
        for mode in ("direct", "draft"):
            issues = validate_config_structure({"voice": {"submit_mode": mode}})
            assert not any("voice.submit_mode" in issue.message for issue in issues)

    def test_invalid_mode_is_reported(self):
        issues = validate_config_structure({"voice": {"submit_mode": "refine"}})

        assert any(
            issue.severity == "error"
            and "voice.submit_mode" in issue.message
            and "direct" in issue.hint
            and "draft" in issue.hint
            for issue in issues
        )


class TestUnknownTopLevelKeys:
    """Arbitrary top-level keys must NOT warn — they are bridged to os.environ.

    Top-level scalars in config.yaml are forwarded into the environment
    (gateway/run.py, hermes send) so users can feed skills and external apps
    env-style keys like DISCORD_HOME_CHANNEL or MY_APP_TOKEN. A closed-world
    allowlist can never enumerate those, so no "Unknown top-level config key"
    warning may exist.
    """


    def test_known_root_keys_derived_from_default_config(self):
        """_KNOWN_ROOT_KEYS must be DEFAULT_CONFIG.keys() plus extras — single source of truth."""
        assert set(DEFAULT_CONFIG.keys()).issubset(_KNOWN_ROOT_KEYS)
        assert _EXTRA_KNOWN_ROOT_KEYS.issubset(_KNOWN_ROOT_KEYS)
        assert _KNOWN_ROOT_KEYS == frozenset(DEFAULT_CONFIG.keys()) | _EXTRA_KNOWN_ROOT_KEYS

    def test_provider_like_unknown_root_keeps_misplaced_message(self):
        """Preserve existing base_url/api_key root-level guidance."""
        issues = validate_config_structure({
            "base_url": "https://example.com/v1",
            "api_key": "secret",
        })
        misplaced = [
            i for i in issues
            if i.severity == "warning" and "looks misplaced" in i.message
        ]
        assert any("base_url" in i.message for i in misplaced)
        assert any("api_key" in i.message for i in misplaced)

