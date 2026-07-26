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
- (FD-016) Verifies dispatch_routed/dispatch_exempted events with real DB
- (FD-017) Passes board parameter through to DB readback
"""

from __future__ import annotations

import json
import time
from pathlib import Path
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
        ke.advance_turn_for("a")
        state_a, turn_a = ke._get_or_create_state("a")
        state_a.record_route("t_a", "worker-terra", "deepseek-v4-flash", "new-api", turn_a)

        ke.advance_turn_for("b")
        state_b, turn_b = ke._get_or_create_state("b")
        state_b.record_exemption("tiny", turn_b)

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


# ---------------------------------------------------------------------------
# FD-016 / FD-017: Durable event verification with real DB
# ---------------------------------------------------------------------------


def _insert_task_row(conn, task_id, assignee="worker-terra",
                     model_override=None, provider_override=None):
    import time
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO tasks (id, title, assignee, status, created_by,"
        " workspace_kind, created_at, model_override, provider_override)"
        " VALUES (?, ?, ?, 'ready', 'test', 'scratch', ?, ?, ?)",
        (task_id, "Test " + task_id, assignee, now,
         model_override, provider_override),
    )
    conn.commit()


def _insert_event(conn, task_id, kind, payload):
    import time
    now = int(time.time())
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (task_id, kind, json.dumps(payload), now),
    )
    conn.commit()


class TestDurableEventVerification:
    """FD-016 / FD-017: Durable event verification with real SQLite DB."""

    def _db(self):
        import sqlite3
        from hermes_cli import kanban_db as kdb
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(kdb.SCHEMA_SQL)
        conn.commit()
        return conn

    def test_route_verifies_dispatch_routed(self, enforcement_on):
        conn = self._db()
        _insert_task_row(conn, "t_r01", "worker-terra",
                         model_override="deepseek-v4-flash",
                         provider_override="new-api")
        _insert_event(conn, "t_r01", "dispatch_routed",
                      {"route": "worker-terra", "model": "deepseek-v4-flash",
                       "provider": "new-api"})
        with patch("hermes_cli.kanban_db.connect", return_value=conn):
            assert ke._verify_dispatch_decision_from_db(
                "t_r01", "worker-terra", "deepseek-v4-flash", "new-api", None,
            ) is True
        conn.close()

    def test_route_missing_event_fails(self, enforcement_on):
        conn = self._db()
        _insert_task_row(conn, "t_r02", "worker-terra",
                         model_override="deepseek-v4-flash",
                         provider_override="new-api")
        with patch("hermes_cli.kanban_db.connect", return_value=conn):
            assert ke._verify_dispatch_decision_from_db(
                "t_r02", "worker-terra", "deepseek-v4-flash", "new-api", None,
            ) is False
        conn.close()

    def test_route_wrong_event_kind_fails(self, enforcement_on):
        conn = self._db()
        _insert_task_row(conn, "t_r03", "worker-terra",
                         model_override="deepseek-v4-flash",
                         provider_override="new-api")
        _insert_event(conn, "t_r03", "dispatch_exempted", {"exemption": "tiny"})
        with patch("hermes_cli.kanban_db.connect", return_value=conn):
            assert ke._verify_dispatch_decision_from_db(
                "t_r03", "worker-terra", "deepseek-v4-flash", "new-api", None,
            ) is False
        conn.close()

    def test_exemption_verifies_dispatch_exempted(self, enforcement_on):
        conn = self._db()
        _insert_task_row(conn, "t_e01", "worker-terra")
        _insert_event(conn, "t_e01", "dispatch_exempted", {"exemption": "tiny"})
        with patch("hermes_cli.kanban_db.connect", return_value=conn):
            assert ke._verify_dispatch_decision_from_db(
                "t_e01", None, None, None, "tiny",
            ) is True
        conn.close()

    def test_exemption_wrong_event_kind_fails(self, enforcement_on):
        conn = self._db()
        _insert_task_row(conn, "t_e02", "worker-terra")
        _insert_event(conn, "t_e02", "dispatch_routed",
                      {"route": "worker-terra", "model": "deepseek-v4-flash",
                       "provider": "new-api"})
        with patch("hermes_cli.kanban_db.connect", return_value=conn):
            assert ke._verify_dispatch_decision_from_db(
                "t_e02", None, None, None, "tiny",
            ) is False
        conn.close()

    def test_exemption_keyword_mismatch_fails(self, enforcement_on):
        conn = self._db()
        _insert_task_row(conn, "t_e03", "worker-terra")
        _insert_event(conn, "t_e03", "dispatch_exempted",
                      {"exemption": "controller_judgment"})
        with patch("hermes_cli.kanban_db.connect", return_value=conn):
            assert ke._verify_dispatch_decision_from_db(
                "t_e03", None, None, None, "tiny",
            ) is False
        conn.close()

    def test_exemption_missing_event_fails(self, enforcement_on):
        conn = self._db()
        _insert_task_row(conn, "t_e04", "worker-terra")
        with patch("hermes_cli.kanban_db.connect", return_value=conn):
            assert ke._verify_dispatch_decision_from_db(
                "t_e04", None, None, None, "tiny",
            ) is False
        conn.close()

    def test_board_passed_to_connect(self, enforcement_on):
        conn = self._db()
        _insert_task_row(conn, "t_board01", "worker-terra",
                         model_override="deepseek-v4-flash",
                         provider_override="new-api")
        _insert_event(conn, "t_board01", "dispatch_routed",
                      {"route": "worker-terra", "model": "deepseek-v4-flash",
                       "provider": "new-api"})
        with patch("hermes_cli.kanban_db.connect",
                   return_value=conn) as mock_connect:
            assert ke._verify_dispatch_decision_from_db(
                "t_board01", "worker-terra", "deepseek-v4-flash", "new-api", None,
                board="my-custom-board",
            ) is True
            mock_connect.assert_called_once_with(board="my-custom-board")
        conn.close()

    def test_wrong_board_task_not_found(self, enforcement_on):
        import sqlite3
        from hermes_cli import kanban_db as kdb
        conn_a = sqlite3.connect(":memory:")
        conn_a.row_factory = sqlite3.Row
        conn_a.executescript(kdb.SCHEMA_SQL)
        conn_a.commit()
        _insert_task_row(conn_a, "t_cross", "worker-terra",
                         model_override="deepseek-v4-flash",
                         provider_override="new-api")
        _insert_event(conn_a, "t_cross", "dispatch_routed",
                      {"route": "worker-terra", "model": "deepseek-v4-flash",
                       "provider": "new-api"})
        conn_b = sqlite3.connect(":memory:")
        conn_b.row_factory = sqlite3.Row
        conn_b.executescript(kdb.SCHEMA_SQL)
        conn_b.commit()

        def _fake_connect(board=None):
            if board == "board-a":
                return conn_a
            return conn_b

        with patch("hermes_cli.kanban_db.connect",
                   side_effect=_fake_connect):
            assert ke._verify_dispatch_decision_from_db(
                "t_cross", "worker-terra", "deepseek-v4-flash", "new-api", None,
                board="board-b",
            ) is False
        conn_a.close()
        conn_b.close()

    def test_correct_board_finds_task(self, enforcement_on):
        conn = self._db()
        _insert_task_row(conn, "t_board_ok", "worker-terra",
                         model_override="deepseek-v4-flash",
                         provider_override="new-api")
        _insert_event(conn, "t_board_ok", "dispatch_routed",
                      {"route": "worker-terra", "model": "deepseek-v4-flash",
                       "provider": "new-api"})
        with patch("hermes_cli.kanban_db.connect",
                   return_value=conn) as mock_connect:
            assert ke._verify_dispatch_decision_from_db(
                "t_board_ok", "worker-terra", "deepseek-v4-flash", "new-api", None,
                board="correct-board",
            ) is True
            mock_connect.assert_called_once_with(board="correct-board")
        conn.close()


# ---------------------------------------------------------------------------
# FD-018: Hook registration via PluginContext
# ---------------------------------------------------------------------------


class TestHookRegistration:
    """register_enforcement_hooks via PluginContext registers all 5 hooks."""

    def test_registers_all_hooks(self):
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        pm = PluginManager()
        m = PluginManifest(name="kanban-enforcement", version="1.0.0",
                           source="bundled", kind="standalone")
        ctx = PluginContext(manifest=m, manager=pm)
        ke.register_enforcement_hooks(ctx)
        for hook_name in ("pre_tool_call", "post_tool_call", "post_llm_call",
                          "on_session_start", "on_session_end"):
            assert hook_name in pm._hooks, f"{hook_name} should be registered"

    def test_registration_idempotent(self):
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        pm = PluginManager()
        m = PluginManifest(name="kanban-enforcement", version="1.0.0",
                           source="bundled", kind="standalone")
        ctx = PluginContext(manifest=m, manager=pm)
        ke.register_enforcement_hooks(ctx)
        pre = dict(pm._hooks)
        ke.register_enforcement_hooks(ctx)
        assert dict(pm._hooks) == pre, "no double-registration"

    def test_session_start_cleanup(self, enforcement_on):
        ke.advance_turn_for("test_sesh")
        s, _ = ke._get_or_create_state("test_sesh")
        s.record_route("t_stale", "worker-terra", "deepseek-v4-flash", "new-api", 0)
        assert ke.dispatch_enforcement_is_established("test_sesh")
        ke._on_session_start_enforcement(session_id="test_sesh")
        assert not ke.dispatch_enforcement_is_established("test_sesh")

    def test_session_end_cleanup(self, enforcement_on):
        ke.advance_turn_for("test_endsesh")
        s, _ = ke._get_or_create_state("test_endsesh")
        s.record_route("t_end", "worker-terra", "deepseek-v4-flash", "new-api", 0)
        assert ke.dispatch_enforcement_is_established("test_endsesh")
        ke._on_session_end_enforcement(session_id="test_endsesh")
        assert not ke.dispatch_enforcement_is_established("test_endsesh")

# ---------------------------------------------------------------------------
# FD-018: Real plugin discovery + invoke_hook integration
# ---------------------------------------------------------------------------


class TestPluginIntegration:
    """Real plugin discovery, hook invocation, and dual opt-in enforcement."""

    def _setup_temp_hermes_home(self, tmp_path, monkeypatch, *, enabled=True):
        """Create a temp HERMES_HOME with enforced config and bundled plugin."""
        import yaml

        home = tmp_path / ".hermes"
        home.mkdir()
        plugins_dir = home / "plugins"
        plugins_dir.mkdir()

        config = {
            "kanban": {"enforce_dispatch_routing": True},
            "plugins": {"enabled": ["kanban-enforcement"] if enabled else []},
        }
        (home / "config.yaml").write_text(yaml.dump(config))

        monkeypatch.setenv("HERMES_HOME", str(home))
        # Point to the repo's bundled plugins so kanban-enforcement is found.
        repo_plugins = (
            Path(__file__).resolve().parent.parent.parent / "plugins"
        )
        monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(repo_plugins))
        # Force fresh discovery in tests.
        from hermes_cli.plugins import get_plugin_manager

        pm = get_plugin_manager()
        pm._discovered = False
        pm._plugins.clear()
        pm._hooks.clear()
        return home

    def test_plugin_enabled_blocks_substantial(self, tmp_path, monkeypatch):
        """With plugins.enabled=['kanban-enforcement'] and enforce_dispatch_routing=true,
        discover_plugins + invoke_hook returns a block for terminal."""
        self._setup_temp_hermes_home(tmp_path, monkeypatch, enabled=True)
        from hermes_cli.plugins import discover_plugins, invoke_hook

        discover_plugins()
        results = invoke_hook("pre_tool_call", tool_name="terminal")
        assert len(results) == 1, f"expected 1 block result, got {len(results)}"
        assert results[0]["action"] == "block"
        assert "BLOCKED" in results[0]["message"]

    def test_plugin_disabled_no_block(self, tmp_path, monkeypatch):
        """With plugins.enabled=[] (empty), discover_plugins does not load the
        enforcement plugin, so invoke_hook returns no results."""
        self._setup_temp_hermes_home(tmp_path, monkeypatch, enabled=False)
        from hermes_cli.plugins import discover_plugins, invoke_hook

        discover_plugins()
        results = invoke_hook("pre_tool_call", tool_name="terminal")
        assert results == [], "no plugin loaded -> no block"

    def test_config_disabled_no_block(self, tmp_path, monkeypatch):
        """With plugins.enabled but enforce_dispatch_routing=false in config,
        the hook callback runs but returns None (no block)."""
        import yaml

        home = tmp_path / ".hermes"
        home.mkdir()
        home.joinpath("plugins").mkdir()
        config = {
            "kanban": {"enforce_dispatch_routing": False},
            "plugins": {"enabled": ["kanban-enforcement"]},
        }
        (home / "config.yaml").write_text(yaml.dump(config))

        monkeypatch.setenv("HERMES_HOME", str(home))
        repo_plugins = (
            Path(__file__).resolve().parent.parent.parent / "plugins"
        )
        monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(repo_plugins))
        from hermes_cli.plugins import get_plugin_manager

        pm = get_plugin_manager()
        pm._discovered = False
        pm._plugins.clear()
        pm._hooks.clear()

        from hermes_cli.plugins import discover_plugins, invoke_hook

        discover_plugins()
        results = invoke_hook("pre_tool_call", tool_name="terminal")
        assert results == [], "config disabled -> no block"

    def test_allowed_tool_not_blocked(self, tmp_path, monkeypatch):
        """With enforcement active, read-only tools are still allowed."""
        self._setup_temp_hermes_home(tmp_path, monkeypatch, enabled=True)
        from hermes_cli.plugins import discover_plugins, invoke_hook

        discover_plugins()
        results = invoke_hook("pre_tool_call", tool_name="read_file")
        assert results == [], "read_file is always allowed"

    def test_on_session_hooks_registered(self, tmp_path, monkeypatch):
        """After discovery, on_session_start/end hooks are registered."""
        self._setup_temp_hermes_home(tmp_path, monkeypatch, enabled=True)
        from hermes_cli.plugins import discover_plugins, has_hook

        discover_plugins()
        assert has_hook("pre_tool_call"), "pre_tool_call should be registered"
        assert has_hook("post_tool_call"), "post_tool_call should be registered"
        assert has_hook("post_llm_call"), "post_llm_call should be registered"
        assert has_hook("on_session_start"), "on_session_start should be registered"
        assert has_hook("on_session_end"), "on_session_end should be registered"
