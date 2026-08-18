"""Tests for hindsight recall dedup — collapse restated facts, don't merge contradictions."""

import json
from types import SimpleNamespace

import pytest

from plugins.memory.hindsight import (
    HindsightMemoryProvider,
    _RecallResult,
    _dedup_recalled_texts,
    _recall_similarity,
)


# ---------------------------------------------------------------------------
# Similarity helper
# ---------------------------------------------------------------------------


class TestRecallSimilarity:
    def test_identical_facts_are_maximally_similar(self):
        assert _recall_similarity("user prefers dark mode", "user prefers dark mode") == 1.0

    def test_reordering_is_still_similar(self):
        assert _recall_similarity("dark mode user prefers", "user prefers dark mode") == 1.0

    def test_different_facts_are_clearly_dissimilar(self):
        assert _recall_similarity("user prefers dark mode", "the project is written in Python") < 0.5


# ---------------------------------------------------------------------------
# Pure dedup helper
# ---------------------------------------------------------------------------


class TestDedupRecalledTexts:
    def test_exact_restatements_collapse_to_first(self):
        out = _dedup_recalled_texts([
            "user prefers dark mode",
            "user prefers dark mode",
            "user prefers dark mode",
        ])
        assert out == ["user prefers dark mode"]

    def test_near_identical_rephrasing_collapses(self):
        out = _dedup_recalled_texts([
            "user prefers dark mode",
            "user prefers the dark color scheme",
        ])
        assert out == ["user prefers dark mode"]

    def test_distinct_facts_are_all_kept(self):
        out = _dedup_recalled_texts([
            "user prefers dark mode",
            "the project uses a SQLite database",
            "team meets on wednesdays",
        ])
        assert len(out) == 3

    def test_contradictory_near_duplicates_are_both_kept(self):
        # Near-identical but opposite polarity — a contradiction, not a restatement.
        out = _dedup_recalled_texts([
            "user prefers dark mode",
            "user does not prefer dark mode",
        ])
        assert len(out) == 2

    def test_negation_spelling_variants_do_not_merge(self):
        out = _dedup_recalled_texts([
            "user likes dark mode",
            "user doesn't like dark mode",
        ])
        assert len(out) == 2

    def test_order_is_preserved_first_wins(self):
        out = _dedup_recalled_texts([
            "first fact about cats",
            "second fact about dogs",
            "first fact about cats",  # duplicate of first, later — dropped
        ])
        assert out == ["first fact about cats", "second fact about dogs"]


# ---------------------------------------------------------------------------
# Integration: dedup runs on both recall paths
# ---------------------------------------------------------------------------


@pytest.fixture()
def provider(tmp_path, monkeypatch):
    from pathlib import Path

    config = {
        "mode": "cloud",
        "apiKey": "test-key",
        "api_url": "http://localhost:9999",
        "bank_id": "test-bank",
        "budget": "mid",
        "memory_mode": "hybrid",
    }
    config_path = tmp_path / "hindsight" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config))

    monkeypatch.setattr(
        "plugins.memory.hindsight.get_hermes_home", lambda: tmp_path
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user-home"))

    p = HindsightMemoryProvider()
    p.initialize(session_id="test-session", hermes_home=str(tmp_path), platform="cli")

    from unittest.mock import AsyncMock

    client = SimpleNamespace()
    client.arecall = AsyncMock(
        return_value=SimpleNamespace(
            results=[
                SimpleNamespace(text="user prefers dark mode"),
                SimpleNamespace(text="user prefers dark mode"),
                SimpleNamespace(text="user prefers dark mode"),
            ]
        )
    )
    p._client = client
    return p


class TestRecallToolDedup:
    def test_recall_tool_dedupes_restated_facts(self, provider):
        result = json.loads(provider.handle_tool_call("hindsight_recall", {"query": "dark"}))
        assert result["result"] == "1. user prefers dark mode"
        assert result["result"].count("dark mode") == 1


class TestRecallAutoInjectionDedup:
    def test_auto_injection_dedupes_and_collapses_other_facts(self, provider):
        # Rework the mock to return two distinct + one duplicate.
        from unittest.mock import AsyncMock

        provider._client.arecall = AsyncMock(
            return_value=SimpleNamespace(
                results=[
                    SimpleNamespace(text="fact alpha"),
                    SimpleNamespace(text="fact alpha"),
                    SimpleNamespace(text="fact beta"),
                ]
            )
        )
        res = provider._do_recall("alpha")
        assert isinstance(res, _RecallResult)
        assert res.count == 2  # fact alpha collapsed to 1, fact beta kept
        assert res.text.count("fact alpha") == 1
        assert res.text.count("fact beta") == 1

    def test_auto_injection_keeps_contradictions(self, provider):
        from unittest.mock import AsyncMock

        provider._client.arecall = AsyncMock(
            return_value=SimpleNamespace(
                results=[
                    SimpleNamespace(text="user prefers dark mode"),
                    SimpleNamespace(text="user does not prefer dark mode"),
                ]
            )
        )
        res = provider._do_recall("dark")
        assert res.count == 2  # contradiction preserved — no merge
        assert "does not prefer" in res.text
        assert "prefers dark" in res.text
