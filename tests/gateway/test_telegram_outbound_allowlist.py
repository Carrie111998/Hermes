"""Telegram outbound destination allowlist — fail-closed.

WhatsApp sends traverse the Node bridge's `outboundPolicyDecision()`, which
refuses everything when the destination filter is unconfigured. Telegram talks
to the Bot API directly, so the equivalent floor lives in the adapter. These
tests pin the fail-closed semantics and the single-chokepoint property.
"""

import asyncio
import os

import pytest

from gateway.config import Platform, PlatformConfig, load_gateway_config
from gateway.platforms.telegram import (
    TelegramAdapter,
    TelegramOutboundBlocked,
    _OutboundGuardedBot,
)


def _make_adapter(outbound_allowed_chats=None, outbound_disabled=None):
    extra = {}
    if outbound_allowed_chats is not None:
        extra["outbound_allowed_chats"] = outbound_allowed_chats
    if outbound_disabled is not None:
        extra["outbound_disabled"] = outbound_disabled

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, extra=extra)
    return adapter


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_OUTBOUND_ALLOWED_CHATS", raising=False)
    monkeypatch.delenv("TELEGRAM_OUTBOUND_DISABLED", raising=False)


# --------------------------------------------------------------------------
# Policy decision
# --------------------------------------------------------------------------

def test_unconfigured_allowlist_refuses_everything():
    """The whole point: a misconfiguration silences the agent, never opens it."""
    adapter = _make_adapter()
    decision = adapter._outbound_policy_decision("-100123")
    assert decision.allowed is False
    assert decision.reason == "filter_unconfigured"


def test_empty_allowlist_refuses_everything():
    adapter = _make_adapter(outbound_allowed_chats=[])
    decision = adapter._outbound_policy_decision("-100123")
    assert decision.allowed is False
    assert decision.reason == "not_in_outbound_allowlist"


def test_allowlisted_chat_is_permitted():
    adapter = _make_adapter(outbound_allowed_chats=["-100123"])
    decision = adapter._outbound_policy_decision("-100123")
    assert decision.allowed is True
    assert decision.reason == "explicitly_allowed"


def test_non_allowlisted_chat_is_refused():
    adapter = _make_adapter(outbound_allowed_chats=["-100123"])
    decision = adapter._outbound_policy_decision("-100999")
    assert decision.allowed is False
    assert decision.reason == "not_in_outbound_allowlist"


def test_chat_id_int_str_equivalence():
    """Chat ids arrive as both int and str; the gate must not be fooled."""
    adapter = _make_adapter(outbound_allowed_chats=[-100123])
    assert adapter._outbound_policy_decision("-100123").allowed is True
    assert adapter._outbound_policy_decision(-100123).allowed is True
    assert adapter._outbound_policy_decision("-100124").allowed is False


def test_kill_switch_overrides_allowlist():
    adapter = _make_adapter(outbound_allowed_chats=["-100123"], outbound_disabled=True)
    decision = adapter._outbound_policy_decision("-100123")
    assert decision.allowed is False
    assert decision.reason == "global_disabled"


def test_env_var_configures_allowlist(monkeypatch):
    monkeypatch.setenv("TELEGRAM_OUTBOUND_ALLOWED_CHATS", "-100123,-100456")
    adapter = _make_adapter()
    assert adapter._outbound_policy_decision("-100123").allowed is True
    assert adapter._outbound_policy_decision("-100456").allowed is True
    assert adapter._outbound_policy_decision("-100789").allowed is False


def test_env_kill_switch(monkeypatch):
    monkeypatch.setenv("TELEGRAM_OUTBOUND_ALLOWED_CHATS", "-100123")
    monkeypatch.setenv("TELEGRAM_OUTBOUND_DISABLED", "true")
    adapter = _make_adapter()
    assert adapter._outbound_policy_decision("-100123").reason == "global_disabled"


# --------------------------------------------------------------------------
# send() surfaces a clean refusal
# --------------------------------------------------------------------------

def test_send_to_non_allowlisted_chat_returns_error():
    adapter = _make_adapter(outbound_allowed_chats=["-100123"])
    adapter._bot = object()  # non-None: we must fail on policy, not on "not connected"

    result = asyncio.run(adapter.send("-100999", "hello"))
    assert result.success is False
    assert "not in the outbound allowlist" in result.error
    assert "not_in_outbound_allowlist" in result.error


def test_send_with_unconfigured_allowlist_returns_error():
    adapter = _make_adapter()
    adapter._bot = object()

    result = asyncio.run(adapter.send("-100123", "hello"))
    assert result.success is False
    assert "filter_unconfigured" in result.error


# --------------------------------------------------------------------------
# The guarded-bot proxy is the structural chokepoint
# --------------------------------------------------------------------------

class _FakeBot:
    def __init__(self):
        self.calls = []
        self.username = "christopher_bot"

    async def send_message(self, chat_id=None, text=None, **kwargs):
        self.calls.append(("send_message", chat_id))
        return "sent"

    async def send_photo(self, chat_id=None, **kwargs):
        self.calls.append(("send_photo", chat_id))
        return "sent"

    async def send_chat_action(self, chat_id=None, **kwargs):
        self.calls.append(("send_chat_action", chat_id))
        return "sent"

    async def get_chat(self, chat_id=None):
        self.calls.append(("get_chat", chat_id))
        return "chat"

    async def set_my_commands(self, commands):
        self.calls.append(("set_my_commands", None))
        return "ok"


def _guarded(allowed):
    adapter = _make_adapter(outbound_allowed_chats=allowed)
    fake = _FakeBot()
    return fake, _OutboundGuardedBot(fake, adapter._outbound_policy_decision)


def test_proxy_allows_allowlisted_destination():
    fake, bot = _guarded(["-100123"])
    assert asyncio.run(bot.send_message(chat_id="-100123", text="hi")) == "sent"
    assert fake.calls == [("send_message", "-100123")]


@pytest.mark.parametrize("method", ["send_message", "send_photo", "send_chat_action"])
def test_proxy_blocks_every_emitting_method(method):
    """Not just send() — every chat-targeted verb is gated by the same proxy."""
    fake, bot = _guarded(["-100123"])
    with pytest.raises(TelegramOutboundBlocked) as exc:
        asyncio.run(getattr(bot, method)(chat_id="-100999"))
    assert exc.value.reason == "not_in_outbound_allowlist"
    assert fake.calls == []


def test_proxy_blocks_positional_chat_id():
    fake, bot = _guarded(["-100123"])
    with pytest.raises(TelegramOutboundBlocked):
        asyncio.run(bot.send_message("-100999", "text"))
    assert fake.calls == []


def test_proxy_blocks_when_unconfigured():
    adapter = _make_adapter()
    fake = _FakeBot()
    bot = _OutboundGuardedBot(fake, adapter._outbound_policy_decision)
    with pytest.raises(TelegramOutboundBlocked) as exc:
        asyncio.run(bot.send_message(chat_id="-100123", text="hi"))
    assert exc.value.reason == "filter_unconfigured"
    assert fake.calls == []


def test_proxy_gates_bound_method_handed_to_a_helper():
    """`_call_with_retry(self._bot.send_voice, ...)` must still be gated."""
    fake, bot = _guarded(["-100123"])
    fn = bot.send_photo  # bound and passed around, as the retry helpers do
    with pytest.raises(TelegramOutboundBlocked):
        asyncio.run(fn(chat_id="-100999"))
    assert fake.calls == []


def test_proxy_permits_read_only_methods():
    fake, bot = _guarded(["-100123"])
    assert asyncio.run(bot.get_chat(chat_id="-100999")) == "chat"
    assert asyncio.run(bot.set_my_commands(["x"])) == "ok"


def test_proxy_passes_through_non_callable_attributes():
    fake, bot = _guarded(["-100123"])
    assert bot.username == "christopher_bot"


def test_unknown_emitting_method_is_gated_by_default():
    """A newly added send_* helper is fail-closed without anyone remembering."""

    class _BotWithNewVerb(_FakeBot):
        async def send_brand_new_thing(self, chat_id=None):
            self.calls.append(("send_brand_new_thing", chat_id))
            return "sent"

    adapter = _make_adapter(outbound_allowed_chats=["-100123"])
    fake = _BotWithNewVerb()
    bot = _OutboundGuardedBot(fake, adapter._outbound_policy_decision)
    with pytest.raises(TelegramOutboundBlocked):
        asyncio.run(bot.send_brand_new_thing(chat_id="-100999"))
    assert fake.calls == []


# --------------------------------------------------------------------------
# YAML config wiring
# --------------------------------------------------------------------------

def test_yaml_outbound_allowed_chats_reaches_extra_and_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "telegram:\n"
        "  bot_token: dummy\n"
        "  outbound_allowed_chats:\n"
        "    - \"-1002345678901\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("TELEGRAM_OUTBOUND_ALLOWED_CHATS", raising=False)
    config = load_gateway_config()

    assert config.platforms[Platform.TELEGRAM].extra["outbound_allowed_chats"] == [
        "-1002345678901"
    ]
    assert os.environ["TELEGRAM_OUTBOUND_ALLOWED_CHATS"] == "-1002345678901"
