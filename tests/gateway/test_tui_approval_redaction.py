"""Regression test for TUI approval-prompt credential redaction (#48456).

Follow-up to #50767, which redacted the chat-platform and SSE/API approval
transports. The TUI JSON-RPC transport is the third egress: three
`register_gateway_notify` callbacks in `tui_gateway/server.py` emit the raw
`approval_data` (with an unredacted `command`) to the TUI client. They route
through `_emit_approval_request` → `_approval_request_payload`, which redacts
`payload["command"]` via `agent.redact.redact_sensitive_text(force=True)`
before emitting.

Importing `gateway.run` from this path is deliberately avoided: a long-lived
dashboard/TUI process can already have a stale `agent.turn_context` in
`sys.modules`, and `gateway.run`'s import chain then raises ImportError.
Approval notify treats that as hard-block ("Failed to send approval request").
"""

import inspect

import pytest


class TestTuiApprovalEmitRedaction:
    def test_emit_approval_request_redacts_command_in_payload(self, monkeypatch):
        from tui_gateway import server as tui_server

        emitted = {}
        monkeypatch.setattr(
            tui_server,
            "_emit",
            lambda event, sid, payload=None: emitted.update(
                {"event": event, "sid": sid, "payload": payload}
            ),
        )
        raw = (
            "curl -H 'Authorization: token "
            "ghp_01...uvwx' https://api.github.com"
        )
        tui_server._emit_approval_request(
            "sess-1", {"command": raw, "description": "x"}
        )

        assert emitted["event"] == "approval.request"
        cmd = emitted["payload"]["command"]
        assert "ghp_01...uvwx" not in cmd
        assert emitted["payload"]["description"] == "x"
        assert "github.com" in cmd

    @pytest.mark.parametrize(
        ("allow_session", "allow_permanent", "expected"),
        [
            (True, True, ["once", "session", "always", "deny"]),
            (True, False, ["once", "session", "deny"]),
            (False, False, ["once", "deny"]),
        ],
    )
    def test_emit_approval_request_honors_allowed_scopes(
        self, monkeypatch, allow_session, allow_permanent, expected
    ):
        from tui_gateway import server as tui_server

        emitted = {}
        monkeypatch.setattr(
            tui_server,
            "_emit",
            lambda event, sid, payload=None: emitted.update({"payload": payload}),
        )

        tui_server._emit_approval_request(
            "sess-1",
            {
                "allow_permanent": allow_permanent,
                "allow_session": allow_session,
                "command": "<write to AGENTS.md>",
            },
        )

        assert emitted["payload"]["choices"] == expected

    def test_approval_payload_source_avoids_gateway_run_import(self):
        """TUI payload builder must not import gateway.run (stale-process ImportError)."""
        from tui_gateway import server as tui_server

        source = inspect.getsource(tui_server._approval_request_payload)
        assert "from gateway.run import" not in source
        assert "agent.redact" in source
        assert "redact_sensitive_text" in source
