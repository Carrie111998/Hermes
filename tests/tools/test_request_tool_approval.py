"""Tests for tools.approval.request_tool_approval — the plugin pre_tool_call
``{"action": "approve"}`` escalation into the human-approval gate.

These verify that a plugin-driven approval reuses the SAME machinery as a
Tier-2 dangerous-command match: session/permanent allowlist, the CLI prompt,
the gateway submit_pending path, cron_mode, and fail-closed timeouts.
"""

import pytest

import tools.approval as approval
from tools.approval import request_tool_approval


@pytest.fixture(autouse=True)
def _isolate_approval_state(monkeypatch):
    """Give each test a clean session key and empty allowlists."""
    monkeypatch.setattr(
        approval, "get_current_session_key",
        lambda default="default": "test-session",
    )
    # Empty session + permanent approval stores so nothing pre-approves.
    monkeypatch.setattr(approval, "is_approved", lambda sk, pk: False)
    # Not a yolo session (the shared gate checks this first).
    monkeypatch.setattr(approval, "is_current_session_yolo_enabled", lambda: False)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False, raising=False)
    # No thread-registered CLI callback by default.
    monkeypatch.setattr(
        "tools.terminal_tool._get_approval_callback", lambda: None, raising=False
    )
    yield


class TestRequestToolApproval:
    def test_session_cached_approval_short_circuits(self, monkeypatch):
        monkeypatch.setattr(approval, "is_approved", lambda sk, pk: True)
        # Should NOT prompt at all.
        monkeypatch.setattr(
            approval, "prompt_dangerous_approval",
            lambda *a, **k: pytest.fail("should not prompt when already approved"),
        )
        res = request_tool_approval("write_file", "sensitive path", rule_key="ssh")
        assert res == {"approved": True, "message": None}

    def test_cli_approve_once(self, monkeypatch):
        monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
        monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
        monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *a, **k: "once")
        res = request_tool_approval("write_file", "writing ~/.ssh/authorized_keys")
        assert res["approved"] is True

    def test_cli_deny_blocks(self, monkeypatch):
        from hermes_cli import lifecycle

        monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
        monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
        monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *a, **k: "deny")
        events = []
        monkeypatch.setattr(
            lifecycle,
            "invoke_hook",
            lambda hook_name, **kwargs: events.append((hook_name, kwargs)) or [],
        )
        tokens = approval.set_current_observability_context(
            turn_id="turn-1",
            tool_call_id="call-1",
        )
        try:
            res = request_tool_approval("terminal", "curl PUT to external API")
        finally:
            approval.reset_current_observability_context(tokens)
        assert res["approved"] is False
        assert "denied" in res["message"].lower()
        assert res["pattern_key"].startswith("plugin_rule:")
        assert [name for name, _ in events] == [
            "pre_approval_request",
            "post_approval_response",
        ]
        assert all(event["turn_id"] == "turn-1" for _, event in events)
        assert all(event["tool_call_id"] == "call-1" for _, event in events)
        assert events[-1][1]["choice"] == "deny"

    def test_cli_session_persists_session_only(self, monkeypatch):
        monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
        monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
        monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *a, **k: "session")
        calls = {"session": [], "permanent": []}
        monkeypatch.setattr(approval, "approve_session",
                            lambda sk, pk: calls["session"].append(pk))
        monkeypatch.setattr(approval, "approve_permanent",
                            lambda pk: calls["permanent"].append(pk))
        monkeypatch.setattr(approval, "save_permanent_allowlist", lambda x: None)
        res = request_tool_approval("write_file", "reason", rule_key="ssh-writes")
        assert res["approved"] is True
        assert calls["session"] == ["plugin_rule:ssh-writes"]
        assert calls["permanent"] == []  # session != always


    def test_cron_deny_mode_blocks(self, monkeypatch):
        monkeypatch.setattr(approval, "_is_interactive_cli", lambda: False)
        monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
        monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: True)
        monkeypatch.setattr(approval, "_get_cron_approval_mode", lambda: "deny")
        res = request_tool_approval("terminal", "smtp send")
        assert res["approved"] is False
        assert "cron" in res["message"].lower()

    def test_cron_approve_mode_allows(self, monkeypatch):
        monkeypatch.setattr(approval, "_is_interactive_cli", lambda: False)
        monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
        monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: True)
        monkeypatch.setattr(approval, "_get_cron_approval_mode", lambda: "approve")
        res = request_tool_approval("terminal", "smtp send")
        assert res["approved"] is True


    def test_distinct_reasons_get_distinct_keys(self, monkeypatch):
        """Two different reasons on the SAME tool must not share an [a]lways
        allowlist entry (Finding 3: tool_name alone was too coarse)."""
        monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
        monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
        monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *a, **k: "deny")
        k1 = request_tool_approval("write_file", "write to ~/.ssh")["pattern_key"]
        k2 = request_tool_approval("write_file", "send an email")["pattern_key"]
        assert k1 != k2

    def test_explicit_rule_key_overrides_derivation(self, monkeypatch):
        monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
        monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
        monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *a, **k: "deny")
        res = request_tool_approval("terminal", "any", rule_key="my-rule")
        assert res["pattern_key"] == "plugin_rule:my-rule"

    def test_no_human_non_cron_fails_closed(self, monkeypatch):
        """Non-interactive, non-gateway, NON-cron context blocks (fail-closed)
        — a plugin-flagged action never runs ungated without a human."""
        monkeypatch.setattr(approval, "_is_interactive_cli", lambda: False)
        monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
        monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: False)
        res = request_tool_approval("terminal", "smtp send")
        assert res["approved"] is False
        assert "no interactive user or gateway" in res["message"].lower()

    def test_yolo_session_bypasses_gate(self, monkeypatch):
        """A --yolo session skips the plugin approval gate (parity with the
        dangerous-command path, via the shared _run_approval_gate)."""
        monkeypatch.setattr(approval, "is_current_session_yolo_enabled", lambda: True)
        monkeypatch.setattr(
            approval, "prompt_dangerous_approval",
            lambda *a, **k: pytest.fail("yolo must not prompt"),
        )
        res = request_tool_approval("terminal", "curl PUT", rule_key="ext")
        assert res == {"approved": True, "message": None}


class TestOfferedApprovalScopes:
    """A plugin rule may drop [s]ession / [a]lways from the prompt.

    ``rule_key`` already lets a plugin choose the allowlist *grain*, but a key
    that names a single subject — one file, one recipient, one event — has no
    future call a stored answer could correctly apply to. Before this, such a
    rule still rendered an ``always`` button whose only possible effect was to
    widen a decision the human meant to make once.
    """

    def test_cli_prompt_is_told_which_scopes_to_offer(self, monkeypatch):
        seen = {}

        def _prompt(command, description, *a, **kw):
            seen.update(kw)
            return "once"

        monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
        monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
        monkeypatch.setattr(approval, "prompt_dangerous_approval", _prompt)

        res = request_tool_approval(
            "terminal", "delete one record", rule_key="rec:42",
            allow_session=False, allow_permanent=False,
        )
        assert res["approved"] is True
        assert seen["allow_permanent"] is False
        assert seen["allow_session"] is False

    def test_scopes_are_offered_by_default(self, monkeypatch):
        seen = {}

        def _prompt(command, description, *a, **kw):
            seen.update(kw)
            return "once"

        monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
        monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
        monkeypatch.setattr(approval, "prompt_dangerous_approval", _prompt)

        request_tool_approval("terminal", "smtp send")
        assert seen["allow_permanent"] is True
        assert seen["allow_session"] is True

    def test_unoffered_always_is_not_persisted(self, monkeypatch):
        """A scope that was never on screen is a stale client, not consent.

        The action still goes through — a human did answer yes — but nothing
        is written to the permanent allowlist.
        """
        calls = {"session": [], "permanent": []}
        monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
        monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
        monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *a, **k: "always")
        monkeypatch.setattr(
            approval, "approve_session", lambda sk, pk: calls["session"].append(pk)
        )
        monkeypatch.setattr(
            approval, "approve_permanent", lambda pk: calls["permanent"].append(pk)
        )
        monkeypatch.setattr(approval, "save_permanent_allowlist", lambda *a, **k: None)

        res = request_tool_approval(
            "terminal", "delete one record", rule_key="rec:42", allow_permanent=False,
        )
        assert res["approved"] is True
        assert calls["permanent"] == []
        assert calls["session"] == []

    def test_gateway_payload_carries_the_offered_scopes(self, monkeypatch):
        """The gateway surface renders buttons straight from this payload."""
        sent = {}

        monkeypatch.setattr(approval, "_is_interactive_cli", lambda: False)
        monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: True)
        monkeypatch.setitem(approval._gateway_notify_cbs, "test-session", lambda *a, **k: True)
        monkeypatch.setattr(
            approval, "_await_gateway_decision",
            lambda session_key, notify_cb, approval_data, surface="gateway": (
                sent.update(approval_data)
                or {"resolved": True, "choice": "once", "reason": None}
            ),
        )
        res = request_tool_approval(
            "terminal", "delete one record", rule_key="rec:42",
            allow_session=False, allow_permanent=False,
        )
        assert res["approved"] is True
        assert sent["allow_permanent"] is False
        assert sent["allow_session"] is False

    def test_gateway_unoffered_always_is_not_persisted(self, monkeypatch):
        calls = {"permanent": []}
        monkeypatch.setattr(approval, "_is_interactive_cli", lambda: False)
        monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: True)
        monkeypatch.setitem(approval._gateway_notify_cbs, "test-session", lambda *a, **k: True)
        monkeypatch.setattr(
            approval, "_await_gateway_decision",
            lambda *a, **kw: {"resolved": True, "choice": "always", "reason": None},
        )
        monkeypatch.setattr(approval, "approve_session", lambda sk, pk: None)
        monkeypatch.setattr(
            approval, "approve_permanent", lambda pk: calls["permanent"].append(pk)
        )
        monkeypatch.setattr(approval, "save_permanent_allowlist", lambda *a, **k: None)

        res = request_tool_approval(
            "terminal", "delete one record", rule_key="rec:42", allow_permanent=False,
        )
        assert res["approved"] is True
        assert calls["permanent"] == []

    def test_deny_is_never_reinterpreted(self, monkeypatch):
        """Clamping narrows a scope; it must never turn a refusal into a yes."""
        monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
        monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
        monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *a, **k: "deny")
        res = request_tool_approval(
            "terminal", "delete one record", allow_session=False, allow_permanent=False,
        )
        assert res["approved"] is False
