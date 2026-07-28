"""Tests for agent.transports.acp_client_session -- ACP session adapter.

Tests cover session lifecycle (ensure_started, close), turn execution,
streaming delta projection, should_retire policy on crash/timeout, and
server-request handling (permission allow, fs/terminal decline).

Fix-specific tests:
  Fix 1 -- model pin: set_config_option sent after session/new when model is set.
  Fix 2 -- thought-chunk leak: agent_thought_chunk NOT in extracted text; agent_message_chunk IS.
  Fix 3 -- permission response shape: uses {outcome:{outcome:...}} ACP spec form.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional
from unittest.mock import MagicMock, call, patch

import pytest

from agent.transports.acp_client import ACPClientError
from agent.transports.acp_client_session import (
    ACPClientSession,
    TurnResult,
    _coerce_user_input,
    _extract_text_from_update,
    _is_tool_iteration,
    _pick_allow_option,
    _stringify_tool_payload,
    _translate_mcp_servers,
)


# ---------------------------------------------------------------------------
# Helpers -- mock ACPClient
# ---------------------------------------------------------------------------


def _make_session(
    *,
    command: str = "fake-acp",
    args=None,
    model: Optional[str] = None,
    permission_mode: Optional[str] = None,
    on_delta=None,
    approval_callback=None,
    auto_approve_permissions: bool = False,
    client_mock: Optional[MagicMock] = None,
) -> tuple[ACPClientSession, MagicMock]:
    """Return an ACPClientSession with a mock ACPClient injected."""
    if client_mock is None:
        client_mock = MagicMock()
        client_mock.is_alive.return_value = True
        client_mock.initialize.return_value = {"protocolVersion": 1}
        client_mock.request.return_value = {}
        client_mock.take_notification.return_value = None
        client_mock.take_server_request.return_value = None
        client_mock.stderr_tail.return_value = []

    session = ACPClientSession(
        command=command,
        args=args,
        model=model,
        permission_mode=permission_mode,
        on_delta=on_delta,
        approval_callback=approval_callback,
        auto_approve_permissions=auto_approve_permissions,
        client_factory=lambda **kw: client_mock,
    )
    return session, client_mock


# ---------------------------------------------------------------------------
# Tests: ensure_started / session lifecycle
# ---------------------------------------------------------------------------


class TestEnsureStarted:
    def test_ensure_started_initializes_and_creates_session(self):
        """ensure_started() calls initialize then session/new, stores session_id."""
        session, mock_client = _make_session()
        mock_client.request.side_effect = [
            {"sessionId": "sess-abc-123"},  # session/new
        ]

        sid = session.ensure_started(cwd="/tmp")
        assert sid == "sess-abc-123"
        assert session._session_id == "sess-abc-123"

        # initialize was called once
        mock_client.initialize.assert_called_once()
        # session/new was called with correct cwd
        mock_client.request.assert_called_once()
        call_args = mock_client.request.call_args
        assert call_args[0][0] == "session/new"
        assert call_args[0][1]["cwd"] == "/tmp"

    def test_ensure_started_idempotent(self):
        """ensure_started() called twice returns same session_id."""
        session, mock_client = _make_session()
        mock_client.request.return_value = {"sessionId": "sess-001"}

        sid1 = session.ensure_started(cwd="/tmp")
        sid2 = session.ensure_started(cwd="/other")
        assert sid1 == sid2 == "sess-001"
        # initialize and session/new called only once
        assert mock_client.initialize.call_count == 1
        assert mock_client.request.call_count == 1

    def test_ensure_started_raises_on_missing_session_id(self):
        """ensure_started() raises ACPClientError if no sessionId in response."""
        session, mock_client = _make_session()
        mock_client.request.return_value = {}  # no sessionId

        with pytest.raises(ACPClientError) as exc_info:
            session.ensure_started()
        assert "sessionId" in str(exc_info.value)
        assert session._session_id is None

    def test_ensure_started_error_sets_should_retire(self):
        """run_turn() -> ensure_started() failure sets should_retire=True."""
        session, mock_client = _make_session()
        mock_client.initialize.side_effect = ACPClientError(
            code=-32603, message="initialize failed"
        )

        result = session.run_turn("hello")
        assert result.should_retire is True
        assert result.error is not None
        assert "startup" in result.error.lower()


# ---------------------------------------------------------------------------
# Tests: Fix 1 -- model pin via session/set_config_option (verify behaviour)
# ---------------------------------------------------------------------------

def _make_config_response(current_value: str) -> dict:
    """Build a realistic set_config_option response with the given model currentValue."""
    return {
        "configOptions": [
            {
                "id": "model",
                "name": "Model",
                "type": "select",
                "category": "model",
                "currentValue": current_value,
                "options": [
                    {"value": "default", "name": "Default (Opus 4.8)"},
                    {"value": "sonnet",  "name": "Sonnet 4.6"},
                    {"value": "haiku",   "name": "Haiku 4.5"},
                ],
            },
        ]
    }


class TestModelPin:
    def test_set_config_option_sent_after_session_new_when_model_set(self):
        """Fix 1: when model is configured, session/set_config_option is sent
        after session/new with configId='model' and the resolved model string."""
        session, mock_client = _make_session(model="haiku")
        mock_client.request.side_effect = [
            {"sessionId": "sess-model"},    # session/new
            _make_config_response("haiku"), # set_config_option -> match
        ]

        session.ensure_started(cwd="/tmp")

        calls = mock_client.request.call_args_list
        assert len(calls) == 2

        new_call = calls[0]
        assert new_call[0][0] == "session/new"

        cfg_call = calls[1]
        assert cfg_call[0][0] == "session/set_config_option"
        params = cfg_call[0][1]
        assert params["sessionId"] == "sess-model"
        assert params["configId"] == "model"
        assert params["value"] == "haiku"

    def test_set_config_option_not_sent_when_model_not_set(self):
        """Fix 1: when no model is configured, set_config_option is NOT sent
        (existing tests must remain unaffected -- only session/new is called)."""
        session, mock_client = _make_session()  # no model
        mock_client.request.side_effect = [
            {"sessionId": "sess-nomodel"},  # session/new only
        ]

        session.ensure_started(cwd="/tmp")

        # Only one request call: session/new, no set_config_option
        assert mock_client.request.call_count == 1
        assert mock_client.request.call_args[0][0] == "session/new"

    # -- verify: currentValue matches -> silent OK --

    def test_model_pin_verified_match_is_silent(self):
        """Task B: currentValue == requested -> log info, no exception, session OK."""
        session, mock_client = _make_session(model="haiku")
        mock_client.request.side_effect = [
            {"sessionId": "sess-match"},
            _make_config_response("haiku"),  # currentValue matches
        ]

        sid = session.ensure_started(cwd="/tmp")
        assert sid == "sess-match"
        assert session._session_id == "sess-match"

    # -- verify: currentValue mismatch (server supported) -> raises, not swallowed --

    def test_model_pin_mismatch_raises_acp_error(self):
        """Task B: server supported set_config_option but currentValue != requested
        -> ACPClientError raised (NOT swallowed by the tolerance except)."""
        session, mock_client = _make_session(model="haiku")
        mock_client.request.side_effect = [
            {"sessionId": "sess-mismatch"},
            _make_config_response("default"),  # currentValue stayed on Opus default
        ]

        with pytest.raises(ACPClientError) as exc_info:
            session.ensure_started(cwd="/tmp")
        err = exc_info.value
        assert err.code == 1  # positive = config rejection, not transport crash
        assert "haiku" in str(err)
        assert "default" in str(err)
        # Session cleared so retry does not short-circuit idempotency guard
        assert session._session_id is None

    def test_model_pin_mismatch_does_not_retire_session(self):
        """Task B: mismatch is a config error, not a session crash.
        should_retire must be False so we don't loop (respawn -> same mismatch -> loop)."""
        session, mock_client = _make_session(model="haiku")

        def req_side(method, params=None, timeout=30, **kwargs):
            if method == "session/new":
                return {"sessionId": "sess-noretire"}
            if method == "session/set_config_option":
                return _make_config_response("default")  # mismatch
            return {}

        mock_client.request.side_effect = req_side

        result = session.run_turn("hello")
        assert result.error is not None
        assert "haiku" in result.error
        assert result.should_retire is False  # MUST NOT retire -> would loop

    def test_model_pin_mismatch_error_message_names_accepted_aliases(self):
        """Task B: error message includes the accepted alias list so operator can fix."""
        session, mock_client = _make_session(model="claude-haiku-4-5-20251001")
        mock_client.request.side_effect = [
            {"sessionId": "sess-alias"},
            _make_config_response("default"),
        ]

        with pytest.raises(ACPClientError) as exc_info:
            session.ensure_started(cwd="/tmp")
        msg = str(exc_info.value)
        # Should list the accepted values from the options array
        assert "haiku" in msg
        assert "sonnet" in msg
        assert "default" in msg

    # -- verify: request() raises (server lacks method) -> tolerated --

    def test_set_config_option_request_raises_is_tolerated(self):
        """Task B: if request() raises (server doesn't support set_config_option),
        session is NOT retired -- ensure_started returns the session_id."""
        session, mock_client = _make_session(model="haiku")
        mock_client.request.side_effect = [
            {"sessionId": "sess-cfg-fail"},
            ACPClientError(code=-32601, message="Method not found"),  # set_config_option
        ]

        sid = session.ensure_started(cwd="/tmp")
        assert sid == "sess-cfg-fail"  # session not aborted
        assert session._session_id == "sess-cfg-fail"

    def test_set_config_option_timeout_is_tolerated(self):
        """Task B: TimeoutError from set_config_option is tolerated (not a mismatch)."""
        session, mock_client = _make_session(model="haiku")
        mock_client.request.side_effect = [
            {"sessionId": "sess-timeout"},
            TimeoutError("set_config_option timed out"),
        ]

        sid = session.ensure_started(cwd="/tmp")
        assert sid == "sess-timeout"
        assert session._session_id == "sess-timeout"

    # -- verify: no model configOption in response -> warn + proceed --

    def test_no_model_config_option_in_response_is_tolerated(self):
        """Task B: server responds but carries no 'model' configOption
        (generic ACP server) -- cannot verify, warn + proceed."""
        session, mock_client = _make_session(model="haiku")
        mock_client.request.side_effect = [
            {"sessionId": "sess-generic"},
            {"configOptions": []},  # no model option
        ]

        sid = session.ensure_started(cwd="/tmp")
        assert sid == "sess-generic"
        assert session._session_id == "sess-generic"


# --------------------------------------------------------------------------- #
# Tests: permission_mode startup pin (sent alongside the model pin)
# --------------------------------------------------------------------------- #


def _make_mode_config_response(current_value: str) -> dict:
    """Build a realistic set_config_option response for the 'mode' config."""
    return {
        "configOptions": [
            {
                "id": "mode",
                "name": "Permission Mode",
                "type": "select",
                "category": "permission",
                "currentValue": current_value,
                "options": [
                    {"value": "default", "name": "Default"},
                    {"value": "acceptEdits", "name": "Accept Edits"},
                    {"value": "plan", "name": "Plan"},
                    {"value": "bypassPermissions", "name": "Bypass"},
                ],
            },
        ]
    }


class TestPermissionModePin:
    def test_permission_mode_pin_sent_after_session_new(self):
        """When permission_mode is configured, session/set_config_option is
        sent with configId='mode' after session/new (and after the model pin
        if both are set)."""
        session, mock_client = _make_session(permission_mode="acceptEdits")
        mock_client.request.side_effect = [
            {"sessionId": "sess-mode"},                  # session/new
            _make_mode_config_response("acceptEdits"),   # set_config_option mode
        ]

        session.ensure_started(cwd="/tmp")

        calls = mock_client.request.call_args_list
        assert len(calls) == 2
        assert calls[0][0][0] == "session/new"
        cfg_call = calls[1]
        assert cfg_call[0][0] == "session/set_config_option"
        params = cfg_call[0][1]
        assert params["sessionId"] == "sess-mode"
        assert params["configId"] == "mode"
        assert params["value"] == "acceptEdits"

    def test_permission_mode_pin_not_sent_when_not_set(self):
        """No permission_mode -> no 'mode' set_config_option sent."""
        session, mock_client = _make_session()
        mock_client.request.side_effect = [{"sessionId": "s"}]
        session.ensure_started(cwd="/tmp")
        assert mock_client.request.call_count == 1
        assert mock_client.request.call_args[0][0] == "session/new"

    def test_permission_mode_pin_mismatch_raises(self):
        """Server accepts the call but currentValue != requested -> raise."""
        session, mock_client = _make_session(permission_mode="bypassPermissions")
        mock_client.request.side_effect = [
            {"sessionId": "s"},
            _make_mode_config_response("default"),  # mismatch
        ]
        with pytest.raises(ACPClientError) as exc_info:
            session.ensure_started(cwd="/tmp")
        assert exc_info.value.code == 1
        assert session._session_id is None

    def test_permission_mode_pin_transport_error_tolerated(self):
        """Transport failure from the mode pin is tolerated."""
        session, mock_client = _make_session(permission_mode="acceptEdits")
        mock_client.request.side_effect = [
            {"sessionId": "s"},
            ACPClientError(code=-32601, message="Method not found"),
        ]
        sid = session.ensure_started(cwd="/tmp")
        assert sid == "s"

    def test_model_and_permission_mode_pins_both_sent(self):
        """When both model and permission_mode are set, both pins are sent
        (model first, then mode)."""
        session, mock_client = _make_session(
            model="sonnet", permission_mode="plan",
        )
        mock_client.request.side_effect = [
            {"sessionId": "s-both"},                 # session/new
            _make_config_response("sonnet"),         # model pin
            _make_mode_config_response("plan"),      # mode pin
        ]
        session.ensure_started(cwd="/tmp")
        calls = mock_client.request.call_args_list
        assert len(calls) == 3
        assert calls[0][0][0] == "session/new"
        assert calls[1][0][1]["configId"] == "model"
        assert calls[1][0][1]["value"] == "sonnet"
        assert calls[2][0][1]["configId"] == "mode"
        assert calls[2][0][1]["value"] == "plan"


# --------------------------------------------------------------------------- #
# Tests: live set_config_option / set_model / set_permission_mode
# --------------------------------------------------------------------------- #


class TestLiveSetConfigOption:
    """Runtime live-switch of model / permission_mode without rebuilding the
    session."""

    def test_set_config_option_sends_against_live_session(self):
        """set_config_option('model', 'X') issues a session/set_config_option
        against the already-started session."""
        session, mock_client = _make_session(model="haiku")
        mock_client.request.side_effect = [
            {"sessionId": "sess-live"},                # session/new
            _make_config_response("haiku"),            # startup model pin
            _make_config_response("sonnet"),           # live switch -> match
        ]
        session.ensure_started(cwd="/tmp")
        session.set_config_option("model", "sonnet")

        last_call = mock_client.request.call_args_list[-1]
        assert last_call[0][0] == "session/set_config_option"
        assert last_call[0][1]["sessionId"] == "sess-live"
        assert last_call[0][1]["configId"] == "model"
        assert last_call[0][1]["value"] == "sonnet"

    def test_set_config_option_before_ensure_started_is_noop(self):
        """Calling set_config_option before ensure_started is a logged no-op
        (no exception, no request issued)."""
        session, mock_client = _make_session()
        mock_client.request.return_value = {}
        session.set_config_option("model", "sonnet")
        # Only the session/new request should have been issued -- but we
        # haven't called ensure_started at all, so zero requests.
        assert mock_client.request.call_count == 0

    def test_set_model_delegates_to_set_config_option(self):
        """set_model('X') is sugar for set_config_option('model', 'X')."""
        session, mock_client = _make_session()
        mock_client.request.side_effect = [
            {"sessionId": "s"},                       # session/new
            _make_config_response("sonnet"),          # live switch
        ]
        session.ensure_started(cwd="/tmp")
        session.set_model("sonnet")
        last_call = mock_client.request.call_args_list[-1]
        assert last_call[0][1]["configId"] == "model"
        assert last_call[0][1]["value"] == "sonnet"

    def test_set_permission_mode_delegates_to_set_config_option(self):
        """set_permission_mode('X') is sugar for set_config_option('mode', 'X')."""
        session, mock_client = _make_session()
        mock_client.request.side_effect = [
            {"sessionId": "s"},                       # session/new
            _make_mode_config_response("acceptEdits"),  # live switch
        ]
        session.ensure_started(cwd="/tmp")
        session.set_permission_mode("acceptEdits")
        last_call = mock_client.request.call_args_list[-1]
        assert last_call[0][1]["configId"] == "mode"
        assert last_call[0][1]["value"] == "acceptEdits"

    def test_set_config_option_value_rejected_raises(self):
        """Server accepts the call but currentValue != requested -> raise."""
        session, mock_client = _make_session()
        mock_client.request.side_effect = [
            {"sessionId": "s"},                       # session/new
            _make_config_response("default"),         # mismatch on live switch
        ]
        session.ensure_started(cwd="/tmp")
        with pytest.raises(ACPClientError) as exc_info:
            session.set_model("sonnet")
        assert exc_info.value.code == 1

    def test_set_config_option_transport_error_tolerated(self):
        """Server does not implement set_config_option at runtime -> tolerated."""
        session, mock_client = _make_session()
        mock_client.request.side_effect = [
            {"sessionId": "s"},                       # session/new
            ACPClientError(code=-32601, message="Method not found"),
        ]
        session.ensure_started(cwd="/tmp")
        # Must not raise -- transport failure is tolerated.
        session.set_model("sonnet")



# ---------------------------------------------------------------------------
# Tests: Fix 2 -- thought-chunk leak guard
# ---------------------------------------------------------------------------


class TestThoughtChunkLeak:
    def test_agent_message_chunk_extracted(self):
        """Fix 2: agent_message_chunk produces user-facing text (positive case)."""
        params = {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hello"},
            },
        }
        assert _extract_text_from_update(params) == "hello"

    def test_agent_thought_chunk_not_extracted(self):
        """Fix 2: agent_thought_chunk (internal reasoning) returns empty string --
        must never leak into the user-facing reply."""
        params = {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "I am thinking..."},
            },
        }
        assert _extract_text_from_update(params) == ""

    def test_agent_thought_chunk_not_in_run_turn_output(self):
        """Fix 2: thought chunks from session/update are NOT included in final_text."""
        session, mock_client = _make_session()

        def req_side_effect(method, params=None, timeout=30, **kwargs):
            if method == "session/new":
                return {"sessionId": "sess-think"}
            if method == "session/prompt":
                time.sleep(0.05)
                return {"stopReason": "end_turn"}
            return {}

        mock_client.request.side_effect = req_side_effect

        notes = iter([
            # internal reasoning -- must be excluded
            {
                "method": "session/update",
                "params": {
                    "sessionId": "sess-think",
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "reasoning step"},
                    },
                },
            },
            # user-facing reply -- must be included
            {
                "method": "session/update",
                "params": {
                    "sessionId": "sess-think",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "user reply"},
                    },
                },
            },
            None,
        ])
        mock_client.take_notification.side_effect = lambda timeout=0.0: next(notes, None)

        result = session.run_turn("test")
        assert "user reply" in result.final_text
        assert "reasoning step" not in result.final_text

    def test_snake_case_thought_chunk_not_extracted(self):
        """Fix 2: snake_case 'session_update' discriminator variant also blocked."""
        params = {
            "sessionId": "s",
            "update": {
                "session_update": "agent_thought_chunk",
                "content": {"type": "text", "text": "hidden thought"},
            },
        }
        assert _extract_text_from_update(params) == ""

    def test_snake_case_message_chunk_extracted(self):
        """Fix 2: snake_case 'session_update' agent_message_chunk still works."""
        params = {
            "sessionId": "s",
            "update": {
                "session_update": "agent_message_chunk",
                "content": {"type": "text", "text": "visible"},
            },
        }
        assert _extract_text_from_update(params) == "visible"


# ---------------------------------------------------------------------------
# Tests: Fix 3 -- permission response shape
# ---------------------------------------------------------------------------


class TestPermissionResponseShape:
    def test_permission_request_uses_acp_outcome_shape(self):
        """Fix 3: permission response uses {outcome:{outcome:'selected',optionId:'...'}}
        NOT the old {granted: false} which is not a valid ACP shape.

        Uses auto_approve_permissions=True to preserve the original bypass behavior
        that these Fix 3 tests were written for.
        """
        session, mock_client = _make_session(auto_approve_permissions=True)
        mock_client.request.return_value = {"sessionId": "sess-perm"}
        session.ensure_started()

        req = {
            "id": 42,
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess-perm",
                "toolCall": {"title": "bash", "kind": "tool", "toolCallId": "call-1"},
                "options": [
                    {"optionId": "opt-allow", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "opt-deny", "name": "Deny", "kind": "reject_once"},
                ],
            },
        }
        session._handle_server_request(req)

        mock_client.respond.assert_called_once()
        args = mock_client.respond.call_args
        assert args[0][0] == 42
        payload = args[0][1]
        # Must use the ACP spec shape, not {granted: ...}
        assert "outcome" in payload
        assert "granted" not in payload
        inner = payload["outcome"]
        assert inner["outcome"] == "selected"
        assert inner["optionId"] == "opt-allow"  # prefers allow_once kind

    def test_permission_prefers_allow_once_option(self):
        """Fix 3: the allow_once-kinded option is preferred over others (bypass mode)."""
        session, mock_client = _make_session(auto_approve_permissions=True)
        mock_client.request.return_value = {"sessionId": "s"}
        session.ensure_started()

        req = {
            "id": 1,
            "method": "session/request_permission",
            "params": {
                "sessionId": "s",
                "toolCall": {"title": "bash", "kind": "tool", "toolCallId": "c1"},
                "options": [
                    {"optionId": "deny-id", "name": "Deny", "kind": "reject_once"},
                    {"optionId": "allow-id", "name": "Allow", "kind": "allow_once"},
                ],
            },
        }
        session._handle_server_request(req)

        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"]["optionId"] == "allow-id"

    def test_permission_falls_back_to_first_option_when_no_allow_once(self):
        """Fix 3: bypass mode — if no allow_once, falls back to first optionId."""
        session, mock_client = _make_session(auto_approve_permissions=True)
        mock_client.request.return_value = {"sessionId": "s"}
        session.ensure_started()

        req = {
            "id": 2,
            "method": "session/request_permission",
            "params": {
                "sessionId": "s",
                "toolCall": {"title": "bash", "kind": "tool", "toolCallId": "c2"},
                "options": [
                    {"optionId": "custom-1", "name": "Custom 1", "kind": "allow_always"},
                ],
            },
        }
        session._handle_server_request(req)

        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"]["outcome"] == "selected"
        assert payload["outcome"]["optionId"] == "custom-1"

    def test_permission_cancelled_when_no_options(self):
        """Fix 3: malformed request with no options -> cancelled outcome (not wedged)."""
        session, mock_client = _make_session(auto_approve_permissions=True)
        mock_client.request.return_value = {"sessionId": "s"}
        session.ensure_started()

        req = {
            "id": 3,
            "method": "session/request_permission",
            "params": {"sessionId": "s", "toolCall": {"title": "bash", "kind": "tool", "toolCallId": "c3"}, "options": []},
        }
        session._handle_server_request(req)

        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"] == {"outcome": "cancelled"}

    def test_pick_allow_option_helper(self):
        """_pick_allow_option() unit tests.

        Prefers allow_once, then allow_always; returns None when no allow-kind
        option exists (never returns a reject-kind option).
        """
        # allow_once present → selected
        opts = [
            {"optionId": "r", "kind": "reject_once"},
            {"optionId": "a", "kind": "allow_once"},
        ]
        assert _pick_allow_option(opts) == "a"

        # no allow_once but allow_always → allow_always
        opts2 = [
            {"optionId": "r", "kind": "reject_once"},
            {"optionId": "aa", "kind": "allow_always"},
        ]
        assert _pick_allow_option(opts2) == "aa"

        # only reject → None (must NOT select a reject option)
        opts3 = [{"optionId": "x", "kind": "reject_once"}]
        assert _pick_allow_option(opts3) is None

        # empty → None
        assert _pick_allow_option([]) is None


# ---------------------------------------------------------------------------
# Tests: Permission approval callback, bypass, and fail-closed
# ---------------------------------------------------------------------------


def _make_perm_request(
    *,
    req_id: int = 100,
    options: list = None,
    tool_call: dict = None,
) -> dict:
    """Build a realistic session/request_permission server request.

    Uses ACP schema fields: toolCall.title / kind / toolCallId (no toolName).
    """
    if tool_call is None:
        tool_call = {"title": "bash -c ls", "kind": "tool", "toolCallId": "call-abc"}
    if options is None:
        options = [
            {"optionId": "allow-once", "kind": "allow_once"},
            {"optionId": "allow-always", "kind": "allow_always"},
            {"optionId": "reject-once", "kind": "reject_once"},
            {"optionId": "reject-always", "kind": "reject_always"},
        ]
    return {
        "id": req_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "s",
            "toolCall": tool_call,
            "options": options,
        },
    }


class TestPermissionApprovalCallback:
    """Tests for the approval_callback-driven permission path."""

    def _setup(self, callback, **kw):
        session, mock_client = _make_session(
            approval_callback=callback,
            auto_approve_permissions=False,
            **kw,
        )
        mock_client.request.return_value = {"sessionId": "s"}
        session.ensure_started()
        return session, mock_client

    def test_callback_once_selects_allow_once(self):
        """Callback returns 'once' → selects allow_once optionId."""
        cb = MagicMock(return_value="once")
        session, mock_client = self._setup(cb)
        session._handle_server_request(_make_perm_request())

        mock_client.respond.assert_called_once()
        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"] == {"outcome": "selected", "optionId": "allow-once"}

    def test_callback_session_prefers_allow_always(self):
        """Callback returns 'session' → prefers allow_always optionId."""
        cb = MagicMock(return_value="session")
        session, mock_client = self._setup(cb)
        session._handle_server_request(_make_perm_request())

        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"]["optionId"] == "allow-always"

    def test_callback_session_falls_back_to_allow_once(self):
        """Callback returns 'session' but only allow_once available → allow_once."""
        cb = MagicMock(return_value="session")
        session, mock_client = self._setup(cb)
        opts = [
            {"optionId": "a-once", "kind": "allow_once"},
            {"optionId": "r-once", "kind": "reject_once"},
        ]
        session._handle_server_request(_make_perm_request(options=opts))

        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"] == {"outcome": "selected", "optionId": "a-once"}

    def test_callback_deny_prefers_reject_once(self):
        """Callback returns 'deny' → prefers reject_once optionId."""
        cb = MagicMock(return_value="deny")
        session, mock_client = self._setup(cb)
        session._handle_server_request(_make_perm_request())

        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"]["optionId"] == "reject-once"

    def test_callback_deny_falls_back_to_reject_always(self):
        """Callback returns 'deny' but only reject_always available → reject_always."""
        cb = MagicMock(return_value="deny")
        session, mock_client = self._setup(cb)
        opts = [
            {"optionId": "a-once", "kind": "allow_once"},
            {"optionId": "r-always", "kind": "reject_always"},
        ]
        session._handle_server_request(_make_perm_request(options=opts))

        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"]["optionId"] == "r-always"

    def test_callback_unknown_returns_fail_closed(self):
        """Callback returns an unrecognized string → deny/reject path."""
        cb = MagicMock(return_value="whatever")
        session, mock_client = self._setup(cb)
        session._handle_server_request(_make_perm_request())

        payload = mock_client.respond.call_args[0][1]
        # Should be reject_once (fail-closed on unknown)
        assert payload["outcome"]["optionId"] == "reject-once"

    def test_callback_exception_fail_closed(self):
        """Callback raises → must not propagate; fail-closed to reject_once."""
        cb = MagicMock(side_effect=RuntimeError("UI crashed"))
        session, mock_client = self._setup(cb)
        session._handle_server_request(_make_perm_request())

        # Must have responded (not wedged)
        mock_client.respond.assert_called_once()
        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"]["optionId"] == "reject-once"

    def test_no_callback_no_bypass_fail_closed(self):
        """No callback + not bypass → fail-closed (deny path, not allow)."""
        session, mock_client = _make_session(
            approval_callback=None,
            auto_approve_permissions=False,
        )
        mock_client.request.return_value = {"sessionId": "s"}
        session.ensure_started()

        session._handle_server_request(_make_perm_request())

        mock_client.respond.assert_called_once()
        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"]["optionId"] == "reject-once"

    def test_no_matching_kind_cancelled(self):
        """Callback says 'once' but no allow_once in options → cancelled."""
        cb = MagicMock(return_value="once")
        session, mock_client = self._setup(cb)
        opts = [
            {"optionId": "reject-only", "kind": "reject_once"},
        ]
        session._handle_server_request(_make_perm_request(options=opts))

        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"] == {"outcome": "cancelled"}

    def test_no_options_at_all_cancelled(self):
        """Empty options list → cancelled regardless of callback."""
        cb = MagicMock(return_value="once")
        session, mock_client = self._setup(cb)
        session._handle_server_request(_make_perm_request(options=[]))

        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"] == {"outcome": "cancelled"}

    def test_callback_receives_raw_tool_call_dict(self):
        """The session is a pure protocol forwarder: the callback receives the
        raw ``toolCall`` dict extracted from the JSON-RPC request, untouched.

        No field extraction, label building, or description formatting happens
        in the session layer — that is the approval bridge / plugin adapter's
        job.  The dict is forwarded by identity (same object), including any
        ``rawInput`` (which may carry secrets; the callback/adapter is
        responsible for never leaking it).
        """
        cb = MagicMock(return_value="once")
        session, mock_client = self._setup(cb)
        tool_call = {
            "title": "git push",
            "kind": "tool",
            "toolCallId": "tc-1",
            "rawInput": "AWS_SECRET_KEY=xxxx",
        }
        session._handle_server_request(_make_perm_request(tool_call=tool_call))

        cb.assert_called_once()
        args, kwargs = cb.call_args
        # Single positional arg: the raw tool_call dict, forwarded as-is.
        assert args[0] is tool_call
        assert kwargs.get("allow_permanent") is False


class TestPermissionCallbackNonStringReturn:
    """Fail-closed when the approval callback returns a truthy non-string value.

    The callback contract says the return value must be a string ("once",
    "session", "always", "deny", or anything else → fail-closed).  But buggy
    callbacks may return non-string truthy values like ``True``, ``42``, or
    arbitrary objects.  The old code did ``decision.strip()`` which raises
    ``AttributeError`` on non-str types — that exception escapes the try/except
    guard (the try only wraps the *call*, not the *normalisation*) and crashes
    the turn / wedges the permission.

    Fix: normalise in a safe try or type-check before ``.strip()``.  Non-str
    values must go through the deny (fail-closed) path, never raise.
    """

    @pytest.mark.parametrize("ret_val", [True, 42, [1, 2], object()])
    def test_non_string_callback_return_fails_closed(self, ret_val):
        """Non-string return (True/42/list/object) → reject_once or cancelled,
        never AttributeError, never allow."""
        cb = MagicMock(return_value=ret_val)
        session, mock_client = _make_session(
            approval_callback=cb,
            auto_approve_permissions=False,
        )
        mock_client.request.return_value = {"sessionId": "s"}
        session.ensure_started()

        session._handle_server_request(_make_perm_request())

        # Must have responded (not wedged / crashed)
        mock_client.respond.assert_called_once()
        payload = mock_client.respond.call_args[0][1]
        outcome = payload["outcome"]
        # Must be deny or cancelled — never allow
        oid = outcome.get("optionId", "")
        assert oid.startswith("reject") or outcome["outcome"] == "cancelled"


class TestNonStringCallbackNoSecretLeak:
    """Regression: non-string approval callback warning must not log repr.

    A buggy callback object's __repr__ may embed secrets (API keys, tokens).
    The warning for non-string returns must log only the *type name*, never
    the value or repr, so secrets never reach the log stream.
    """

    SECRET = "AKIA-SECRET-TOKEN-DO-NOT-LEAK"

    def test_object_repr_secret_not_in_log(self, caplog):
        """Callback returns an object whose __repr__ contains a sentinel
        secret.  The warning log must contain the type name but NOT the
        sentinel.  Outcome must still be fail-closed (deny path)."""
        secret = self.SECRET

        class BuggyDecision:
            def __repr__(self) -> str:
                return f"BuggyDecision(secret={secret})"

        cb = MagicMock(return_value=BuggyDecision())
        session, mock_client = _make_session(
            approval_callback=cb,
            auto_approve_permissions=False,
        )
        mock_client.request.return_value = {"sessionId": "s"}
        session.ensure_started()

        import logging as _logging
        with caplog.at_level(_logging.WARNING, logger="agent.transports.acp_client_session"):
            session._handle_server_request(_make_perm_request())

        # Must have responded (fail-closed), not wedged
        mock_client.respond.assert_called_once()
        payload = mock_client.respond.call_args[0][1]
        outcome = payload["outcome"]
        oid = outcome.get("optionId", "")
        assert oid.startswith("reject") or outcome["outcome"] == "cancelled"

        # The warning must exist, contain the type name, but NOT the secret
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("non-string" in r.getMessage() for r in warnings)
        full_log = " ".join(r.getMessage() for r in warnings)
        assert "BuggyDecision" in full_log  # type name IS logged
        assert self.SECRET not in full_log  # secret value is NOT logged

    def test_object_repr_secret_not_in_log_even_if_format_has_placeholder(self, caplog):
        """Ensure no %r format slot remains that could interpolate the value.

        Double-check: even if the message template changed, the rendered
        text must never contain the sentinel secret from the object repr.
        """
        secret = self.SECRET

        class BuggyDecision:
            def __repr__(self) -> str:
                return f"<leak>{secret}</leak>"

        cb = MagicMock(return_value=BuggyDecision())
        session, mock_client = _make_session(
            approval_callback=cb,
            auto_approve_permissions=False,
        )
        mock_client.request.return_value = {"sessionId": "s"}
        session.ensure_started()

        import logging as _logging
        with caplog.at_level(_logging.WARNING, logger="agent.transports.acp_client_session"):
            session._handle_server_request(_make_perm_request())

        # Outcome fail-closed
        mock_client.respond.assert_called_once()
        payload = mock_client.respond.call_args[0][1]
        outcome = payload["outcome"]
        assert outcome.get("outcome") in ("cancelled", "selected")
        oid = outcome.get("optionId", "")
        assert oid.startswith("reject") or outcome["outcome"] == "cancelled"

        # Secret must never appear in any log line
        all_log = " ".join(r.getMessage() for r in caplog.records)
        assert self.SECRET not in all_log


class TestPermissionBypass:
    """Tests for auto_approve_permissions=True bypass mode."""

    def test_bypass_selects_allow_once(self):
        """auto_approve_permissions=True → selects allow_once optionId."""
        session, mock_client = _make_session(auto_approve_permissions=True)
        mock_client.request.return_value = {"sessionId": "s"}
        session.ensure_started()

        session._handle_server_request(_make_perm_request())

        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"] == {"outcome": "selected", "optionId": "allow-once"}

    def test_bypass_no_allow_once_falls_back_to_first(self):
        """Bypass: no allow_once but allow_always available → selects allow_always.

        (Updated: previously fell back to 'first option of any kind'; now
        prefers allow_once → allow_always, never selects a reject option.)
        """
        session, mock_client = _make_session(auto_approve_permissions=True)
        mock_client.request.return_value = {"sessionId": "s"}
        session.ensure_started()

        opts = [
            {"optionId": "allow-always-id", "kind": "allow_always"},
            {"optionId": "deny-id", "kind": "reject_once"},
        ]
        session._handle_server_request(_make_perm_request(options=opts))

        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"]["optionId"] == "allow-always-id"

    def test_bypass_prefers_allow_always_over_reject(self):
        """Bypass: options [reject_once, allow_always] → must select allow_always,
        NOT the reject_once that appears first."""
        session, mock_client = _make_session(auto_approve_permissions=True)
        mock_client.request.return_value = {"sessionId": "s"}
        session.ensure_started()

        opts = [
            {"optionId": "reject-first", "kind": "reject_once"},
            {"optionId": "allow-always-second", "kind": "allow_always"},
        ]
        session._handle_server_request(_make_perm_request(options=opts))

        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"]["optionId"] == "allow-always-second"

    def test_bypass_only_reject_options_cancelled(self):
        """Bypass: options contain only reject kinds → cancelled, NOT reject.

        Previously the 'first-any' fallback would pick the reject option,
        which violates auto-approve semantics (bypass should never reject).
        """
        session, mock_client = _make_session(auto_approve_permissions=True)
        mock_client.request.return_value = {"sessionId": "s"}
        session.ensure_started()

        opts = [
            {"optionId": "reject-1", "kind": "reject_once"},
            {"optionId": "reject-2", "kind": "reject_always"},
        ]
        session._handle_server_request(_make_perm_request(options=opts))

        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"] == {"outcome": "cancelled"}

    def test_bypass_no_options_cancelled(self):
        """Bypass: no options at all → cancelled."""
        session, mock_client = _make_session(auto_approve_permissions=True)
        mock_client.request.return_value = {"sessionId": "s"}
        session.ensure_started()

        session._handle_server_request(_make_perm_request(options=[]))

        payload = mock_client.respond.call_args[0][1]
        assert payload["outcome"] == {"outcome": "cancelled"}


# ---------------------------------------------------------------------------
# Tests: close
# ---------------------------------------------------------------------------


class TestClose:
    def test_close_sends_session_close_and_closes_client(self):
        """close() calls session/close then client.close()."""
        session, mock_client = _make_session()
        mock_client.request.side_effect = [
            {"sessionId": "sess-xyz"},  # session/new
            {},  # session/close
        ]
        session.ensure_started()
        session.close()

        # session/close request was made
        close_call = mock_client.request.call_args_list[-1]
        assert close_call[0][0] == "session/close"
        assert close_call[0][1]["sessionId"] == "sess-xyz"
        # client.close() was called
        mock_client.close.assert_called_once()

    def test_close_idempotent(self):
        """close() called twice does not raise."""
        session, mock_client = _make_session()
        mock_client.request.return_value = {"sessionId": "sess-x"}
        session.ensure_started()
        session.close()
        session.close()  # must not raise

    def test_context_manager_calls_close(self):
        """ACPClientSession used as context manager calls close() on exit."""
        session, mock_client = _make_session()
        mock_client.request.side_effect = [{"sessionId": "s1"}, {}]
        with session:
            session.ensure_started()
        mock_client.close.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: run_turn -- happy path
# ---------------------------------------------------------------------------


class TestRunTurn:
    def _setup_happy_session(self):
        """Return session + mock configured for a successful prompt turn."""
        session, mock_client = _make_session()
        mock_client.request.side_effect = [
            {"sessionId": "sess-happy"},  # session/new
            {"stopReason": "end_turn"},   # session/prompt
        ]
        return session, mock_client

    def test_run_turn_sends_session_prompt(self):
        """run_turn() sends session/prompt with the user text."""
        session, mock_client = self._setup_happy_session()
        # No streaming notifications
        mock_client.take_notification.side_effect = [
            None,  # polled once, returns None
            None,  # second poll -> triggers req_thread to finish
        ]

        result = session.run_turn("hello world", cwd="/tmp")
        # Check session/prompt was called
        prompt_call = None
        for c in mock_client.request.call_args_list:
            if c[0][0] == "session/prompt":
                prompt_call = c
                break
        assert prompt_call is not None
        assert prompt_call[0][1]["sessionId"] == "sess-happy"
        assert prompt_call[0][1]["prompt"][0]["text"] == "hello world"

    def test_run_turn_collects_text_from_streaming_chunks(self):
        """Text chunks from session/update notifications are assembled."""
        session, mock_client = _make_session()
        mock_client.request.side_effect = [
            {"sessionId": "sess-stream"},  # session/new
        ]

        deltas_received = []

        def on_delta(text):
            deltas_received.append(text)

        session2 = ACPClientSession(
            command="fake",
            on_delta=on_delta,
            client_factory=lambda **kw: mock_client,
        )

        # Notifications: two text chunks, then None to stop
        # The session/prompt result arrives through request()
        notes_iter = iter([
            {
                "method": "session/update",
                "params": {
                    "sessionId": "sess-stream",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "Hello "},
                    },
                },
            },
            {
                "method": "session/update",
                "params": {
                    "sessionId": "sess-stream",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "world!"},
                    },
                },
            },
            None,
        ])

        def take_notif(timeout=0.0):
            try:
                return next(notes_iter)
            except StopIteration:
                return None

        mock_client.take_notification.side_effect = take_notif
        mock_client.request.return_value = {"sessionId": "sess-stream"}

        # Override request to return promptResponse after chunks
        call_count = [0]
        def req_side_effect(method, params=None, timeout=30, **kwargs):
            call_count[0] += 1
            if method == "session/new":
                return {"sessionId": "sess-stream"}
            if method == "session/prompt":
                # Small sleep to let notification drain happen first
                time.sleep(0.05)
                return {"stopReason": "end_turn"}
            return {}

        mock_client.request.side_effect = req_side_effect

        result = session2.run_turn("test", cwd="/tmp")
        assert "Hello " in result.final_text
        assert "world!" in result.final_text
        assert "Hello " in deltas_received
        assert "world!" in deltas_received

    def test_run_turn_projects_message_into_messages(self):
        """A final text turn is projected into projected_messages."""
        session, mock_client = _make_session()

        def req_side_effect(method, params=None, timeout=30, **kwargs):
            if method == "session/new":
                return {"sessionId": "sess-proj"}
            if method == "session/prompt":
                time.sleep(0.02)
                return {"stopReason": "end_turn"}
            return {}

        mock_client.request.side_effect = req_side_effect

        # Push one text chunk via notification
        notes = [
            {
                "method": "session/update",
                "params": {
                    "sessionId": "sess-proj",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "Answer here."},
                    },
                },
            },
            None,
        ]
        notes_iter = iter(notes)
        mock_client.take_notification.side_effect = lambda timeout=0.0: next(notes_iter, None)

        result = session.run_turn("question")
        assert len(result.projected_messages) == 1
        assert result.projected_messages[0]["role"] == "assistant"
        assert result.projected_messages[0]["content"] == "Answer here."


# ---------------------------------------------------------------------------
# Tests: tool call capture -- assistant+tool message pairs from session/update
# ---------------------------------------------------------------------------


def _tool_call_note(
    *,
    session_id: str = "s",
    kind: str = "tool_call",
    tool_call_id: str = "tc-1",
    title: str = "bash",
    status: Optional[str] = None,
    raw_input: Any = None,
    raw_output: Any = None,
) -> dict:
    """Build a session/update notification carrying a tool-call lifecycle event.

    Field names mirror the ACP schema (ToolCallStart / ToolCallProgress): the
    payload lives under ``params.update`` with camelCase aliases.
    """
    update: dict = {"sessionUpdate": kind, "toolCallId": tool_call_id}
    if title is not None:
        update["title"] = title
    if status is not None:
        update["status"] = status
    if raw_input is not None:
        update["rawInput"] = raw_input
    if raw_output is not None:
        update["rawOutput"] = raw_output
    return {
        "method": "session/update",
        "params": {"sessionId": session_id, "update": update},
    }


def _text_chunk_note(text: str, session_id: str = "s") -> dict:
    return {
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    }


class TestToolCallCapture:
    """Capture tool-call lifecycle events into OpenAI-shaped message pairs.

    A ``tool_call`` start records the pending call and ticks
    ``tool_iterations`` once; a terminal ``tool_call_update`` pops it and
    appends an assistant(tool_calls) + tool result pair to
    ``projected_messages``, ahead of the final assistant text message.
    """

    @staticmethod
    def _run_with_notifications(notes: list) -> TurnResult:
        """Drive run_turn with a fixed list of notifications then a sentinel None."""
        session, mock_client = _make_session()

        def req_side_effect(method, params=None, timeout=30, **kwargs):
            if method == "session/new":
                return {"sessionId": "sess-tool"}
            if method == "session/prompt":
                time.sleep(0.05)  # let the main loop drain notifications first
                return {"stopReason": "end_turn"}
            return {}

        mock_client.request.side_effect = req_side_effect

        notes_iter = iter(list(notes) + [None, None])
        mock_client.take_notification.side_effect = lambda timeout=0.0: next(notes_iter, None)

        return session.run_turn("use a tool", cwd="/tmp")

    def test_start_then_complete_builds_message_pair(self):
        """tool_call start + tool_call_update(completed) -> assistant+tool pair."""
        result = self._run_with_notifications([
            _tool_call_note(
                kind="tool_call",
                title="bash",
                raw_input={"command": "ls -la"},
                status="in_progress",
            ),
            _tool_call_note(
                kind="tool_call_update",
                status="completed",
                raw_output="file1\nfile2",
            ),
        ])

        # tool_iterations ticks once per call (at start), not per notification.
        assert result.tool_iterations == 1

        msgs = result.projected_messages
        assert len(msgs) == 2

        assistant_call = msgs[0]
        assert assistant_call["role"] == "assistant"
        assert assistant_call["content"] is None
        tc = assistant_call["tool_calls"][0]
        assert tc["id"] == "tc-1"
        assert tc["function"]["name"] == "bash"
        # dict rawInput is JSON-encoded into the arguments string
        assert tc["function"]["arguments"] == '{"command": "ls -la"}'

        tool_result = msgs[1]
        assert tool_result["role"] == "tool"
        assert tool_result["tool_call_id"] == "tc-1"
        assert tool_result["content"] == "file1\nfile2"
        assert tool_result["tool_name"] == "bash"

    def test_pair_lands_before_final_assistant_text(self):
        """The tool pair is projected before the final assistant text message."""
        result = self._run_with_notifications([
            _tool_call_note(kind="tool_call", title="bash", raw_input={"command": "pwd"}),
            _tool_call_note(kind="tool_call_update", status="completed", raw_output="/tmp"),
            _text_chunk_note("All done."),
        ])

        roles = [m["role"] for m in result.projected_messages]
        assert roles == ["assistant", "tool", "assistant"]
        # The trailing assistant message is the assembled text reply.
        assert result.projected_messages[-1]["content"] == "All done."

    def test_in_progress_update_does_not_emit_pair(self):
        """A non-terminal tool_call_update (in_progress) emits no pair and keeps
        the call pending until the terminal update arrives."""
        result = self._run_with_notifications([
            _tool_call_note(kind="tool_call", title="bash", raw_input={"command": "ls"}),
            _tool_call_note(kind="tool_call_update", status="in_progress"),
            _tool_call_note(kind="tool_call_update", status="completed", raw_output="ok"),
        ])

        assert result.tool_iterations == 1
        msgs = result.projected_messages
        assert len(msgs) == 2  # only the terminal update produced a pair
        assert msgs[0]["role"] == "assistant"
        assert msgs[1]["role"] == "tool"
        assert msgs[1]["content"] == "ok"

    def test_failed_status_is_terminal(self):
        """A 'failed' status also closes the call and projects the pair."""
        result = self._run_with_notifications([
            _tool_call_note(kind="tool_call", title="bash", raw_input={"command": "bad"}),
            _tool_call_note(kind="tool_call_update", status="failed", raw_output="command not found"),
        ])

        assert result.tool_iterations == 1
        assert len(result.projected_messages) == 2
        assert result.projected_messages[1]["content"] == "command not found"

    def test_terminal_update_without_start_is_skipped(self):
        """A terminal update with no matching start does not fabricate a pair."""
        result = self._run_with_notifications([
            _tool_call_note(
                kind="tool_call_update",
                tool_call_id="orphan",
                status="completed",
                raw_output="ghost",
            ),
        ])

        assert result.tool_iterations == 0
        assert result.projected_messages == []

    def test_dict_raw_input_json_encoded_empty_becomes_empty_object(self):
        """Missing rawInput -> arguments defaults to '{}' (valid JSON), not ''."""
        result = self._run_with_notifications([
            _tool_call_note(kind="tool_call", title="noop", raw_input=None),
            _tool_call_note(kind="tool_call_update", status="completed", raw_output=None),
        ])

        assistant_call = result.projected_messages[0]
        assert assistant_call["tool_calls"][0]["function"]["arguments"] == "{}"
        tool_result = result.projected_messages[1]
        assert tool_result["content"] == ""  # None rawOutput -> empty string

    def test_multiple_distinct_tool_calls_each_projected(self):
        """Two independent tool calls produce two separate pairs, in order."""
        result = self._run_with_notifications([
            _tool_call_note(kind="tool_call", tool_call_id="a", title="ls", raw_input={}),
            _tool_call_note(kind="tool_call_update", tool_call_id="a", status="completed", raw_output="a-out"),
            _tool_call_note(kind="tool_call", tool_call_id="b", title="cat", raw_input={"path": "x"}),
            _tool_call_note(kind="tool_call_update", tool_call_id="b", status="completed", raw_output="b-out"),
        ])

        assert result.tool_iterations == 2
        ids = [
            m["tool_calls"][0]["id"]
            for m in result.projected_messages
            if m.get("role") == "assistant"
        ]
        assert ids == ["a", "b"]

    def test_pending_tool_calls_cleared_between_turns(self):
        """A tool call that starts but never completes does not leak into the
        next turn's projected history."""
        session, mock_client = _make_session()

        def req_side_effect(method, params=None, timeout=30, **kwargs):
            if method == "session/new":
                return {"sessionId": "sess-clear"}
            if method == "session/prompt":
                time.sleep(0.03)
                return {"stopReason": "end_turn"}
            return {}

        mock_client.request.side_effect = req_side_effect

        # Turn 1: a tool call starts but never reaches a terminal update.
        notes1 = iter([
            _tool_call_note(kind="tool_call", tool_call_id="leak", title="bash", raw_input={}),
            None, None,
        ])
        mock_client.take_notification.side_effect = lambda timeout=0.0: next(notes1, None)
        result1 = session.run_turn("interrupt me", cwd="/tmp")
        assert result1.tool_iterations == 1
        assert result1.projected_messages == []  # no completion -> no pair
        assert "leak" in session._pending_tool_calls  # dangling in-turn

        # Turn 2: clean -- no stale pair from the leaked pending entry.
        notes2 = iter([None, None])
        mock_client.take_notification.side_effect = lambda timeout=0.0: next(notes2, None)
        result2 = session.run_turn("fresh turn", cwd="/tmp")
        assert result2.tool_iterations == 0
        assert result2.projected_messages == []
        assert session._pending_tool_calls == {}


class TestStringifyToolPayload:
    """Unit tests for the rawInput/rawOutput -> string coercion helper."""

    def test_none_returns_empty_string(self):
        assert _stringify_tool_payload(None) == ""

    def test_string_passed_through(self):
        assert _stringify_tool_payload("plain text") == "plain text"

    def test_dict_json_encoded(self):
        assert _stringify_tool_payload({"a": 1, "b": "x"}) == '{"a": 1, "b": "x"}'

    def test_list_json_encoded(self):
        assert _stringify_tool_payload([1, 2, 3]) == "[1, 2, 3]"

    def test_int_stringified(self):
        assert _stringify_tool_payload(42) == "42"

    def test_unicode_preserved(self):
        assert _stringify_tool_payload({"city": "Zürich"}) == '{"city": "Zürich"}'


# ---------------------------------------------------------------------------
# Tests: should_retire policy
# ---------------------------------------------------------------------------


class TestShouldRetire:
    def test_subprocess_crash_sets_should_retire(self):
        """When the process exits unexpectedly, should_retire=True."""
        session, mock_client = _make_session()

        call_count = [0]
        def req_side_effect(method, params=None, timeout=30, **kwargs):
            call_count[0] += 1
            if method == "session/new":
                return {"sessionId": "sess-crash"}
            if method == "session/prompt":
                # Simulate blocking while process dies
                time.sleep(0.1)
                raise RuntimeError("stdin closed unexpectedly")
            return {}

        mock_client.request.side_effect = req_side_effect
        # Process dies after first poll
        alive_iter = iter([True, True, False, False])
        mock_client.is_alive.side_effect = lambda: next(alive_iter, False)

        result = session.run_turn("hello")
        assert result.should_retire is True
        assert result.error is not None

    def test_session_prompt_acp_error_sets_should_retire_for_negative_code(self):
        """ACPClientError with negative code (system error) -> should_retire."""
        session, mock_client = _make_session()

        def req_side_effect(method, params=None, timeout=30, **kwargs):
            if method == "session/new":
                return {"sessionId": "sess-err"}
            if method == "session/prompt":
                time.sleep(0.02)
                raise ACPClientError(code=-32603, message="internal error")
            return {}

        mock_client.request.side_effect = req_side_effect

        result = session.run_turn("hello")
        assert result.error is not None
        assert "session/prompt failed" in result.error
        assert result.should_retire is True

    def test_session_prompt_timeout_sets_should_retire(self):
        """TimeoutError from session/prompt sets should_retire."""
        session, mock_client = _make_session()

        def req_side_effect(method, params=None, timeout=30, **kwargs):
            if method == "session/new":
                return {"sessionId": "sess-timeout"}
            if method == "session/prompt":
                raise TimeoutError("ACP method timed out")
            return {}

        mock_client.request.side_effect = req_side_effect

        result = session.run_turn("hello")
        assert result.should_retire is True
        assert result.error is not None


# ---------------------------------------------------------------------------
# Tests: inactivity timeout -- wait_cb, last_activity renewal, retirement
# ---------------------------------------------------------------------------


class TestInactivityTimeout:
    """Tests for the ACP session inactivity-based timeout mechanism.

    These tests exercise the _wait_cb / _touch_activity / _idle_age closures
    defined inside run_turn().  The closures are not directly accessible, so
    we verify their behaviour through observable effects:

    - The mock client's request() receives wait_cb as a keyword arg and
      invokes it, simulating the queue.Empty path that the real ACPClient
      would follow.  This tests the closure logic, not a mock in isolation:
      the closures are PRODUCTION code that runs inside run_turn().

    - For the inactivity-limit test, wait_cb is called with enough idle time
      to exceed turn_timeout, causing RuntimeError -> should_retire=True.

    Key production behaviours under test:
    - session/prompt is called with timeout=turn_timeout and wait_cb=_wait_cb
    - Notifications (session/update) call _touch_activity() -> renewal
    - Server requests call _touch_activity() -> renewal (before handler)
    - Non-session/update notifications do NOT call _touch_activity()
    - When idle >= turn_timeout, _wait_cb raises RuntimeError containing
      'inactive' -> result.should_retire=True, result.error contains 'inactive'
    """

    @staticmethod
    def _make_session_with_captured_wait_cb(
        *,
        turn_timeout: float = 0.2,
        notifications: list = None,
        server_requests: list = None,
    ) -> tuple:
        """Create a session whose mock client captures and invokes wait_cb.

        The mock client's request() side_effect:
        - For session/new: returns a sessionId
        - For session/prompt: captures wait_cb, simulates queue.Empty by
          calling wait_cb(), and returns a result if wait_cb doesn't raise.

        Notifications and server_requests are delivered through the mock
        client's take_notification / take_server_request, simulating the
        main drain loop in run_turn().
        """
        captured = {"wait_cb": None, "request_calls": []}

        def req_side_effect(method, params=None, timeout=30, **kwargs):
            captured["request_calls"].append({
                "method": method,
                "timeout": timeout,
                "has_wait_cb": "wait_cb" in kwargs,
            })
            if method == "session/new":
                return {"sessionId": "sess-inactive"}
            if method == "session/prompt":
                wait_cb = kwargs.get("wait_cb")
                captured["wait_cb"] = wait_cb
                if wait_cb is not None:
                    # Simulate the request blocking briefly so the main loop
                    # can drain notifications/server requests, then simulate
                    # queue.Empty -> wait_cb is called.
                    time.sleep(0.05)
                    # If wait_cb raises, the exception propagates (as in production).
                    next_t = wait_cb()
                    # If it returns a positive value, simulate a response
                    # arriving after the wait.
                    return {"stopReason": "end_turn"}
                return {"stopReason": "end_turn"}
            return {}

        mock_client = MagicMock()
        mock_client.is_alive.return_value = True
        mock_client.initialize.return_value = {"protocolVersion": 1}
        mock_client.request.side_effect = req_side_effect
        mock_client.stderr_tail.return_value = []

        # Set up notification delivery
        notif_iter = iter(notifications or [None])
        mock_client.take_notification.side_effect = lambda timeout=0.0: next(notif_iter, None)

        # Set up server request delivery
        sreq_iter = iter(server_requests or [None])
        mock_client.take_server_request.side_effect = lambda timeout=0.0: next(sreq_iter, None)

        session = ACPClientSession(
            command="fake-acp",
            client_factory=lambda **kw: mock_client,
        )
        return session, mock_client, captured

    def test_notification_updates_last_activity_crossing_initial_60s(self):
        """A session/update notification received while session/prompt is
        blocking calls _touch_activity(), resetting the idle clock.

        We verify that _wait_cb returns a positive finite timeout when
        activity has been renewed by a notification, rather than raising
        RuntimeError.  Without the renewal, idle would exceed turn_timeout
        and _wait_cb would raise.

        We use a mock time.monotonic to control idle age without real waits.
        """
        # We need to control time.monotonic to simulate idle age.
        # The _wait_cb closure uses time.monotonic() via _idle_age().
        # We patch time.monotonic in the acp_client_session module.
        mock_times = [1000.0]  # start time
        time_call_count = [0]

        original_monotonic = time.monotonic

        def mock_monotonic():
            time_call_count[0] += 1
            # First few calls are for _last_activity initialization.
            # Subsequent calls represent "now" -- we advance time to simulate
            # idle age crossing the turn_timeout boundary, then reset after
            # a notification.
            if time_call_count[0] <= 1:
                return 1000.0  # _last_activity = 1000.0
            # After notification is processed, _touch_activity resets
            # _last_activity.  We return a value that keeps idle < turn_timeout.
            return 1000.0 + 0.05  # idle = 0.05s, well under turn_timeout=0.2

        notif = {
            "method": "session/update",
            "params": {
                "sessionId": "sess-inactive",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "working..."},
                },
            },
        }

        session, mock_client, captured = self._make_session_with_captured_wait_cb(
            turn_timeout=0.2,
            notifications=[notif, None],
        )

        with patch("agent.transports.acp_client_session.time.monotonic", mock_monotonic):
            result = session.run_turn("hello", turn_timeout=0.2)

        # _wait_cb should have been called and returned a positive value
        # (not raised RuntimeError), proving the notification renewed activity.
        assert captured["wait_cb"] is not None, "wait_cb was not passed to request()"
        assert result.error is None or "inactive" not in (result.error or ""), (
            f"Session timed out despite notification renewal: {result.error}"
        )

    def test_server_request_updates_activity(self):
        """A server-initiated request (e.g. session/request_permission)
        received while session/prompt is blocking calls _touch_activity(),
        renewing the idle clock so _wait_cb doesn't raise.

        Uses auto_approve_permissions=True so the permission is auto-granted.
        """
        mock_times = [1000.0]
        time_call_count = [0]

        def mock_monotonic():
            time_call_count[0] += 1
            if time_call_count[0] <= 1:
                return 1000.0
            return 1000.0 + 0.05  # idle = 0.05s, under turn_timeout=0.2

        sreq = {
            "id": 99,
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess-inactive",
                "toolCall": {"title": "bash", "kind": "tool", "toolCallId": "c1"},
                "options": [{"optionId": "allow-once", "kind": "allow_once"}],
            },
        }

        session, mock_client, captured = self._make_session_with_captured_wait_cb(
            turn_timeout=0.2,
            server_requests=[sreq, None],
        )
        session._auto_approve_permissions = True

        with patch("agent.transports.acp_client_session.time.monotonic", mock_monotonic):
            result = session.run_turn("hello", turn_timeout=0.2)

        # Server request should have been handled (respond called)
        mock_client.respond.assert_called_once()
        # _wait_cb should not have raised RuntimeError
        assert captured["wait_cb"] is not None
        assert result.error is None or "inactive" not in (result.error or ""), (
            f"Session timed out despite server request renewal: {result.error}"
        )

    def test_inactivity_limit_returns_inactive_error_and_retires(self):
        """When the session is silent for >= turn_timeout (no notifications,
        no server requests, no prompt response), _wait_cb raises RuntimeError
        with 'inactive' in the message, and the TurnResult has
        should_retire=True and error containing 'inactive'.
        """
        # Simulate time advancing past turn_timeout
        time_call_count = [0]

        def mock_monotonic():
            time_call_count[0] += 1
            if time_call_count[0] <= 1:
                return 1000.0  # _last_activity = 1000.0
            # idle = 0.3s, which exceeds turn_timeout=0.2
            return 1000.0 + 0.3

        session, mock_client, captured = self._make_session_with_captured_wait_cb(
            turn_timeout=0.2,
            notifications=[None],  # no notifications
        )

        with patch("agent.transports.acp_client_session.time.monotonic", mock_monotonic):
            result = session.run_turn("hello", turn_timeout=0.2)

        assert result.should_retire is True
        assert result.error is not None
        assert "inactive" in result.error.lower(), (
            f"Expected 'inactive' in error, got: {result.error}"
        )

    def test_initial_timeout_uses_turn_timeout_when_below_60(self):
        """The session/prompt request's initial timeout equals turn_timeout
        (not the old hardcoded 60.0), so the first inactivity check happens
        at the configured interval.

        With turn_timeout=0.2 (< 60), the initial timeout passed to
        request() must be 0.2, not 60.0.
        """
        wait_cb_returns: list = []
        prompt_timeout_seen: list = []

        def req_side_effect(method, params=None, timeout=30, **kwargs):
            if method == "session/new":
                return {"sessionId": "sess-inactive"}
            if method == "session/prompt":
                prompt_timeout_seen.append(timeout)
                wait_cb = kwargs.get("wait_cb")
                if wait_cb is not None:
                    ret = wait_cb()
                    wait_cb_returns.append(ret)
                    return {"stopReason": "end_turn"}
            return {}

        mock_client = MagicMock()
        mock_client.is_alive.return_value = True
        mock_client.initialize.return_value = {"protocolVersion": 1}
        mock_client.request.side_effect = req_side_effect
        mock_client.stderr_tail.return_value = []
        mock_client.take_notification.side_effect = lambda timeout=0.0: None
        mock_client.take_server_request.side_effect = lambda timeout=0.0: None

        session = ACPClientSession(
            command="fake-acp",
            client_factory=lambda **kw: mock_client,
        )

        result = session.run_turn("hello", turn_timeout=0.2)

        # The initial timeout must equal turn_timeout (0.2), not 60.0
        assert len(prompt_timeout_seen) >= 1
        t = prompt_timeout_seen[0]
        assert t == 0.2
        assert 0 < t < float("inf")

        # wait_cb returns max(60.0, turn_timeout - idle) which is always
        # positive and finite when idle < turn_timeout.
        assert len(wait_cb_returns) >= 1
        for v in wait_cb_returns:
            assert v is not None
            assert v > 0
            assert v != float("inf")

    def test_non_session_update_notification_does_not_renew_activity(self):
        """A notification whose method is NOT ``session/update`` must NOT
        renew the idle clock.  Without the fix, the caller unconditionally
        called _touch_activity() after _process_notification() regardless
        of whether the notification was legitimate, so junk notifications
        would keep the session alive indefinitely.

        We simulate idle time exceeding turn_timeout via mock_monotonic,
        then deliver a junk notification.  The junk notification should be
        dropped (not touch), and _wait_cb should raise RuntimeError ->
        should_retire=True.
        """
        time_call_count = [0]

        def mock_monotonic():
            time_call_count[0] += 1
            if time_call_count[0] <= 1:
                return 1000.0
            # idle = 0.3s, exceeds turn_timeout=0.2
            return 1000.0 + 0.3

        # Junk notification: method is NOT session/update
        junk_note = {
            "method": "some/unknown_method",
            "params": {},
        }

        session, mock_client, captured = self._make_session_with_captured_wait_cb(
            turn_timeout=0.2,
            notifications=[junk_note, None],
        )

        with patch("agent.transports.acp_client_session.time.monotonic", mock_monotonic):
            result = session.run_turn("hello", turn_timeout=0.2)

        # The junk notification should NOT have renewed activity -> timeout
        assert result.should_retire is True
        assert result.error is not None
        assert "inactive" in result.error.lower(), (
            f"Expected inactivity timeout because junk notification did not renew: {result.error}"
        )

    def test_legitimate_session_update_notification_renews_activity(self):
        """A legitimate ``session/update`` notification DOES renew the idle
        clock so _wait_cb returns a positive finite timeout (no RuntimeError).

        We simulate a very short idle (via mock_monotonic returning a value
        just past initialization) and deliver a real session/update.  The
        notification should touch activity, and _wait_cb should not raise.
        """
        time_call_count = [0]

        def mock_monotonic():
            time_call_count[0] += 1
            if time_call_count[0] <= 1:
                return 1000.0
            return 1000.0 + 0.05  # idle = 0.05s, under turn_timeout=0.2

        notif = {
            "method": "session/update",
            "params": {
                "sessionId": "sess-inactive",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "working..."},
                },
            },
        }

        session, mock_client, captured = self._make_session_with_captured_wait_cb(
            turn_timeout=0.2,
            notifications=[notif, None],
        )

        with patch("agent.transports.acp_client_session.time.monotonic", mock_monotonic):
            result = session.run_turn("hello", turn_timeout=0.2)

        assert captured["wait_cb"] is not None, "wait_cb was not passed to request()"
        assert result.error is None or "inactive" not in (result.error or ""), (
            f"Session timed out despite legitimate notification renewal: {result.error}"
        )

    def test_server_request_touch_before_handler(self):
        """Server request touch must happen BEFORE _handle_server_request, not
        after.  Permission approval may block, and if touch happened after
        the handler, the request thread's _wait_cb would see stale idle time
        during the block and fire inactivity.

        We verify the ordering by having the handler check idle age via the
        captured wait_cb: if touch happened first, idle should be small
        (just renewed); if touch happened after, idle would be large and
        _wait_cb would raise.

        Uses a controllable monotonic clock: starts at 1000.0, then advances
        to 1000.0 + 0.3 (exceeding turn_timeout=0.2).  The handler calls
        wait_cb(); if touch happened before the handler, _last_activity was
        just reset to ~1000.3, so idle is ~0 -> wait_cb returns positive.
        If touch happened after, _last_activity is still 1000.0, idle is 0.3
        -> wait_cb raises RuntimeError.
        """
        time_call_count = [0]

        def mock_monotonic():
            time_call_count[0] += 1
            if time_call_count[0] <= 1:
                return 1000.0  # _last_activity initialization
            # Advance time past turn_timeout so that if touch hasn't happened,
            # _wait_cb would raise.
            return 1000.0 + 0.3

        handler_wait_cb_result: list = []  # captures what wait_cb returns inside handler
        handler_raised: list = []

        sreq = {
            "id": 99,
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess-inactive",
                "toolCall": {"title": "bash", "kind": "tool", "toolCallId": "c1"},
                "options": [{"optionId": "allow-once", "kind": "allow_once"}],
            },
        }

        # Build session with captured wait_cb and a custom handler that
        # calls wait_cb to verify the idle clock was just renewed.
        captured = {"wait_cb": None}

        def req_side_effect(method, params=None, timeout=30, **kwargs):
            if method == "session/new":
                return {"sessionId": "sess-inactive"}
            if method == "session/prompt":
                wait_cb = kwargs.get("wait_cb")
                captured["wait_cb"] = wait_cb
                time.sleep(0.05)
                if wait_cb is not None:
                    next_t = wait_cb()
                    return {"stopReason": "end_turn"}
                return {"stopReason": "end_turn"}
            return {}

        mock_client = MagicMock()
        mock_client.is_alive.return_value = True
        mock_client.initialize.return_value = {"protocolVersion": 1}
        mock_client.request.side_effect = req_side_effect
        mock_client.stderr_tail.return_value = []
        mock_client.take_notification.side_effect = lambda timeout=0.0: None

        # Deliver the server request, then None
        sreq_iter = iter([sreq, None])
        mock_client.take_server_request.side_effect = lambda timeout=0.0: next(sreq_iter, None)

        session = ACPClientSession(
            command="fake-acp",
            auto_approve_permissions=True,
            client_factory=lambda **kw: mock_client,
        )

        # Patch _handle_server_request to call the captured wait_cb inside it.
        # If touch happened before this handler, wait_cb should return a
        # positive value (idle was just reset).  If touch happened after,
        # wait_cb would raise RuntimeError.
        original_handler = session._handle_server_request

        def verifying_handler(req):
            # Inside the handler, call wait_cb to check idle state.
            # If touch happened before us, idle should be ~0 (just renewed).
            # If not, idle would be 0.3 > turn_timeout=0.2 -> RuntimeError.
            wc = captured.get("wait_cb")
            if wc is not None:
                try:
                    ret = wc()
                    handler_wait_cb_result.append(ret)
                except RuntimeError as exc:
                    handler_raised.append(exc)
            original_handler(req)

        session._handle_server_request = verifying_handler

        with patch("agent.transports.acp_client_session.time.monotonic", mock_monotonic):
            result = session.run_turn("hello", turn_timeout=0.2)

        # The handler should have seen a renewed idle clock (no RuntimeError)
        assert not handler_raised, (
            f"_wait_cb raised inside handler -- touch did not happen before handler: "
            f"{handler_raised[0] if handler_raised else ''}"
        )
        assert len(handler_wait_cb_result) >= 1, "wait_cb was not called inside handler"
        assert handler_wait_cb_result[0] > 0, (
            f"wait_cb returned non-positive inside handler: {handler_wait_cb_result[0]}"
        )
        # The session should not have timed out
        assert result.error is None or "inactive" not in (result.error or ""), (
            f"Session timed out -- touch did not happen before handler: {result.error}"
        )



class TestServerRequestHandling:
    def test_permission_request_grants_allow_once(self):
        """Fix 3: Permission requests use ACP outcome shape and grant allow_once (bypass)."""
        session, mock_client = _make_session(auto_approve_permissions=True)
        mock_client.request.side_effect = [
            {"sessionId": "sess-perm"},  # session/new
        ]

        session.ensure_started()
        req = {
            "id": 42,
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess-perm",
                "toolCall": {"title": "bash", "kind": "tool", "toolCallId": "c42"},
                "options": [
                    {"optionId": "allow-once-id", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "deny-id", "name": "Deny", "kind": "reject_once"},
                ],
            },
        }
        session._handle_server_request(req)

        # Must use ACP spec outcome shape, not {granted: ...}
        mock_client.respond.assert_called_once()
        payload = mock_client.respond.call_args[0][1]
        assert "outcome" in payload
        assert "granted" not in payload
        assert payload["outcome"]["outcome"] == "selected"
        assert payload["outcome"]["optionId"] == "allow-once-id"

    def test_fs_write_declined_with_error(self):
        """fs/write_text_file is declined with respond_error."""
        session, mock_client = _make_session()
        mock_client.request.return_value = {"sessionId": "sess-fs"}
        session.ensure_started()

        req = {"id": 7, "method": "fs/write_text_file", "params": {"path": "/etc/passwd", "content": "bad"}}
        session._handle_server_request(req)

        mock_client.respond_error.assert_called_once()
        call_args = mock_client.respond_error.call_args
        # respond_error(rid, code=..., message=...) -- rid is positional
        assert call_args[0][0] == 7
        assert call_args[1]["code"] == -32601  # method not supported

    def test_unknown_server_request_declined_with_error(self):
        """Unknown server requests receive respond_error."""
        session, mock_client = _make_session()
        mock_client.request.return_value = {"sessionId": "sess-unk"}
        session.ensure_started()

        req = {"id": 99, "method": "some/unknown_method", "params": {}}
        session._handle_server_request(req)
        mock_client.respond_error.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_extract_text_from_text_chunk(self):
        params = {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hello"},
            },
        }
        assert _extract_text_from_update(params) == "hello"

    def test_extract_text_from_non_text_chunk_returns_empty(self):
        params = {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "tool_call_update",
                "content": {"type": "image"},
            },
        }
        assert _extract_text_from_update(params) == ""

    def test_is_tool_iteration_for_tool_call_update(self):
        params = {"update": {"sessionUpdate": "tool_call_update"}}
        assert _is_tool_iteration(params) is True

    def test_is_tool_iteration_for_agent_message_returns_false(self):
        params = {"update": {"sessionUpdate": "agent_message_chunk"}}
        assert _is_tool_iteration(params) is False

    def test_coerce_user_input_string(self):
        assert _coerce_user_input("hello") == "hello"

    def test_coerce_user_input_list_of_text_blocks(self):
        result = _coerce_user_input([{"type": "text", "text": "hello"}])
        assert result == "hello"

    def test_coerce_user_input_image_block_replaced(self):
        result = _coerce_user_input([{"type": "image"}])
        assert "[image attached]" in result

    def test_coerce_user_input_none(self):
        assert _coerce_user_input(None) == ""

    def test_coerce_user_input_integer(self):
        assert _coerce_user_input(42) == "42"


# ---------------------------------------------------------------------------
# Tests: MCP server forwarding -- translator + session/new plumbing
# ---------------------------------------------------------------------------


class TestTranslateMcpServers:
    """Unit tests for _translate_mcp_servers() covering all ACP wire shapes.

    Ground truth (empirically probed against claude-agent-acp v0.39):
      stdio  (NO type field): {name, command, args:[str], env:[{name,value}]}
      http:  {type:"http", name, url, headers:[{name,value}]}
      sse:   {type:"sse",  name, url, headers:[{name,value}]}
    env/headers must always be present as arrays ([] when empty).
    """

    def test_stdio_with_env_dict_converted_to_array(self):
        """env dict -> [{name, value}] array; no 'type' field in output."""
        result = _translate_mcp_servers({
            "myserver": {
                "command": "npx",
                "args": ["-y", "@my/mcp-server"],
                "env": {"API_KEY": "secret", "DEBUG": "1"},
            }
        })
        assert len(result) == 1
        srv = result[0]
        assert srv["name"] == "myserver"
        assert srv["command"] == "npx"
        assert srv["args"] == ["-y", "@my/mcp-server"]
        # env must be an array, not a dict
        assert isinstance(srv["env"], list)
        env_map = {e["name"]: e["value"] for e in srv["env"]}
        assert env_map == {"API_KEY": "secret", "DEBUG": "1"}
        # stdio must NOT have a "type" field
        assert "type" not in srv

    def test_stdio_with_no_env_emits_empty_array(self):
        """env absent in config -> env:[] in output (REQUIRED by ACP spec)."""
        result = _translate_mcp_servers({
            "bare": {"command": "node", "args": ["index.js"]},
        })
        assert len(result) == 1
        srv = result[0]
        assert srv["env"] == []   # must be [] not missing
        assert "type" not in srv

    def test_stdio_with_explicit_empty_env_emits_empty_array(self):
        """env:{} in config -> env:[] in output."""
        result = _translate_mcp_servers({
            "srv": {"command": "python3", "args": ["-m", "mcp"], "env": {}},
        })
        assert result[0]["env"] == []

    def test_http_with_headers_dict_converted_to_array(self):
        """url + headers dict -> type:http + headers array."""
        result = _translate_mcp_servers({
            "remote": {
                "url": "https://example.com/mcp",
                "headers": {"X-Api-Key": "abc123", "Accept": "application/json"},
            }
        })
        assert len(result) == 1
        srv = result[0]
        assert srv["type"] == "http"
        assert srv["name"] == "remote"
        assert srv["url"] == "https://example.com/mcp"
        assert isinstance(srv["headers"], list)
        hdr_map = {h["name"]: h["value"] for h in srv["headers"]}
        assert hdr_map == {"X-Api-Key": "abc123", "Accept": "application/json"}
        # http must NOT have env or command
        assert "env" not in srv
        assert "command" not in srv

    def test_http_with_no_headers_emits_empty_array(self):
        """headers absent -> headers:[] (REQUIRED by ACP spec)."""
        result = _translate_mcp_servers({
            "pub": {"url": "https://pub.example.com/mcp"},
        })
        srv = result[0]
        assert srv["type"] == "http"
        assert srv["headers"] == []

    def test_sse_transport_hint_sets_type_sse(self):
        """Hermes 'transport: sse' -> ACP type:'sse'."""
        result = _translate_mcp_servers({
            "events": {
                "url": "http://localhost:8000/sse",
                "transport": "sse",
                "headers": {},
            }
        })
        srv = result[0]
        assert srv["type"] == "sse"
        assert srv["name"] == "events"
        assert srv["url"] == "http://localhost:8000/sse"
        assert srv["headers"] == []

    def test_sse_via_type_key_also_accepted(self):
        """Hermes 'type: sse' (alternative key) -> ACP type:'sse'."""
        result = _translate_mcp_servers({
            "events2": {"url": "http://localhost:9000/sse", "type": "sse"},
        })
        assert result[0]["type"] == "sse"

    def test_malformed_no_command_no_url_skipped(self):
        """Entry with neither command nor url is skipped, not an error."""
        result = _translate_mcp_servers({
            "bad": {"timeout": 30, "auth": {"token": "x"}},
        })
        assert result == []

    def test_malformed_entry_does_not_block_valid_entries(self):
        """Malformed entry is skipped; valid sibling entries are still translated."""
        result = _translate_mcp_servers({
            "bad": {"timeout": 30},
            "good": {"command": "npx", "args": []},
        })
        assert len(result) == 1
        assert result[0]["name"] == "good"

    def test_both_command_and_url_prefers_stdio(self):
        """Both command+url set -> stdio wins (no type field)."""
        result = _translate_mcp_servers({
            "ambig": {
                "command": "my-cmd",
                "url": "https://remote.example.com",
                "env": {},
            }
        })
        srv = result[0]
        assert "type" not in srv
        assert srv["command"] == "my-cmd"

    def test_hermes_only_keys_dropped(self):
        """timeout/connect_timeout/auth/sampling are NOT forwarded to ACP."""
        result = _translate_mcp_servers({
            "srv": {
                "command": "node",
                "args": [],
                "env": {},
                "timeout": 30,
                "connect_timeout": 5,
                "auth": {"type": "oauth"},
                "sampling": True,
            }
        })
        srv = result[0]
        for dropped in ("timeout", "connect_timeout", "auth", "sampling"):
            assert dropped not in srv

    def test_empty_config_returns_empty_list(self):
        """No servers configured -> []."""
        assert _translate_mcp_servers({}) == []

    def test_none_config_returns_empty_list(self):
        """None config -> []."""
        assert _translate_mcp_servers(None) == []

    def test_multiple_servers_all_translated(self):
        """Multiple entries all appear in the output list."""
        result = _translate_mcp_servers({
            "stdio1": {"command": "cmd1", "args": []},
            "http1":  {"url": "https://a.com"},
            "sse1":   {"url": "https://b.com/sse", "transport": "sse"},
        })
        names = {s["name"] for s in result}
        assert names == {"stdio1", "http1", "sse1"}


class TestMcpServersPlumbedIntoSessionNew:
    """Assert the translated mcp_servers list reaches session/new."""

    def _make_acp_session(self, mcp_servers):
        mock_client = MagicMock()
        mock_client.is_alive.return_value = True
        mock_client.initialize.return_value = {"protocolVersion": 1}
        mock_client.take_notification.return_value = None
        mock_client.take_server_request.return_value = None
        mock_client.stderr_tail.return_value = []
        # session/new returns a sessionId
        mock_client.request.return_value = {"sessionId": "sess-mcp"}

        session = ACPClientSession(
            command="fake-acp",
            mcp_servers=mcp_servers,
            client_factory=lambda **kw: mock_client,
        )
        return session, mock_client

    def test_translated_servers_forwarded_in_session_new(self):
        """session/new receives the exact translated list, not []."""
        servers = [
            {"name": "srv1", "command": "npx", "args": [], "env": []},
            {"type": "http", "name": "srv2", "url": "https://x.com", "headers": []},
        ]
        session, mock_client = self._make_acp_session(mcp_servers=servers)
        session.ensure_started(cwd="/tmp")

        call = mock_client.request.call_args_list[0]
        assert call[0][0] == "session/new"
        params = call[0][1]
        assert params["mcpServers"] == servers

    def test_empty_mcp_servers_sends_empty_list(self):
        """None/[] -> mcpServers:[] in session/new (preserved original behavior)."""
        session, mock_client = self._make_acp_session(mcp_servers=None)
        session.ensure_started(cwd="/tmp")

        params = mock_client.request.call_args_list[0][0][1]
        assert params["mcpServers"] == []

    def test_end_to_end_translation_to_session_new(self):
        """Translator output -> ACPClientSession -> session/new mcpServers roundtrip."""
        hermes_cfg = {
            "fs-mcp": {"command": "uvx", "args": ["mcp-server-filesystem", "/data"],
                       "env": {"HOME": "/root"}},
        }
        translated = _translate_mcp_servers(hermes_cfg)
        session, mock_client = self._make_acp_session(mcp_servers=translated)
        session.ensure_started(cwd="/tmp")

        params = mock_client.request.call_args_list[0][0][1]
        mcp_list = params["mcpServers"]
        assert len(mcp_list) == 1
        srv = mcp_list[0]
        assert srv["name"] == "fs-mcp"
        assert srv["command"] == "uvx"
        assert srv["args"] == ["mcp-server-filesystem", "/data"]
        assert srv["env"] == [{"name": "HOME", "value": "/root"}]
        assert "type" not in srv  # stdio: no type field


# ---------------------------------------------------------------------------
# Tests: session_meta / opaque _meta passthrough
# ---------------------------------------------------------------------------


class TestSessionMetaPassthrough:
    """session/new ``_meta`` injection via the generic ``session_meta`` param.

    The core must NOT construct vendor-specific ``_meta`` structures (such as
    ``claudeCode.options.settingSources``).  Instead, callers pass an opaque
    dict which is forwarded verbatim as ``params["_meta"]`` only when truthy.
    This keeps the core vendor-agnostic — a coding-tool plugin (or future
    trusted config seam) constructs whatever ``_meta`` the target ACP server
    expects.
    """

    def _make_session(self, **kwargs):
        mock_client = MagicMock()
        mock_client.initialize.return_value = {}
        mock_client.request.return_value = {"sessionId": "sess-meta"}
        mock_client.take_notification.return_value = None
        mock_client.take_server_request.return_value = None
        mock_client.stderr_tail.return_value = []
        session = ACPClientSession(
            command="fake-acp",
            client_factory=lambda **kw: mock_client,
            **kwargs,
        )
        return session, mock_client

    def _session_new_params(self, mock_client):
        """Extract the params dict sent to session/new."""
        for c in mock_client.request.call_args_list:
            if c[0][0] == "session/new":
                return c[0][1]
        raise AssertionError("session/new not called")

    def test_default_no_meta_key(self):
        """Default (session_meta=None) → no ``_meta`` key in session/new params.

        The core must not invent a vendor-specific _meta on its own.
        """
        session, mock_client = self._make_session()
        session.ensure_started(cwd="/tmp")

        params = self._session_new_params(mock_client)
        assert "_meta" not in params

    def test_vendor_meta_forwarded_verbatim(self):
        """An arbitrary vendor _meta dict is forwarded as-is — no restructuring.

        Whatever the caller passes lands verbatim in params["_meta"].  The core
        does not inspect or rewrap it.
        """
        vendor_meta = {
            "claudeCode": {
                "options": {"settingSources": ["project", "local"]},
            }
        }
        session, mock_client = self._make_session(session_meta=vendor_meta)
        session.ensure_started(cwd="/tmp")

        params = self._session_new_params(mock_client)
        assert params["_meta"] == vendor_meta

    def test_different_vendor_meta_forwarded(self):
        """Non-Claude vendor meta is also passed through unchanged."""
        other_meta = {
            "acpOptions": {"region": "eu-west-1", "verbose": True},
        }
        session, mock_client = self._make_session(session_meta=other_meta)
        session.ensure_started(cwd="/tmp")

        params = self._session_new_params(mock_client)
        assert params["_meta"] == other_meta

    def test_empty_dict_omits_meta(self):
        """An empty dict (session_meta={}) → no ``_meta`` key.

        Falsy session_meta is skipped so the server sees raw defaults.
        """
        session, mock_client = self._make_session(session_meta={})
        session.ensure_started(cwd="/tmp")

        params = self._session_new_params(mock_client)
        assert "_meta" not in params

    def test_caller_mutation_after_init_does_not_change_internal_value(self):
        """The session must copy session_meta so later caller mutations are safe.

        If the caller mutates the dict after ACPClientSession.__init__, the
        value sent in session/new must NOT change — otherwise a plugin that
        reuses a shared dict across sessions could corrupt an in-flight one.
        """
        shared = {"acpOptions": {"flag": True}}
        session, mock_client = self._make_session(session_meta=shared)
        # Caller mutates AFTER __init__ but BEFORE ensure_started
        shared["acpOptions"]["flag"] = False
        shared["extra"] = 1
        session.ensure_started(cwd="/tmp")

        params = self._session_new_params(mock_client)
        assert params["_meta"] == {"acpOptions": {"flag": True}}
        assert "extra" not in params.get("_meta", {})

    def test_cwd_and_mcp_servers_still_present_alongside_meta(self):
        """_meta addition does not displace cwd or mcpServers."""
        servers = [{"name": "s", "command": "cmd", "args": [], "env": []}]
        session, mock_client = self._make_session(
            mcp_servers=servers,
            session_meta={"acpOptions": {"x": 1}},
        )
        session.ensure_started(cwd="/work")

        params = self._session_new_params(mock_client)
        assert params["cwd"] == "/work"
        assert params["mcpServers"] == servers
        assert params["_meta"] == {"acpOptions": {"x": 1}}


# ---------------------------------------------------------------------------
# Tests: stderr redaction in _format_error (force=True)
# ---------------------------------------------------------------------------


class TestStderrRedaction:
    """_format_error must redact secrets in stderr output with force=True.

    ACP agents log diagnostics to stderr. If a secret leaks there (e.g. an API
    key echoed in a stack trace), it must NOT appear in user-visible error
    strings. This test uses a FAKE secret pattern that is safe to include.
    """

    @staticmethod
    def _make_session_with_client():
        """Create a session where _client is already wired (ensure_started done)."""
        client_mock = MagicMock()
        client_mock.is_alive.return_value = True
        client_mock.initialize.return_value = {"protocolVersion": 1}
        client_mock.request.return_value = {"sessionId": "s"}
        client_mock.take_notification.return_value = None
        client_mock.take_server_request.return_value = None
        client_mock.stderr_tail.return_value = []
        session = ACPClientSession(
            command="fake-acp",
            client_factory=lambda **kw: client_mock,
        )
        session.ensure_started()
        return session, client_mock

    def test_fake_secret_in_stderr_is_redacted(self):
        """A fake secret in stderr must not appear in the formatted error."""
        session, mock_client = self._make_session_with_client()
        fake_secret = "sk-fake-test-secret-1234567890abcdef"
        mock_client.stderr_tail.return_value = [
            "agent starting...",
            f"error: auth failed with key={fake_secret}",
        ]

        result = session._format_error("ACP agent subprocess exited")

        # The raw secret must NOT appear in the output
        assert fake_secret not in result
        # But the error context should still be there
        assert "ACP agent subprocess exited" in result
        assert "stderr" in result.lower()

    def test_no_secret_stderr_passes_through(self):
        """Normal stderr without secrets is preserved."""
        session, mock_client = self._make_session_with_client()
        mock_client.stderr_tail.return_value = [
            "loading model config",
            "session ready",
        ]

        result = session._format_error("agent crashed")

        assert "agent crashed" in result
        assert "loading model config" in result
        assert "session ready" in result

    def test_no_stderr_returns_prefix_only(self):
        """When there is no stderr, just return the prefix."""
        session, mock_client = self._make_session_with_client()
        mock_client.stderr_tail.return_value = []

        result = session._format_error("agent crashed")

        assert result == "agent crashed"
