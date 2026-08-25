"""Regression tests for MCP elicitation approval routing (#94488).

Before the fix, ``request_elicitation_consent``'s CLI branch called
``prompt_dangerous_approval`` without an ``approval_callback``. The
prompt_toolkit fail-closed guard in ``_prompt_dangerous_approval_inner`` then
returned ``"deny"`` without rendering any prompt, so a destructive MCP tool
(e.g. Wincontainer ``remove_container``) was silently rejected with
``human_approval_denied`` and the user never saw an approval request.
"""

from unittest.mock import patch

from tools import approval


def test_resolve_prefers_explicit_callback():
    explicit = object()
    assert approval._resolve_cli_approval_callback(explicit) is explicit


def test_resolve_falls_back_to_contextvar_callback():
    ctx_cb = object()
    token = approval._cli_approval_callback.set(ctx_cb)
    try:
        # The contextvar is hit before the thread-local lookup, so the callback
        # survives the MCP background-loop thread hop even when the
        # thread-local slot is empty.
        assert approval._resolve_cli_approval_callback() is ctx_cb
    finally:
        approval._cli_approval_callback.reset(token)


def test_elicitation_passes_contextvar_callback_to_prompt():
    ctx_cb = object()
    token = approval._cli_approval_callback.set(ctx_cb)
    try:
        with patch("tools.approval.prompt_dangerous_approval", return_value="once") as prompt, \
             patch("tools.approval.get_current_session_key", return_value="default"), \
             patch("tools.approval._is_gateway_approval_context", return_value=False):
            result = approval.request_elicitation_consent("msg", "desc", timeout_seconds=1)
            assert result == "accept"
            _, kwargs = prompt.call_args
            assert kwargs["approval_callback"] is ctx_cb
    finally:
        approval._cli_approval_callback.reset(token)


def test_elicitation_no_callback_fails_closed():
    # No contextvar and no thread-local callback -> approval_callback=None,
    # and prompt_dangerous_approval's own guard returns "deny" (fail closed).
    with patch("tools.approval.prompt_dangerous_approval", return_value="deny") as prompt, \
         patch("tools.approval.get_current_session_key", return_value="default"), \
         patch("tools.approval._is_gateway_approval_context", return_value=False):
        result = approval.request_elicitation_consent("msg", "desc", timeout_seconds=1)
        assert result == "decline"
        _, kwargs = prompt.call_args
        assert kwargs["approval_callback"] is None
