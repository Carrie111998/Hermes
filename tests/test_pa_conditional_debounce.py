"""Unit tests for PA conditional turn-debounce (PA-portal, minimal scope).

Covers:
  (a) brief schema — the three optional fields (require_mention,
      debounce_passive_ms, debounce_addressed_ms) parse leniently and fall
      back to None when absent; selector resolution surfaces them per chat;
  (b) WhatsApp require_mention reads the brief first, then config.extra, then
      env — and None in the brief falls back;
  (c) conditional-window selection — passive vs addressed picks the right ms,
      sourced from brief, then config.extra, then default;
  (d) mid-burst collapse — a passive burst that gains an addressed event
      switches to the short (addressed) window and reschedules the flush.

Pure unit tests — no live bridge. The debounce path is exercised against a
fake event loop sleep / handle_message so no real timers fire.
"""

import asyncio

import pytest

from agent.pa_constitution import load_constitution_data, resolve_context
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SessionSource
from gateway.platforms.whatsapp import WhatsAppAdapter


# ── (a) brief schema + selector resolution ──────────────────────────────


def _constitution_dict(**ops_overrides):
    ops_brief = {
        "title": "Ops",
        "purpose": "ingest",
        "instructions": ["do x"],
    }
    ops_brief.update(ops_overrides)
    return {
        "id": "test_pa",
        "agent_name": "Tester",
        "identity": {"role": "pa"},
        "client": {"name": "T"},
        "job_briefs": {
            "ops": ops_brief,
            "mgmt": {
                "title": "Mgmt",
                "purpose": "manage",
                "instructions": ["do y"],
                "require_mention": True,
            },
        },
        "selectors": [
            {
                "job_type": "ops",
                "match": {"source.platform": "whatsapp", "source.chat_id": "ops@g.us"},
            },
            {
                "job_type": "mgmt",
                "match": {"source.platform": "whatsapp", "source.chat_id": "mgmt@g.us"},
            },
        ],
    }


def test_brief_fields_default_none_when_absent():
    c = load_constitution_data(_constitution_dict())
    ops = c.job_briefs["ops"]
    assert ops.require_mention is None
    assert ops.debounce_passive_ms is None
    assert ops.debounce_addressed_ms is None


def test_brief_fields_parse_when_set():
    c = load_constitution_data(
        _constitution_dict(
            require_mention=False,
            debounce_passive_ms=10000,
            debounce_addressed_ms=1500,
        )
    )
    ops = c.job_briefs["ops"]
    assert ops.require_mention is False
    assert ops.debounce_passive_ms == 10000
    assert ops.debounce_addressed_ms == 1500


def test_brief_fields_lenient_parse_strings():
    c = load_constitution_data(
        _constitution_dict(
            require_mention="false",
            debounce_passive_ms="10000",
            debounce_addressed_ms="1500",
        )
    )
    ops = c.job_briefs["ops"]
    assert ops.require_mention is False
    assert ops.debounce_passive_ms == 10000
    assert ops.debounce_addressed_ms == 1500


def test_brief_fields_resolve_per_chat_via_selector():
    c = load_constitution_data(
        _constitution_dict(
            require_mention=False,
            debounce_passive_ms=10000,
            debounce_addressed_ms=1500,
        )
    )
    cfg = {"constitution": c, "enabled": True}
    ops_ctx = resolve_context(
        cfg, {"source": {"platform": "whatsapp", "chat_id": "ops@g.us"}}
    )
    assert ops_ctx.job_brief.require_mention is False
    assert ops_ctx.job_brief.debounce_passive_ms == 10000

    mgmt_ctx = resolve_context(
        cfg, {"source": {"platform": "whatsapp", "chat_id": "mgmt@g.us"}}
    )
    assert mgmt_ctx.job_brief.require_mention is True
    # mgmt brief leaves debounce windows unset -> None (caller falls back).
    assert mgmt_ctx.job_brief.debounce_passive_ms is None
    assert mgmt_ctx.job_brief.debounce_addressed_ms is None


# ── adapter helpers ──────────────────────────────────────────────────────


def _adapter(extra=None, resolver=None):
    extra = dict(extra or {})
    if resolver is not None:
        extra["pa_brief_resolver"] = resolver
    config = PlatformConfig(enabled=True, extra=extra)
    return WhatsAppAdapter(config)


# ── (b) require_mention reads the brief ──────────────────────────────────


def test_require_mention_brief_overrides_config():
    # brief says True even though config.extra says False
    resolver = lambda chat_id: {"require_mention": True} if chat_id == "c1" else None
    adapter = _adapter(extra={"require_mention": False}, resolver=resolver)
    assert adapter._whatsapp_require_mention("c1") is True
    # different chat -> no brief -> falls back to config.extra False
    assert adapter._whatsapp_require_mention("other") is False


def test_require_mention_none_in_brief_falls_back():
    resolver = lambda chat_id: {"require_mention": None}
    adapter = _adapter(extra={"require_mention": True}, resolver=resolver)
    # brief require_mention=None -> falls back to config.extra True
    assert adapter._whatsapp_require_mention("c1") is True


def test_require_mention_no_resolver_uses_config():
    adapter = _adapter(extra={"require_mention": True})
    assert adapter._whatsapp_require_mention("c1") is True
    adapter2 = _adapter(extra={"require_mention": False})
    assert adapter2._whatsapp_require_mention("c1") is False


# ── (c) conditional-window selection ─────────────────────────────────────


def test_window_passive_vs_addressed_from_brief():
    resolver = lambda chat_id: {
        "debounce_passive_ms": 10000,
        "debounce_addressed_ms": 1500,
    }
    adapter = _adapter(resolver=resolver)
    assert adapter._debounce_window_ms("c1", addressed=False) == 10000
    assert adapter._debounce_window_ms("c1", addressed=True) == 1500


def test_window_falls_back_to_config_then_default():
    # no resolver, config sets passive only
    adapter = _adapter(extra={"debounce_passive_ms": 7777})
    assert adapter._debounce_window_ms("c1", addressed=False) == 7777
    # addressed not set anywhere -> universal default 1500
    assert adapter._debounce_window_ms("c1", addressed=True) == 1500


def test_window_defaults_when_nothing_set():
    adapter = _adapter()
    assert adapter._debounce_window_ms("c1", addressed=False) == 10000  # passive default
    assert adapter._debounce_window_ms("c1", addressed=True) == 1500    # addressed default


def test_window_brief_none_falls_back_to_config():
    resolver = lambda chat_id: {
        "debounce_passive_ms": None,
        "debounce_addressed_ms": None,
    }
    adapter = _adapter(extra={"debounce_passive_ms": 5555}, resolver=resolver)
    assert adapter._debounce_window_ms("c1", addressed=False) == 5555


# ── (d) mid-burst collapse ───────────────────────────────────────────────


def _event(chat_id, text, addressed=False):
    source = SessionSource(
        platform=Platform.WHATSAPP,
        chat_id=chat_id,
        chat_name=chat_id,
        chat_type="group",
        user_id="u1",
        user_name="User",
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        addressed=addressed,
    )


def _spy_windows(adapter, monkeypatch):
    """Record the window (ms) chosen at each _queue_or_handle_event call.

    Spies on _debounce_window_ms so the decision is captured synchronously,
    independent of asyncio task cancellation (cancelled flush tasks never run).
    Also stubs the flush timer so no real sleep fires.
    """
    chosen = []
    real = adapter._debounce_window_ms

    def spy(chat_id, addressed):
        ms = real(chat_id, addressed)
        chosen.append(ms)
        return ms

    async def fake_flush_after(chat_id, window_s):
        return

    monkeypatch.setattr(adapter, "_debounce_window_ms", spy)
    monkeypatch.setattr(adapter, "_flush_turn_after", fake_flush_after)
    return chosen


def test_mid_burst_collapse_switches_to_short_window(monkeypatch):
    """A passive burst that gains an addressed event reschedules on the short window."""
    resolver = lambda chat_id: {
        "debounce_passive_ms": 10000,
        "debounce_addressed_ms": 1500,
    }
    adapter = _adapter(resolver=resolver)
    chosen = _spy_windows(adapter, monkeypatch)

    async def run():
        # passive event -> passive window
        await adapter._queue_or_handle_event(_event("c1", "obs1", addressed=False))
        # another passive -> still passive window
        await adapter._queue_or_handle_event(_event("c1", "obs2", addressed=False))
        # addressed event arrives mid-burst -> collapse to short window
        await adapter._queue_or_handle_event(_event("c1", "@bot help", addressed=True))

    asyncio.run(run())

    assert chosen[0] == 10000   # passive
    assert chosen[1] == 10000   # still passive (burst not yet addressed)
    assert chosen[2] == 1500    # collapsed to addressed once an addressed event lands


def test_addressed_first_uses_short_window(monkeypatch):
    resolver = lambda chat_id: {
        "debounce_passive_ms": 10000,
        "debounce_addressed_ms": 1500,
    }
    adapter = _adapter(resolver=resolver)
    chosen = _spy_windows(adapter, monkeypatch)

    async def run():
        await adapter._queue_or_handle_event(_event("c1", "@bot hi", addressed=True))

    asyncio.run(run())
    assert chosen == [1500]


def test_burst_event_bundle_addressed_propagates():
    """A bundled multi-event turn is addressed if any constituent event is."""
    adapter = _adapter()
    events = [
        _event("c1", "obs1", addressed=False),
        _event("c1", "@bot ping", addressed=True),
    ]
    bundled = adapter._build_turn_event(events)
    assert bundled.addressed is True

    passive_only = [
        _event("c1", "obs1", addressed=False),
        _event("c1", "obs2", addressed=False),
    ]
    assert adapter._build_turn_event(passive_only).addressed is False
