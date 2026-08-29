"""Tests for the Telegram task-router Approve/Reject inline buttons.

Covers ``TelegramAdapter.send_task_approval`` (the HIGH/CRITICAL approval
prompt) and the ``tr:`` branch of ``_handle_callback_query`` — idempotent
button handling (approve-once, reject-never) and authorization, plus a
regression check that the pre-existing ``ea:`` exec-approval callback path
(shell command approval) is unaffected.
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import Platform, PlatformConfig
from gateway.task_router import TaskApprovalRegistry, RiskLevel, select_agent_for_risk


def _make_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


class _AuthRunner:
    """Minimal runner shim providing both auth and task-resume hooks."""

    def __init__(self, authorized: bool = True):
        self.authorized = authorized
        self.resumed_task_ids = []

    async def _handle_message(self, event):
        return None

    def _is_user_authorized(self, source):
        return self.authorized

    async def _resume_approved_telegram_task(self, task_id):
        self.resumed_task_ids.append(task_id)


def _make_route(registry, *, risk=RiskLevel.HIGH, chat_id="12345", thread_id="",
                 request_text="please deploy to production"):
    agent = select_agent_for_risk(risk)
    session_key = f"agent:main:telegram:group:{chat_id}:{thread_id}" if thread_id \
        else f"agent:main:telegram:dm:{chat_id}"
    return registry.create_or_get(
        dedupe_key=None,
        session_key=session_key,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id="111",
        request_text=request_text,
        risk=risk,
        agent=agent,
    )


def _make_callback_query(*, data, chat_id=12345, chat_type="private", thread_id=None,
                          user_id="111", user_name="Alice"):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = chat_id
    query.message.chat = MagicMock()
    query.message.chat.type = chat_type
    query.message.message_thread_id = thread_id
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.from_user.first_name = user_name
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    context = MagicMock()
    return update, context, query


# ===========================================================================
# send_task_approval
# ===========================================================================

class TestSendTaskApproval:
    @pytest.mark.asyncio
    async def test_sends_approval_summary_and_action_buttons(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 77
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        result = await adapter.send_task_approval(
            chat_id="12345",
            task_id="task-abc",
            request_text="please deploy to production",
            risk="high",
            session_key="agent:main:telegram:dm:12345",
            environment="production",
            reason="deployment change",
            proposed_agent="claude-sonnet-5 via anthropic",
        )

        assert result.success is True
        assert result.message_id == "77"
        adapter._bot.send_message.assert_called_once()
        kwargs = adapter._bot.send_message.call_args[1]
        assert kwargs["chat_id"] == 12345
        assert "APPROVAL REQUIRED" in kwargs["text"]
        assert "HIGH" in kwargs["text"] or "high" in kwargs["text"].lower()
        assert "production" in kwargs["text"]
        assert "claude\\-sonnet\\-5" in kwargs["text"]
        assert "Reason" in kwargs["text"]
        assert "Actions" in kwargs["text"]
        assert kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_records_task_approval_state_for_callback_lookup(self):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))

        await adapter.send_task_approval(
            chat_id="999", task_id="task-xyz", request_text="drop table users",
            risk="critical", session_key="agent:main:telegram:dm:999",
        )

        assert adapter._task_approval_state["task-xyz"] == "agent:main:telegram:dm:999"

    @pytest.mark.asyncio
    async def test_callback_data_uses_tr_prefix_for_approve_and_reject(self, monkeypatch):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        captured = []
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardButton",
            lambda text, callback_data: captured.append((text, callback_data)) or text,
        )
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardMarkup", lambda rows: rows
        )

        await adapter.send_task_approval(
            chat_id="1", task_id="task-1", request_text="deploy", risk="high",
            session_key="sk",
        )

        callback_data = {cd for _, cd in captured}
        assert "tr:approve:task-1" in callback_data
        assert "tr:reject:task-1" in callback_data
        assert "tr:revise:task-1" in callback_data


# ===========================================================================
# _handle_callback_query — tr: approve/reject idempotency
# ===========================================================================

class TestTaskApprovalCallback:
    @pytest.mark.asyncio
    async def test_approve_tap_resolves_and_resumes_the_task(self, monkeypatch):
        adapter = _make_adapter()
        registry = TaskApprovalRegistry()
        monkeypatch.setattr("gateway.task_router.get_task_registry", lambda: registry)
        route = _make_route(registry, risk=RiskLevel.HIGH)
        adapter._task_approval_state[route.task_id] = route.session_key

        runner = _AuthRunner(authorized=True)
        adapter._message_handler = runner._handle_message

        update, context, query = _make_callback_query(data=f"tr:approve:{route.task_id}")
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("TELEGRAM_ALLOWED_USERS", "*")
            await adapter._handle_callback_query(update, context)

        stored = registry.get(route.task_id)
        assert stored.status.value == "executing" or stored.status.value == "approved"
        assert route.task_id not in adapter._task_approval_state
        assert runner.resumed_task_ids == [route.task_id]
        query.answer.assert_called_once()
        assert "Approved" in query.answer.call_args[1]["text"]

    @pytest.mark.asyncio
    async def test_second_approve_tap_is_a_noop_and_does_not_resume_again(self, monkeypatch):
        """Approve twice must execute (resume) only once."""
        adapter = _make_adapter()
        registry = TaskApprovalRegistry()
        monkeypatch.setattr("gateway.task_router.get_task_registry", lambda: registry)
        route = _make_route(registry, risk=RiskLevel.HIGH)
        adapter._task_approval_state[route.task_id] = route.session_key

        runner = _AuthRunner(authorized=True)
        adapter._message_handler = runner._handle_message

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("TELEGRAM_ALLOWED_USERS", "*")
            update1, context1, query1 = _make_callback_query(data=f"tr:approve:{route.task_id}")
            await adapter._handle_callback_query(update1, context1)

            update2, context2, query2 = _make_callback_query(data=f"tr:approve:{route.task_id}")
            await adapter._handle_callback_query(update2, context2)

        assert runner.resumed_task_ids == [route.task_id]  # resumed exactly once
        assert "already been resolved" in query2.answer.call_args[1]["text"].lower()

    @pytest.mark.asyncio
    async def test_reject_tap_resolves_and_never_resumes(self, monkeypatch):
        adapter = _make_adapter()
        registry = TaskApprovalRegistry()
        monkeypatch.setattr("gateway.task_router.get_task_registry", lambda: registry)
        route = _make_route(registry, risk=RiskLevel.CRITICAL, request_text="rm -rf /data")
        adapter._task_approval_state[route.task_id] = route.session_key

        runner = _AuthRunner(authorized=True)
        adapter._message_handler = runner._handle_message

        update, context, query = _make_callback_query(data=f"tr:reject:{route.task_id}")
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("TELEGRAM_ALLOWED_USERS", "*")
            await adapter._handle_callback_query(update, context)

        stored = registry.get(route.task_id)
        assert stored.status.value == "rejected"
        assert runner.resumed_task_ids == []
        assert "Rejected" in query.answer.call_args[1]["text"]

    @pytest.mark.asyncio
    async def test_reject_then_approve_tap_never_executes(self, monkeypatch):
        """Once rejected, a later (replayed/duplicate) approve tap must not run it."""
        adapter = _make_adapter()
        registry = TaskApprovalRegistry()
        monkeypatch.setattr("gateway.task_router.get_task_registry", lambda: registry)
        route = _make_route(registry, risk=RiskLevel.CRITICAL, request_text="rm -rf /data")

        runner = _AuthRunner(authorized=True)
        adapter._message_handler = runner._handle_message

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("TELEGRAM_ALLOWED_USERS", "*")
            adapter._task_approval_state[route.task_id] = route.session_key
            update1, context1, _ = _make_callback_query(data=f"tr:reject:{route.task_id}")
            await adapter._handle_callback_query(update1, context1)

            # Simulate a stale/duplicate button re-registering state (defense
            # in depth): even if the adapter-side marker were somehow still
            # present, the registry itself must refuse to approve/execute a
            # REJECTED task.
            adapter._task_approval_state[route.task_id] = route.session_key
            update2, context2, query2 = _make_callback_query(data=f"tr:approve:{route.task_id}")
            await adapter._handle_callback_query(update2, context2)

        stored = registry.get(route.task_id)
        assert stored.status.value == "rejected"
        assert runner.resumed_task_ids == []
        assert registry.consume_for_execution(route.task_id) is None

    @pytest.mark.asyncio
    async def test_unauthorized_user_cannot_decide_task(self, monkeypatch):
        adapter = _make_adapter()
        registry = TaskApprovalRegistry()
        monkeypatch.setattr("gateway.task_router.get_task_registry", lambda: registry)
        route = _make_route(registry, risk=RiskLevel.HIGH)
        adapter._task_approval_state[route.task_id] = route.session_key

        runner = _AuthRunner(authorized=False)
        adapter._message_handler = runner._handle_message

        update, context, query = _make_callback_query(data=f"tr:approve:{route.task_id}")
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("TELEGRAM_ALLOWED_USERS", "")
            await adapter._handle_callback_query(update, context)

        assert "not authorized" in query.answer.call_args[1]["text"].lower()
        stored = registry.get(route.task_id)
        assert stored.status.value == "pending"
        assert route.task_id in adapter._task_approval_state
        assert runner.resumed_task_ids == []


# ===========================================================================
# Regression: pre-existing shell exec-approval (ea:) callback is unaffected
# ===========================================================================

class TestExecApprovalRegression:
    @pytest.mark.asyncio
    async def test_ea_callback_still_resolves_independently_of_task_router(self):
        adapter = _make_adapter()
        adapter._approval_state[9] = "agent:main:telegram:dm:12345"

        update, context, query = _make_callback_query(data="ea:once:9")
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("TELEGRAM_ALLOWED_USERS", "*")
            from unittest.mock import patch
            with patch("tools.approval.resolve_gateway_approval", return_value=1):
                await adapter._handle_callback_query(update, context)

        assert 9 not in adapter._approval_state
        assert "Approved" in query.edit_message_text.call_args[1]["text"] \
            or query.edit_message_text.called
