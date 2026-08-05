"""Regression tests for the Wave-1 s2 mixin extraction (clusters c11 + c12).

Methods moved verbatim from ``GatewayRunner`` (``gateway/run.py``) into:

- ``gateway/telegram_topics_mixin.py`` (``TelegramTopicsMixin``, cluster c11):
  Telegram DM topic-mode classification, lobby/lane routing, topic header and
  lobby reminder message builders, reminder rate-limiting.
- ``gateway/session_config_mixin.py`` (``SessionConfigMixin``, cluster c12):
  ``/reasoning`` arg parsing and the config loaders (busy input/text modes,
  service tier, show_reasoning, restart drain/after-turn timeouts).

These tests pin the PURE behavior of the moved methods so the extraction can
never silently change semantics: every assertion encodes the pre-extraction
behavior observed in ``gateway/run.py``.

Test seam: bare mixin instances are built with ``object.__new__`` and the
class attributes they read (``_TELEGRAM_GENERAL_TOPIC_IDS``,
``_TELEGRAM_LOBBY_REMINDER_COOLDOWN_S``) attached as instance attributes —
those attributes intentionally stay on ``GatewayRunner`` and resolve via the
MRO in production. The lazy ``from gateway.run import
_load_gateway_runtime_config`` seam is monkeypatched through the
``gateway.run`` module attribute, and the mixin's ``cfg_get`` binding is
patched to read the fixture dict, making every loader deterministic.
"""

from types import SimpleNamespace

import pytest

import gateway.run as gateway_run
import gateway.session_config_mixin as scc_mixin
from gateway.config import Platform
from gateway.restart import (
    DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT,
    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
)
from gateway.session_config_mixin import SessionConfigMixin
from gateway.telegram_topics_mixin import TelegramTopicsMixin

# Class attributes that stay on GatewayRunner (MRO-resolved in production) and
# are attached to bare mixin instances here — mirroring the real values.
_TELEGRAM_GENERAL_TOPIC_IDS = frozenset({"", "1"})
_TELEGRAM_LOBBY_REMINDER_COOLDOWN_S = 300.0


def _bare(cls, **attrs):
    """Build a bare mixin instance with the given instance attributes."""
    inst = object.__new__(cls)
    for key, value in attrs.items():
        setattr(inst, key, value)
    return inst


def _telegram_mixin(*, topic_mode=True):
    inst = _bare(
        TelegramTopicsMixin,
        _TELEGRAM_GENERAL_TOPIC_IDS=_TELEGRAM_GENERAL_TOPIC_IDS,
        _TELEGRAM_LOBBY_REMINDER_COOLDOWN_S=_TELEGRAM_LOBBY_REMINDER_COOLDOWN_S,
    )
    inst._telegram_topic_mode_enabled = lambda source: topic_mode
    return inst


def _source(platform=Platform.TELEGRAM, chat_type="dm", chat_id="123",
            user_id="456", thread_id=None):
    return SimpleNamespace(
        platform=platform,
        chat_type=chat_type,
        chat_id=chat_id,
        user_id=user_id,
        thread_id=thread_id,
    )


def _patch_config(monkeypatch, cfg):
    """Point _load_gateway_runtime_config + the mixin's cfg_get at a fixture dict."""
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: cfg)

    def fake_cfg_get(cfg_dict, section, key, default=None):
        return cfg_dict.get(section, {}).get(key, default)

    monkeypatch.setattr(scc_mixin, "cfg_get", fake_cfg_get)


class TestTelegramTopicClassification:
    """Lobby/lane classification and topic message builders (cluster c11)."""

    def test_root_lobby_general_thread_ids(self):
        inst = _telegram_mixin()
        # General topic arrives as empty thread_id or "1" in some clients.
        assert inst._is_telegram_topic_root_lobby(_source(thread_id=None)) is True
        assert inst._is_telegram_topic_root_lobby(_source(thread_id="1")) is True

    def test_root_lobby_plain_dm_without_topic_mode(self):
        # Topic mode off -> the DM is a normal chat, not a lobby.
        inst = _telegram_mixin(topic_mode=False)
        assert inst._is_telegram_topic_root_lobby(_source(thread_id=None)) is False

    def test_root_lobby_non_telegram_or_non_dm(self):
        inst = _telegram_mixin()
        assert inst._is_telegram_topic_root_lobby(
            _source(platform=Platform.DISCORD, thread_id=None)
        ) is False
        assert inst._is_telegram_topic_root_lobby(
            _source(chat_type="group", thread_id=None)
        ) is False

    def test_root_lobby_unknown_thread_is_not_lobby(self):
        inst = _telegram_mixin()
        assert inst._is_telegram_topic_root_lobby(_source(thread_id="42")) is False

    def test_lane_user_created_topic(self):
        inst = _telegram_mixin()
        assert inst._is_telegram_topic_lane(_source(thread_id="42")) is True

    def test_lane_rejects_general_and_unknown(self):
        inst = _telegram_mixin()
        assert inst._is_telegram_topic_lane(_source(thread_id=None)) is False
        assert inst._is_telegram_topic_lane(_source(thread_id="1")) is False

    def test_lane_requires_topic_mode(self):
        inst = _telegram_mixin(topic_mode=False)
        assert inst._is_telegram_topic_lane(_source(thread_id="42")) is False

    def test_new_header_only_for_lanes(self):
        inst = _telegram_mixin()
        header = inst._telegram_topic_new_header(_source(thread_id="42"))
        assert header is not None
        assert "Started a new Hermes session" in header
        assert inst._telegram_topic_new_header(_source(thread_id="1")) is None

    def test_lobby_message_texts_mention_all_messages_topic(self):
        inst = _telegram_mixin()
        assert "All Messages topic" in inst._telegram_topic_root_lobby_message()
        assert "All Messages topic" in inst._telegram_topic_root_new_message()

    def test_lobby_reminder_rate_limited(self):
        inst = _telegram_mixin()
        assert inst._should_send_telegram_lobby_reminder(_source(chat_id="777")) is True
        # Immediately after, still inside the cooldown window -> suppressed.
        assert inst._should_send_telegram_lobby_reminder(_source(chat_id="777")) is False
        # A different chat is not affected by the first chat's cooldown.
        assert inst._should_send_telegram_lobby_reminder(_source(chat_id="888")) is True

    def test_lobby_reminder_without_chat_id(self):
        inst = _telegram_mixin()
        assert inst._should_send_telegram_lobby_reminder(
            _source(chat_id="")
        ) is True


class TestParseReasoningCommandArgs:
    """/reasoning arg parsing (cluster c12) — pure, static."""

    def test_empty_input(self):
        assert SessionConfigMixin._parse_reasoning_command_args(None) == ("", False)
        assert SessionConfigMixin._parse_reasoning_command_args("") == ("", False)

    def test_simple_value(self):
        assert SessionConfigMixin._parse_reasoning_command_args("fast") == ("fast", False)

    def test_value_lowercased(self):
        assert SessionConfigMixin._parse_reasoning_command_args("FAST") == ("fast", False)

    def test_global_any_position(self):
        assert SessionConfigMixin._parse_reasoning_command_args("--global fast") == ("fast", True)
        assert SessionConfigMixin._parse_reasoning_command_args("fast --global") == ("fast", True)

    def test_quoted_value(self):
        assert SessionConfigMixin._parse_reasoning_command_args(
            '"high detail"'
        ) == ("high detail", False)

    def test_em_dash_normalized(self):
        # Unicode em-dash is normalized to -- so it never becomes a value token.
        value, persist = SessionConfigMixin._parse_reasoning_command_args("auto —off")
        assert persist is False
        assert "--off" in value

    def test_extra_tokens_kept(self):
        value, persist = SessionConfigMixin._parse_reasoning_command_args(
            "fast --global extra"
        )
        assert value == "fast extra"
        assert persist is True


class TestSessionConfigLoaders:
    """Deterministic config loaders via the monkeypatched runtime-config seam."""

    def test_busy_input_mode_env_wins(self, monkeypatch):
        _patch_config(monkeypatch, {"display": {}})
        monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "queue")
        assert SessionConfigMixin._load_busy_input_mode() == "queue"
        monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "steer")
        assert SessionConfigMixin._load_busy_input_mode() == "steer"
        monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "bogus")
        assert SessionConfigMixin._load_busy_input_mode() == "interrupt"

    def test_busy_input_mode_from_config(self, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_BUSY_INPUT_MODE", raising=False)
        _patch_config(monkeypatch, {"display": {"busy_input_mode": "steer"}})
        assert SessionConfigMixin._load_busy_input_mode() == "steer"

    def test_busy_input_mode_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_BUSY_INPUT_MODE", raising=False)
        _patch_config(monkeypatch, {"display": {}})
        assert SessionConfigMixin._load_busy_input_mode() == "interrupt"

    def test_busy_text_mode_config(self, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_BUSY_TEXT_MODE", raising=False)
        _patch_config(monkeypatch, {"display": {"busy_text_mode": "queue"}})
        assert SessionConfigMixin._load_busy_text_mode() == "queue"

    def test_busy_text_mode_falls_back_to_busy_input_mode(self, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_BUSY_TEXT_MODE", raising=False)
        _patch_config(monkeypatch, {"display": {}})
        # busy_input_mode resolves to "interrupt" -> text mode follows.
        assert SessionConfigMixin._load_busy_text_mode() == "interrupt"

    def test_show_reasoning(self, monkeypatch):
        _patch_config(monkeypatch, {"display": {"show_reasoning": "true"}})
        assert SessionConfigMixin._load_show_reasoning() is True
        _patch_config(monkeypatch, {"display": {"show_reasoning": "false"}})
        assert SessionConfigMixin._load_show_reasoning() is False
        _patch_config(monkeypatch, {"display": {}})
        assert SessionConfigMixin._load_show_reasoning() is False

    def test_service_tier_mapping(self, monkeypatch):
        for raw, expected in [
            ("fast", "priority"),
            ("priority", "priority"),
            ("on", "priority"),
            ("normal", None),
            ("off", None),
            ("", None),
            ("bogus", None),
        ]:
            _patch_config(monkeypatch, {"agent": {"service_tier": raw}})
            assert SessionConfigMixin._load_service_tier() == expected, raw

    def test_restart_drain_timeout_env_and_default(self, monkeypatch):
        monkeypatch.setenv("HERMES_RESTART_DRAIN_TIMEOUT", "42")
        assert SessionConfigMixin._load_restart_drain_timeout() == 42.0
        monkeypatch.delenv("HERMES_RESTART_DRAIN_TIMEOUT", raising=False)
        _patch_config(monkeypatch, {"agent": {"restart_drain_timeout": "30"}})
        assert SessionConfigMixin._load_restart_drain_timeout() == 30.0
        _patch_config(monkeypatch, {"agent": {}})
        assert (
            SessionConfigMixin._load_restart_drain_timeout()
            == DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
        )

    def test_restart_after_turn_timeout_env_and_default(self, monkeypatch):
        monkeypatch.setenv("HERMES_RESTART_AFTER_TURN_TIMEOUT", "60")
        assert SessionConfigMixin._load_restart_after_turn_timeout() == 60.0
        monkeypatch.delenv("HERMES_RESTART_AFTER_TURN_TIMEOUT", raising=False)
        _patch_config(monkeypatch, {"agent": {"restart_after_turn_timeout": 15}})
        assert SessionConfigMixin._load_restart_after_turn_timeout() == 15.0
        _patch_config(monkeypatch, {"agent": {}})
        assert (
            SessionConfigMixin._load_restart_after_turn_timeout()
            == DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT
        )


class TestWiringSmoke:
    """The extraction must leave GatewayRunner fully wired via the mixins."""

    def test_gateway_runner_still_exposes_moved_methods(self):
        assert hasattr(gateway_run.GatewayRunner, "_load_busy_input_mode")
        assert hasattr(gateway_run.GatewayRunner, "_parse_reasoning_command_args")
        assert hasattr(gateway_run.GatewayRunner, "_is_telegram_topic_lane")
        assert hasattr(gateway_run.GatewayRunner, "_recover_telegram_topic_thread_id")

    def test_mixins_in_mro_and_class_attrs_stay(self):
        mro = gateway_run.GatewayRunner.__mro__
        assert TelegramTopicsMixin in mro
        assert SessionConfigMixin in mro
        assert gateway_run.GatewayRunner._TELEGRAM_GENERAL_TOPIC_IDS == frozenset({"", "1"})

    def test_mixin_bare_instance_behavior_matches_gateway_runner(self):
        # Same inputs -> same outputs whether dispatched on GatewayRunner or the
        # bare mixin: proves the MRO move is behavior-neutral for pure methods.
        runner = object.__new__(gateway_run.GatewayRunner)
        runner._telegram_topic_mode_enabled = lambda source: True
        runner._TELEGRAM_GENERAL_TOPIC_IDS = frozenset({"", "1"})
        runner._TELEGRAM_LOBBY_REMINDER_COOLDOWN_S = 300.0
        source = _source(thread_id="42")
        assert runner._is_telegram_topic_lane(source) is True
        assert TelegramTopicsMixin._is_telegram_topic_lane(
            _telegram_mixin(), source
        ) is True
