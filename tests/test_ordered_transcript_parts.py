"""Gateway-level contract tests for bounded ordered transcript parts."""

import json
import sqlite3
import threading
from pathlib import Path

from hermes_state import SessionDB
from transcript_parts import MAX_BYTES, MAX_PARTS, message_parts
from tui_gateway import server


def _kinds(envelope):
    return [part.get("kind") for part in envelope["parts"]]


def test_mixed_parts_keep_order_identity_and_redact_media_authority():
    envelope = message_parts({
        "role": "assistant",
        "reasoning": "checking",
        "content": [
            {"type": "text", "text": "before", "id": "p-1", "timestamp": 10.0},
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png?token=secret"}},
            {"type": "text", "text": "after"},
            {"type": "input_audio", "input_audio": {"data": "secret-audio"}},
            {"type": "file", "file": {"file_id": "file-7"}},
        ],
        "tool_calls": [{
            "id": "call-7",
            "function": {"name": "terminal", "arguments": {"command": "pwd"}},
        }],
    })

    assert _kinds(envelope)[:7] == [
        "reasoning", "text", "image", "text", "audio", "file", "tool-call",
    ]
    assert envelope["parts"][1]["id"] == "p-1"
    assert envelope["parts"][1]["timestamp"] == 10.0
    encoded = json.dumps(envelope, ensure_ascii=False)
    assert "token=secret" not in encoded
    assert "secret-audio" not in encoded
    local = message_parts({"role": "user", "content": [{"type": "image", "ref": "@image:/Users/alice/private.png"}]})
    assert "/Users/alice" not in json.dumps(local)
    assert local["parts"][0]["ref"] == "@image:private.png"
    assert len(encoded.encode("utf-8")) <= MAX_BYTES


def test_hostile_nodes_depth_scalars_and_bytes_are_bounded_with_evidence():
    nested = "x"
    for _ in range(40):
        nested = [nested]
    envelope = message_parts({
        "role": "user",
        "parts": [
            {"type": "text", "text": "🧑🏽‍💻" * 100_000},
            {"type": "mystery", "value": nested},
        ] * 200,
    })

    assert envelope["clipped"] is True
    assert any(part.get("kind") in {"unknown", "clipped"} for part in envelope["parts"])
    assert len(json.dumps(envelope, ensure_ascii=False).encode("utf-8")) <= MAX_BYTES

    numeric = message_parts({
        "role": "tool",
        "tool_call_id": "bomb",
        "content": {"bad": float("nan"), "huge": 10**5000},
    })
    assert numeric["clipped"] is True
    assert len(numeric["parts"]) <= MAX_PARTS
    assert "nan" not in json.dumps(numeric).lower()
    unknown_numeric = message_parts({
        "role": "user",
        "parts": [{"type": "mystery", "value": 10**5000}],
    })
    assert unknown_numeric["clipped"] is True
    assert "int" in json.dumps(unknown_numeric)
    surrogate = message_parts({"role": "user", "content": "ok\ud800tail"})
    assert surrogate["clipped"] is True
    json.dumps(surrogate, ensure_ascii=False).encode("utf-8")
    invalid_url = message_parts({
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "https://host:99999/path"}}],
    })
    assert invalid_url["clipped"] is True
    assert "99999" not in json.dumps(invalid_url)
    ipv6 = message_parts({
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "https://[::1]:8443/x?q=secret"}}],
    })
    assert ipv6["parts"][0]["ref"] == "https://[::1]:8443/x"


def test_session_db_round_trip_persists_parts_without_mutating_model_content(tmp_path: Path):
    db_path = tmp_path / "state.db"
    content = [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,hidden"}},
        {"type": "text", "text": "here"},
    ]
    db = SessionDB(db_path=db_path)
    try:
        db.create_session("parts-session", "desktop")
        db.append_message(
            "parts-session",
            role="user",
            # The legacy model-facing value remains exactly what this caller
            # supplied; the additive envelope is stored in its own column.
            content="look [image] here",
            parts=message_parts({"role": "user", "content": content}),
        )
        rows = db.get_messages("parts-session")
        assert rows[0]["content"] == "look [image] here"
        assert rows[0]["parts"]["parts"][0]["kind"] == "text"
        assert rows[0]["parts"]["parts"][1]["kind"] == "image"
        assert "hidden" not in json.dumps(rows[0]["parts"])

        model_rows = db.get_messages_as_conversation(
            "parts-session", include_row_ids=True, include_parts=True
        )
        assert model_rows[0]["content"] == "look [image] here"
        assert model_rows[0]["parts"]["version"] == 1
    finally:
        db.close()

    reopened = SessionDB(db_path=db_path)
    try:
        assert reopened.get_messages("parts-session")[0]["parts"]["parts"][2]["text"] == "here"
    finally:
        reopened.close()


def test_canonical_tool_results_survive_repeated_storage_and_wire_admission():
    structured = message_parts({
        "role": "tool",
        "tool_call_id": "call-json",
        "tool_name": "inspect",
        "content": {"ok": True, "rows": [1, 2, 3]},
    })
    structured_again = message_parts({"parts": structured})
    result = structured_again["parts"][0]
    assert result["kind"] == "tool-result"
    assert result["id"] == "call-json"
    assert result["name"] == "inspect"
    assert result["value"] == {"ok": True, "rows": [1, 2, 3]}

    media = message_parts({
        "role": "tool",
        "tool_call_id": "call-image",
        "tool_name": "render",
        "content": [{
            "type": "image_url",
            "image_url": {"url": "https://example.test/render.png?token=secret"},
            "mime_type": "image/png",
        }],
    })
    media_again = message_parts({"parts": media})
    media_result = media_again["parts"][0]
    assert media_result["kind"] == "tool-result"
    assert media_result["content_kind"] == "image"
    assert media_result["ref"] == "https://example.test/render.png"
    assert media_result["mime_type"] == "image/png"
    assert "secret" not in json.dumps(media_again)


def test_existing_schema_gets_parts_column_and_model_projection_stays_unchanged(tmp_path: Path):
    db_path = tmp_path / "legacy-state.db"
    db = SessionDB(db_path=db_path)
    try:
        db.create_session("legacy-session", "desktop")
        db.append_message("legacy-session", "user", "run the tool")
        db.append_message(
            "legacy-session",
            "assistant",
            "",
            tool_calls=[{"id": "call-legacy", "function": {"name": "pwd", "arguments": "{}"}}],
        )
        db.append_message("legacy-session", "tool", "ok", tool_call_id="call-legacy", tool_name="pwd")
        db.append_message("legacy-session", "assistant", "done")
    finally:
        db.close()

    # Simulate a pre-parts install.  The next writable open must reconcile the
    # additive column instead of assuming CREATE TABLE IF NOT EXISTS changed it.
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("ALTER TABLE messages DROP COLUMN parts")
        raw.commit()
    finally:
        raw.close()

    reopened = SessionDB(db_path=db_path)
    try:
        columns = {
            row[1]
            for row in reopened._conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        assert "parts" in columns
        baseline = reopened.get_messages_as_conversation(
            "legacy-session", repair_alternation=True, include_row_ids=True
        )
        model_history, display_history = reopened.get_resume_conversations("legacy-session")
        assert model_history == baseline
        assert "parts" not in model_history[0]
        assert [row["role"] for row in model_history] == ["user", "assistant", "tool", "assistant"]
        assert [row["role"] for row in display_history] == ["user", "assistant", "tool", "assistant"]
        assert all("parts" in row for row in display_history)
        assert display_history[1]["content"] == ""
        assert display_history[1]["tool_calls"][0]["id"] == "call-legacy"
    finally:
        reopened.close()


def test_legacy_and_new_rows_project_additive_parts_without_leaking_raw_content():
    history = [
        {"role": "user", "content": "old text"},
        {
            "role": "assistant",
            "content": "visible",
            "parts": message_parts({
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "before"},
                    {"type": "image_url", "image_url": {"url": "https://e.test/x?key=private"}},
                    {"type": "text", "text": "after"},
                ],
            }),
        },
    ]

    projected = server._history_to_messages(history)

    assert projected[0]["text"] == "old text"
    assert projected[0]["parts"] == [{"kind": "text", "text": "old text"}]
    assert [part["kind"] for part in projected[1]["parts"][:3]] == ["text", "image", "text"]
    assert "key=private" not in json.dumps(projected)


def test_hydrated_commentary_tool_run_emits_each_invocation_once():
    projected = server._history_to_messages([
        {"role": "user", "content": "inspect it"},
        {
            "role": "assistant",
            "content": "I will inspect it.",
            "tool_calls": [{
                "id": "call-1",
                "function": {"name": "terminal", "arguments": {"command": "pwd"}},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "tool_name": "terminal",
            "content": "ok",
        },
        {"role": "assistant", "content": "Done."},
    ])

    kinds = [part["kind"] for row in projected for part in row.get("parts", [])]
    assert kinds.count("tool-call") == 1
    assert kinds.count("tool-result") == 1
    assert projected[2]["args"] == {"command": "pwd"}


def test_live_submit_emits_incremental_and_terminal_parts_in_order(monkeypatch, tmp_path):
    events = []

    class ImmediateThread:
        def __init__(self, target=None, **_kwargs):
            self.target = target

        def start(self):
            if self.target:
                self.target()

        def join(self, **_kwargs):
            return None

        def is_alive(self):
            return False

    class Agent:
        model = "test-model"
        provider = "test-provider"
        api_mode = "chat_completions"
        base_url = ""
        api_key = ""

        def clear_interrupt(self):
            return None

        def run_conversation(self, prompt, conversation_history=None, stream_callback=None, **_kwargs):
            stream_callback("live")
            self.interim_assistant_callback("live", already_streamed=True)
            self.interim_assistant_callback("new commentary", already_streamed=False)
            return {
                "final_response": "after",
                "messages": [
                    {"role": "user", "content": prompt},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "before"},
                            {"type": "image_url", "image_url": {"url": "https://e.test/a?secret=x"}},
                            {"type": "text", "text": "after"},
                        ],
                        "tool_calls": [{
                            "id": "call-1",
                            "function": {"name": "terminal", "arguments": {"command": "pwd"}},
                        }],
                    },
                    {"role": "tool", "tool_call_id": "call-1", "tool_name": "terminal", "content": "ok"},
                ],
            }

    session = {
        "agent": Agent(),
        "session_key": "parts-live",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": True,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        "tool_started_at": {},
        "transport": None,
    }
    server._sessions["parts-live-sid"] = session
    monkeypatch.setattr(server.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: events.append((event, payload)))
    monkeypatch.setattr(server, "make_stream_renderer", lambda _cols: None)
    monkeypatch.setattr(server, "render_message", lambda _raw, _cols: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda _sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *_args: None)
    monkeypatch.setattr(server, "_session_cwd", lambda _session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    monkeypatch.setattr(server, "_set_session_context", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda _tokens: None)
    monkeypatch.setattr(server, "_get_usage", lambda _agent: {})
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *_args: False)
    monkeypatch.setattr(server, "_voice_tts_enabled", lambda: False)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_start_usage_ticker", lambda *_args, **_kwargs: (threading.Event(), ImmediateThread()))
    try:
        server._run_prompt_submit("parts-live-rid", "parts-live-sid", session, "question")
    finally:
        server._sessions.pop("parts-live-sid", None)

    starts = [payload for event, payload in events if event == "message.start"]
    deltas = [payload for event, payload in events if event == "message.delta"]
    interims = [payload for event, payload in events if event == "message.interim"]
    completes = [payload for event, payload in events if event == "message.complete"]
    assert starts and starts[0]["parts"][0]["kind"] == "text"
    assert deltas and deltas[0]["parts_mode"] == "append"
    assert [payload["parts_mode"] for payload in interims] == ["seal", "append"]
    assert completes
    assert [part["kind"] for part in completes[-1]["parts"][:5]] == [
        "text", "image", "text", "tool-call", "tool-result",
    ]
    assert "secret=x" not in json.dumps(completes[-1])


def test_parts_budget_never_truncates_legacy_inflight_text():
    legacy_text = "x" * 20_000
    session = {}
    server._start_inflight_turn(session, legacy_text)
    server._append_inflight_delta(session, legacy_text)

    snapshot = server._inflight_snapshot(session)
    assert snapshot["user"] == legacy_text
    assert snapshot["assistant"] == legacy_text
    assert snapshot["parts_clipped"] is True
    assert snapshot["user_parts_clipped"] is True


def test_terminal_parts_keep_the_whole_logical_turn_across_internal_user_nudges():
    # Repeated identical prompts must not make the collector jump back to the
    # older turn when locating the current external-user boundary.
    history = [{"role": "user", "content": "current prompt"},
               {"role": "assistant", "content": "old answer"}]
    rows = history + [
        {"role": "user", "content": "current prompt"},
        {"role": "assistant", "content": "I will inspect.", "tool_calls": [{
            "id": "call-1",
            "function": {"name": "terminal", "arguments": {"command": "pwd"}},
        }]},
        {"role": "tool", "tool_call_id": "call-1", "tool_name": "terminal",
         "content": "ok"},
        {"role": "user", "content": "[internal continuation nudge]"},
        {"role": "assistant", "content": "Done."},
    ]

    envelope = server._assistant_turn_parts(
        rows,
        prior_history_count=len(history),
        turn_user_content="current prompt",
    )

    assert [part["kind"] for part in envelope["parts"]] == [
        "text", "tool-call", "tool-result", "text",
    ]
    assert [part.get("text") for part in envelope["parts"] if part["kind"] == "text"] == [
        "I will inspect.", "Done.",
    ]
