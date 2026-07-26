"""Tests for hermes_cli.kanban_enforcement — dispatch-routing enforcement gate.

Validates that the enforcement middleware:
- Blocks substantial worker tools when enforcement enabled and no dispatch
- Allows read-only tools always
- Classifies dual-use tools (cronjob, process) by action parameter
- Fails closed for unknown tools (new core tools must be classified)
- Respects turn boundaries (authorisation expires each turn)
- Is session-scoped (session A route cannot authorize session B)
- Captures kanban_create dispatch decisions via post_tool_call
- Verifies durable task state via DB readback (mocked in unit tests)
- Is a no-op when enforcement disabled (default)
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch, MagicMock

import pytest

from hermes_cli import kanban_enforcement as ke


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_enforcement():
    """Reset enforcement state before and after each test."""
    ke.reset_all_state()
    ke._CONFIG_CACHE.clear()
    ke._CONFIG_LAST_READ = 0.0
    yield
    ke.reset_all_state()
    ke._CONFIG_CACHE.clear()
    ke._CONFIG_LAST_READ = 0.0


@pytest.fixture
def enforcement_on():
    """Enable enforcement for a test."""
    ke.set_enforcement_enabled_for_test(True)
    yield
    ke.set_enforcement_enabled_for_test(False)


def _pre_result(tool_name, args=None, session_id="", **kwargs):
    """Call the pre_tool_call hook and return the result dict or None."""
    return ke._pre_tool_call_enforcement(
        tool_name=tool_name, args=args, session_id=session_id, **kwargs,
    )


# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------


class TestSubstantialWorkerTools:
    """Substantial worker tools are blocked when enforcement enabled."""

    def test_blocked_when_no_dispatch(self, enforcement_on):
        """Substantial tools blocked without dispatch."""
        result = _pre_result("terminal")
        assert result is not None
        assert result["action"] == "block"
        assert "No dispatch route" in result["message"]

    def test_blocked_write_file(self, enforcement_on):
        result = _pre_result("write_file")
        assert result is not None
        assert result["action"] == "block"

    def test_blocked_patch(self, enforcement_on):
        result = _pre_result("patch")
        assert result is not None
        assert result["action"] == "block"

    def test_blocked_execute_code(self, enforcement_on):
        result = _pre_result("execute_code")
        assert result is not None
        assert result["action"] == "block"

    def test_blocked_delegate_task(self, enforcement_on):
        result = _pre_result("delegate_task")
        assert result is not None
        assert result["action"] == "block"


class TestAlwaysAllowedTools:
    """Read-only / framing tools are never blocked."""

    @pytest.mark.parametrize(
        "tool_name",
        [
            "read_file",
            "search_files",
            "web_search",
            "vision_analyze",
            "clarify",
            "skills_list",
            "skill_view",
            "session_search",
            "kanban_show",
            "kanban_list",
            "kanban_comment",
            "kanban_link",
            "kanban_block",
            "kanban_unblock",
            "kanban_complete",
            "kanban_heartbeat",
        ],
    )
    def test_always_allowed_when_enforcement_enabled(
        self, enforcement_on, tool_name
    ):
        """Read-only tools pass even when enforcement is enabled."""
        result = _pre_result(tool_name)
        assert result is None, f"{tool_name} should be allowed"

    @pytest.mark.parametrize(
        "tool_name",
        ["terminal", "write_file", "patch", "execute_code", "delegate_task"],
    )
    def test_substantial_allowed_when_enforcement_disabled(self, tool_name):
        """Substantial tools pass when enforcement is disabled."""
        result = _pre_result(tool_name)
        assert result is None, f"{tool_name} should be allowed when enforcement off"


class TestDualUseTools:
    """Dual-use tools are classified by their action parameter."""

    def test_cronjob_list_allowed(self, enforcement_on):
        """cronjob action='list' is read-only — allowed."""
        result = _pre_result("cronjob", args={"action": "list"})
        assert result is None, "cronjob list should be allowed"

    def test_cronjob_create_blocked(self, enforcement_on):
        """cronjob action='create' is stateful — blocked."""
        result = _pre_result("cronjob", args={"action": "create", "schedule": "30m", "prompt": "test"})
        assert result is not None
        assert result["action"] == "block"

    def test_cronjob_update_blocked(self, enforcement_on):
        """cronjob action='update' is stateful — blocked."""
        result = _pre_result("cronjob", args={"action": "update", "job_id": "x"})
        assert result is not None
        assert result["action"] == "block"

    def test_cronjob_remove_blocked(self, enforcement_on):
        """cronjob action='remove' is stateful — blocked."""
        result = _pre_result("cronjob", args={"action": "remove", "job_id": "x"})
        assert result is not None
        assert result["action"] == "block"

    def test_cronjob_no_action_fail_closed(self, enforcement_on):
        """cronjob without action param is fail-closed."""
        result = _pre_result("cronjob", args={"schedule": "30m"})
        assert result is not None
        assert result["action"] == "block"

    def test_cronjob_unknown_action_fail_closed(self, enforcement_on):
        """cronjob with unknown action is fail-closed."""
        result = _pre_result("cronjob", args={"action": "foobar"})
        assert result is not None
        assert result["action"] == "block"

    def test_process_list_allowed(self, enforcement_on):
        """process action='list' is read-only — allowed."""
        result = _pre_result("process", args={"action": "list"})
        assert result is None, "process list should be allowed"

    def test_process_kill_blocked(self, enforcement_on):
        """process action='kill' is stateful — blocked."""
        result = _pre_result("process", args={"action": "kill", "session_id": "x"})
        assert result is not None
        assert result["action"] == "block"

    def test_process_write_blocked(self, enforcement_on):
        """process action='write' is stateful — blocked."""
        result = _pre_result("process", args={"action": "write", "session_id": "x", "data": "hi"})
        assert result is not None
        assert result["action"] == "block"


class TestUnknownTools:
    """Unknown tools fail closed when enforcement is enabled."""

    def test_unknown_tool_blocked(self, enforcement_on):
        """Future/additional tools not classified are blocked (fail-closed)."""
        result = _pre_result("some_future_tool_v7")
        assert result is not None
        assert result["action"] == "block"

    def test_unknown_tool_allowed_when_disabled(self):
        """Unknown tools pass when enforcement is disabled."""
        result = _pre_result("some_future_tool_v7")
        assert result is None


# ---------------------------------------------------------------------------
# Dispatch state management (session-scoped)
# ---------------------------------------------------------------------------


class TestDispatchState:
    """_DispatchState records and resets correctly."""

    def test_initial_not_established(self):
        state = ke._DispatchState()
        assert not state.is_established(0)

    def test_record_route(self):
        state = ke._DispatchState()
        state.record_route("t_abc", "worker-terra", "deepseek-v4-flash", "new-api", 1)
        assert state.is_established(1)
        assert state.route_task_id == "t_abc"
        assert state.route_assignee == "worker-terra"
        assert state.route_model == "deepseek-v4-flash"
        assert state.route_provider == "new-api"
        assert state.exemption_keyword is None
        assert state.turn_ordinal == 1

    def test_record_exemption(self):
        state = ke._DispatchState()
        state.record_exemption("tiny", 1)
        assert state.is_established(1)
        assert state.route_task_id is None
        assert state.exemption_keyword == "tiny"
        assert state.turn_ordinal == 1

    def test_reset(self):
        state = ke._DispatchState()
        state.record_route("t_abc", "worker-terra", "deepseek-v4-flash", "new-api", 1)
        state.reset()
        assert not state.is_established(0)
        assert state.route_task_id is None
        assert state.exemption_keyword is None
        assert state.turn_ordinal == -1

    def test_exemption_overrides_route(self):
        """Recording an exemption after a route replaces it."""
        state = ke._DispatchState()
        state.record_route("t_abc", "worker-terra", "deepseek-v4-flash", "new-api", 1)
        state.record_exemption("controller_judgment", 2)
        assert state.is_established(2)
        assert state.route_task_id is None
        assert state.exemption_keyword == "controller_judgment"

    def test_stale_turn_not_established(self):
        """State from turn 1 is not established at turn 2."""
        state = ke._DispatchState()
        state.record_route("t_abc", "worker-terra", "deepseek-v4-flash", "new-api", 1)
        assert not state.is_established(2)


class TestSessionScoping:
    """Dispatch state is scoped to session_id."""

    def test_session_a_route_not_visible_to_session_b(self, enforcement_on):
        """Route established in session A does not authorize session B."""
        ke.advance_turn_for("session_a")
        state_a, _ = ke._get_or_create_state("session_a")
        state_a.record_route("t_a", "worker-terra", "deepseek-v4-flash", "new-api", 0)

        # session_a should be authorized
        result_a = _pre_result("terminal", session_id="session_a")
        assert result_a is None, "session A should be authorized"

        # session_b should be blocked
        result_b = _pre_result("terminal", session_id="session_b")
        assert result_b is not None
        assert result_b["action"] == "block", "session B should NOT be authorized"

    def test_session_reset_does_not_affect_other_session(self, enforcement_on):
        """Cleaning up session A should not affect session B."""
        ke.advance_turn_for("session_a")
        state_a, _ = ke._get_or_create_state("session_a")
        state_a.record_route("t_a", "worker-terra", "deepseek-v4-flash", "new-api", 0)

        ke.advance_turn_for("session_b")
        state_b, _ = ke._get_or_create_state("session_b")
        state_b.record_route("t_b", "worker-terra", "deepseek-v4-flash", "new-api", 0)

        # Clean up session A
        ke.cleanup_session("session_a")

        # session B should still be authorized
        result_b = _pre_result("terminal", session_id="session_b")
        assert result_b is None, "session B should remain authorized"

        # session A state should be gone
        ke.advance_turn_for("session_a")
        result_a = _pre_result("terminal", session_id="session_a")
        assert result_a is not None
        assert result_a["action"] == "block", "session A state should be cleaned up"

    def test_unsessioned_calls_are_isolated(self, enforcement_on):
        """Calls without session_id use a sentinel key that doesn't leak."""
        # Establish route for unsessioned
        ke.advance_turn()
        ke._get_state().record_route("t_x", "worker-terra", "deepseek-v4-flash", "new-api", 0)

        # Unsatisfied calls use empty session_id → gets the same sentinel
        result_anon = _pre_result("terminal")
        assert result_anon is None, "unsessioned should be authorized"

        # Session "a" should not be authorized
        result_a = _pre_result("terminal", session_id="a")
        assert result_a is not None
        assert result_a["action"] == "block"


# ---------------------------------------------------------------------------
# Post-tool-call enforcement (with mocked DB readback)
# ---------------------------------------------------------------------------


class TestPostToolCallEnforcement:
    """post_tool_call hook captures kanban_create dispatch decisions."""

    def test_captures_route_decision(self, enforcement_on):
        """Successful kanban_create with route decision records state."""
        args = {
            "dispatch_decision": {
                "route": "worker-terra",
                "model": "deepseek-v4-flash",
                "provider": "new-api",
            },
            "assignee": "worker-terra",
        }
        result = json.dumps({"success": True, "task_id": "t_test01"})

        with patch.object(ke, "_verify_dispatch_decision_from_db", return_value=True):
            ke._post_tool_call_enforcement(
                tool_name="kanban_create",
                args=args,
                result=result,
                status="ok",
            )

        assert ke.dispatch_enforcement_is_established()
        summary = ke.dispatch_enforcement_summary()
        assert summary["route_task_id"] == "t_test01"
        assert summary["route_assignee"] == "worker-terra"
        assert summary["route_model"] == "deepseek-v4-flash"
        assert summary["route_provider"] == "new-api"

    def test_captures_exemption_decision(self, enforcement_on):
        """Successful kanban_create with exemption records state."""
        args = {
            "dispatch_decision": {"exemption": "tiny"},
        }
        result = json.dumps({"success": True, "task_id": "t_test02"})

        with patch.object(ke, "_verify_dispatch_decision_from_db", return_value=True):
            ke._post_tool_call_enforcement(
                tool_name="kanban_create",
                args=args,
                result=result,
                status="ok",
            )

        assert ke.dispatch_enforcement_is_established()
        summary = ke.dispatch_enforcement_summary()
        assert summary["exemption_keyword"] == "tiny"

    def test_ignores_failed_kanban_create(self, enforcement_on):
        """Failed kanban_create does not record dispatch."""
        args = {
            "dispatch_decision": {
                "route": "worker-terra",
                "model": "deepseek-v4-flash",
                "provider": "new-api",
            },
        }
        result = json.dumps({"success": False, "error": "something went wrong"})
        ke._post_tool_call_enforcement(
            tool_name="kanban_create", args=args, result=result, status="error",
        )
        assert not ke.dispatch_enforcement_is_established()

    def test_ignores_blocked_kanban_create(self, enforcement_on):
        """Blocked kanban_create does not record dispatch."""
        args = {
            "dispatch_decision": {
                "route": "worker-terra",
                "model": "deepseek-v4-flash",
                "provider": "new-api",
            },
        }
        ke._post_tool_call_enforcement(
            tool_name="kanban_create", args=args, result="{}", status="blocked",
        )
        assert not ke.dispatch_enforcement_is_established()

    def test_ignores_non_kanban_create(self, enforcement_on):
        """post_tool_call hook ignores non-kanban_create tools."""
        args = {"command": "echo hello"}
        result = json.dumps({"output": "hello", "exit_code": 0})
        ke._post_tool_call_enforcement(
            tool_name="terminal", args=args, result=result, status="ok",
        )
        assert not ke.dispatch_enforcement_is_established()

    def test_ignores_when_enforcement_disabled(self):
        """post_tool_call hook is a no-op when enforcement is off."""
        args = {
            "dispatch_decision": {
                "route": "worker-terra",
                "model": "deepseek-v4-flash",
                "provider": "new-api",
            },
        }
        result = json.dumps({"success": True, "task_id": "t_test03"})
        ke._post_tool_call_enforcement(
            tool_name="kanban_create", args=args, result=result, status="ok",
        )
        assert not ke.dispatch_enforcement_is_established()

    def test_db_readback_failure_does_not_authorize(self, enforcement_on):
        """When DB readback fails, dispatch is NOT authorized."""
        args = {
            "dispatch_decision": {
                "route": "worker-terra",
                "model": "deepseek-v4-flash",
                "provider": "new-api",
            },
            "assignee": "worker-terra",
        }
        result = json.dumps({"success": True, "task_id": "t_bad"})

        # DB readback returns False — forged args or mismatched task
        with patch.object(ke, "_verify_dispatch_decision_from_db", return_value=False):
            ke._post_tool_call_enforcement(
                tool_name="kanban_create", args=args, result=result, status="ok",
            )

        assert not ke.dispatch_enforcement_is_established(), (
            "dispatch should NOT be authorized when DB readback fails"
        )


# ---------------------------------------------------------------------------
# Turn boundaries
# ---------------------------------------------------------------------------


class TestTurnBoundaries:
    """Dispatch authorisation expires at turn boundaries."""

    def test_authorisation_expires_on_advance(self, enforcement_on):
        """After advance_turn, previous authorisation is cleared."""
        state = ke._get_state()
        state.record_route("t_abc", "worker-terra", "deepseek-v4-flash", "new-api", 0)
        assert ke.dispatch_enforcement_is_established()

        ke.advance_turn()
        assert not ke.dispatch_enforcement_is_established()
        assert ke._current_turn() == 1

    def test_substantial_tool_blocked_after_turn_advance(self, enforcement_on):
        """After turn advance, substantial tools are blocked again."""
        state = ke._get_state()
        state.record_route("t_abc", "worker-terra", "deepseek-v4-flash", "new-api", 0)
        ke.advance_turn()

        result = _pre_result("terminal")
        assert result is not None
        assert result["action"] == "block"
        assert "expire" in result["message"].lower() or "turn" in result["message"]

    def test_same_turn_authorisation_persists(self, enforcement_on):
        """Authorisation from earlier in the same turn persists."""
        state = ke._get_state()
        state.record_route("t_abc", "worker-terra", "deepseek-v4-flash", "new-api", 0)
        result = _pre_result("terminal")
        assert result is None

    def test_reset_enforcement_state(self, enforcement_on):
        """reset_all_state clears all sessions."""
        state_a, _ = ke._get_or_create_state("a")
        state_a.record_route("t_a", "worker-terra", "deepseek-v4-flash", "new-api", 0)

        state_b, _ = ke._get_or_create_state("b")
        state_b.record_exemption("tiny", 0)

        assert ke.dispatch_enforcement_is_established("a")
        assert ke.dispatch_enforcement_is_established("b")

        ke.reset_all_state()
        assert not ke.dispatch_enforcement_is_established("a")
        assert not ke.dispatch_enforcement_is_established("b")


class TestPostLlmCallEnforcement:
    """post_llm_call hook advances turn on text responses."""

    def test_advances_on_text_response(self, enforcement_on):
        """Text response from model advances the turn."""
        initial_turn = ke._current_turn()
        ke._post_llm_call_enforcement(response_text="Here is the result.", tool_calls_count=0)
        assert ke._current_turn() == initial_turn + 1

    def test_does_not_advance_on_tool_calls(self, enforcement_on):
        """Tool call responses do not advance the turn."""
        initial_turn = ke._current_turn()
        ke._post_llm_call_enforcement(response_text="", tool_calls_count=3)
        assert ke._current_turn() == initial_turn

    def test_noop_when_disabled(self):
        """post_llm_call is a no-op when enforcement off."""
        initial_turn = ke._current_turn()
        ke._post_llm_call_enforcement(response_text="Here is the result.", tool_calls_count=0)
        assert ke._current_turn() == initial_turn


class TestConfigGate:
    """Enforcement is gated behind config flag."""

    def test_enforcement_on(self, enforcement_on):
        """When enabled, enforcement blocks substantial tools."""
        assert ke._is_enforcement_enabled()
        result = _pre_result("terminal")
        assert result is not None
        assert result["action"] == "block"

    def test_enforcement_off(self):
        """When disabled (default), enforcement is a no-op."""
        assert not ke._is_enforcement_enabled()
        result = _pre_result("terminal")
        assert result is None


class TestEnforcementSummary:
    """dispatch_enforcement_summary returns correct state."""

    def test_summary_when_empty(self, enforcement_on):
        summary = ke.dispatch_enforcement_summary()
        assert summary["established"] is False
        assert summary["route_task_id"] is None
        assert summary["exemption_keyword"] is None
        assert summary["enforcement_enabled"] is True

    def test_summary_when_route_established(self, enforcement_on):
        state = ke._get_state()
        state.record_route("t_xyz", "worker-terra", "deepseek-v4-flash", "new-api", 0)
        summary = ke.dispatch_enforcement_summary()
        assert summary["established"] is True
        assert summary["route_task_id"] == "t_xyz"
        assert summary["route_assignee"] == "worker-terra"

    def test_summary_when_exempted(self, enforcement_on):
        state = ke._get_state()
        state.record_exemption("security_critical", 0)
        summary = ke.dispatch_enforcement_summary()
        assert summary["established"] is True
        assert summary["exemption_keyword"] == "security_critical"
