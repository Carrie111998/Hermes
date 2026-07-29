"""Regression test for TUI approval-prompt credential redaction (#48456).

Follow-up to #50767, which redacted the chat-platform and SSE/API approval
transports. The TUI JSON-RPC transport is the third egress: three
`register_gateway_notify` callbacks in `tui_gateway/server.py` emit the raw
`approval_data` (with an unredacted `command`) to the TUI client. They now
route through the module-level `_emit_approval_request` helper, which redacts
`payload["command"]` via the shared `gateway.run._redact_approval_command` seam
before emitting.
"""

import inspect

import pytest


class TestTuiApprovalEmitRedaction:
    def test_emit_approval_request_redacts_command_in_payload(self, monkeypatch):
        from tui_gateway import server as tui_server

        emitted = {}
        monkeypatch.setattr(
            tui_server, "_emit",
            lambda event, sid, payload=None: emitted.update(
                {"event": event, "sid": sid, "payload": payload}
            ),
        )
        raw = "curl -H 'Authorization: token ghp_01...6789' https://api.github.com"
        tui_server._emit_approval_request("sess-1", {"command": raw, "description": "x"})

        assert emitted["event"] == "approval.request"
        # credential removed, non-command field + command structure preserved
        assert "ghp_01...6789" not in emitted["payload"]["command"]
        assert emitted["payload"]["description"] == "x"
        assert "github.com" in emitted["payload"]["command"]

    def test_cross_surface_bridge_receives_only_redacted_payload_and_session_owner(
        self, monkeypatch
    ):
        from tui_gateway import server as tui_server

        published = {}
        monkeypatch.setattr(tui_server, "_emit", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "gateway.cross_surface_bridge.publish_approval",
            lambda session_key, payload: published.update(
                {"session_key": session_key, "payload": dict(payload)}
            ),
        )
        with tui_server._sessions_lock:
            tui_server._sessions["ui-sid"] = {
                "session_key": "desktop-session-owner",
                "source": "desktop",
            }
        try:
            raw = "curl -H 'Authorization: Bearer secret-value' https://example.test"
            tui_server._emit_approval_request(
                "ui-sid",
                {
                    "command": raw,
                    "description": "token ghp_0123456789abcdefghijklmnopqrstuvwxyzABCD",
                },
            )
        finally:
            with tui_server._sessions_lock:
                tui_server._sessions.pop("ui-sid", None)

        assert published["session_key"] == "desktop-session-owner"
        assert "secret-value" not in published["payload"]["command"]
        assert "example.test" in published["payload"]["command"]
        assert "ghp_0123456789abcdefghijklmnopqrstuvwxyzABCD" not in (
            published["payload"]["description"]
        )

    def test_process_bridge_never_publishes_command_output_or_pattern(self, monkeypatch):
        from tui_gateway import server as tui_server

        published = {}
        monkeypatch.setattr(
            "gateway.cross_surface_bridge.publish_notification",
            lambda text, dedupe_key=None: published.update(
                {"text": text, "dedupe_key": dedupe_key}
            ),
        )
        tui_server._publish_cross_surface_process_notification(
            {
                "type": "watch_match",
                "session_id": "proc-opaque-1",
                "message_id": "event-7",
                "command": "curl -H 'Authorization: secret-value' example.test",
                "pattern": "secret-pattern",
                "output": "secret-output",
            }
        )

        persisted = f"{published['text']} {published['dedupe_key']}"
        assert "secret-value" not in persisted
        assert "secret-pattern" not in persisted
        assert "secret-output" not in persisted
        assert "example.test" not in persisted
        assert published["text"] == (
            "Desktop background process matched a notification watch."
        )

    def test_process_bridge_binds_secondary_profile_home(self, monkeypatch, tmp_path):
        from hermes_constants import get_hermes_home
        from tui_gateway import server as tui_server

        profile_home = tmp_path / "secondary"
        profile_home.mkdir()
        observed = {}

        def capture(_text, dedupe_key=None):
            observed["home"] = get_hermes_home()
            observed["dedupe_key"] = dedupe_key

        monkeypatch.setattr(
            "gateway.cross_surface_bridge.publish_notification", capture
        )
        tui_server._publish_cross_surface_process_notification(
            {"type": "completion", "session_id": "proc-1"},
            profile_home=str(profile_home),
        )

        assert observed["home"] == profile_home

    def test_emit_approval_request_handles_missing_command(self, monkeypatch):
        from tui_gateway import server as tui_server

        emitted = {}
        monkeypatch.setattr(
            tui_server, "_emit",
            lambda event, sid, payload=None: emitted.update({"payload": payload}),
        )
        tui_server._emit_approval_request("s", {"description": "no command here"})
        assert emitted["payload"] == {"description": "no command here"}
        tui_server._emit_approval_request("s", None)
        assert emitted["payload"] == {}

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            ({"smart_denied": True, "allow_permanent": True}, ["once", "deny"]),
            ({"allow_permanent": False}, ["once", "session", "deny"]),
            ({"allow_permanent": True}, ["once", "session", "always", "deny"]),
        ],
    )
    def test_emit_approval_request_derives_choices(self, monkeypatch, data, expected):
        from tui_gateway import server as tui_server

        emitted = {}
        monkeypatch.setattr(
            tui_server,
            "_emit",
            lambda event, sid, payload=None: emitted.update({"payload": payload}),
        )

        tui_server._emit_approval_request("s", data)

        assert emitted["payload"]["choices"] == expected

    def test_no_raw_command_emit_in_approval_registrations(self):
        """Every register_gateway_notify approval callback must route through the
        redacting `_emit_approval_request` helper — no registration may emit the
        raw payload via `_emit("approval.request", ...)` directly. The ONLY
        allowed raw emit is inside the helper itself."""
        from tui_gateway import server as tui_server

        src = inspect.getsource(tui_server)
        raw_emits = src.count('_emit("approval.request"')
        assert raw_emits == 1, (
            f'expected exactly 1 raw _emit("approval.request") (inside the '
            f"redacting helper), found {raw_emits} — a registration may be "
            f"emitting the unredacted command"
        )
        assert "_emit_approval_request(sid, data)" in src, (
            "registration lambdas must route through _emit_approval_request"
        )
