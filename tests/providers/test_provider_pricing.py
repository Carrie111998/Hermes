"""Unit tests for hermes_cli/provider_pricing.py — no network, no heavy deps.

Fixtures mirror the real OpenRouter /api/v1/models response shape (verified
against the live endpoint: pricing is USD per TOKEN as strings, context
length in tokens, supported_parameters carries "tools"/"reasoning_effort").
"""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import provider_pricing as pp


# ---------------------------------------------------------------------------
# Fixtures (realistic OpenRouter /api/v1/models entries)
# ---------------------------------------------------------------------------

AGENTIC_MODEL = {
    "id": "qwen/qwen3.8-27b",
    "name": "Qwen: Qwen3.8 27B",
    "context_length": 262144,
    "pricing": {"prompt": "0.00000045", "completion": "0.0000032"},
    "supported_parameters": [
        "frequency_penalty",
        "include_reasoning",
        "max_tokens",
        "reasoning",
        "reasoning_effort",
        "structured_outputs",
        "temperature",
        "tool_choice",
        "tools",
        "top_p",
    ],
    "reasoning": {"supported_efforts": ["xhigh", "medium", "low"]},
}

FREE_MODEL = {
    "id": "dots-studio/dots-3-note-preview:free",
    "name": "Dots Studio: Dots3-Note Preview (free)",
    "context_length": 512000,
    "pricing": {"prompt": "0", "completion": "0"},
    "supported_parameters": ["max_tokens", "tools"],
}

NON_AGENTIC_MODEL = {
    "id": "openai/text-embedding-3-large",
    "name": "OpenAI: text-embedding-3-large",
    "context_length": 8191,
    "pricing": {"prompt": "0.00000013", "completion": "0"},
    "supported_parameters": ["max_tokens", "input_audio"],
}

RAW_PAYLOAD = {"data": [AGENTIC_MODEL, FREE_MODEL, NON_AGENTIC_MODEL, "garbage"]}


def make_models_dev_info(**overrides):
    """Minimal ModelInfo stand-in (only the attrs we read)."""
    base = dict(
        id="deepseek/deepseek-chat",
        name="DeepSeek: DeepSeek Chat",
        context_window=64000,
        cost_input=0.27,
        cost_output=1.10,
        tool_call=True,
        reasoning=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _n(item: dict) -> dict:
    """Normalize one raw entry, asserting it survived (never None in fixtures)."""
    row = pp.normalize_from_openrouter(item)
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalizeOpenRouter:
    def test_full_entry(self):
        row = _n(AGENTIC_MODEL)
        assert row["id"] == "qwen/qwen3.8-27b"
        assert row["lab"] == "qwen"
        assert row["context"] == 262144
        # 0.00000045 USD/token == 0.45 USD per 1M tokens.
        assert row["in"] == pytest.approx(0.45)
        assert row["out"] == pytest.approx(3.2)
        assert row["agentic"] is True
        assert row["reasoning"] is True

    def test_free_entry(self):
        row = _n(FREE_MODEL)
        assert row["in"] == 0.0
        assert row["out"] == 0.0
        assert row["agentic"] is True

    def test_non_agentic_entry(self):
        row = _n(NON_AGENTIC_MODEL)
        assert row["agentic"] is False
        assert row["reasoning"] is False

    def test_missing_id_returns_none(self):
        assert pp.normalize_from_openrouter({"name": "no id"}) is None

    def test_payload_skips_garbage_and_keeps_dicts(self):
        rows = pp.normalize_from_openrouter_payload(RAW_PAYLOAD["data"])
        assert len(rows) == 3
        ids = {r["id"] for r in rows}
        assert ids == {
            "qwen/qwen3.8-27b",
            "dots-studio/dots-3-note-preview:free",
            "openai/text-embedding-3-large",
        }

    def test_parse_price_edge_cases(self):
        assert pp._parse_price("0.00000045") == pytest.approx(0.45)
        assert pp._parse_price("0") == 0.0
        assert pp._parse_price(None) == 0.0
        assert pp._parse_price("") == 0.0
        assert pp._parse_price("garbage") == 0.0
        assert pp._parse_price("-5") == 0.0
        assert pp._parse_price("nan") == 0.0


class TestNormalizeModelsDev:
    def test_full_info(self):
        row = pp.normalize_from_models_dev(make_models_dev_info())
        assert row["id"] == "deepseek/deepseek-chat"
        assert row["lab"] == "deepseek"
        assert row["context"] == 64000
        assert row["in"] == pytest.approx(0.27)
        assert row["out"] == pytest.approx(1.10)
        assert row["agentic"] is True
        assert row["reasoning"] is False

    def test_none_returns_none(self):
        assert pp.normalize_from_models_dev(None) is None


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


@pytest.fixture
def mixed_rows():
    """Cheap reasoning model, pricier chat model, free non-agentic model."""
    return [
        _n(AGENTIC_MODEL),  # $3.65/M, reasoning
        {
            "id": "meta/llama-4-scout",
            "lab": "meta",
            "name": "Meta: Llama 4 Scout",
            "context": 100000,
            "in": 0.10,
            "out": 0.30,
            "agentic": True,
            "reasoning": False,
        },
        {
            "id": "cohere/command-r",
            "lab": "cohere",
            "name": "Cohere: Command R",
            "context": 128000,
            "in": 0.15,
            "out": 0.60,
            "agentic": True,
            "reasoning": False,
        },
        _n(FREE_MODEL),  # free, agentic
        _n(NON_AGENTIC_MODEL),  # cheap but not agentic
        {
            "id": "openai/embed-x",
            "lab": "openai",
            "name": "embed",
            "context": 8192,
            "in": 0.0,
            "out": 0.0,
            "agentic": False,
            "reasoning": False,
        },
    ]


class TestRankByValue:
    def test_excludes_non_agentic_by_default(self, mixed_rows):
        ranked = pp.rank_by_value(mixed_rows)
        ids = [r["id"] for r in ranked]
        assert "openai/text-embedding-3-large" not in ids
        assert "openai/embed-x" not in ids

    def test_free_first_then_cost_ascending(self, mixed_rows):
        ranked = pp.rank_by_value(mixed_rows)
        assert ranked[0]["id"] == "dots-studio/dots-3-note-preview:free"
        ids = [r["id"] for r in ranked]
        # llama (0.40/M) and cohere (0.75/M) both beat qwen (3.65/M).
        assert ids.index("meta/llama-4-scout") < ids.index("qwen/qwen3.8-27b")
        assert ids.index("cohere/command-r") < ids.index("qwen/qwen3.8-27b")

    def test_include_all_keeps_non_agentic(self, mixed_rows):
        ranked = pp.rank_by_value(mixed_rows, include_all=True)
        ids = {r["id"] for r in ranked}
        assert "openai/text-embedding-3-large" in ids

    def test_min_context_filter(self, mixed_rows):
        ranked = pp.rank_by_value(mixed_rows, min_context=150000)
        ids = {r["id"] for r in ranked}
        assert "qwen/qwen3.8-27b" in ids
        assert "dots-studio/dots-3-note-preview:free" in ids
        assert "meta/llama-4-scout" not in ids

    def test_task_reasoning(self, mixed_rows):
        ranked = pp.rank_by_value(mixed_rows, task="reasoning")
        ids = [r["id"] for r in ranked]
        assert ids == ["qwen/qwen3.8-27b"]

    def test_task_code_filters_by_lab(self, mixed_rows):
        ranked = pp.rank_by_value(mixed_rows, task="code")
        ids = {r["id"] for r in ranked}
        assert "meta/llama-4-scout" in ids
        assert "qwen/qwen3.8-27b" in ids  # qwen IS a coding-lab model
        assert "cohere/command-r" not in ids  # cohere is not
        assert "dots-studio/dots-3-note-preview:free" not in ids

    def test_top_truncates(self, mixed_rows):
        ranked = pp.rank_by_value(mixed_rows, top=2)
        assert len(ranked) == 2

    def test_empty_input(self):
        assert pp.rank_by_value([]) == []


class TestSearch:
    def test_substring_on_id(self, mixed_rows):
        hits = pp.search_models(mixed_rows, "llama-4")
        assert [r["id"] for r in hits] == ["meta/llama-4-scout"]

    def test_case_insensitive_on_name(self, mixed_rows):
        hits = pp.search_models(mixed_rows, "QWEN3.8")
        assert [r["id"] for r in hits] == ["qwen/qwen3.8-27b"]

    def test_empty_query_returns_empty(self, mixed_rows):
        assert pp.search_models(mixed_rows, "") == []
        assert pp.search_models(mixed_rows, "   ") == []

    def test_no_match(self, mixed_rows):
        assert pp.search_models(mixed_rows, "zzz-not-there") == []


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


class TestFormat:
    def test_fmt_cost(self):
        assert pp.fmt_cost(0.0) == "free"
        assert pp.fmt_cost(0.45) == "$0.45"
        assert pp.fmt_cost(3.2) == "$3.20"
        assert pp.fmt_cost(0.0018) == "$0.0018"

    def test_format_rows_aligns_and_marks_caps(self, mixed_rows):
        lines = pp.format_rows(mixed_rows[:4])
        joined = "\n".join(lines)
        assert "MODEL" in joined and "CONTEXT" in joined
        assert "qwen/qwen3.8-27b" in joined
        assert "262,144" in joined
        assert "tools+reasoning" in joined
        assert "free" in joined

    def test_format_rows_empty(self):
        assert pp.format_rows([]) == ["  (no models match)"]


# ---------------------------------------------------------------------------
# Fetch (fail-soft + JSON parsing, no real network)
# ---------------------------------------------------------------------------


class TestFetch:
    def test_fail_soft_on_network_error(self, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("no network")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        assert pp.fetch_openrouter_models() == []

    def test_parses_payload(self, monkeypatch):
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(RAW_PAYLOAD).encode()

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
        rows = pp.fetch_openrouter_models()
        assert len(rows) == 3
