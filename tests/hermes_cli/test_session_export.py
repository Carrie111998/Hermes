import json
import sys

from hermes_cli.session_export import export_record_count, render_sessions_export
from hermes_cli.session_export_html import (
    _generate_messages_html,
    generate_multi_session_html_export,
)


def _sample_session():
    return {
        "id": "sess-123",
        "source": "cli",
        "model": "test/model",
        "title": "Debug auth flow",
        "started_at": 1700000000,
        "message_count": 5,
        "messages": [
            {
                "id": 1,
                "role": "system",
                "content": "hidden system context",
                "timestamp": 1700000000,
            },
            {
                "id": 2,
                "role": "user",
                "content": "Why is login broken?",
                "timestamp": 1700000001,
                "platform_message_id": "evt-2",
            },
            {
                "id": 3,
                "role": "assistant",
                "content": "I will inspect the auth middleware.",
                "timestamp": 1700000002,
            },
            {
                "id": 4,
                "role": "tool",
                "tool_name": "read_file",
                "content": "def redirect_after_login(): pass",
                "timestamp": 1700000003,
            },
            {
                "id": 5,
                "role": "user",
                "content": [{"type": "text", "text": "Only show me the prompts."}],
                "timestamp": 1700000004,
            },
        ],
    }










def test_html_export_escapes_tool_call_names():
    payload = '<img src=x onerror="alert(document.domain)">'

    rendered = _generate_messages_html(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": payload, "arguments": "<b>x</b>"},
                    }
                ],
            }
        ]
    )

    assert payload not in rendered
    assert '&lt;img src=x onerror=&quot;alert(document.domain)&quot;&gt;' in rendered
    assert "&lt;b&gt;x&lt;/b&gt;" in rendered




def test_export_record_count_switches_unit_for_prompt_only_exports():
    assert export_record_count([_sample_session()]) == (1, "session")
    assert export_record_count([_sample_session()], only="user-prompts") == (
        2,
        "prompt",
    )


def test_jsonl_export_redacts_embedded_client_secret_dict_value():
    """#20785 follow-up: opaque client_secret nested in structured message
    content must not survive export. The key context alone identifies the
    value as a secret, even though 'opaque-client-secret-...' has no
    recognizable prefix and the serialized key is JSON-escaped."""
    opaque = "opaque-client-secret-abc123XYZ"
    session = _sample_session()
    session["messages"].append(
        {
            "id": 6,
            "role": "tool",
            "tool_name": "google_api",
            "content": {"client_secret": opaque, "client_id": "cid-123"},
            "timestamp": 1700000005,
        }
    )
    rendered = render_sessions_export([session], fmt="jsonl")
    assert opaque not in rendered
    # JSONL stays parseable after redaction.
    for line in rendered.strip().splitlines():
        json.loads(line)


def test_jsonl_export_redacts_embedded_client_secret_json_string():
    """Same leak through a JSON *string* payload (tool output that embeds a
    credential blob as text)."""
    opaque = "opaque-client-secret-abc123XYZ"
    session = _sample_session()
    session["messages"].append(
        {
            "id": 6,
            "role": "tool",
            "tool_name": "google_api",
            "content": json.dumps({"client_secret": opaque}),
            "timestamp": 1700000005,
        }
    )
    rendered = render_sessions_export([session], fmt="jsonl")
    assert opaque not in rendered
    for line in rendered.strip().splitlines():
        json.loads(line)


def test_export_redaction_forced_even_when_global_toggle_disabled(monkeypatch):
    """Egress boundary must redact regardless of security.redact_secrets."""
    import agent.redact as redact_mod

    opaque = "GOCSPX-abcdefghij1234567890"
    monkeypatch.setattr(redact_mod, "_REDACT_ENABLED", False)
    session = _sample_session()
    session["messages"].append(
        {
            "id": 6,
            "role": "tool",
            "tool_name": "google_api",
            "content": {"client_secret": opaque},
            "timestamp": 1700000005,
        }
    )
    rendered = render_sessions_export([session], fmt="jsonl")
    assert opaque not in rendered
    assert "GOCSPX" not in rendered


def test_sessions_export_cli_prompt_only_stdout(monkeypatch, capsys):
    import hermes_cli.main as main_mod
    import hermes_state

    captured = {}

    class FakeDB:
        def resolve_session_id(self, session_id):
            captured["resolved_from"] = session_id
            return "sess-123"

        def export_session(self, session_id):
            captured["exported"] = session_id
            return _sample_session()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(hermes_state, "SessionDB", lambda: FakeDB())
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "sessions", "export", "-", "--session-id", "sess", "--only", "user-prompts"],
    )

    main_mod.main()

    output = capsys.readouterr().out
    records = [json.loads(line) for line in output.splitlines()]
    assert [record["text"] for record in records] == [
        "Why is login broken?",
        "Only show me the prompts.",
    ]
    assert captured == {
        "resolved_from": "sess",
        "exported": "sess-123",
        "closed": True,
    }


