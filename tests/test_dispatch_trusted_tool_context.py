"""Tests that handle_function_call forwards a trusted tool invocation context
into registry.dispatch — and only when one legitimately exists.

Companion to test_dispatch_session_id.py, which established this
mock-the-registry pattern for asserting exactly what kwargs
``handle_function_call`` forwards to ``registry.dispatch`` without exercising
every real tool handler.
"""

import json
from unittest.mock import MagicMock, patch

from gateway.session_context import clear_session_vars, reset_session_vars, set_session_vars
from gateway.tool_context import TrustedToolInvocationContext


def _make_registry(captured: dict):
    """Return a mock registry whose dispatch records the kwargs it receives."""
    registry = MagicMock()

    def _dispatch(name, args, **kwargs):
        captured.update(kwargs)
        return json.dumps({"result": "ok"})

    registry.dispatch.side_effect = _dispatch
    return registry


def _bind_feishu_human(**overrides):
    defaults = dict(
        platform="feishu",
        chat_id="oc_chat_1",
        chat_type="dm",
        user_id="ou_user_1",
        session_key="sk_session_1",
        message_id="om_message_1",
        is_bot=False,
    )
    defaults.update(overrides)
    return set_session_vars(**defaults)


class TestNoSessionBound:
    """CLI / API-server / cron / any non-gateway caller: no actor is fabricated."""

    def setup_method(self):
        reset_session_vars()

    def teardown_method(self):
        reset_session_vars()

    def test_standard_path_tool_context_is_none(self):
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "web_search",
                {"query": "test"},
                task_id="t1",
                session_id="sess-abc",
                skip_pre_tool_call_hook=True,
            )
        assert "tool_context" in captured
        assert captured["tool_context"] is None

    def test_execute_code_path_tool_context_is_none(self):
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "execute_code",
                {"code": "print(1)"},
                task_id="t1",
                session_id="sess-xyz",
                skip_pre_tool_call_hook=True,
            )
        assert "tool_context" in captured
        assert captured["tool_context"] is None

    def test_model_supplied_args_cannot_fabricate_a_context(self):
        """function_args are never consulted when deriving tool_context."""
        captured = {}
        with patch("model_tools.registry", _make_registry(captured)):
            from model_tools import handle_function_call
            handle_function_call(
                "web_search",
                {
                    "query": "test",
                    "tool_context": "forged",
                    "actor": {"open_id": "ou_attacker", "source": "feishu"},
                },
                task_id="t1",
                skip_pre_tool_call_hook=True,
            )
        assert captured["tool_context"] is None


class TestFeishuSessionBound:
    """A real, authenticated, non-bot Feishu inbound message binds the context."""

    def setup_method(self):
        reset_session_vars()

    def teardown_method(self):
        reset_session_vars()

    def test_standard_path_forwards_typed_context(self):
        tokens = _bind_feishu_human()
        try:
            captured = {}
            with patch("model_tools.registry", _make_registry(captured)):
                from model_tools import handle_function_call
                handle_function_call(
                    "devtask_target_options",
                    {},
                    task_id="t1",
                    session_id="sess-abc",
                    skip_pre_tool_call_hook=True,
                )
            ctx = captured.get("tool_context")
            assert isinstance(ctx, TrustedToolInvocationContext)
            assert ctx.source.user_id == "ou_user_1"
            assert ctx.anchor_id == "om_message_1"
            assert ctx.context_id == "sk_session_1"
        finally:
            clear_session_vars(tokens)

    def test_execute_code_path_forwards_typed_context(self):
        """Nested tool calls made from inside a sandbox reach the same seam.

        (The RPC threads that dispatch nested calls from execute_code's
        sandbox wrap their target with tools.thread_context's
        propagate_context_to_thread, which snapshots ALL ContextVars —
        including the ones this seam reads — onto the worker thread, so the
        context is neither lost nor fabricated across that boundary.)
        """
        tokens = _bind_feishu_human()
        try:
            captured = {}
            with patch("model_tools.registry", _make_registry(captured)):
                from model_tools import handle_function_call
                handle_function_call(
                    "execute_code",
                    {"code": "print(1)"},
                    task_id="t1",
                    session_id="sess-abc",
                    skip_pre_tool_call_hook=True,
                )
            ctx = captured.get("tool_context")
            assert isinstance(ctx, TrustedToolInvocationContext)
        finally:
            clear_session_vars(tokens)

    def test_bot_sender_never_forwards_a_context(self):
        tokens = _bind_feishu_human(is_bot=True)
        try:
            captured = {}
            with patch("model_tools.registry", _make_registry(captured)):
                from model_tools import handle_function_call
                handle_function_call(
                    "devtask_target_options",
                    {},
                    task_id="t1",
                    skip_pre_tool_call_hook=True,
                )
            assert captured.get("tool_context") is None
        finally:
            clear_session_vars(tokens)

    def test_non_feishu_platform_never_forwards_a_context(self):
        tokens = _bind_feishu_human(platform="telegram")
        try:
            captured = {}
            with patch("model_tools.registry", _make_registry(captured)):
                from model_tools import handle_function_call
                handle_function_call(
                    "devtask_target_options",
                    {},
                    task_id="t1",
                    skip_pre_tool_call_hook=True,
                )
            assert captured.get("tool_context") is None
        finally:
            clear_session_vars(tokens)

    def test_existing_kwargs_still_forwarded_unchanged(self):
        """Regression guard: adding tool_context must not disturb task_id/
        session_id/user_task forwarding for ordinary tool dispatch."""
        tokens = _bind_feishu_human()
        try:
            captured = {}
            with patch("model_tools.registry", _make_registry(captured)):
                from model_tools import handle_function_call
                handle_function_call(
                    "web_search",
                    {"query": "test"},
                    task_id="task-999",
                    session_id="sess-1",
                    user_task="find the bug",
                    skip_pre_tool_call_hook=True,
                )
            assert captured.get("task_id") == "task-999"
            assert captured.get("session_id") == "sess-1"
            assert captured.get("user_task") == "find the bug"
        finally:
            clear_session_vars(tokens)
