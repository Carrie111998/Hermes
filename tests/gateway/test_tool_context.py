"""Tests for gateway/tool_context.py — the trusted tool invocation context seam.

``build_trusted_tool_invocation_context()`` is the ONLY supported way to
derive a typed proof of "who authenticated-sent the message driving this
tool call". It must construct that proof strictly from the current task's
``ContextVar``-bound gateway session state (see ``gateway/session_context.py``)
and fail closed (return ``None``) whenever that state isn't a fully-bound,
non-bot Feishu inbound message — never from model tool-call arguments, a
plain mapping, or an environment variable.
"""

from __future__ import annotations

import os

import pytest

from gateway.session_context import (
    clear_session_vars,
    reset_session_vars,
    set_session_vars,
)
from gateway.tool_context import (
    TrustedToolInvocationContext,
    build_trusted_tool_invocation_context,
)


@pytest.fixture(autouse=True)
def _isolated_session_context():
    """Every test starts and ends with no session bound in this task."""
    reset_session_vars()
    yield
    reset_session_vars()


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


class TestAuthenticatedFeishuHuman:
    def test_builds_typed_context_with_correct_fields(self):
        tokens = _bind_feishu_human()
        try:
            ctx = build_trusted_tool_invocation_context()
            assert isinstance(ctx, TrustedToolInvocationContext)
            assert ctx.source.platform.value == "feishu"
            assert ctx.source.user_id == "ou_user_1"
            assert ctx.source.is_bot is False
            assert ctx.anchor_id == "om_message_1"
            assert ctx.context_id == "sk_session_1"
            assert ctx.authenticated is True
        finally:
            clear_session_vars(tokens)

    def test_require_valid_does_not_raise(self):
        tokens = _bind_feishu_human()
        try:
            ctx = build_trusted_tool_invocation_context()
            assert ctx.require_valid() is ctx
        finally:
            clear_session_vars(tokens)

    def test_context_id_falls_back_to_session_id_when_session_key_empty(self):
        tokens = _bind_feishu_human(session_key="", session_id="sid_fallback")
        try:
            ctx = build_trusted_tool_invocation_context()
            assert ctx is not None
            assert ctx.context_id == "sid_fallback"
        finally:
            clear_session_vars(tokens)


class TestFailClosed:
    def test_unbound_session_returns_none(self):
        assert build_trusted_tool_invocation_context() is None

    def test_bot_sender_returns_none(self):
        tokens = _bind_feishu_human(is_bot=True)
        try:
            assert build_trusted_tool_invocation_context() is None
        finally:
            clear_session_vars(tokens)

    def test_non_feishu_platform_returns_none(self):
        tokens = _bind_feishu_human(platform="telegram")
        try:
            assert build_trusted_tool_invocation_context() is None
        finally:
            clear_session_vars(tokens)

    def test_missing_message_anchor_returns_none(self):
        tokens = _bind_feishu_human(message_id="")
        try:
            assert build_trusted_tool_invocation_context() is None
        finally:
            clear_session_vars(tokens)

    def test_missing_user_id_returns_none(self):
        tokens = _bind_feishu_human(user_id="")
        try:
            assert build_trusted_tool_invocation_context() is None
        finally:
            clear_session_vars(tokens)

    def test_missing_context_id_returns_none(self):
        tokens = _bind_feishu_human(session_key="", session_id="")
        try:
            assert build_trusted_tool_invocation_context() is None
        finally:
            clear_session_vars(tokens)

    def test_cleared_session_returns_none(self):
        tokens = _bind_feishu_human()
        clear_session_vars(tokens)
        assert build_trusted_tool_invocation_context() is None

    def test_require_valid_raises_on_bot_source(self):
        ctx = build_trusted_tool_invocation_context()  # unbound -> None first
        assert ctx is None
        tokens = _bind_feishu_human()
        try:
            ctx = build_trusted_tool_invocation_context()
            assert ctx is not None
            # Hand-construct an invalid instance (a builder never would) to
            # prove require_valid() is real validation, not a rubber stamp.
            forged = TrustedToolInvocationContext(
                source=ctx.source.__class__(**{**ctx.source.__dict__, "is_bot": True}),
                context_id=ctx.context_id,
                anchor_id=ctx.anchor_id,
            )
            with pytest.raises(PermissionError):
                forged.require_valid()
        finally:
            clear_session_vars(tokens)


class TestCannotBeForgedFromEnvOrArgs:
    def test_os_environ_alone_does_not_produce_a_context(self, monkeypatch):
        """A same-process env var must never masquerade as a bound session.

        This is exactly the leak get_session_var_strict is designed to
        block: os.environ fallback is fine for legacy get_session_env
        (CLI/cron compatibility) but must never feed an authenticated-actor
        derivation.
        """
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "feishu")
        monkeypatch.setenv("HERMES_SESSION_USER_ID", "ou_attacker")
        monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", "om_attacker")
        monkeypatch.setenv("HERMES_SESSION_KEY", "sk_attacker")
        try:
            assert build_trusted_tool_invocation_context() is None
        finally:
            for name in (
                "HERMES_SESSION_PLATFORM",
                "HERMES_SESSION_USER_ID",
                "HERMES_SESSION_MESSAGE_ID",
                "HERMES_SESSION_KEY",
            ):
                os.environ.pop(name, None)

    def test_builder_takes_no_arguments(self):
        """The builder cannot read model tool-call arguments: it has none."""
        import inspect

        sig = inspect.signature(build_trusted_tool_invocation_context)
        assert len(sig.parameters) == 0
