"""Tests for agent/handoff.py — durable session handoff files."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agent.handoff import (
    generate_handoff,
    read_handoff_excerpt,
    write_gateway_active_handoff,
    _safe_session_id,
    _message_excerpt,
    EMPTY_EXCERPT_SENTINEL,
    HANDOFFS_DIRNAME,
)


@pytest.fixture()
def handoff_dir(tmp_path):
    """Return a temporary handoffs directory."""
    d = tmp_path / HANDOFFS_DIRNAME
    d.mkdir()
    return d


class TestSafeSessionId:
    def test_normal_id(self):
        assert _safe_session_id("abc-123_def") == "abc-123_def"

    def test_special_chars(self):
        result = _safe_session_id("session/with spaces@#$$$")
        assert " " not in result
        assert "@" not in result

    def test_none_returns_unknown(self):
        assert _safe_session_id(None) == "unknown-session"

    def test_empty_returns_unknown(self):
        assert _safe_session_id("") == "unknown-session"


class TestMessageExcerpt:
    def test_basic_messages(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = _message_excerpt(msgs)
        assert "user" in result
        assert "hello" in result
        assert "assistant" in result
        assert "hi there" in result

    def test_truncation(self):
        msgs = [{"role": "user", "content": "x" * 500}] * 100
        result = _message_excerpt(msgs, max_messages=10, max_chars=2000)
        assert len(result) < 500 * 100

    def test_none_content_skipped(self):
        msgs = [{"role": "assistant", "content": None}]
        result = _message_excerpt(msgs)
        assert result.strip() == "" or result == EMPTY_EXCERPT_SENTINEL

    def test_tool_calls_content(self):
        msgs = [
            {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "search"}}]},
        ]
        result = _message_excerpt(msgs)
        assert "tool_calls" in result

    def test_empty_list_returns_fallback(self):
        result = _message_excerpt([])
        assert result == EMPTY_EXCERPT_SENTINEL


class TestGenerateHandoffExtractive:
    def test_creates_file(self, tmp_path):
        with patch("agent.handoff.get_hermes_home", return_value=tmp_path):
            path = generate_handoff(
                session_id="test-123",
                messages=[{"role": "user", "content": "do stuff"}],
                reason="manual_test",
                hermes_home=tmp_path,
            )
        assert path.exists()
        content = path.read_text()
        assert "test-123" in content
        assert "manual_test" in content
        assert "do stuff" in content

    def test_creates_latest_symlink(self, tmp_path):
        with patch("agent.handoff.get_hermes_home", return_value=tmp_path):
            path = generate_handoff(
                session_id="test-symlink",
                messages=[],
                reason="test",
                hermes_home=tmp_path,
            )
        latest = path.parent / "latest.md"
        assert latest.exists()

    def test_updates_index(self, tmp_path):
        with patch("agent.handoff.get_hermes_home", return_value=tmp_path):
            generate_handoff(
                session_id="test-index",
                messages=[],
                reason="test",
                hermes_home=tmp_path,
            )
        index_path = tmp_path / HANDOFFS_DIRNAME / "index.json"
        assert index_path.exists()
        data = json.loads(index_path.read_text())
        assert len(data) == 1
        assert data[0]["session_id"] == "test-index"


class TestGenerateHandoffLLM:
    def test_llm_path_writes_content(self, tmp_path):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "# LLM Handoff\n\nGoal: test goal"

        with patch("agent.handoff.get_hermes_home", return_value=tmp_path), \
             patch("agent.handoff._call_auxiliary_llm", return_value="# LLM Handoff\n\nGoal: test goal"):
            path = generate_handoff(
                session_id="llm-test",
                messages=[{"role": "user", "content": "do stuff"}],
                reason="manual_handoff",
                llm_summarize=True,
                hermes_home=tmp_path,
            )

        content = path.read_text()
        assert "LLM Handoff" in content
        assert "test goal" in content

    def test_llm_failure_falls_back_to_extractive(self, tmp_path):
        with patch("agent.handoff.get_hermes_home", return_value=tmp_path), \
             patch("agent.handoff._call_auxiliary_llm", return_value=None):
            path = generate_handoff(
                session_id="llm-fallback",
                messages=[{"role": "user", "content": "hello world"}],
                reason="auto_compaction",
                llm_summarize=True,
                hermes_home=tmp_path,
            )

        content = path.read_text()
        # Should contain extractive fallback content
        assert "hello world" in content
        assert "auto_compaction" in content

    def test_llm_index_marks_summarized(self, tmp_path):
        with patch("agent.handoff.get_hermes_home", return_value=tmp_path), \
             patch("agent.handoff._call_auxiliary_llm", return_value="LLM content"):
            generate_handoff(
                session_id="llm-index",
                messages=[],
                reason="test",
                llm_summarize=True,
                hermes_home=tmp_path,
            )

        index_path = tmp_path / HANDOFFS_DIRNAME / "index.json"
        data = json.loads(index_path.read_text())
        assert data[0].get("llm_summarized") is True

    def test_llm_path_includes_extra_metadata(self, tmp_path):
        """extra_metadata (chat_id/thread_id) must land in the file content on
        the LLM-summary path too, not just the extractive path. The LLM writes
        llm_content verbatim from its own prompt (which never mentions
        extra_metadata), so generate_handoff must append it deterministically
        rather than rely on the LLM reproducing it."""
        with patch("agent.handoff.get_hermes_home", return_value=tmp_path), \
             patch(
                 "agent.handoff._call_auxiliary_llm",
                 return_value="# LLM Handoff\n\nGoal: test goal",
             ):
            path = generate_handoff(
                session_id="llm-metadata",
                messages=[{"role": "user", "content": "do stuff"}],
                reason="session_expired",
                platform="telegram",
                llm_summarize=True,
                hermes_home=tmp_path,
                extra_metadata={
                    "chat_id": "12345",
                    "thread_id": "678",
                    "new_session_id": None,
                },
            )

        content = path.read_text()
        # Original LLM content is preserved verbatim, metadata appended.
        assert "LLM Handoff" in content
        assert "test goal" in content
        assert "platform: telegram" in content
        assert "chat_id: 12345" in content
        assert "thread_id: 678" in content
        # None-valued keys are dropped (matches the extractive path's rule).
        assert "new_session_id" not in content

    def test_llm_path_no_metadata_block_when_extra_metadata_none(self, tmp_path):
        """Regression guard: callers that pass extra_metadata=None must see
        byte-identical llm_content — no stray metadata block appended."""
        llm_text = "# LLM Handoff\n\nGoal: test goal"
        with patch("agent.handoff.get_hermes_home", return_value=tmp_path), \
             patch("agent.handoff._call_auxiliary_llm", return_value=llm_text):
            path = generate_handoff(
                session_id="llm-no-metadata",
                messages=[{"role": "user", "content": "do stuff"}],
                reason="manual_handoff",
                llm_summarize=True,
                hermes_home=tmp_path,
            )

        assert path.read_text() == llm_text

    def test_extractive_path_includes_extra_metadata(self, tmp_path):
        """The extractive (non-LLM) path folds extra_metadata into
        metadata_lines — pin that behaviour so it does not regress."""
        with patch("agent.handoff.get_hermes_home", return_value=tmp_path):
            path = generate_handoff(
                session_id="extractive-metadata",
                messages=[{"role": "user", "content": "hello"}],
                reason="session_expired",
                platform="telegram",
                hermes_home=tmp_path,
                extra_metadata={"chat_id": "12345", "thread_id": "678"},
            )

        content = path.read_text()
        assert "Platform: telegram" in content
        assert "chat_id: 12345" in content
        assert "thread_id: 678" in content


class TestReadHandoffExcerpt:
    def test_reads_full_file(self, tmp_path):
        f = tmp_path / "handoff.md"
        f.write_text("full content here", encoding="utf-8")
        assert read_handoff_excerpt(f) == "full content here"

    def test_truncates_long_file(self, tmp_path):
        f = tmp_path / "handoff.md"
        f.write_text("x" * 100_000, encoding="utf-8")
        result = read_handoff_excerpt(f, max_tokens=1000)
        assert len(result) < 100_000
        assert "truncated" in result


class TestWriteGatewayActiveHandoff:
    def test_creates_file(self, tmp_path):
        with patch("agent.handoff.get_hermes_home", return_value=tmp_path):
            path = write_gateway_active_handoff(
                {"session_key": "tg:-100:5", "handoff_path": "/tmp/handoff.md"},
                hermes_home=tmp_path,
            )
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["session_key"] == "tg:-100:5"

    def test_deduplicates_by_key(self, tmp_path):
        with patch("agent.handoff.get_hermes_home", return_value=tmp_path):
            write_gateway_active_handoff(
                {"session_key": "tg:-100:5", "handoff_path": "/tmp/old.md"},
                hermes_home=tmp_path,
            )
            write_gateway_active_handoff(
                {"session_key": "tg:-100:5", "handoff_path": "/tmp/new.md"},
                hermes_home=tmp_path,
            )
        data = json.loads((tmp_path / HANDOFFS_DIRNAME / "gateway-active.json").read_text())
        assert len(data) == 1
        assert data[0]["handoff_path"] == "/tmp/new.md"
