"""Regression tests for fail-closed Gateway session attribution context."""

from agent.runtime_cwd import authoritative_session_cwd
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionContext, SessionSource
from gateway.session_context import get_session_env, reset_session_vars


def test_gateway_ingress_binds_session_id_but_masks_process_cwd(monkeypatch):
    reset_session_vars()
    monkeypatch.setenv("TERMINAL_CWD", "/process-wide-not-authoritative")
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    context = SessionContext(
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-a",
            user_id="user-a",
        ),
        connected_platforms=[],
        home_channels={},
        session_key="gateway-key",
        session_id="gateway-session-a",
    )

    tokens = runner._set_session_env(context)
    try:
        assert get_session_env("HERMES_SESSION_ID") == "gateway-session-a"
        assert authoritative_session_cwd() == ""
    finally:
        runner._clear_session_env(tokens)
