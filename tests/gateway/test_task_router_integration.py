"""Integration tests for the Telegram task risk router wiring.

Complements ``tests/gateway/test_task_router.py`` (pure ``gateway.task_router``
unit tests) by exercising the actual GatewayRunner hook
(``_route_telegram_task_risk`` / ``_resume_approved_telegram_task`` /
``_mark_task_route_executed`` in gateway/run.py) and the Telegram adapter's
``tr:approve:``/``tr:reject:`` callback branch, plus a regression lock on the
existing ``build_session_key`` topic-isolation/General-lane compatibility
that the router must not disturb.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, build_session_key
from gateway.task_router import TaskApprovalStatus, get_task_registry, select_agent_for_risk, RiskLevel
from tests.gateway.test_telegram_topic_mode import _make_event, _make_runner, _make_source


# ---------------------------------------------------------------------------
# Regression lock: task routing must not touch session-key derivation.
# ---------------------------------------------------------------------------

class TestExistingTopicKeyCompatibilityIsUnchanged:
    def test_dm_topic_key_still_includes_thread_id(self):
        source = _make_source(thread_id="17585")
        assert build_session_key(source) == "agent:main:telegram:dm:208214988:17585"

    def test_dm_without_thread_still_falls_back_to_the_general_lane_key(self):
        source = _make_source(thread_id=None)
        assert build_session_key(source) == "agent:main:telegram:dm:208214988"

    def test_distinct_topics_still_produce_distinct_keys(self):
        key_a = build_session_key(_make_source(thread_id="111"))
        key_b = build_session_key(_make_source(thread_id="222"))
        general_key = build_session_key(_make_source(thread_id=None))
        assert len({key_a, key_b, general_key}) == 3


@pytest.fixture
def fake_runtime_kwargs(monkeypatch):
    """Deterministic credential stand-in so tests never touch real config."""
    import gateway.run as gateway_run

    def _fake(provider):
        return {
            "provider": provider,
            "api_key": f"fake-key-for-{provider}",
            "base_url": f"https://example.test/{provider}",
            "api_mode": "chat_completions",
            "credential_pool": None,
        }

    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs_for_provider", _fake)
    return _fake


class TestGatewayRouteTelegramTaskRisk:
    @pytest.mark.asyncio
    async def test_low_risk_task_routes_to_codex_without_pausing(self, fake_runtime_kwargs):
        runner = _make_runner()
        event = _make_event("please summarize this article", thread_id="17585")
        source = event.source
        session_key = runner._session_key_for_source(source)

        paused, task_id = await runner._route_telegram_task_risk(
            source=source, session_key=session_key,
            message_text=event.text, event=event,
        )

        assert paused is False
        assert task_id is not None
        override = runner._session_model_overrides[session_key]
        assert override["provider"] == "openai-codex"
        assert override["model"] == "gpt-5.6-luna"

    @pytest.mark.asyncio
    async def test_high_risk_task_pauses_and_sends_approval_prompt(self, fake_runtime_kwargs):
        runner = _make_runner()
        adapter = runner.adapters[Platform.TELEGRAM]
        adapter.send_task_approval = AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="99")
        )
        event = _make_event("please deploy this to production", thread_id="17585")
        source = event.source
        session_key = runner._session_key_for_source(source)

        paused, task_id = await runner._route_telegram_task_risk(
            source=source, session_key=session_key,
            message_text=event.text, event=event,
        )

        assert paused is True
        assert task_id is None  # nothing ran; nothing to mark-executed
        adapter.send_task_approval.assert_awaited_once()
        kwargs = adapter.send_task_approval.await_args.kwargs
        assert kwargs["risk"] == "high"
        assert kwargs["chat_id"] == source.chat_id
        assert session_key not in dict(runner._session_model_overrides)

    @pytest.mark.asyncio
    async def test_critical_risk_task_also_pauses(self, fake_runtime_kwargs):
        runner = _make_runner()
        adapter = runner.adapters[Platform.TELEGRAM]
        adapter.send_task_approval = AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="99")
        )
        event = _make_event("rm -rf the old deployment directory", thread_id="17585")
        source = event.source
        session_key = runner._session_key_for_source(source)

        paused, task_id = await runner._route_telegram_task_risk(
            source=source, session_key=session_key,
            message_text=event.text, event=event,
        )

        assert paused is True
        assert adapter.send_task_approval.await_args.kwargs["risk"] == "critical"

    @pytest.mark.asyncio
    async def test_duplicate_delivery_of_pending_task_sends_only_one_prompt(
        self, fake_runtime_kwargs,
    ):
        runner = _make_runner()
        adapter = runner.adapters[Platform.TELEGRAM]
        adapter.send_task_approval = AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="99")
        )
        source = _make_source(thread_id="17585")
        event = MessageEvent(text="please deploy this to production", source=source, message_id="dup-1")
        session_key = runner._session_key_for_source(source)

        for _ in range(2):
            paused, _ = await runner._route_telegram_task_risk(
                source=source, session_key=session_key,
                message_text=event.text, event=event,
            )
            assert paused is True

        adapter.send_task_approval.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_telegram_platform_is_never_gated(self, fake_runtime_kwargs):
        runner = _make_runner()
        source = SessionSource(platform=Platform.DISCORD, chat_id="1", chat_type="dm", user_id="1")
        event = MessageEvent(text="please deploy this to production", source=source, message_id="d1")

        paused, task_id = await runner._route_telegram_task_risk(
            source=source, session_key="agent:main:discord:dm:1",
            message_text=event.text, event=event,
        )

        assert (paused, task_id) == (False, None)

    @pytest.mark.asyncio
    async def test_internal_system_events_bypass_routing(self, fake_runtime_kwargs):
        runner = _make_runner()
        source = _make_source(thread_id=None)
        event = MessageEvent(
            text="[SYSTEM: kanban task completed]", source=source,
            message_id="wake-1", internal=True,
        )
        session_key = runner._session_key_for_source(source)

        paused, task_id = await runner._route_telegram_task_risk(
            source=source, session_key=session_key, message_text=event.text, event=event,
        )

        assert (paused, task_id) == (False, None)
        assert session_key not in dict(runner._session_model_overrides)

    @pytest.mark.asyncio
    async def test_private_dm_low_risk_message_still_flows_through(self, fake_runtime_kwargs):
        """Regression: an ordinary private-chat task must not be paused, and
        its session key must be derived exactly as before (existing
        DM/topic behavior preserved)."""
        runner = _make_runner()
        event = _make_event("what's on my calendar today?", thread_id=None)
        source = event.source
        assert source.chat_type == "dm"
        session_key_before = runner._session_key_for_source(source)

        paused, task_id = await runner._route_telegram_task_risk(
            source=source, session_key=session_key_before,
            message_text=event.text, event=event,
        )

        assert paused is False
        assert task_id is not None
        assert runner._session_key_for_source(source) == session_key_before
        assert session_key_before == build_session_key(source)


class TestGatewayResumeAfterApproval:
    @pytest.mark.asyncio
    async def test_resume_after_approval_dispatches_claude_sonnet_task_once(
        self, fake_runtime_kwargs,
    ):
        runner = _make_runner()
        runner._handle_message = AsyncMock(return_value="done")
        registry = get_task_registry()
        route = registry.create_or_get(
            dedupe_key=f"resume-test-{id(runner)}",
            session_key="agent:main:telegram:dm:208214988:17585",
            chat_id="208214988", thread_id="17585", user_id="208214988",
            request_text="please deploy this to production",
            risk=RiskLevel.HIGH, agent=select_agent_for_risk(RiskLevel.HIGH),
        )
        registry.approve(route.task_id, "telegram:208214988:Owner")

        await runner._resume_approved_telegram_task(route.task_id)

        runner._handle_message.assert_awaited_once()
        dispatched_event = runner._handle_message.await_args.args[0]
        assert dispatched_event.text == route.request_text
        assert dispatched_event.source.chat_id == "208214988"
        assert dispatched_event.source.thread_id == "17585"
        assert dispatched_event.metadata == {"task_router_resume_task_id": route.task_id}
        assert registry.get(route.task_id).status == TaskApprovalStatus.EXECUTING

    @pytest.mark.asyncio
    async def test_resume_is_noop_for_rejected_task(self, fake_runtime_kwargs):
        runner = _make_runner()
        runner._handle_message = AsyncMock(return_value="done")
        registry = get_task_registry()
        route = registry.create_or_get(
            dedupe_key=f"resume-reject-test-{id(runner)}",
            session_key="agent:main:telegram:dm:1:2",
            chat_id="1", thread_id="2", user_id="1",
            request_text="please deploy this to production",
            risk=RiskLevel.HIGH, agent=select_agent_for_risk(RiskLevel.HIGH),
        )
        registry.reject(route.task_id, "telegram:1:Owner")

        await runner._resume_approved_telegram_task(route.task_id)

        runner._handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_resumed_task_route_applies_claude_sonnet_override(self, fake_runtime_kwargs):
        runner = _make_runner()
        registry = get_task_registry()
        route = registry.create_or_get(
            dedupe_key=f"resume-override-test-{id(runner)}",
            session_key="agent:main:telegram:dm:1:2",
            chat_id="1", thread_id="2", user_id="1",
            request_text="please deploy this to production",
            risk=RiskLevel.HIGH, agent=select_agent_for_risk(RiskLevel.HIGH),
        )
        registry.approve(route.task_id, "telegram:1:Owner")
        assert registry.consume_for_execution(route.task_id) is not None

        source = SessionSource(
            platform=Platform.TELEGRAM, chat_id="1", chat_type="dm", user_id="1", thread_id="2",
        )
        event = MessageEvent(
            text=route.request_text, source=source, message_id="resume-evt",
            metadata={"task_router_resume_task_id": route.task_id},
        )

        paused, task_id = await runner._route_telegram_task_risk(
            source=source, session_key="agent:main:telegram:dm:1:2",
            message_text=event.text, event=event,
        )

        assert paused is False
        assert task_id == route.task_id
        override = runner._session_model_overrides["agent:main:telegram:dm:1:2"]
        assert override["provider"] == "anthropic"
        assert override["model"] == "claude-sonnet-5"


# ---------------------------------------------------------------------------
# Telegram adapter: tr:approve:/tr:reject: callback branch
# ---------------------------------------------------------------------------

def _make_adapter(extra=None):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _make_query(data, *, chat_id=12345, user_id="12345", first_name="Owner"):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = chat_id
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.from_user.first_name = first_name
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


class TestTelegramTaskApprovalCallback:
    @pytest.mark.asyncio
    async def test_approve_callback_resolves_registry_and_resumes_task(self, monkeypatch):
        import os

        adapter = _make_adapter()
        registry = get_task_registry()
        route = registry.create_or_get(
            dedupe_key="cb-approve-1", session_key="agent:main:telegram:dm:12345:9",
            chat_id="12345", thread_id="9", user_id="12345",
            request_text="please deploy this", risk=RiskLevel.HIGH,
            agent=select_agent_for_risk(RiskLevel.HIGH),
        )
        adapter._task_approval_state[route.task_id] = route.session_key

        resume_mock = AsyncMock()
        runner_stub = SimpleNamespace(_resume_approved_telegram_task=resume_mock)
        bound = SimpleNamespace(__self__=runner_stub)
        adapter._message_handler = bound

        update, query = _make_query(f"tr:approve:{route.task_id}")
        context = MagicMock()

        import os as _os
        monkeypatch.setattr(_os, "environ", {**_os.environ, "TELEGRAM_ALLOWED_USERS": "*"})
        await adapter._handle_callback_query(update, context)

        assert registry.get(route.task_id).status == TaskApprovalStatus.APPROVED
        resume_mock.assert_awaited_once_with(route.task_id)
        query.answer.assert_called_once()
        assert "Approved" in query.answer.call_args.kwargs.get("text", "")

    @pytest.mark.asyncio
    async def test_reject_callback_resolves_registry_and_never_resumes(self, monkeypatch):
        import os as _os

        adapter = _make_adapter()
        registry = get_task_registry()
        route = registry.create_or_get(
            dedupe_key="cb-reject-1", session_key="agent:main:telegram:dm:12345:9",
            chat_id="12345", thread_id="9", user_id="12345",
            request_text="rm -rf /data", risk=RiskLevel.CRITICAL,
            agent=select_agent_for_risk(RiskLevel.CRITICAL),
        )
        adapter._task_approval_state[route.task_id] = route.session_key

        resume_mock = AsyncMock()
        runner_stub = SimpleNamespace(_resume_approved_telegram_task=resume_mock)
        adapter._message_handler = SimpleNamespace(__self__=runner_stub)

        update, query = _make_query(f"tr:reject:{route.task_id}")
        context = MagicMock()

        monkeypatch.setattr(_os, "environ", {**_os.environ, "TELEGRAM_ALLOWED_USERS": "*"})
        await adapter._handle_callback_query(update, context)

        assert registry.get(route.task_id).status == TaskApprovalStatus.REJECTED
        resume_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_double_click_approve_only_resolves_once(self, monkeypatch):
        import os as _os

        adapter = _make_adapter()
        registry = get_task_registry()
        route = registry.create_or_get(
            dedupe_key="cb-double-1", session_key="agent:main:telegram:dm:12345:9",
            chat_id="12345", thread_id="9", user_id="12345",
            request_text="please deploy this", risk=RiskLevel.HIGH,
            agent=select_agent_for_risk(RiskLevel.HIGH),
        )
        adapter._task_approval_state[route.task_id] = route.session_key
        resume_mock = AsyncMock()
        adapter._message_handler = SimpleNamespace(
            __self__=SimpleNamespace(_resume_approved_telegram_task=resume_mock)
        )
        monkeypatch.setattr(_os, "environ", {**_os.environ, "TELEGRAM_ALLOWED_USERS": "*"})

        update1, query1 = _make_query(f"tr:approve:{route.task_id}")
        await adapter._handle_callback_query(update1, MagicMock())
        update2, query2 = _make_query(f"tr:approve:{route.task_id}")
        await adapter._handle_callback_query(update2, MagicMock())

        resume_mock.assert_awaited_once_with(route.task_id)
        assert "already been resolved" in query2.answer.call_args.kwargs.get("text", "")
