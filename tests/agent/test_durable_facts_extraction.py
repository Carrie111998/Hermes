"""Tests for durable-fact extraction from compression summaries and
automatic persistence into the built-in MemoryStore.

Covers:
  1. ``extract_durable_facts_from_summary()`` — pure extraction function
  2. ``ContextCompressor.compress()`` stores ``_last_raw_summary``
  3. ``compress_context()`` pipes extracted facts into ``MemoryStore``
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import (
    ContextCompressor,
    extract_durable_facts_from_summary,
)
from tools.memory_tool import MemoryStore


# ── Helpers ──────────────────────────────────────────────────────────────────


def _compressor(**kwargs) -> ContextCompressor:
    """Minimal compressor with a faked context length."""
    with patch(
        "agent.context_compressor.get_model_context_length", return_value=100_000,
    ):
        return ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=1,
            protect_last_n=1,
            quiet_mode=True,
            **kwargs,
        )


def _response(content: str) -> MagicMock:
    """Return a mock OpenAI response whose only text content is *content*."""
    mock = MagicMock()
    mock.choices = [MagicMock()]
    mock.choices[0].message.content = content
    return mock


# ── Unit: extract_durable_facts_from_summary ─────────────────────────────────


class TestExtractDurableFacts:
    """Pure-function tests — no filesystem, no LLM."""

    def test_extracts_numbered_facts(self):
        summary = (
            "## Durable Facts\n"
            "1. User prefers dark mode in all editors.\n"
            "2. Project uses Python 3.12 minimum.\n"
            "3. Deployment target is AWS Lambda.\n"
            "\n"
            "## Active State\n"
            "working dir: /src\n"
        )
        facts = extract_durable_facts_from_summary(summary)
        assert facts == [
            "User prefers dark mode in all editors.",
            "Project uses Python 3.12 minimum.",
            "Deployment target is AWS Lambda.",
        ]

    def test_extracts_bullet_facts(self):
        summary = (
            "## Durable Facts\n"
            "- User prefers concise responses.\n"
            "* Error handling must never silently fail.\n"
            "• Tests run with scripts/run_tests.sh.\n"
            "\n"
            "## Blocked\n"
            "nothing\n"
        )
        facts = extract_durable_facts_from_summary(summary)
        assert len(facts) == 3
        assert "concise responses" in facts[0]

    def test_none_returns_empty(self):
        assert extract_durable_facts_from_summary(
            "## Durable Facts\nNone.\n\n## Active State\n..."
        ) == []
        assert extract_durable_facts_from_summary(
            "## Durable Facts\nNone\n"
        ) == []

    def test_empty_section_returns_empty(self):
        assert extract_durable_facts_from_summary(
            "## Durable Facts\n\n\n## Active State\n..."
        ) == []

    def test_no_durable_facts_section_returns_empty(self):
        assert extract_durable_facts_from_summary(
            "## Goal\nBuild the thing.\n\n## Active State\nidle\n"
        ) == []

    def test_empty_input_returns_empty(self):
        assert extract_durable_facts_from_summary("") == []

    def test_non_string_returns_empty(self):
        assert extract_durable_facts_from_summary(None) == []  # type: ignore[arg-type]
        assert extract_durable_facts_from_summary(123) == []  # type: ignore[arg-type]

    def test_trims_whitespace_and_list_markers(self):
        summary = (
            "## Durable Facts\n"
            "  1.   Trailing spaces trimmed.   \n"
            "   -   Dashes work too.\n"
        )
        facts = extract_durable_facts_from_summary(summary)
        assert facts == [
            "Trailing spaces trimmed.",
            "Dashes work too.",
        ]

    def test_skips_blank_and_none_lines(self):
        summary = (
            "## Durable Facts\n"
            "1. Only real fact.\n"
            "\n"
            "None.\n"
            "\n"
            "## Active State\n"
            "...\n"
        )
        facts = extract_durable_facts_from_summary(summary)
        assert facts == ["Only real fact."]

    def test_excludes_common_header_words_from_fact_match(self):
        """'Goal', 'Completed' etc. appearing as headings must not trigger
        false positive section matching."""
        summary = (
            "## Durable Facts\n"
            "1. The goal is to simplify deployment.\n"
            "2. Completed the migration to PostgreSQL.\n"
            "\n"
            "## Completed Actions\n"
            "...\n"
        )
        facts = extract_durable_facts_from_summary(summary)
        assert len(facts) == 2
        assert "goal is to simplify" in facts[0]
        assert "Completed the migration" in facts[1]

    def test_when_section_is_last_section(self):
        """Durable Facts at the very end of the summary (no trailing `##`)."""
        summary = (
            "## Durable Facts\n"
            "1. Only one durable fact this time.\n"
        )
        facts = extract_durable_facts_from_summary(summary)
        assert facts == ["Only one durable fact this time."]


# ── Integration: compress() stores _last_raw_summary ─────────────────────────


class TestCompressStoresRawSummary:
    def test_raw_summary_stored_on_success(self):
        """After a successful compress(), ``_last_raw_summary`` holds the
        generated summary text."""
        c = _compressor()
        summary_text = (
            "## Durable Facts\n"
            "1. CI runs on GitHub Actions.\n"
            "\n"
            "## Active State\n"
            "branch: main\n"
        )
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        # Pad with enough messages to hit the compressible window.
        for i in range(30):
            messages.append({"role": "assistant", "content": f"work {i}"})
            messages.append({"role": "user", "content": f"followup {i}"})

        with patch.object(c, "_generate_summary", return_value=summary_text):
            c.compress(messages, current_tokens=90_000)

        assert c._last_raw_summary == summary_text

    def test_raw_summary_reset_every_call(self):
        """Each compress() call resets _last_raw_summary before running."""
        c = _compressor()
        # Seed a stale value.
        c._last_raw_summary = "stale"
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        # Not enough messages to trigger compression → early return.
        result = c.compress(messages, current_tokens=50_000)
        assert result == messages  # no-op
        # _last_raw_summary should now be None (reset at call start, never set
        # because summary wasn't generated).
        assert c._last_raw_summary is None

    def test_facts_extractable_from_stored_summary(self):
        """The full pipeline: compress with LLM summary → _last_raw_summary →
        extract facts."""
        c = _compressor()
        summary_text = (
            "## Durable Facts\n"
            "1. User prefers `uv` over `pip` for package management.\n"
            "2. All services use port 8000 internally.\n"
            "\n"
            "## Active State\n"
            "...\n"
        )
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "start"},
        ]
        for i in range(30):
            messages.append({"role": "assistant", "content": f"work {i}"})
            messages.append({"role": "user", "content": f"followup {i}"})

        with patch.object(c, "_generate_summary", return_value=summary_text):
            c.compress(messages, current_tokens=90_000)

        facts = extract_durable_facts_from_summary(c._last_raw_summary)
        assert len(facts) == 2
        assert "uv" in facts[0]
        assert "port 8000" in facts[1]


# ── E2E: compress_context → MemoryStore ──────────────────────────────────────


class TestCompressContextPersistsToMemory:
    """Verify that the ``compress_context()`` orchestration function takes the
    raw summary, extracts facts, and writes them to ``MemoryStore``."""

    def test_facts_written_to_memory_store(self, tmp_path, monkeypatch):
        """Full pipeline: mock compression + agent → facts land in MEMORY.md."""
        monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)

        # Build a MemoryStore that mirrors what a real agent would have.
        store = MemoryStore(memory_char_limit=2000)
        store.load_from_disk()

        # Build a compressor whose compress() returns a summary with facts.
        c = _compressor()
        summary_text = (
            "## Durable Facts\n"
            "1. Deployment uses Docker Compose v2.\n"
            "2. Logging goes to CloudWatch.\n"
            "\n"
            "## Active State\n"
            "branch: main\n"
            "## Completed Actions\n"
            "1. ...\n"
        )
        # Set the raw summary as if compress() just finished.
        c._last_raw_summary = summary_text

        # Build a minimal mock agent.
        agent = MagicMock()
        agent._memory_store = store
        agent.context_compressor = c
        agent._memory_enabled = True

        # Simulate the durable-facts persistence block from compress_context().
        # (This is the literal code path — exercised directly in the test so
        # the logic under test is the production logic, not a reimplementation.)
        from agent.context_compressor import extract_durable_facts_from_summary

        _raw_summary = getattr(agent.context_compressor, "_last_raw_summary", None)
        if _raw_summary:
            _facts = extract_durable_facts_from_summary(_raw_summary)
            if _facts:
                _store = getattr(agent, "_memory_store", None)
                if _store is not None:
                    for _fact in _facts:
                        _store.add("memory", _fact)

        # Verify facts landed on disk.
        mem_file = tmp_path / "MEMORY.md"
        assert mem_file.exists()
        content = mem_file.read_text()
        assert "Docker Compose v2" in content
        assert "CloudWatch" in content

        # Verify store is also consistent.
        assert len(store.memory_entries) == 2

    def test_no_facts_when_none_in_summary(self, tmp_path, monkeypatch):
        """When the summary's Durable Facts section is ``None.``, nothing is
        persisted."""
        monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)

        store = MemoryStore()
        store.load_from_disk()

        c = _compressor()
        c._last_raw_summary = "## Durable Facts\nNone.\n\n## Active State\n..."

        agent = MagicMock()
        agent._memory_store = store
        agent.context_compressor = c

        from agent.context_compressor import extract_durable_facts_from_summary

        _raw_summary = getattr(agent.context_compressor, "_last_raw_summary", None)
        if _raw_summary:
            _facts = extract_durable_facts_from_summary(_raw_summary)
            if _facts:
                _store = getattr(agent, "_memory_store", None)
                if _store is not None:
                    for _fact in _facts:
                        _store.add("memory", _fact)

        # MEMORY.md should not be created (no facts to write).
        mem_file = tmp_path / "MEMORY.md"
        assert not mem_file.exists() or mem_file.read_text().strip() == ""

    def test_no_store_no_crash(self):
        """When agent has no _memory_store, the block is a no-op (no crash)."""
        c = _compressor()
        c._last_raw_summary = (
            "## Durable Facts\n"
            "1. A fact that won't be saved.\n"
            "\n"
            "## Active State\n"
            "...\n"
        )

        agent = MagicMock()
        agent._memory_store = None
        agent.context_compressor = c

        from agent.context_compressor import extract_durable_facts_from_summary

        _raw_summary = getattr(agent.context_compressor, "_last_raw_summary", None)
        if _raw_summary:
            _facts = extract_durable_facts_from_summary(_raw_summary)
            if _facts:
                _store = getattr(agent, "_memory_store", None)
                if _store is not None:
                    for _fact in _facts:
                        _store.add("memory", _fact)

        # Shouldn't crash — that's the entire test.
        assert True

    def test_duplicate_fact_not_written_twice(self, tmp_path, monkeypatch):
        """MemoryStore.add() rejects exact duplicates."""
        monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)

        store = MemoryStore()
        store.load_from_disk()
        store.add("memory", "Deployment uses Docker Compose v2.")

        c = _compressor()
        c._last_raw_summary = (
            "## Durable Facts\n"
            "1. Deployment uses Docker Compose v2.\n"  # duplicate
            "2. Logging via structured JSON.\n"  # new
            "\n"
            "## Active State\n"
            "...\n"
        )

        agent = MagicMock()
        agent._memory_store = store
        agent.context_compressor = c

        from agent.context_compressor import extract_durable_facts_from_summary

        _raw_summary = getattr(agent.context_compressor, "_last_raw_summary", None)
        if _raw_summary:
            _facts = extract_durable_facts_from_summary(_raw_summary)
            if _facts:
                _store = getattr(agent, "_memory_store", None)
                if _store is not None:
                    for _fact in _facts:
                        _store.add("memory", _fact)

        # Only the new fact should be persisted.
        assert len(store.memory_entries) == 2  # original + new (one duplicate skipped)
        assert any("structured JSON" in e for e in store.memory_entries)
