from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionSource


SESSION_KEY = "agent:main:telegram:dm:12345"


def _entry(*, reset: bool = True, reason: str = "idle") -> SessionEntry:
    now = datetime.now()
    return SessionEntry(
        session_key=SESSION_KEY,
        session_id="fresh",
        created_at=now,
        updated_at=now,
        platform=Platform.TELEGRAM,
        was_auto_reset=reset,
        auto_reset_reason=reason if reset else None,
        prev_session_id="previous" if reset else None,
    )


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="12345",
    )


def _event(text: str, *, internal: bool = False, reply: bool = False):
    return SimpleNamespace(
        text=text,
        metadata={},
        internal=internal,
        reply_to_message_id="reply-1" if reply else None,
        reply_to_text="Earlier message" if reply else None,
    )


def _runner(entry: SessionEntry):
    runner = gateway_run.GatewayRunner(GatewayConfig())
    backing_store = object()
    previous = SessionEntry(
        session_key=SESSION_KEY,
        session_id="previous",
        created_at=datetime.now() - timedelta(hours=2),
        updated_at=datetime.now() - timedelta(hours=1),
        platform=Platform.TELEGRAM,
    )
    facade = SimpleNamespace(
        _store=backing_store,
        get_or_create_session=AsyncMock(return_value=entry),
        switch_session=AsyncMock(return_value=previous),
    )
    runner.session_store = backing_store
    runner._async_session_store = facade
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_telegram_topic_lane = lambda _source: False
    runner._sync_telegram_topic_binding = MagicMock()
    return runner, facade


class StopAfterResetDecision(RuntimeError):
    pass


@pytest.mark.parametrize("reason", ["idle", "daily"])
@pytest.mark.asyncio
async def test_contextual_followup_switches_to_previous_session(monkeypatch, reason):
    runner, store = _runner(_entry(reason=reason))
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"session_reset": {"auto_resume_previous_if_contextual": True}},
    )

    def stop_after_decision(*_args, **_kwargs):
        raise StopAfterResetDecision

    monkeypatch.setattr(gateway_run, "build_session_context", stop_after_decision)

    with pytest.raises(StopAfterResetDecision):
        await runner._handle_message_with_agent(
            _event("Please continue"), _source(), SESSION_KEY, 1
        )

    store.switch_session.assert_awaited_once_with(SESSION_KEY, "previous")
    runner._sync_telegram_topic_binding.assert_called_once()
    assert (
        runner._sync_telegram_topic_binding.call_args.kwargs["reason"]
        == "contextual-auto-resume"
    )


@pytest.mark.asyncio
async def test_explicit_reply_switches_without_text_heuristic(monkeypatch):
    runner, store = _runner(_entry())
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"session_reset": {"auto_resume_previous_if_contextual": True}},
    )

    def stop_after_decision(*_args, **_kwargs):
        raise StopAfterResetDecision

    monkeypatch.setattr(gateway_run, "build_session_context", stop_after_decision)

    with pytest.raises(StopAfterResetDecision):
        await runner._handle_message_with_agent(
            _event("What time is it in Tokyo?", reply=True),
            _source(),
            SESSION_KEY,
            1,
        )

    store.switch_session.assert_awaited_once_with(SESSION_KEY, "previous")


@pytest.mark.asyncio
async def test_independent_request_keeps_fresh_session(monkeypatch):
    runner, store = _runner(_entry())
    topic_db = SimpleNamespace(
        get_telegram_topic_binding=AsyncMock(
            return_value={"session_id": "previous"}
        )
    )
    runner._session_db = topic_db
    runner._is_telegram_topic_lane = lambda _source: True
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"session_reset": {"auto_resume_previous_if_contextual": True}},
    )

    def stop_after_decision(*_args, **_kwargs):
        raise StopAfterResetDecision

    runner._clear_conversation_scope = stop_after_decision

    with pytest.raises(StopAfterResetDecision):
        await runner._handle_message_with_agent(
            _event("What time is it in Tokyo?"), _source(), SESSION_KEY, 1
        )

    store.switch_session.assert_not_awaited()
    topic_db.get_telegram_topic_binding.assert_not_awaited()
    assert (
        runner._sync_telegram_topic_binding.call_args.kwargs["reason"]
        == "contextual-auto-reset"
    )


@pytest.mark.parametrize(
    ("enabled", "reason"),
    [
        (False, "idle"),
        (True, "suspended"),
    ],
)
@pytest.mark.asyncio
async def test_ineligible_reset_keeps_fresh_session(
    monkeypatch, enabled, reason
):
    runner, store = _runner(_entry(reason=reason))
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "session_reset": {
                "auto_resume_previous_if_contextual": enabled
            }
        },
    )

    def stop_after_decision(*_args, **_kwargs):
        raise StopAfterResetDecision

    runner._clear_conversation_scope = stop_after_decision

    with pytest.raises(StopAfterResetDecision):
        await runner._handle_message_with_agent(
            _event("Please continue"), _source(), SESSION_KEY, 1
        )

    store.switch_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_internal_event_leaves_reset_decision_for_user(monkeypatch):
    entry = _entry()
    runner, store = _runner(entry)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"session_reset": {"auto_resume_previous_if_contextual": True}},
    )

    result = await runner._handle_message_with_agent(
        _event("Please continue", internal=True), _source(), SESSION_KEY, 1
    )

    assert result == ""
    assert entry.was_auto_reset is True
    store.switch_session.assert_not_awaited()


@pytest.mark.parametrize(
    "text",
    [
        "Please continue",
        "Can you do that?",
        "Use the answer from our previous discussion",
        "Please revise the above message",
        "Same thing.",
    ],
)
def test_contextual_detector_accepts_explicit_continuations(text):
    assert gateway_run.GatewayRunner._looks_like_contextual_reset_followup(text)


@pytest.mark.parametrize(
    "text",
    [
        "What time is it in Tokyo?",
        "Show my previous invoices",
        "Continue straight for two miles",
        "Finish this report.",
        "/new",
        ("Write a standalone analysis without relying on any earlier discussion."),
    ],
)
def test_contextual_detector_rejects_standalone_requests(text):
    assert not gateway_run.GatewayRunner._looks_like_contextual_reset_followup(text)
