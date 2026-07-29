"""Tests for ${ENV_VAR} substitution in config.yaml values."""

import pytest
from hermes_cli.config import _expand_env_vars, load_config


class TestExpandEnvVars:
    def test_simple_substitution(self):
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("MY_KEY", "secret123")
            assert _expand_env_vars("${MY_KEY}") == "secret123"

    def test_missing_var_kept_verbatim(self):
        with pytest.MonkeyPatch().context() as mp:
            mp.delenv("UNDEFINED_VAR_XYZ", raising=False)
            assert _expand_env_vars("${UNDEFINED_VAR_XYZ}") == "${UNDEFINED_VAR_XYZ}"

    def test_no_placeholder_unchanged(self):
        assert _expand_env_vars("plain-value") == "plain-value"

    def test_dict_recursive(self):
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("TOKEN", "tok-abc")
            result = _expand_env_vars({"key": "${TOKEN}", "other": "literal"})
            assert result == {"key": "tok-abc", "other": "literal"}

    def test_nested_dict(self):
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("API_KEY", "sk-xyz")
            result = _expand_env_vars({"model": {"api_key": "${API_KEY}"}})
            assert result["model"]["api_key"] == "sk-xyz"

    def test_list_items(self):
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("VAL", "hello")
            result = _expand_env_vars(["${VAL}", "literal", 42])
            assert result == ["hello", "literal", 42]

    def test_non_string_values_untouched(self):
        assert _expand_env_vars(42) == 42
        assert _expand_env_vars(3.14) == 3.14
        assert _expand_env_vars(True) is True
        assert _expand_env_vars(None) is None

    def test_multiple_placeholders_in_one_string(self):
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("HOST", "localhost")
            mp.setenv("PORT", "5432")
            assert _expand_env_vars("${HOST}:${PORT}") == "localhost:5432"

    def test_dict_keys_not_expanded(self):
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("KEY", "value")
            result = _expand_env_vars({"${KEY}": "no-expand-key"})
            assert "${KEY}" in result


class TestLoadConfigExpansion:
    def test_load_config_expands_env_vars(self, tmp_path, monkeypatch):
        config_yaml = (
            "model:\n"
            "  api_key: ${GOOGLE_API_KEY}\n"
            "platforms:\n"
            "  telegram:\n"
            "    token: ${TELEGRAM_BOT_TOKEN}\n"
            "plain: no-substitution\n"
        )
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_yaml)

        monkeypatch.setenv("GOOGLE_API_KEY", "gsk-test-key")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234567:ABC-token")
        # Patch the imported function's own globals. Other tests may reload
        # hermes_cli.config, making string-target monkeypatches hit a different
        # module object than this collection-time imported load_config().
        monkeypatch.setitem(load_config.__globals__, "get_config_path", lambda: config_file)

        config = load_config()

        assert config["model"]["api_key"] == "gsk-test-key"
        assert config["platforms"]["telegram"]["token"] == "1234567:ABC-token"
        assert config["plain"] == "no-substitution"

    def test_load_config_unresolved_kept_verbatim(self, tmp_path, monkeypatch):
        config_yaml = "model:\n  api_key: ${NOT_SET_XYZ_123}\n"
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_yaml)

        monkeypatch.delenv("NOT_SET_XYZ_123", raising=False)
        monkeypatch.setitem(load_config.__globals__, "get_config_path", lambda: config_file)

        config = load_config()

        assert config["model"]["api_key"] == "${NOT_SET_XYZ_123}"


class TestLoadConfigCacheEnvStaleness:
    """The load_config() cache must not pin expansions made against a stale
    environment (#58514): a load before load_hermes_dotenv() runs, or an env
    var rotated in-process, must not keep serving the old expansion."""

    def test_env_var_appearing_after_first_load_invalidates_cache(self, tmp_path, monkeypatch):
        config_yaml = "auxiliary:\n  vision:\n    api_key: ${LATE_DOTENV_KEY_58514}\n"
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_yaml)

        monkeypatch.delenv("LATE_DOTENV_KEY_58514", raising=False)
        monkeypatch.setitem(load_config.__globals__, "get_config_path", lambda: config_file)

        # First load happens before the var exists (pre-dotenv): literal kept.
        assert load_config()["auxiliary"]["vision"]["api_key"] == "${LATE_DOTENV_KEY_58514}"

        # .env load brings the var in — same file mtime/size, env changed.
        monkeypatch.setenv("LATE_DOTENV_KEY_58514", "nvapi-real")
        assert load_config()["auxiliary"]["vision"]["api_key"] == "nvapi-real"

    def test_env_var_rotation_invalidates_cache(self, tmp_path, monkeypatch):
        config_yaml = "providers:\n  mistral:\n    api_key: ${ROTATED_KEY_58514}\n"
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_yaml)

        monkeypatch.setenv("ROTATED_KEY_58514", "key-v1")
        monkeypatch.setitem(load_config.__globals__, "get_config_path", lambda: config_file)

        assert load_config()["providers"]["mistral"]["api_key"] == "key-v1"

        monkeypatch.setenv("ROTATED_KEY_58514", "key-v2")
        assert load_config()["providers"]["mistral"]["api_key"] == "key-v2"

    def test_unchanged_env_still_serves_cache(self, tmp_path, monkeypatch):
        config_yaml = "providers:\n  mistral:\n    api_key: ${STABLE_KEY_58514}\n"
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_yaml)

        monkeypatch.setenv("STABLE_KEY_58514", "key-stable")
        monkeypatch.setitem(load_config.__globals__, "get_config_path", lambda: config_file)

        load_config()
        # load_config_readonly() returns the cached object itself, so object
        # identity across calls proves the cache-hit path was taken (a rebuild
        # would produce a fresh dict).
        readonly = load_config.__globals__["load_config_readonly"]
        first = readonly()
        second = readonly()

        assert first is second
        assert first["providers"]["mistral"]["api_key"] == "key-stable"


class TestLoadCliConfigExpansion:
    """Verify that load_cli_config() also expands ${VAR} references."""

    def test_cli_config_ignores_empty_terminal_section(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("terminal:\n")

        monkeypatch.setattr("cli._hermes_home", tmp_path)

        from cli import load_cli_config
        config = load_cli_config()

        assert isinstance(config["terminal"], dict)
        assert config["terminal"]["env_type"] == "local"

    def test_cli_config_expands_auxiliary_api_key(self, tmp_path, monkeypatch):
        config_yaml = (
            "auxiliary:\n"
            "  vision:\n"
            "    api_key: ${TEST_VISION_KEY_XYZ}\n"
        )
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_yaml)

        monkeypatch.setenv("TEST_VISION_KEY_XYZ", "vis-key-123")
        # Patch the hermes home so load_cli_config finds our test config
        monkeypatch.setattr("cli._hermes_home", tmp_path)

        from cli import load_cli_config
        config = load_cli_config()

        assert config["auxiliary"]["vision"]["api_key"] == "vis-key-123"

    def test_cli_config_unresolved_kept_verbatim(self, tmp_path, monkeypatch):
        config_yaml = (
            "auxiliary:\n"
            "  vision:\n"
            "    api_key: ${UNSET_CLI_VAR_ABC}\n"
        )
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_yaml)

        monkeypatch.delenv("UNSET_CLI_VAR_ABC", raising=False)
        monkeypatch.setattr("cli._hermes_home", tmp_path)

        from cli import load_cli_config
        config = load_cli_config()

        assert config["auxiliary"]["vision"]["api_key"] == "${UNSET_CLI_VAR_ABC}"


class TestGatewayConfigExpandsEnvVars:
    """Regression tests for issue #72096: gateway/config.py::load_gateway_config()
    read config.yaml directly with yaml.safe_load() and never called
    _expand_env_vars(), unlike hermes_cli.config.load_config() and the
    existing expansion call sites in gateway/run.py. platforms.<name>.extra
    values therefore kept literal ${VAR} text, which gateway/slash_access.py's
    policy_from_extra() then treated as a real (unmatchable) admin id --
    silently enabling slash-command gating with an admin list nothing could
    satisfy.
    """

    def test_platform_extra_admin_list_var_is_expanded(self, monkeypatch, tmp_path):
        from gateway.config import load_gateway_config, Platform

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "platforms:\n"
            "  telegram:\n"
            "    enabled: true\n"
            "    token: xxx\n"
            "    extra:\n"
            "      group_allow_admin_from:\n"
            "        - '${TELEGRAM_ADMIN_ID}'\n"
            "      group_user_allowed_commands:\n"
            "        - status\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("TELEGRAM_ADMIN_ID", "555444333")

        config = load_gateway_config()

        extra = config.platforms[Platform.TELEGRAM].extra
        assert extra["group_allow_admin_from"] == ["555444333"], (
            "The ${TELEGRAM_ADMIN_ID} reference must resolve to the real "
            "env var value, not stay as the literal string"
        )

    def test_platform_extra_var_used_in_slash_access_policy(self, monkeypatch, tmp_path):
        """End-to-end: the exact reported scenario -- resolving the
        reference must let the configured admin actually match, rather
        than locking every user out of every tiered slash command."""
        from gateway.config import load_gateway_config, Platform
        from gateway.slash_access import policy_from_extra

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "platforms:\n"
            "  telegram:\n"
            "    enabled: true\n"
            "    token: xxx\n"
            "    extra:\n"
            "      group_allow_admin_from:\n"
            "        - '${TELEGRAM_ADMIN_ID}'\n"
            "      group_user_allowed_commands:\n"
            "        - status\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("TELEGRAM_ADMIN_ID", "555444333")

        config = load_gateway_config()
        extra = config.platforms[Platform.TELEGRAM].extra
        policy = policy_from_extra(extra, "group")

        assert policy.enabled is True
        assert policy.is_admin("555444333") is True, (
            "The admin's real id must match the resolved policy -- this "
            "was the silent-lockout bug: the admin got refused with the "
            "same message as a non-admin"
        )

    def test_unresolved_var_reference_does_not_enable_impossible_admin_gate(
        self, monkeypatch, tmp_path
    ):
        """If the env var is genuinely unset (operator error, or a var name
        typo), the reference stays unresolved after loading -- the
        defense-in-depth filter in gateway.slash_access._coerce_id_list()
        must then treat it as no admin configured (policy disabled), not
        as an admin list nothing can ever match."""
        from gateway.config import load_gateway_config, Platform
        from gateway.slash_access import policy_from_extra

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "platforms:\n"
            "  telegram:\n"
            "    enabled: true\n"
            "    token: xxx\n"
            "    extra:\n"
            "      group_allow_admin_from:\n"
            "        - '${TELEGRAM_ADMIN_ID_TYPO}'\n"
            "      group_user_allowed_commands:\n"
            "        - status\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("TELEGRAM_ADMIN_ID_TYPO", raising=False)

        config = load_gateway_config()
        extra = config.platforms[Platform.TELEGRAM].extra
        policy = policy_from_extra(extra, "group")

        assert policy.enabled is False, (
            "An admin list consisting only of an unresolved ${VAR} must "
            "not silently enable gating with an admin nothing can match"
        )
