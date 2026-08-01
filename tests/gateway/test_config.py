"""Tests for gateway configuration management."""

import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.secret_scope import (
    reset_secret_scope,
    set_multiplex_active,
    set_secret_scope,
)
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from gateway.config import (
    ChannelOverride,
    GatewayConfig,
    HomeChannel,
    Platform,
    PlatformConfig,
    SessionResetPolicy,
    StreamingConfig,
    _apply_env_overrides,
    load_gateway_config,
    persist_home_channel,
)


class TestHomeChannelRoundtrip:
    def test_to_dict_from_dict(self):
        hc = HomeChannel(
            platform=Platform.DISCORD,
            chat_id="999",
            name="general",
            user_id="user-123",
            scope_id="guild-456",
        )
        d = hc.to_dict()
        restored = HomeChannel.from_dict(d)

        assert restored.platform == Platform.DISCORD
        assert restored.chat_id == "999"
        assert restored.name == "general"
        assert restored.user_id == "user-123"
        assert restored.scope_id == "guild-456"


class TestPlatformConfigRoundtrip:
    def test_to_dict_from_dict(self):
        pc = PlatformConfig(
            enabled=True,
            token="tok_123",
            home_channel=HomeChannel(
                platform=Platform.TELEGRAM,
                chat_id="555",
                name="Home",
            ),
            extra={"foo": "bar"},
        )
        d = pc.to_dict()
        restored = PlatformConfig.from_dict(d)

        assert restored.enabled is True
        assert restored.token == "tok_123"
        assert restored.home_channel.chat_id == "555"
        assert restored.extra == {"foo": "bar"}

    def test_disabled_no_token(self):
        pc = PlatformConfig()
        d = pc.to_dict()
        restored = PlatformConfig.from_dict(d)
        assert restored.enabled is False
        assert restored.token is None

    def test_from_dict_coerces_quoted_false_enabled(self):
        restored = PlatformConfig.from_dict({"enabled": "false"})
        assert restored.enabled is False


    def test_gateway_restart_notification_roundtrip_false(self):
        pc = PlatformConfig(enabled=True, gateway_restart_notification=False)
        restored = PlatformConfig.from_dict(pc.to_dict())
        assert restored.gateway_restart_notification is False


    def test_typing_status_text_resolved_from_extra(self):
        # Same bridge route as typing_indicator: the shared-key loop copies a
        # nested platforms.<plat> value into extra.
        restored = PlatformConfig.from_dict(
            {"extra": {"typing_status_text": "chasing yarn…"}}
        )
        assert restored.typing_status_text == "chasing yarn…"


    def test_channel_overrides_roundtrip(self):
        pc = PlatformConfig(
            enabled=True,
            channel_overrides={
                "1234567890": ChannelOverride(
                    model="openrouter/healer-alpha",
                    provider="openrouter",
                    system_prompt="You are a daily news summarizer.",
                ),
                "9876543210": ChannelOverride(
                    model="anthropic/claude-opus-4.6",
                    provider="anthropic",
                    system_prompt="You are a coding assistant.",
                ),
            },
        )
        d = pc.to_dict()
        assert "channel_overrides" in d
        assert d["channel_overrides"]["1234567890"]["model"] == "openrouter/healer-alpha"
        assert d["channel_overrides"]["9876543210"]["system_prompt"] == "You are a coding assistant."
        restored = PlatformConfig.from_dict(d)
        assert restored.channel_overrides["1234567890"].model == "openrouter/healer-alpha"
        assert restored.channel_overrides["9876543210"].provider == "anthropic"


class TestChannelOverride:
    def test_from_dict_empty(self):
        assert ChannelOverride.from_dict({}).model is None
        assert ChannelOverride.from_dict(None).model is None


class TestPlatformConfigMalformedSections:
    def test_from_dict_ignores_malformed_nested_sections(self):
        restored = PlatformConfig.from_dict(
            {
                "enabled": True,
                "home_channel": "telegram:123",
                "extra": "oops",
            }
        )

        assert restored.enabled is True
        assert restored.home_channel is None
        assert restored.extra == {}


class TestGetConnectedPlatforms:
    def test_returns_enabled_with_token(self):
        config = GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(enabled=True, token="t"),
                Platform.DISCORD: PlatformConfig(enabled=False, token="d"),
                Platform.SLACK: PlatformConfig(enabled=True),  # no token
            },
        )
        connected = config.get_connected_platforms()
        assert Platform.TELEGRAM in connected
        assert Platform.DISCORD not in connected
        assert Platform.SLACK not in connected


    def test_dingtalk_recognised_via_env_vars(self, monkeypatch):
        """DingTalk configured via env vars (no extras) should still be
        recognised as connected — covers the case where _apply_env_overrides
        hasn't populated extras yet."""
        monkeypatch.setenv("DINGTALK_CLIENT_ID", "env_cid")
        monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "env_sec")
        config = GatewayConfig(
            platforms={
                Platform.DINGTALK: PlatformConfig(enabled=True, extra={}),
            },
        )
        assert Platform.DINGTALK in config.get_connected_platforms()


class TestSessionResetPolicy:
    def test_roundtrip(self):
        policy = SessionResetPolicy(mode="idle", at_hour=6, idle_minutes=120,
                                    bg_process_max_age_hours=48)
        d = policy.to_dict()
        restored = SessionResetPolicy.from_dict(d)
        assert restored.mode == "idle"
        assert restored.at_hour == 6
        assert restored.idle_minutes == 120
        assert restored.bg_process_max_age_hours == 48


    def test_from_dict_treats_null_values_as_defaults(self):
        restored = SessionResetPolicy.from_dict(
            {"mode": None, "at_hour": None, "idle_minutes": None,
             "bg_process_max_age_hours": None}
        )
        assert restored.mode == "none"
        assert restored.at_hour == 4
        assert restored.idle_minutes == 1440
        assert restored.bg_process_max_age_hours == 24


class TestStreamingConfig:


    def test_from_dict_malformed_numeric_values_fall_back_to_defaults(self):
        restored = StreamingConfig.from_dict(
            {
                "edit_interval": "oops",
                "buffer_threshold": "oops",
                "fresh_final_after_seconds": "oops",
            }
        )
        assert restored.edit_interval == 0.8
        assert restored.buffer_threshold == 24
        assert restored.fresh_final_after_seconds == 0.0


class TestGatewayConfigRoundtrip:

    def test_systemd_watchdog_from_dict_disables_invalid_values(self):
        invalid_values = [
            None,
            0,
            -1,
            True,
            1.5,
            float("nan"),
            float("inf"),
            "120.0",
            "1e3",
            "bad",
            2_147_483_648,
        ]

        for raw in invalid_values:
            config = GatewayConfig.from_dict({"systemd_watchdog_seconds": raw})
            assert config.systemd_watchdog_seconds == 0


    def test_max_concurrent_sessions_from_dict_ignores_invalid_values(self, caplog):
        caplog.set_level(logging.WARNING, logger="gateway.config")

        config = GatewayConfig.from_dict({"max_concurrent_sessions": "many"})

        assert config.max_concurrent_sessions is None
        assert any(
            "Ignoring invalid max_concurrent_sessions='many'" in record.message
            for record in caplog.records
        )


    def test_roundtrip_preserves_unauthorized_dm_behavior(self):
        config = GatewayConfig(
            unauthorized_dm_behavior="ignore",
            platforms={
                Platform.WHATSAPP: PlatformConfig(
                    enabled=True,
                    extra={"unauthorized_dm_behavior": "pair"},
                ),
            },
        )

        restored = GatewayConfig.from_dict(config.to_dict())

        assert restored.unauthorized_dm_behavior == "ignore"
        assert restored.platforms[Platform.WHATSAPP].extra["unauthorized_dm_behavior"] == "pair"

    def test_email_defaults_to_ignore_for_unauthorized_dm_behavior(self):
        config = GatewayConfig(
            platforms={Platform.EMAIL: PlatformConfig(enabled=True)},
        )

        assert config.get_unauthorized_dm_behavior(Platform.EMAIL) == "ignore"

    def test_email_can_opt_into_pairing_for_unauthorized_dm_behavior(self):
        config = GatewayConfig(
            platforms={
                Platform.EMAIL: PlatformConfig(
                    enabled=True,
                    extra={"unauthorized_dm_behavior": "pair"},
                ),
            },
        )

        assert config.get_unauthorized_dm_behavior(Platform.EMAIL) == "pair"


class TestLoadGatewayConfig:
    def test_shipped_template_does_not_enable_auto_reset(self, tmp_path, monkeypatch):
        """A fresh install seeded from cli-config.yaml.example must not
        auto-reset sessions.

        Installers (scripts/install.sh, scripts/install.ps1,
        docker/stage2-hook.sh, hermes doctor) copy the template verbatim to
        ~/.hermes/config.yaml, so whatever ``session_reset.mode`` the template
        ships becomes an EXPLICIT user setting that overrides the code
        default. After #60194 flipped the default to "none", the template
        still said "both" — every new install kept 24h-idle resets on
        (Luciano's report, July 2026). This pins the invariant: template
        seed == no auto-reset.
        """
        template = (
            Path(__file__).resolve().parents[2] / "cli-config.yaml.example"
        )
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            template.read_text(encoding="utf-8"), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.default_reset_policy.mode == "none"

    def test_no_config_yaml_means_no_auto_reset(self, tmp_path, monkeypatch):
        """With no config.yaml at all, sessions must never auto-reset."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.default_reset_policy.mode == "none"


    def test_explicit_session_reset_opt_in_is_honored(self, tmp_path, monkeypatch):
        """Users who explicitly opt in to auto-reset keep their policy."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "session_reset:\n  mode: idle\n  idle_minutes: 30\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.default_reset_policy.mode == "idle"
        assert config.default_reset_policy.idle_minutes == 30


    def test_slack_ignored_channels_config_sets_env_bridge(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "slack:\n"
            "  ignored_channels:\n"
            "    - C0123456789\n"
            "    - C0987654321\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("SLACK_IGNORED_CHANNELS", raising=False)

        load_gateway_config()

        assert os.getenv("SLACK_IGNORED_CHANNELS") == "C0123456789,C0987654321"


    def test_typing_status_text_from_nested_platforms_block(self, tmp_path, monkeypatch):
        """``platforms.slack.typing_status_text`` reaches PlatformConfig via
        _merge_platform_map + the from_dict top-level read."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "platforms:\n"
            "  slack:\n"
            "    enabled: true\n"
            '    typing_status_text: "chasing yarn…"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert (
            config.platforms[Platform.SLACK].typing_status_text == "chasing yarn…"
        )

    def test_multiplex_profiles_from_nested_gateway_section(self, tmp_path, monkeypatch):
        """``gateway.multiplex_profiles: true`` (the nested form written by
        ``hermes config set gateway.multiplex_profiles true``) must enable
        multiplexing when loaded via load_gateway_config().

        Regression: load_gateway_config() only surfaced the *top-level*
        ``multiplex_profiles`` key into gw_data, so a config.yaml that pinned
        the flag under the nested ``gateway:`` section silently loaded with
        multiplex_profiles=False. (from_dict honors the nested fallback, but
        load_gateway_config builds gw_data from the top-level keys before
        calling from_dict, so the nested value never reached it.)
        """
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "gateway:\n  multiplex_profiles: true\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.multiplex_profiles is True

    def test_discord_websocket_health_settings_seed_platform_extra(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "discord:\n"
            "  websocket_liveness_interval_seconds: 17\n"
            "  websocket_liveness_failure_threshold: 4\n"
            "  websocket_heartbeat_ack_max_age_seconds: 75\n"
            "  websocket_max_latency_seconds: 30\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        for key in (
            "HERMES_DISCORD_LIVENESS_INTERVAL_SECONDS",
            "HERMES_DISCORD_LIVENESS_FAILURE_THRESHOLD",
        ):
            monkeypatch.delenv(key, raising=False)

        config = load_gateway_config()

        extra = config.platforms[Platform.DISCORD].extra
        assert extra["websocket_liveness_interval_seconds"] == 17
        assert extra["websocket_liveness_failure_threshold"] == 4
        assert extra["websocket_heartbeat_ack_max_age_seconds"] == 75
        assert extra["websocket_max_latency_seconds"] == 30

    def test_session_reset_from_nested_gateway_section(self, tmp_path, monkeypatch):
        """``gateway.session_reset`` (nested form) must reach default_reset_policy,
        mirroring the gateway.multiplex_profiles precedent."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "gateway:\n  session_reset:\n    mode: idle\n    idle_minutes: 30\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.default_reset_policy.mode == "idle"
        assert config.default_reset_policy.idle_minutes == 30

    def test_quick_commands_from_nested_gateway_section(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "gateway:\n  quick_commands:\n    limits:\n      type: exec\n      command: echo ok\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.quick_commands == {"limits": {"type": "exec", "command": "echo ok"}}

    def test_stt_from_nested_gateway_section(self, tmp_path, monkeypatch):
        """Asserts False (not the True default) so the test fails if the
        nested gateway.stt value never reaches from_dict() and silently
        falls back to the class default instead."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "gateway:\n  stt:\n    enabled: false\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.stt_enabled is False


    @staticmethod
    def _clear_api_server_env(monkeypatch):
        """Keep _apply_env_overrides from masking the YAML path under test."""
        for key in (
            "API_SERVER_ENABLED",
            "API_SERVER_KEY",
            "API_SERVER_PORT",
            "API_SERVER_HOST",
            "API_SERVER_CORS_ORIGINS",
            "API_SERVER_MODEL_NAME",
        ):
            monkeypatch.delenv(key, raising=False)

    def test_api_server_from_nested_gateway_section(self, tmp_path, monkeypatch):
        """``gateway.api_server:`` (nested YAML form) must be discovered and
        enable the platform.

        Regression for #66630: load_gateway_config handled gateway.streaming
        and gateway.platforms.* but silently dropped nested gateway.<platform>
        blocks, so ``gateway.api_server.enabled: true`` never started the API
        server unless API_SERVER_* env vars were also set.
        """
        self._clear_api_server_env(monkeypatch)
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "gateway:\n  api_server:\n    enabled: true\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert Platform.API_SERVER in config.platforms
        assert config.platforms[Platform.API_SERVER].enabled is True

    def test_api_server_port_bridged_into_extra(self, tmp_path, monkeypatch):
        """``gateway.api_server.port`` must land in PlatformConfig.extra —
        the adapter reads port/key/host/cors_origins/model_name from extra
        (gateway/platforms/api_server.py), and from_dict discards unknown
        top-level keys, so without the bridge the port is silently lost."""
        self._clear_api_server_env(monkeypatch)
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "gateway:\n"
            "  api_server:\n"
            "    enabled: true\n"
            "    port: 8642\n"
            "    host: 0.0.0.0\n"
            "    key: sekrit\n"
            "    model_name: my-hermes\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        extra = config.platforms[Platform.API_SERVER].extra
        assert extra["port"] == 8642
        assert extra["host"] == "0.0.0.0"
        assert extra["key"] == "sekrit"
        assert extra["model_name"] == "my-hermes"


    def test_non_platform_gateway_keys_not_misparsed_as_platforms(self, tmp_path, monkeypatch):
        """Nested-platform discovery must only pick up keys matching the
        Platform enum: ``gateway.streaming`` / ``gateway.timeout`` must not
        be turned into phantom platform entries or break loading."""
        self._clear_api_server_env(monkeypatch)
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "gateway:\n"
            "  streaming:\n"
            "    enabled: false\n"
            "  timeout: 120\n"
            "  api_server:\n"
            "    enabled: true\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        # streaming still parsed as the streaming subsection, not a platform
        assert config.streaming.enabled is False
        # only real platforms present; nothing named streaming/timeout leaked in
        assert all(isinstance(p, Platform) for p in config.platforms)
        assert config.platforms[Platform.API_SERVER].enabled is True


    def test_group_sessions_per_user_from_nested_gateway_section(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "gateway:\n  group_sessions_per_user: false\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.group_sessions_per_user is False


    def test_reset_triggers_from_nested_gateway_section(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "gateway:\n  reset_triggers:\n    - /new\n    - /clear\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.reset_triggers == ["/new", "/clear"]

    def test_always_log_local_from_nested_gateway_section(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "gateway:\n  always_log_local: false\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.always_log_local is False


    def test_unauthorized_dm_behavior_from_nested_gateway_section(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "gateway:\n  unauthorized_dm_behavior: ignore\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.unauthorized_dm_behavior == "ignore"


    def test_present_empty_top_level_session_reset_blocks_nested_fallback(self, tmp_path, monkeypatch):
        """Key-presence precedence: a present (even empty) top-level
        session_reset must NOT be replaced by gateway.session_reset —
        the fallback fires only when the top-level key is absent."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "session_reset: {}\n"
            "gateway:\n"
            "  session_reset:\n"
            "    mode: idle\n"
            "    idle_minutes: 30\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        # The nested value must not leak through the present top-level key.
        assert config.default_reset_policy.mode != "idle"


    def test_relay_platform_enabled_from_env_url(self, tmp_path, monkeypatch):
        """GATEWAY_RELAY_URL must enable Platform.RELAY in config.platforms so
        start_gateway()'s connect loop actually dials the connector. Registering
        the adapter in the platform_registry is NOT enough — the connect loop
        iterates config.platforms, so an un-enabled RELAY never connects (the
        'relay registered but no inbound' bug)."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("GATEWAY_RELAY_URL", "https://connector.example/relay/")

        config = load_gateway_config()

        assert Platform.RELAY in config.platforms
        relay = config.platforms[Platform.RELAY]
        assert relay.enabled is True
        # Trailing slash stripped; mirrored into extra for the connected-checker.
        assert relay.extra.get("relay_url") == "https://connector.example/relay"
        assert Platform.RELAY in config.get_connected_platforms()


    def test_thread_require_mention_yaml_does_not_overwrite_env(self, tmp_path, monkeypatch):
        """Explicit env var should win over config.yaml (env > yaml precedence)."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "discord:\n"
            "  thread_require_mention: false\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("DISCORD_THREAD_REQUIRE_MENTION", "true")  # user override

        load_gateway_config()

        # Env value preserved, not clobbered by yaml.
        assert os.environ.get("DISCORD_THREAD_REQUIRE_MENTION") == "true"


    def test_bridges_nested_gateway_platforms_dingtalk_allowed_users_to_env(self, tmp_path, monkeypatch):
        """gateway.platforms.dingtalk.extra.allowed_users must reach
        DINGTALK_ALLOWED_USERS — it's the documented config.yaml alternative
        to the env var (website/docs/user-guide/messaging/dingtalk.md), the
        adapter reads it from PlatformConfig.extra, but gateway auth
        (_is_user_authorized) only consults the env var.
        """
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "gateway:\n"
            "  platforms:\n"
            "    dingtalk:\n"
            "      enabled: true\n"
            "      extra:\n"
            "        allowed_users:\n"
            "          - user-id-1\n"
            "          - user-id-2\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("DINGTALK_ALLOWED_USERS", raising=False)

        config = load_gateway_config()

        assert config.platforms[Platform.DINGTALK].extra["allowed_users"] == [
            "user-id-1",
            "user-id-2",
        ]
        assert os.environ.get("DINGTALK_ALLOWED_USERS") == "user-id-1,user-id-2"


    def test_top_level_platforms_override_nested_gateway_platforms(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "gateway:\n"
            "  platforms:\n"
            "    telegram:\n"
            "      enabled: false\n"
            "      token: nested-token\n"
            "      extra:\n"
            "        reply_prefix: nested\n"
            "platforms:\n"
            "  telegram:\n"
            "    enabled: true\n"
            "    token: top-token\n"
            "    extra:\n"
            "      reply_prefix: top\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        telegram = config.platforms[Platform.TELEGRAM]
        assert telegram.enabled is True
        assert telegram.token == "top-token"
        assert telegram.extra["reply_prefix"] == "top"

    def test_shared_key_loop_bridges_allow_from_from_nested_platforms(self, tmp_path, monkeypatch):
        """Regression: shared-key loop must bridge allow_from / require_mention
        into PlatformConfig.extra even when the platform is configured only
        under ``platforms:`` (no top-level ``telegram:`` block).

        Before the fix, ``platform_cfg = yaml_cfg.get('telegram')`` returned
        None for nested-only configs, so the loop skipped the platform entirely
        and allow_from was silently ignored.  The apply_yaml_config_fn dispatch
        received the same fix in #44f3e51; the shared-key loop now mirrors it.
        """
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "platforms:\n"
            "  telegram:\n"
            "    allow_from:\n"
            "      - \"111222333\"\n"
            "      - \"444555666\"\n"
            "    require_mention: true\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        telegram = config.platforms[Platform.TELEGRAM]
        assert telegram.extra.get("allow_from") == ["111222333", "444555666"], (
            "allow_from configured under platforms.telegram must be bridged "
            "into PlatformConfig.extra by the shared-key loop"
        )
        assert telegram.extra.get("require_mention") is True, (
            "require_mention configured under platforms.telegram must be "
            "bridged into PlatformConfig.extra by the shared-key loop"
        )


    def test_bridges_unauthorized_dm_behavior_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "unauthorized_dm_behavior: ignore\n"
            "whatsapp:\n"
            "  unauthorized_dm_behavior: pair\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.unauthorized_dm_behavior == "ignore"
        assert config.platforms[Platform.WHATSAPP].extra["unauthorized_dm_behavior"] == "pair"


    def test_loads_telegram_rich_messages_from_gateway_platform_extra(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "gateway:\n"
            "  platforms:\n"
            "    telegram:\n"
            "      extra:\n"
            "        rich_messages: false\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.platforms[Platform.TELEGRAM].extra["rich_messages"] is False


    def test_telegram_proxy_env_takes_precedence_over_config(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "telegram:\n"
            "  proxy_url: http://from-config:8080\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("TELEGRAM_PROXY", "socks5://from-env:1080")

        load_gateway_config()

        import os
        assert os.environ.get("TELEGRAM_PROXY") == "socks5://from-env:1080"

    def test_profile_scoped_env_overrides_do_not_fall_back_to_default_profile_env(
        self,
        tmp_path,
        monkeypatch,
    ):
        default_home = tmp_path / "default-home"
        default_home.mkdir()
        default_config = default_home / "config.yaml"
        default_config.write_text(
            "multiplex_profiles: true\n",
            encoding="utf-8",
        )

        secondary_home = tmp_path / "secondary-home"
        secondary_home.mkdir()
        secondary_config = secondary_home / "config.yaml"
        secondary_config.write_text(
            "multiplex_profiles: true\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(default_home))
        monkeypatch.setenv("API_SERVER_ENABLED", "true")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "default-token")

        # Model the real multiplexed gateway: run.py flips the runtime flag at
        # startup (set_multiplex_active) BEFORE any secondary-profile config
        # loads.  Without the flag, a scope miss legitimately falls through to
        # os.environ (single-profile overlay semantics, #67827) and this test
        # would no longer exercise the cross-profile isolation it's about.
        set_multiplex_active(True)
        home_token = set_hermes_home_override(str(secondary_home))
        secret_token = set_secret_scope({"DISCORD_BOT_TOKEN": "worker-token"})
        try:
            config = load_gateway_config()
        finally:
            reset_secret_scope(secret_token)
            reset_hermes_home_override(home_token)
            set_multiplex_active(False)

        assert config.multiplex_profiles is True
        assert config.platforms[Platform.DISCORD].token == "worker-token"
        assert Platform.API_SERVER not in config.platforms


class TestWebhookPortBridging:
    """Top-level port/host in the YAML platform section must be bridged into
    PlatformConfig.extra so the webhook/api_server adapters can read them.

    The adapters (WebhookAdapter, ApiServerAdapter) read port/host from
    ``config.extra``, but users naturally write them at the top level of the
    platform section in config.yaml:

        platforms:
          webhook:
            enabled: true
            host: 0.0.0.0
            port: 8649

    Without bridging, the port silently falls back to DEFAULT_PORT (8644),
    causing port conflicts between profiles that configure different ports."""

    def test_webhook_port_bridged_from_toplevel(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "platforms:\n"
            "  webhook:\n"
            "    enabled: true\n"
            "    host: 0.0.0.0\n"
            "    port: 8649\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("WEBHOOK_ENABLED", raising=False)
        monkeypatch.delenv("WEBHOOK_PORT", raising=False)

        config = load_gateway_config()

        assert Platform.WEBHOOK in config.platforms
        wh = config.platforms[Platform.WEBHOOK]
        assert wh.enabled is True
        assert wh.extra.get("port") == 8649
        assert wh.extra.get("host") == "0.0.0.0"


    def test_msgraph_webhook_port_host_secret_bridged_from_toplevel(self, tmp_path, monkeypatch):
        """msgraph_webhook top-level port/host/secret must be bridged into extra,
        with an explicit extra: value still winning over the top-level one."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "platforms:\n"
            "  msgraph_webhook:\n"
            "    enabled: true\n"
            "    host: 0.0.0.0\n"
            "    port: 8651\n"
            "    secret: toplevel-secret\n"
            "    extra:\n"
            "      client_state: my-client-state\n"
            "      secret: extra-secret\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("MSGRAPH_WEBHOOK_ENABLED", raising=False)
        monkeypatch.delenv("MSGRAPH_WEBHOOK_PORT", raising=False)
        monkeypatch.delenv("MSGRAPH_WEBHOOK_CLIENT_STATE", raising=False)

        config = load_gateway_config()

        assert Platform.MSGRAPH_WEBHOOK in config.platforms
        ms = config.platforms[Platform.MSGRAPH_WEBHOOK]
        assert ms.enabled is True
        assert ms.extra.get("port") == 8651
        assert ms.extra.get("host") == "0.0.0.0"
        # explicit extra: wins over top-level
        assert ms.extra.get("secret") == "extra-secret"
        assert ms.extra.get("client_state") == "my-client-state"


class TestHomeChannelEnvOverrides:
    """Home channel env vars should apply even when the platform was already
    configured via config.yaml (not just when credential env vars create it)."""

    def test_existing_platform_configs_accept_home_channel_env_overrides(self):
        cases = [
            (
                Platform.SLACK,
                PlatformConfig(enabled=True, token="xoxb-from-config"),
                {"SLACK_HOME_CHANNEL": "C123", "SLACK_HOME_CHANNEL_NAME": "Ops"},
                ("C123", "Ops"),
            ),
            (
                Platform.WHATSAPP,
                PlatformConfig(enabled=True),
                {
                    "WHATSAPP_HOME_CHANNEL": "1234567890@lid",
                    "WHATSAPP_HOME_CHANNEL_NAME": "Owner DM",
                },
                ("1234567890@lid", "Owner DM"),
            ),
            (
                Platform.SIGNAL,
                PlatformConfig(
                    enabled=True,
                    extra={"http_url": "http://localhost:9090", "account": "+15551234567"},
                ),
                {"SIGNAL_HOME_CHANNEL": "+1555000", "SIGNAL_HOME_CHANNEL_NAME": "Phone"},
                ("+1555000", "Phone"),
            ),
            (
                Platform.MATTERMOST,
                PlatformConfig(
                    enabled=True,
                    token="mm-token",
                    extra={"url": "https://mm.example.com"},
                ),
                {"MATTERMOST_HOME_CHANNEL": "ch_abc123", "MATTERMOST_HOME_CHANNEL_NAME": "General"},
                ("ch_abc123", "General"),
            ),
            (
                Platform.MATRIX,
                PlatformConfig(
                    enabled=True,
                    token="syt_abc123",
                    extra={"homeserver": "https://matrix.example.org"},
                ),
                {"MATRIX_HOME_ROOM": "!room123:example.org", "MATRIX_HOME_ROOM_NAME": "Bot Room"},
                ("!room123:example.org", "Bot Room"),
            ),
            (
                Platform.EMAIL,
                PlatformConfig(
                    enabled=True,
                    extra={
                        "address": "hermes@test.com",
                        "imap_host": "imap.test.com",
                        "smtp_host": "smtp.test.com",
                    },
                ),
                {"EMAIL_HOME_ADDRESS": "user@test.com", "EMAIL_HOME_ADDRESS_NAME": "Inbox"},
                ("user@test.com", "Inbox"),
            ),
            (
                Platform.SMS,
                PlatformConfig(enabled=True, api_key="token_abc"),
                {"SMS_HOME_CHANNEL": "+15559876543", "SMS_HOME_CHANNEL_NAME": "My Phone"},
                ("+15559876543", "My Phone"),
            ),
        ]

        for platform, platform_config, env, expected in cases:
            config = GatewayConfig(platforms={platform: platform_config})
            with patch.dict(os.environ, env, clear=True):
                _apply_env_overrides(config)

            home = config.platforms[platform].home_channel
            assert home is not None, f"{platform.value}: home_channel should not be None"
            assert (home.chat_id, home.name) == expected, platform.value


class TestMultiplexProfilesEnvOverride:
    """GATEWAY_MULTIPLEX_PROFILES env override — the 3-tier precedence chain.

    env (recognized token) > config.yaml (top-level or nested gateway.*) >
    default False. A blank / unrecognized env value is treated as UNSET and
    falls through to config (the empty-secret trap: a provisioned-but-empty Fly
    secret arrives as "" and must not shadow a config.yaml opt-in).
    """

    def _load(self, tmp_path, monkeypatch, config_text=None):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir(exist_ok=True)
        if config_text is not None:
            (hermes_home / "config.yaml").write_text(config_text, encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        return load_gateway_config()

    # ── Tier 1: env wins ──────────────────────────────────────────────────

    def test_env_true_overrides_config_false(self, tmp_path, monkeypatch):
        # THE discriminating test: env-set wins over an explicit config value.
        monkeypatch.setenv("GATEWAY_MULTIPLEX_PROFILES", "1")
        config = self._load(
            tmp_path, monkeypatch, config_text="multiplex_profiles: false\n"
        )
        assert config.multiplex_profiles is True


    # ── Tier 2: config.yaml when env unset ────────────────────────────────
    def test_config_true_when_env_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GATEWAY_MULTIPLEX_PROFILES", raising=False)
        config = self._load(
            tmp_path, monkeypatch, config_text="multiplex_profiles: true\n"
        )
        assert config.multiplex_profiles is True

    # ── The empty / unrecognized env trap: fall through, don't force off ──
    def test_empty_env_does_not_shadow_config_true(self, tmp_path, monkeypatch):
        # Provisioned-but-unpopulated Fly secret arrives as "". It must NOT
        # turn OFF a config.yaml opt-in.
        monkeypatch.setenv("GATEWAY_MULTIPLEX_PROFILES", "")
        config = self._load(
            tmp_path, monkeypatch, config_text="multiplex_profiles: true\n"
        )
        assert config.multiplex_profiles is True


    # ── Tier 3: default False ─────────────────────────────────────────────

    # ── The resolver in isolation ─────────────────────────────────────────


class TestMultiplexProfilesConfig:
    """Tests for parsing multiplex_profiles (top-level and nested forms)."""


    def test_multiplex_profiles_nested_under_gateway(self, tmp_path, monkeypatch):
        """gateway.multiplex_profiles (the form written by `hermes config set
        gateway.multiplex_profiles true`) must be honored. Regression test for
        the silent-fallback bug where the loader only forwarded the top-level
        key, so users who wrote it under gateway: got multiplex_profiles=False
        with no warning."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "gateway:\n  multiplex_profiles: true\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.multiplex_profiles is True, (
            "gateway.multiplex_profiles: true was silently ignored — "
            "loader only forwarded the top-level form"
        )


    def test_multiplex_profiles_explicit_top_level_false_not_consulting_nested(
        self, tmp_path, monkeypatch
    ):
        """Lock in the `is None` vs `is False` distinction: when top-level is
        explicitly false, the loader must forward False WITHOUT consulting the
        nested form (so a stale `gateway.multiplex_profiles: true` cannot
        silently re-enable multiplexing). Guards against a future regression
        that flips the check to `not _mp`."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "multiplex_profiles: false\n"
            "gateway:\n  multiplex_profiles: true\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.multiplex_profiles is False, (
            "Explicit top-level false was overridden by nested true — "
            "loader must respect top-level precedence when key is present"
        )


class TestApiServerEnvOverride:
    def test_env_key_does_not_reenable_explicitly_disabled_api_server(self):
        """An explicit ``platforms.api_server.enabled: false`` must survive
        _apply_env_overrides() even when API_SERVER_KEY is present in the env.

        Regression: _apply_env_overrides() force-set api_server.enabled = True
        whenever API_SERVER_KEY (or API_SERVER_ENABLED) was set. In multiplex
        mode a secondary profile pins ``api_server.enabled: false`` so it shares
        the default profile's listener instead of binding its own port, but it
        still inherits the process-level API_SERVER_KEY. The unconditional
        re-enable flipped it back on and tripped the MultiplexConfigError check.

        The fix honors the explicit disable, flagged by ``_enabled_explicit`` in
        the platform's extra (set when the config.yaml pins enabled).
        """
        config = GatewayConfig(
            platforms={
                Platform.API_SERVER: PlatformConfig(
                    enabled=False,
                    extra={"_enabled_explicit": True},
                ),
            },
        )

        api_server_key = "secret-key-at-least-16"
        with patch.dict(os.environ, {"API_SERVER_KEY": api_server_key}, clear=True):
            _apply_env_overrides(config)

        # Explicit disable wins over the env-var presence.
        assert config.platforms[Platform.API_SERVER].enabled is False
        # The key is still wired through for the shared listener.
        assert config.platforms[Platform.API_SERVER].extra.get("key") == api_server_key


class TestYamlEnvProvenance:
    @pytest.fixture(autouse=True)
    def _clear_yaml_env_records(self):
        from gateway.yaml_env import clear_records

        clear_records()
        yield
        clear_records()

    def test_load_identity_guards_refresh_and_source(self, monkeypatch):
        from gateway.yaml_env import YamlEnvLoad, records_snapshot

        monkeypatch.delenv("TEST_YAML_VALUE", raising=False)
        load = YamlEnvLoad("gateway-config:/profile", lambda path: "managed-config")
        assert load.set_env_from_yaml(
            "TEST_YAML_VALUE", "A", "telegram.free_response_chats", predicate=lambda: True
        )
        record = records_snapshot()["TEST_YAML_VALUE"]
        assert record.owner == "gateway-config:/profile"
        assert record.value == "A"
        assert record.source == "managed-config:telegram.free_response_chats"

    def test_successful_load_reconciles_removed_generated_writers(self, monkeypatch):
        from gateway.yaml_env import YamlEnvLoad, records_snapshot
        import tempfile

        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home) / ".hermes"
            home.mkdir()
            config_path = home / "config.yaml"
            config_path.write_text(
                "telegram:\n  free_response_chats:\n    - -1001\n",
                encoding="utf-8",
            )
            monkeypatch.setenv("HERMES_HOME", str(home))
            monkeypatch.delenv("TELEGRAM_FREE_RESPONSE_CHATS", raising=False)
            load_gateway_config()
            assert os.environ["TELEGRAM_FREE_RESPONSE_CHATS"] == "-1001"
            config_path.write_text("telegram:\n  require_mention: true\n", encoding="utf-8")
            load_gateway_config()
            assert "TELEGRAM_FREE_RESPONSE_CHATS" not in os.environ

        monkeypatch.delenv("TEST_YAML_VALUE", raising=False)
        load = YamlEnvLoad("gateway-config:/profile")
        load.set_env_from_yaml("TEST_YAML_VALUE", "A", "telegram.free_response_chats", predicate=lambda: True)
        load2 = YamlEnvLoad("gateway-config:/profile")
        load2.finish()
        assert "TEST_YAML_VALUE" not in os.environ
        assert "TEST_YAML_VALUE" not in records_snapshot()

        os.environ["TEST_YAML_VALUE"] = "external"
        load3 = YamlEnvLoad("gateway-config:/other")
        load3.finish()
        assert os.environ["TEST_YAML_VALUE"] == "external"

    def test_incomplete_load_aborts_absence_cleanup(self, monkeypatch):
        from gateway.yaml_env import YamlEnvLoad, records_snapshot

        monkeypatch.delenv("TEST_YAML_VALUE", raising=False)
        first = YamlEnvLoad("gateway-config:/profile")
        first.set_env_from_yaml("TEST_YAML_VALUE", "A", "telegram.free_response_chats", predicate=lambda: True)
        second = YamlEnvLoad("gateway-config:/profile")
        second.abort()
        second.finish()
        assert os.environ["TEST_YAML_VALUE"] == "A"
        assert records_snapshot()["TEST_YAML_VALUE"].value == "A"

    def test_plugin_discovery_failure_aborts_absence_cleanup(self, tmp_path, monkeypatch):
        from gateway.yaml_env import YamlEnvLoad, clear_records, records_snapshot
        from hermes_cli import plugins

        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text("telegram: {}\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("TEST_YAML_VALUE", "A")
        prior = YamlEnvLoad(f"gateway-config:{home.resolve()}")
        prior.set_env_from_yaml(
            "TEST_YAML_VALUE", "A", "test.value", predicate=lambda: True
        )

        monkeypatch.setattr(
            plugins,
            "discover_plugins",
            lambda: (_ for _ in ()).throw(RuntimeError("discovery failed")),
        )
        load_gateway_config()

        assert os.environ["TEST_YAML_VALUE"] == "A"
        assert records_snapshot()["TEST_YAML_VALUE"].value == "A"
        clear_records()

    def test_yaml_env_writer_preserves_original_guard_semantics(self, monkeypatch):
        from gateway.yaml_env import YamlEnvLoad

        load = YamlEnvLoad("gateway-config:/guards")
        monkeypatch.setenv("PRESENCE_VALUE", "")
        assert not load.set_env_from_yaml("PRESENCE_VALUE", "new", "a", predicate=lambda: "PRESENCE_VALUE" not in os.environ)
        monkeypatch.setenv("FALSY_VALUE", "")
        assert load.set_env_from_yaml("FALSY_VALUE", "new", "b", predicate=lambda: not os.getenv("FALSY_VALUE"))
        monkeypatch.setenv("BLANK_VALUE", "  ")
        assert load.set_env_from_yaml("BLANK_VALUE", "new", "c", predicate=lambda: not os.environ.get("BLANK_VALUE", "").strip())

    def test_same_owner_reload_refreshes_and_reconciles(self, monkeypatch):
        from gateway.yaml_env import YamlEnvLoad

        monkeypatch.delenv("TEST_YAML_VALUE", raising=False)
        first = YamlEnvLoad("gateway-config:/profile")
        first.set_env_from_yaml("TEST_YAML_VALUE", "A", "a", predicate=lambda: True)
        second = YamlEnvLoad("gateway-config:/profile")
        second.set_env_from_yaml("TEST_YAML_VALUE", "B", "b", predicate=lambda: False)
        assert os.environ["TEST_YAML_VALUE"] == "B"
        third = YamlEnvLoad("gateway-config:/profile")
        third.finish()
        assert "TEST_YAML_VALUE" not in os.environ

    def test_profile_load_does_not_adopt_or_reconcile_other_owner_record(self, monkeypatch):
        from gateway.yaml_env import YamlEnvLoad, records_snapshot

        monkeypatch.delenv("TEST_YAML_VALUE", raising=False)
        first = YamlEnvLoad("gateway-config:/one")
        first.set_env_from_yaml("TEST_YAML_VALUE", "A", "a", predicate=lambda: True)
        second = YamlEnvLoad("gateway-config:/two")
        second.set_env_from_yaml("TEST_YAML_VALUE", "B", "b", predicate=lambda: not os.getenv("TEST_YAML_VALUE"))
        assert os.environ["TEST_YAML_VALUE"] == "A"
        assert records_snapshot()["TEST_YAML_VALUE"].owner == "gateway-config:/one"

    def test_equal_external_values_never_acquire_provenance(self, monkeypatch):
        from gateway.yaml_env import YamlEnvLoad, records_snapshot, without_yaml_generated_env

        monkeypatch.setenv("TEST_YAML_VALUE", "same")
        load = YamlEnvLoad("gateway-config:/equal")
        load.set_env_from_yaml("TEST_YAML_VALUE", "same", "a", predicate=lambda: not os.getenv("TEST_YAML_VALUE"))
        assert "TEST_YAML_VALUE" not in records_snapshot()
        assert without_yaml_generated_env(dict(os.environ))["TEST_YAML_VALUE"] == "same"

    def test_managed_source_selection_does_not_override_external_environment(self, monkeypatch):
        from gateway.yaml_env import YamlEnvLoad, records_snapshot

        monkeypatch.setenv("TEST_YAML_VALUE", "external")
        load = YamlEnvLoad("gateway-config:/managed", lambda path: "managed-config")
        load.set_env_from_yaml("TEST_YAML_VALUE", "managed", "telegram.free_response_chats", predicate=lambda: not os.getenv("TEST_YAML_VALUE"))
        assert os.environ["TEST_YAML_VALUE"] == "external"
        assert "TEST_YAML_VALUE" not in records_snapshot()

    def test_nested_platform_source_matches_effective_merge_precedence(self, tmp_path, monkeypatch):
        from hermes_cli import managed_scope
        from gateway.yaml_env import records_snapshot

        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "gateway:\n"
            "  platforms:\n"
            "    telegram:\n"
            "      free_response_chats: [user]\n",
            encoding="utf-8",
        )

        managed_path = "platforms.telegram.free_response_chats"

        def apply_overlay(config):
            config = dict(config)
            platforms = dict(config.get("platforms") or {})
            platforms["telegram"] = {"free_response_chats": ["managed"]}
            config["platforms"] = platforms
            return config

        monkeypatch.setattr(managed_scope, "apply_managed_overlay", apply_overlay)
        monkeypatch.setattr(managed_scope, "managed_config_keys", lambda: {managed_path})
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("TELEGRAM_FREE_RESPONSE_CHATS", raising=False)

        load_gateway_config()

        assert os.environ["TELEGRAM_FREE_RESPONSE_CHATS"] == "managed"
        assert records_snapshot()["TELEGRAM_FREE_RESPONSE_CHATS"].source == (
            "managed-config:platforms.telegram.free_response_chats"
        )

    def test_mixed_platform_blocks_resolve_source_per_leaf(self, tmp_path, monkeypatch):
        from hermes_cli import managed_scope
        from gateway.yaml_env import records_snapshot

        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "gateway:\n"
            "  platforms:\n"
            "    telegram:\n"
            "      free_response_chats: [user-nested]\n"
            "platforms:\n"
            "  telegram:\n"
            "    require_mention: true\n"
            "telegram:\n"
            "  reply_to_mode: thread\n",
            encoding="utf-8",
        )

        managed_path = "gateway.platforms.telegram.free_response_chats"

        def apply_overlay(config):
            config = dict(config)
            gateway = dict(config.get("gateway") or {})
            gateway_platforms = dict(gateway.get("platforms") or {})
            telegram = dict(gateway_platforms.get("telegram") or {})
            telegram["free_response_chats"] = ["managed"]
            gateway_platforms["telegram"] = telegram
            gateway["platforms"] = gateway_platforms
            config["gateway"] = gateway
            return config

        monkeypatch.setattr(managed_scope, "apply_managed_overlay", apply_overlay)
        monkeypatch.setattr(managed_scope, "managed_config_keys", lambda: {managed_path})
        monkeypatch.setenv("HERMES_HOME", str(home))
        for name in (
            "TELEGRAM_FREE_RESPONSE_CHATS",
            "TELEGRAM_REQUIRE_MENTION",
            "TELEGRAM_REPLY_TO_MODE",
        ):
            monkeypatch.delenv(name, raising=False)

        load_gateway_config()

        records = records_snapshot()
        assert records["TELEGRAM_FREE_RESPONSE_CHATS"].source == (
            "managed-config:gateway.platforms.telegram.free_response_chats"
        )
        assert records["TELEGRAM_REQUIRE_MENTION"].source == (
            "user-config:platforms.telegram.require_mention"
        )
        assert records["TELEGRAM_REPLY_TO_MODE"].source == (
            "user-config:telegram.reply_to_mode"
        )

    def test_discord_websocket_liveness_extra_sources_resolve_per_leaf(self, tmp_path, monkeypatch):
        from hermes_cli import managed_scope
        from gateway.yaml_env import records_snapshot

        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "platforms:\n"
            "  discord:\n"
            "    require_mention: true\n",
            encoding="utf-8",
        )

        managed_paths = {
            "gateway.platforms.discord.extra.websocket_liveness_interval_seconds",
            "gateway.platforms.discord.extra.websocket_liveness_failure_threshold",
        }

        def apply_overlay(config):
            config = dict(config)
            gateway = dict(config.get("gateway") or {})
            gateway_platforms = dict(gateway.get("platforms") or {})
            discord = dict(gateway_platforms.get("discord") or {})
            discord["extra"] = {
                **(discord.get("extra") or {}),
                "websocket_liveness_interval_seconds": 11,
                "websocket_liveness_failure_threshold": 8,
            }
            gateway_platforms["discord"] = discord
            gateway["platforms"] = gateway_platforms
            config["gateway"] = gateway
            return config

        monkeypatch.setattr(managed_scope, "apply_managed_overlay", apply_overlay)
        monkeypatch.setattr(managed_scope, "managed_config_keys", lambda: managed_paths)
        monkeypatch.setenv("HERMES_HOME", str(home))
        for name in (
            "HERMES_DISCORD_LIVENESS_INTERVAL_SECONDS",
            "HERMES_DISCORD_LIVENESS_FAILURE_THRESHOLD",
        ):
            monkeypatch.delenv(name, raising=False)

        load_gateway_config()

        records = records_snapshot()
        assert records["HERMES_DISCORD_LIVENESS_INTERVAL_SECONDS"].source == (
            "managed-config:gateway.platforms.discord.extra.websocket_liveness_interval_seconds"
        )
        assert records["HERMES_DISCORD_LIVENESS_FAILURE_THRESHOLD"].source == (
            "managed-config:gateway.platforms.discord.extra.websocket_liveness_failure_threshold"
        )
        assert records["DISCORD_REQUIRE_MENTION"].source == (
            "user-config:platforms.discord.require_mention"
        )

    def test_telegram_group_extra_sources_resolve_per_leaf(self, tmp_path, monkeypatch):
        from hermes_cli import managed_scope
        from gateway.yaml_env import records_snapshot

        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "platforms:\n"
            "  telegram:\n"
            "    require_mention: true\n",
            encoding="utf-8",
        )

        managed_paths = {
            "gateway.platforms.telegram.extra.group_allow_from",
            "gateway.platforms.telegram.extra.group_allowed_chats",
        }

        def apply_overlay(config):
            config = dict(config)
            gateway = dict(config.get("gateway") or {})
            gateway_platforms = dict(gateway.get("platforms") or {})
            telegram = dict(gateway_platforms.get("telegram") or {})
            telegram["extra"] = {
                **(telegram.get("extra") or {}),
                "group_allow_from": ["alice"],
                "group_allowed_chats": ["-1001"],
            }
            gateway_platforms["telegram"] = telegram
            gateway["platforms"] = gateway_platforms
            config["gateway"] = gateway
            return config

        monkeypatch.setattr(managed_scope, "apply_managed_overlay", apply_overlay)
        monkeypatch.setattr(managed_scope, "managed_config_keys", lambda: managed_paths)
        monkeypatch.setenv("HERMES_HOME", str(home))
        for name in (
            "TELEGRAM_GROUP_ALLOWED_USERS",
            "TELEGRAM_GROUP_ALLOWED_CHATS",
        ):
            monkeypatch.delenv(name, raising=False)

        load_gateway_config()

        records = records_snapshot()
        assert records["TELEGRAM_GROUP_ALLOWED_USERS"].source == (
            "managed-config:gateway.platforms.telegram.extra.group_allow_from"
        )
        assert records["TELEGRAM_GROUP_ALLOWED_CHATS"].source == (
            "managed-config:gateway.platforms.telegram.extra.group_allowed_chats"
        )
        assert records["TELEGRAM_REQUIRE_MENTION"].source == (
            "user-config:platforms.telegram.require_mention"
        )

    def test_external_overrides_remain_unmarked_under_all_sources(self, monkeypatch):
        from gateway.yaml_env import YamlEnvLoad, records_snapshot

        for name, value in (("SHELL_VALUE", "shell"), ("SERVICE_VALUE", "service"), ("MANAGED_VALUE", "managed")):
            monkeypatch.setenv(name, value)
            load = YamlEnvLoad(f"gateway-config:/{name}")
            load.set_env_from_yaml(name, value, name.lower(), predicate=lambda name=name: name not in os.environ)
        assert all(name not in records_snapshot() for name in ("SHELL_VALUE", "SERVICE_VALUE", "MANAGED_VALUE"))

    def test_sensitive_values_are_never_registered_or_scrubbed(self, monkeypatch):
        from gateway.yaml_env import YamlEnvLoad, records_snapshot, without_yaml_generated_env

        monkeypatch.delenv("TEST_API_KEY", raising=False)
        load = YamlEnvLoad("gateway-config:/secret")
        assert not load.set_env_from_yaml("TEST_API_KEY", "secret", "api_key", predicate=lambda: True)
        os.environ["TEST_API_KEY"] = "secret"
        assert without_yaml_generated_env(dict(os.environ))["TEST_API_KEY"] == "secret"
        assert "TEST_API_KEY" not in records_snapshot()

    def test_legacy_unmarked_value_requires_clean_external_restart(self, monkeypatch):
        from gateway.yaml_env import YamlEnvLoad, without_yaml_generated_env

        monkeypatch.setenv("TEST_YAML_VALUE", "legacy")
        load = YamlEnvLoad("gateway-config:/legacy")
        load.finish()
        assert without_yaml_generated_env(dict(os.environ))["TEST_YAML_VALUE"] == "legacy"

    def test_all_nonsecret_platform_yaml_writers_route_with_exact_source(self, monkeypatch):
        from gateway.yaml_env import (
            YamlEnvLoad,
            get_yaml_env_context,
            records_snapshot,
            yaml_env_context,
        )
        from plugins.platforms.buzz.adapter import _apply_yaml_config as buzz_apply
        from plugins.platforms.dingtalk.adapter import _apply_yaml_config as dingtalk_apply
        from plugins.platforms.discord.adapter import _apply_yaml_config as discord_apply
        from plugins.platforms.feishu.adapter import _apply_yaml_config as feishu_apply
        from plugins.platforms.matrix.adapter import _apply_yaml_config as matrix_apply
        from plugins.platforms.mattermost.adapter import _apply_yaml_config as mattermost_apply
        from plugins.platforms.slack.adapter import _apply_yaml_config as slack_apply
        from plugins.platforms.telegram.adapter import _apply_yaml_config as telegram_apply
        from plugins.platforms.whatsapp.adapter import _apply_yaml_config as whatsapp_apply

        cases = [
            ("buzz", buzz_apply, {"extra": {"relay_url": "https://buzz"}}, "BUZZ_RELAY_URL", "extra.relay_url"),
            ("dingtalk", dingtalk_apply, {"require_mention": True}, "DINGTALK_REQUIRE_MENTION", "require_mention"),
            ("discord", discord_apply, {"require_mention": True}, "DISCORD_REQUIRE_MENTION", "require_mention"),
            ("feishu", feishu_apply, {"allow_bots": True}, "FEISHU_ALLOW_BOTS", "allow_bots"),
            ("matrix", matrix_apply, {"require_mention": True}, "MATRIX_REQUIRE_MENTION", "require_mention"),
            ("mattermost", mattermost_apply, {"require_mention": True}, "MATTERMOST_REQUIRE_MENTION", "require_mention"),
            ("slack", slack_apply, {"require_mention": True}, "SLACK_REQUIRE_MENTION", "require_mention"),
            ("telegram", telegram_apply, {"free_response_chats": ["A", "B"]}, "TELEGRAM_FREE_RESPONSE_CHATS", "free_response_chats"),
            ("whatsapp", whatsapp_apply, {"require_mention": True}, "WHATSAPP_REQUIRE_MENTION", "require_mention"),
        ]
        for platform, hook, platform_cfg, env_name, leaf in cases:
            monkeypatch.delenv(env_name, raising=False)
            load = YamlEnvLoad("gateway-config:/platform")
            with yaml_env_context(load, f"platforms.{platform}"):
                hook({}, platform_cfg)
            context = get_yaml_env_context()
            assert context is None
            record = records_snapshot()[env_name]
            assert record.source.endswith(f"platforms.{platform}.{leaf}")

    def test_core_guarded_writers_route_through_yaml_env_load(self, tmp_path, monkeypatch):
        from gateway.yaml_env import records_snapshot, clear_records

        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "require_mention: true\nsignal:\n  require_mention: true\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("TELEGRAM_REQUIRE_MENTION", raising=False)
        monkeypatch.delenv("SIGNAL_REQUIRE_MENTION", raising=False)
        clear_records()
        load_gateway_config()
        assert {"TELEGRAM_REQUIRE_MENTION", "SIGNAL_REQUIRE_MENTION"} <= set(records_snapshot())


def _run_yaml_env_proof(name, monkeypatch, tmp_path=None):
    from gateway.yaml_env import clear_records

    clear_records()
    try:
        target = getattr(TestYamlEnvProvenance(), name)
        if tmp_path is None:
            target(monkeypatch)
        else:
            target(tmp_path, monkeypatch)
    finally:
        clear_records()


def test_successful_load_reconciles_removed_generated_writers(monkeypatch):
    _run_yaml_env_proof("test_successful_load_reconciles_removed_generated_writers", monkeypatch)


def test_incomplete_load_aborts_absence_cleanup(monkeypatch):
    _run_yaml_env_proof("test_incomplete_load_aborts_absence_cleanup", monkeypatch)


def test_all_nonsecret_platform_yaml_writers_route_with_exact_source(monkeypatch):
    _run_yaml_env_proof("test_all_nonsecret_platform_yaml_writers_route_with_exact_source", monkeypatch)


def test_core_guarded_writers_route_through_yaml_env_load(tmp_path, monkeypatch):
    _run_yaml_env_proof("test_core_guarded_writers_route_through_yaml_env_load", monkeypatch, tmp_path)


def test_yaml_env_writer_preserves_original_guard_semantics(monkeypatch):
    _run_yaml_env_proof("test_yaml_env_writer_preserves_original_guard_semantics", monkeypatch)


def test_managed_source_selection_does_not_override_external_environment(monkeypatch):
    _run_yaml_env_proof("test_managed_source_selection_does_not_override_external_environment", monkeypatch)


def test_equal_external_values_never_acquire_provenance(monkeypatch):
    _run_yaml_env_proof("test_equal_external_values_never_acquire_provenance", monkeypatch)


def test_same_owner_reload_refreshes_and_reconciles(monkeypatch):
    _run_yaml_env_proof("test_same_owner_reload_refreshes_and_reconciles", monkeypatch)


def test_profile_load_does_not_adopt_or_reconcile_other_owner_record(monkeypatch):
    _run_yaml_env_proof("test_profile_load_does_not_adopt_or_reconcile_other_owner_record", monkeypatch)


def test_mixed_platform_blocks_resolve_source_per_leaf(tmp_path, monkeypatch):
    _run_yaml_env_proof("test_mixed_platform_blocks_resolve_source_per_leaf", monkeypatch, tmp_path)


def test_external_overrides_remain_unmarked_under_all_sources(monkeypatch):
    _run_yaml_env_proof("test_external_overrides_remain_unmarked_under_all_sources", monkeypatch)


def test_sensitive_values_are_never_registered_or_scrubbed(monkeypatch):
    _run_yaml_env_proof("test_sensitive_values_are_never_registered_or_scrubbed", monkeypatch)


def test_legacy_unmarked_value_requires_clean_external_restart(monkeypatch):
    _run_yaml_env_proof("test_legacy_unmarked_value_requires_clean_external_restart", monkeypatch)
