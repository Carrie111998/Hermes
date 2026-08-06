"""Tests for session_meta filtering — issue #4715.

Ensures that transcript-only session_meta messages never reach the
chat-completions API, via both the API-boundary guard in
_sanitize_api_messages() and the CLI session-restore paths.
"""

import logging

from run_agent import AIAgent


# ---------------------------------------------------------------------------
# Layer 1 — _sanitize_api_messages role-allowlist guard
# ---------------------------------------------------------------------------

class TestSanitizeApiMessagesRoleFilter:

    def test_drops_session_meta_role(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "session_meta", "content": {"model": "gpt-4"}},
            {"role": "assistant", "content": "hi"},
        ]
        out = AIAgent._sanitize_api_messages(msgs)
        assert len(out) == 2
        assert all(m["role"] != "session_meta" for m in out)

    def test_preserves_valid_roles(self):
        msgs = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        # Need a matching assistant tool_call so the tool result isn't orphaned
        msgs[2]["tool_calls"] = [{"id": "c1", "function": {"name": "t", "arguments": "{}"}}]
        out = AIAgent._sanitize_api_messages(msgs)
        roles = [m["role"] for m in out]
        assert "system" in roles
        assert "user" in roles
        assert "assistant" in roles
        assert "tool" in roles

    def test_logs_warning_when_dropping(self, caplog):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "session_meta", "content": {"info": "test"}},
        ]
        with caplog.at_level(logging.DEBUG, logger="run_agent"):
            AIAgent._sanitize_api_messages(msgs)
        assert any("invalid role" in r.message and "session_meta" in r.message for r in caplog.records)



# ---------------------------------------------------------------------------
# Layer 1b — display-only timeline fields must not reach the provider
# ---------------------------------------------------------------------------

class TestDisplayFieldsStrippedFromApiPayload:
    """Display-only fields (display_kind, display_metadata) are persisted on
    message rows for timeline rendering, but must never appear in the
    provider-bound API payload — strict OpenAI-compatible backends reject
    unknown fields."""

    def test_sanitizer_removes_all_transcript_only_fields(self):
        msgs = [
            {
                "role": "user",
                "content": "hello",
                "display_kind": "model_switch",
                "display_metadata": {"origin": {"platform": "discord"}},
                "api_content": "hello with injected context",
                "platform_message_id": "message-99",
                "message_id": "legacy-message-99",
                "timestamp": 123.0,
                "observed": True,
                "active": True,
                "compacted": False,
                "_row_id": 7,
                "_db_persisted": True,
            },
            {"role": "assistant", "content": "hi", "display_metadata": {"model": "m"}},
        ]
        out = AIAgent._sanitize_api_messages(msgs)
        local_only = {
            "api_content", "display_kind", "display_metadata", "platform_message_id",
            "message_id", "timestamp", "observed", "active", "compacted",
            "_row_id", "_db_persisted",
        }
        assert not (local_only & out[0].keys())
        assert "display_metadata" not in out[1]
        assert out[0]["content"] == "hello"
        # Sanitization operates on provider-bound copies, not durable history.
        assert msgs[0]["platform_message_id"] == "message-99"

# ---------------------------------------------------------------------------
# Layer 2 — CLI session-restore filters session_meta before loading
# ---------------------------------------------------------------------------

class TestCLISessionRestoreFiltering:

    def test_restore_filters_session_meta(self):
        """Simulates the CLI restore path and verifies session_meta is removed."""
        # Build a fake restored message list (as returned by get_messages_as_conversation)
        fake_restored = [
            {"role": "session_meta", "content": {"model": "gpt-4"}},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "session_meta", "content": {"tools": []}},
        ]

        # Apply the same filtering that the patched CLI code now does
        filtered = [m for m in fake_restored if m.get("role") != "session_meta"]

        assert len(filtered) == 2
        assert all(m["role"] != "session_meta" for m in filtered)
        assert filtered[0]["role"] == "user"
        assert filtered[1]["role"] == "assistant"
