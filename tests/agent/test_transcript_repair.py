"""Unit and contract tests for agent.transcript_repair helpers."""

import sqlite3
from typing import Any

import pytest

from agent.context_compressor import _DB_PERSISTED_MARKER
from agent.transcript_repair import (
    is_content_blank,
    resolve_and_repair_transcript_batch,
    sync_flushed_message_markers,
)


class TestIsContentBlank:
    """Test is_content_blank() across all scalar, text, and multimodal shapes."""

    def test_none_is_blank(self):
        assert is_content_blank(None) is True

    def test_empty_string_is_blank(self):
        assert is_content_blank("") is True
        assert is_content_blank("   ") is True
        assert is_content_blank("\n\t  \n") is True

    def test_non_empty_string_is_not_blank(self):
        assert is_content_blank("hello") is False
        assert is_content_blank("  hello  ") is False

    def test_empty_list_is_blank(self):
        assert is_content_blank([]) is True

    def test_list_of_whitespace_strings_is_blank(self):
        assert is_content_blank(["", "   ", "\n"]) is True

    def test_list_with_non_whitespace_string_is_not_blank(self):
        assert is_content_blank(["", "foo", "  "]) is False

    def test_list_of_empty_text_parts_is_blank(self):
        assert is_content_blank([{"type": "text", "text": ""}]) is True
        assert is_content_blank([
            {"type": "text", "text": "   "},
            {"type": "text", "text": "\n\t"},
        ]) is True

    def test_list_with_valid_text_part_is_not_blank(self):
        assert is_content_blank([{"type": "text", "text": "visible content"}]) is False
        assert is_content_blank([
            {"type": "text", "text": "   "},
            {"type": "text", "text": "content"},
        ]) is False

    def test_multimodal_image_url_is_not_blank(self):
        # Critical P0 check: image_url parts must NEVER be classified as blank
        assert is_content_blank([{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]) is False

    def test_multimodal_image_part_is_not_blank(self):
        assert is_content_blank([{"type": "image", "source": {"bytes": b"fake"}}]) is False

    def test_multimodal_input_audio_is_not_blank(self):
        assert is_content_blank([{"type": "input_audio", "input_audio": {"data": "base64"}}]) is False

    def test_multimodal_document_part_is_not_blank(self):
        assert is_content_blank([{"type": "document", "source": {"bytes": b"pdf"}}]) is False

    def test_mixed_text_and_image_is_not_blank(self):
        assert is_content_blank([
            {"type": "text", "text": "   "},
            {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
        ]) is False

    def test_dict_without_explicit_type_but_with_media_keys(self):
        assert is_content_blank([{"image_url": "https://example.com/img.png"}]) is False
        assert is_content_blank([{"image": "bytes"}]) is False
        assert is_content_blank([{"data": "raw_bytes"}]) is False

    def test_dict_content(self):
        assert is_content_blank({}) is True
        assert is_content_blank({"type": "text", "text": "  "}) is True
        assert is_content_blank({"type": "text", "text": "hello"}) is False
        assert is_content_blank({"type": "image_url", "url": "data:..."}) is False

    def test_empty_and_malformed_dicts_fail_closed_as_blank(self):
        """Empty dictionaries and malformed metadata without durable text/media must be blank."""
        assert is_content_blank([{}]) is True
        assert is_content_blank([{}, None, "   "]) is True
        assert is_content_blank([{"meta": "unknown"}]) is True
        assert is_content_blank([{"type": "unknown_type"}]) is True
        assert is_content_blank([{"text": ""}]) is True



@pytest.fixture
def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL,
            active INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    yield conn
    conn.close()


class TestResolveAndRepairTranscriptBatch:
    """Test resolve_and_repair_transcript_batch in SQLite transactions."""

    def test_fresh_messages_inserted(self, memory_db):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        inserted = resolve_and_repair_transcript_batch(
            memory_db,
            "s1",
            messages,
            encode_content_fn=lambda c: c,
            decode_content_fn=lambda c: c,
        )
        assert inserted == messages

    def test_in_place_repair_of_blank_assistant_row(self, memory_db):
        # Insert a blank assistant placeholder row
        cur = memory_db.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, ?, ?, ?, ?)",
            ("s1", "assistant", "   ", 100.0, 1),
        )
        row_id = cur.lastrowid
        memory_db.commit()

        # Repair with new content
        incoming = [{"role": "assistant", "content": "final streamed response", "_row_id": row_id}]
        inserted = resolve_and_repair_transcript_batch(
            memory_db,
            "s1",
            incoming,
            encode_content_fn=lambda c: c,
            decode_content_fn=lambda c: c,
        )
        # Should be repaired in-place, not inserted as a fresh row
        assert inserted == []
        assert incoming[0]["_row_id"] == row_id

        # Verify DB was updated
        updated_row = memory_db.execute("SELECT content FROM messages WHERE id = ?", (row_id,)).fetchone()
        assert updated_row["content"] == "final streamed response"

    def test_multimodal_assistant_row_is_not_overwritten(self, memory_db):
        import json
        multimodal_content = json.dumps([{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}])
        cur = memory_db.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, ?, ?, ?, ?)",
            ("s1", "assistant", multimodal_content, 100.0, 1),
        )
        row_id = cur.lastrowid
        memory_db.commit()

        incoming = [{"role": "assistant", "content": "new incoming", "_row_id": row_id}]
        inserted = resolve_and_repair_transcript_batch(
            memory_db,
            "s1",
            incoming,
            encode_content_fn=lambda c: json.dumps(c),
            decode_content_fn=lambda c: json.loads(c),
        )
        # Concurrent winner: existing multimodal row must NOT be overwritten
        assert inserted == []
        assert incoming[0]["_row_id"] == row_id
        assert incoming[0]["_canonical_content"] == [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]

        persisted = memory_db.execute("SELECT content FROM messages WHERE id = ?", (row_id,)).fetchone()
        assert persisted["content"] == multimodal_content


class TestSyncFlushedMessageMarkers:
    """Test sync_flushed_message_markers."""

    def test_marks_persisted_and_syncs_row_id(self):
        batch_msgs = [{"role": "user", "content": "hi"}]
        batch_rows = [{"_row_id": 42, "_canonical_content": "canonical hi"}]

        sync_flushed_message_markers(batch_msgs, batch_rows)

        assert batch_msgs[0][_DB_PERSISTED_MARKER] is True
        assert batch_msgs[0]["_row_id"] == 42
        assert batch_msgs[0]["content"] == "canonical hi"
