"""Tests for tools.env_passthrough — skill and config env var passthrough."""

import logging
import os
import pytest
import yaml

import tools.env_passthrough as _ep_mod
from tools.env_passthrough import (
    clear_env_passthrough,
    get_all_passthrough,
    is_env_passthrough,
    register_env_passthrough,
)


@pytest.fixture(autouse=True)
def _clean_passthrough():
    """Ensure a clean passthrough state for every test."""
    clear_env_passthrough()
    _ep_mod._config_passthrough = None
    yield
    clear_env_passthrough()
    _ep_mod._config_passthrough = None


class TestSkillScopedPassthrough:
    def test_register_and_check(self):
        assert not is_env_passthrough("TENOR_API_KEY")
        register_env_passthrough(["TENOR_API_KEY"])
        assert is_env_passthrough("TENOR_API_KEY")

    def test_register_multiple(self):
        register_env_passthrough(["FOO_TOKEN", "BAR_SECRET"])
        assert is_env_passthrough("FOO_TOKEN")
        assert is_env_passthrough("BAR_SECRET")
        assert not is_env_passthrough("OTHER_KEY")

    def test_clear(self):
        register_env_passthrough(["TENOR_API_KEY"])
        assert is_env_passthrough("TENOR_API_KEY")
        clear_env_passthrough()
        assert not is_env_passthrough("TENOR_API_KEY")

    def test_get_all(self):
        register_env_passthrough(["A_KEY", "B_TOKEN"])
        result = get_all_passthrough()
        assert "A_KEY" in result
        assert "B_TOKEN" in result

    def test_strips_whitespace(self):
        register_env_passthrough(["  SPACED_KEY  "])
        assert is_env_passthrough("SPACED_KEY")

    def test_skips_empty(self):
        register_env_passthrough(["", "  ", "VALID_KEY"])
        assert is_env_passthrough("VALID_KEY")
        assert not is_env_passthrough("")


class TestConfigPassthrough:
    def test_reads_from_config(self, tmp_path, monkeypatch):
        config = {"terminal": {"env_passthrough": ["MY_CUSTOM_KEY", "ANOTHER_TOKEN"]}}
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(config))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _ep_mod._config_passthrough = None

        assert is_env_passthrough("MY_CUSTOM_KEY")
        assert is_env_passthrough("ANOTHER_TOKEN")
        assert not is_env_passthrough("UNRELATED_VAR")

    def test_empty_config(self, tmp_path, monkeypatch):
        config = {"terminal": {"env_passthrough": []}}
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(config))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _ep_mod._config_passthrough = None

        assert not is_env_passthrough("ANYTHING")

    def test_missing_config_key(self, tmp_path, monkeypatch):
        config = {"terminal": {"backend": "local"}}
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(config))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _ep_mod._config_passthrough = None

        assert not is_env_passthrough("ANYTHING")

    def test_no_config_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _ep_mod._config_passthrough = None

        assert not is_env_passthrough("ANYTHING")

    def test_union_of_skill_and_config(self, tmp_path, monkeypatch):
        config = {"terminal": {"env_passthrough": ["CONFIG_KEY"]}}
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(config))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _ep_mod._config_passthrough = None

        register_env_passthrough(["SKILL_KEY"])
        all_pt = get_all_passthrough()
        assert "CONFIG_KEY" in all_pt
        assert "SKILL_KEY" in all_pt


class TestExecuteCodeIntegration:
    """Verify that the passthrough is checked in execute_code's env filtering."""

    def test_secret_substring_blocked_by_default(self):
        """TENOR_API_KEY should be blocked without passthrough."""
        _SAFE_ENV_PREFIXES = ("PATH", "HOME", "USER", "LANG", "LC_", "TERM",
                              "TMPDIR", "TMP", "TEMP", "SHELL", "LOGNAME",
                              "XDG_", "PYTHONPATH", "VIRTUAL_ENV", "CONDA")
        _SECRET_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
                              "PASSWD", "AUTH")

        test_env = {"PATH": "/usr/bin", "TENOR_API_KEY": "test123", "HOME": "/home/user"}
        child_env = {}
        for k, v in test_env.items():
            if is_env_passthrough(k):
                child_env[k] = v
                continue
            if any(s in k.upper() for s in _SECRET_SUBSTRINGS):
                continue
            if any(k.startswith(p) for p in _SAFE_ENV_PREFIXES):
                child_env[k] = v

        assert "PATH" in child_env
        assert "HOME" in child_env
        assert "TENOR_API_KEY" not in child_env

    def test_passthrough_allows_secret_through(self):
        """TENOR_API_KEY should pass through when registered."""
        _SAFE_ENV_PREFIXES = ("PATH", "HOME", "USER", "LANG", "LC_", "TERM",
                              "TMPDIR", "TMP", "TEMP", "SHELL", "LOGNAME",
                              "XDG_", "PYTHONPATH", "VIRTUAL_ENV", "CONDA")
        _SECRET_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
                              "PASSWD", "AUTH")

        register_env_passthrough(["TENOR_API_KEY"])

        test_env = {"PATH": "/usr/bin", "TENOR_API_KEY": "test123", "HOME": "/home/user"}
        child_env = {}
        for k, v in test_env.items():
            if is_env_passthrough(k):
                child_env[k] = v
                continue
            if any(s in k.upper() for s in _SECRET_SUBSTRINGS):
                continue
            if any(k.startswith(p) for p in _SAFE_ENV_PREFIXES):
                child_env[k] = v

        assert "PATH" in child_env
        assert "HOME" in child_env
        assert "TENOR_API_KEY" in child_env
        assert child_env["TENOR_API_KEY"] == "test123"


class TestTerminalIntegration:
    """Verify that the passthrough is checked in terminal's env sanitizers."""

    def test_blocklisted_var_blocked_by_default(self):
        from tools.environments.local import _sanitize_subprocess_env, _HERMES_PROVIDER_ENV_BLOCKLIST

        # Pick a var we know is in the blocklist
        blocked_var = next(iter(_HERMES_PROVIDER_ENV_BLOCKLIST))
        env = {blocked_var: "secret_value", "PATH": "/usr/bin"}
        result = _sanitize_subprocess_env(env)
        assert blocked_var not in result
        assert "PATH" in result

    def test_passthrough_cannot_override_provider_blocklist(self):
        """GHSA-rhgp-j443-p4rf: register_env_passthrough must NOT accept
        Hermes provider credentials — that was the bypass where a skill
        could declare ANTHROPIC_TOKEN / OPENAI_API_KEY as passthrough and
        defeat the execute_code sandbox scrubbing."""
        from tools.environments.local import (
            _sanitize_subprocess_env,
            _HERMES_PROVIDER_ENV_BLOCKLIST,
        )

        blocked_var = next(iter(_HERMES_PROVIDER_ENV_BLOCKLIST))
        # Attempt to register — must be silently refused (logged warning).
        register_env_passthrough([blocked_var])

        # is_env_passthrough must NOT report it as allowed
        assert not is_env_passthrough(blocked_var)

        # Sanitizer still strips the var from subprocess env
        env = {blocked_var: "secret_value", "PATH": "/usr/bin"}
        result = _sanitize_subprocess_env(env)
        assert blocked_var not in result
        assert "PATH" in result

    def test_make_run_env_blocklist_override_rejected(self):
        """_make_run_env must NOT expose a blocklisted var to subprocess env
        even after a skill attempts to register it via passthrough."""
        import os
        from tools.environments.local import (
            _make_run_env,
            _HERMES_PROVIDER_ENV_BLOCKLIST,
        )

        blocked_var = next(iter(_HERMES_PROVIDER_ENV_BLOCKLIST))
        os.environ[blocked_var] = "secret_value"
        try:
            # Without passthrough — blocked
            result_before = _make_run_env({})
            assert blocked_var not in result_before

            # Skill tries to register it — must be refused, so still blocked
            register_env_passthrough([blocked_var])
            result_after = _make_run_env({})
            assert blocked_var not in result_after
        finally:
            os.environ.pop(blocked_var, None)

    def test_non_hermes_api_key_still_registerable(self):
        """Third-party API keys (TENOR_API_KEY, NOTION_TOKEN, etc.) are NOT
        Hermes provider credentials and must still pass through — skills
        that legitimately wrap third-party APIs must keep working."""
        # TENOR_API_KEY is a real example — used by the gif-search skill
        register_env_passthrough(["TENOR_API_KEY"])
        assert is_env_passthrough("TENOR_API_KEY")

        # Arbitrary skill-specific var
        register_env_passthrough(["MY_SKILL_CUSTOM_CONFIG"])
        assert is_env_passthrough("MY_SKILL_CUSTOM_CONFIG")


class TestPassthroughLoggingWithholdsNames:
    """The registration path must never write a variable name to the log.

    Skill frontmatter is attacker-influenced, and CodeQL treats anything
    derived from ``_is_hermes_provider_credential`` as secret-tainted
    because the function name matches its sensitive-data heuristic. The
    refusal warning used to interpolate the name; these tests pin the
    replacement so it cannot drift back.
    """

    def test_no_variable_name_reaches_the_log(self, caplog):
        from tools.environments.local import _HERMES_PROVIDER_ENV_BLOCKLIST

        blocked = [
            n for n, _ in zip(sorted(_HERMES_PROVIDER_ENV_BLOCKLIST), range(3))
        ]
        allowed = ["TENOR_API_KEY", "MY_SKILL_CUSTOM_CONFIG"]

        with caplog.at_level("DEBUG", logger="tools.env_passthrough"):
            register_env_passthrough(blocked + allowed)

        blob = "\n".join(r.getMessage() for r in caplog.records)
        for name in blocked + allowed:
            assert name not in blob, f"{name} leaked into a log record"

    def test_counts_are_reported(self, caplog):
        from tools.environments.local import _HERMES_PROVIDER_ENV_BLOCKLIST

        blocked = [
            n for n, _ in zip(sorted(_HERMES_PROVIDER_ENV_BLOCKLIST), range(3))
        ]

        with caplog.at_level("DEBUG", logger="tools.env_passthrough"):
            register_env_passthrough(blocked + ["TENOR_API_KEY"])

        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "refused to register 3 " in blob
        assert "registered 1 variable(s)" in blob
        # Still the actionable pointer, just without the name.
        assert "GHSA-rhgp-j443-p4rf" in blob

    def test_silent_when_nothing_is_refused(self, caplog):
        with caplog.at_level("DEBUG", logger="tools.env_passthrough"):
            register_env_passthrough(["TENOR_API_KEY"])

        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_silent_for_empty_input(self, caplog):
        with caplog.at_level("DEBUG", logger="tools.env_passthrough"):
            register_env_passthrough(["", "   "])

        assert not caplog.records

    def test_config_read_failure_logs_type_not_payload(self, caplog, monkeypatch):
        """A YAML parse error quotes the offending line, which may hold a
        secret — so the handler logs the exception type, never ``str(e)``."""
        secret = "SECRET_VALUE_that_must_not_be_logged"

        def _boom(*args, **kwargs):
            raise ValueError(f"while parsing: env_passthrough: [{secret}]")

        monkeypatch.setattr(_ep_mod, "cfg_get", _boom)
        _ep_mod._config_passthrough = None

        with caplog.at_level("DEBUG", logger="tools.env_passthrough"):
            _ep_mod._load_config_passthrough()

        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert secret not in blob
        assert "ValueError" in blob
