"""Tests for durable-fact extraction from compression summaries and
automatic persistence into the built-in MemoryStore.

Covers:
  1. ``extract_durable_facts_from_summary()`` — pure extraction function
  2. ``ContextCompressor.compress()`` stores ``_last_raw_summary``
  3. ``compress_context()`` pipes extracted facts into ``MemoryStore``
     via the gate-aware ``memory_tool()`` path.
"""

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
            "\u2022 Tests run with scripts/run_tests.sh.\n"
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
        for i in range(30):
            messages.append({"role": "assistant", "content": f"work {i}"})
            messages.append({"role": "user", "content": f"followup {i}"})

        with patch.object(c, "_generate_summary", return_value=summary_text):
            c.compress(messages, current_tokens=90_000)

        assert c._last_raw_summary == summary_text

    def test_raw_summary_reset_every_call(self):
        """Each compress() call resets _last_raw_summary before running."""
        c = _compressor()
        c._last_raw_summary = "stale"
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        result = c.compress(messages, current_tokens=50_000)
        assert result == messages  # no-op
        assert c._last_raw_summary is None

    def test_facts_extractable_from_stored_summary(self):
        """The full pipeline: compress with LLM summary -> _last_raw_summary ->
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


# ── E2E: compress_context -> MemoryStore (via gate-aware memory_tool) ─────────


def _summary_with_facts(*facts: str) -> str:
    """Build a realistic compression summary with a Durable Facts section."""
    numbered = "\n".join(
        f"{i}. {fact}" for i, fact in enumerate(facts, start=1)
    )
    return (
        f"## Goal\nTest session.\n\n"
        f"## Durable Facts\n{numbered}\n\n"
        f"## Active State\nbranch: main\n\n"
        f"## Completed Actions\n1. ...\n"
    )


class TestCompressContextPersistsToMemory:
    """Drive the real ``compress_context()`` orchestration function and verify
    that durable facts are persisted through the gate-aware ``memory_tool()``
    path."""

    def test_facts_persisted_via_compress_context(self, tmp_path, monkeypatch):
        """End-to-end: compress_context() -> durable facts land in MEMORY.md."""
        monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)

        summary_text = _summary_with_facts(
            "Deployment uses Docker Compose v2.",
            "Logging goes to CloudWatch.",
        )

        store = MemoryStore(memory_char_limit=2000)
        store.load_from_disk()

        compressor = _compressor()
        compressor._last_raw_summary = summary_text

        # Build enough messages to pass the minimum-count guard.
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        for i in range(30):
            messages.append({"role": "assistant", "content": f"work {i}"})
            messages.append({"role": "user", "content": f"followup {i}"})

        # Mock the agent just enough for compress_context() to reach the
        # durable-facts persistence block without real DB / network / locks.
        agent = MagicMock()
        agent.model = "test/model"
        agent.platform = "cli"
        agent.session_id = "test-sid"
        agent.tools = []
        agent._memory_store = store
        agent._memory_enabled = True
        agent._memory_manager = None
        agent.context_compressor = compressor
        agent._user_profile_enabled = False
        agent._compression_feasibility_checked = True
        agent.log_prefix = ""
        agent._cached_system_prompt = "cached system prompt"

        # compress_context() calls context_compressor.compress() — return a
        # shortened list so compression is considered to have made progress,
        # which lets the function reach the durable-facts persistence block.
        def _fake_compress(msgs, **kwargs):
            return msgs[:1] + msgs[-3:]  # drop middle messages

        compressor.compress = _fake_compress

        # Session DB lock — must succeed.
        agent._session_db = MagicMock()
        agent._session_db.try_acquire_compression_lock.return_value = True
        agent._session_db.release_compression_lock = MagicMock()

        from agent.conversation_compression import compress_context

        compress_context(
            agent,
            messages,
            system_message="sys",
            approx_tokens=90_000,
            force=True,
        )

        # Verify facts landed on disk.
        mem_file = tmp_path / "MEMORY.md"
        assert mem_file.exists()
        content = mem_file.read_text()
        assert "Docker Compose v2" in content
        assert "CloudWatch" in content
        assert len(store.memory_entries) == 2

    def test_none_section_no_write(self, tmp_path, monkeypatch):
        """When the summary says 'None.', no facts are persisted."""
        monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)

        summary_text = _summary_with_facts().replace(
            "## Durable Facts\n",
            "## Durable Facts\nNone.\n\n## Active State\n",
        )

        store = MemoryStore()
        store.load_from_disk()

        compressor = _compressor()
        compressor._last_raw_summary = summary_text

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        for i in range(30):
            messages.append({"role": "assistant", "content": f"work {i}"})
            messages.append({"role": "user", "content": f"followup {i}"})

        agent = MagicMock()
        agent.model = "test/model"
        agent.platform = "cli"
        agent.session_id = "test-sid"
        agent.tools = []
        agent._memory_store = store
        agent._memory_enabled = True
        agent._memory_manager = None
        agent.context_compressor = compressor
        agent._user_profile_enabled = False
        agent._compression_feasibility_checked = True
        agent.log_prefix = ""
        agent._cached_system_prompt = "cached system prompt"

        compressor.compress = lambda msgs, **kwargs: msgs[:1] + msgs[-3:]

        agent._session_db = MagicMock()
        agent._session_db.try_acquire_compression_lock.return_value = True
        agent._session_db.release_compression_lock = MagicMock()

        from agent.conversation_compression import compress_context

        compress_context(
            agent, messages, system_message="sys", approx_tokens=90_000, force=True,
        )

        # No MEMORY.md should exist (nothing to write).
        mem_file = tmp_path / "MEMORY.md"
        assert not mem_file.exists() or mem_file.read_text().strip() == ""

    def test_no_store_no_crash(self, tmp_path, monkeypatch):
        """When agent has no _memory_store, the block is a no-op."""
        monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)

        summary_text = _summary_with_facts("A fact that cannot be saved.")

        compressor = _compressor()
        compressor._last_raw_summary = summary_text

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        for i in range(30):
            messages.append({"role": "assistant", "content": f"work {i}"})
            messages.append({"role": "user", "content": f"followup {i}"})

        agent = MagicMock()
        agent.model = "test/model"
        agent.platform = "cli"
        agent.session_id = "test-sid"
        agent.tools = []
        agent._memory_store = None
        agent._memory_manager = None
        agent.context_compressor = compressor
        agent._compression_feasibility_checked = True
        agent.log_prefix = ""
        agent._cached_system_prompt = "cached system prompt"

        compressor.compress = lambda msgs, **kwargs: msgs[:1] + msgs[-3:]

        agent._session_db = MagicMock()
        agent._session_db.try_acquire_compression_lock.return_value = True
        agent._session_db.release_compression_lock = MagicMock()

        from agent.conversation_compression import compress_context

        # Must not raise.
        compress_context(
            agent, messages, system_message="sys", approx_tokens=90_000, force=True,
        )
