"""Tests for the in-place tool-result prune (``compression.tool_result_prune``).

Oversized tool results (``role="tool"``, content > threshold_chars) are
pruned in their OWN message node — same position, same role, same
``tool_call_id`` — down to head + marker + tail, as a no-LLM pre-pass that
runs BEFORE the summarization region is selected in the in-loop compression
(``ContextCompressor.compress``). The commit rewrites the persisted
transcript through the same ``archive_and_compact`` mechanism as in-place
compaction, so the reclaimed state is durable even when no summarization
follows.

Mirrors the construction/patching conventions in
test_proactive_tool_result_pruning.py.
"""

from unittest.mock import patch

from agent.context_compressor import (
    PRUNE_MARKER,
    _DB_PERSISTED_MARKER,
    _TOOL_RESULT_PRUNE_DEFAULTS,
    ContextCompressor,
    prune_tool_result_content,
    resolve_tool_result_prune_config,
)

LARGE_WINDOW = 1_000_000
BIG_CHARS = 9200  # > default threshold 8192; ~2300 rough tokens when ASCII


def _compressor(**kw):
    defaults = dict(
        model="test",
        quiet_mode=True,
        threshold_percent=0.50,
        protect_first_n=2,
        protect_last_n=2,
    )
    defaults.update(kw)
    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=LARGE_WINDOW,
    ):
        c = ContextCompressor(**defaults)
        # Context length is resolved lazily on first access (outside the
        # patch context); pre-set it so every derived budget is deterministic.
        c._resolved_context_length = LARGE_WINDOW
    return c


def _assistant_call(cid, name="terminal", args='{"cmd":"ls"}'):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": name, "arguments": args}}
        ],
    }


def _tool_msg(cid, content):
    return {"role": "tool", "tool_call_id": cid, "content": content}


def _build(n_big, tail_pairs=2, big_chars=BIG_CHARS):
    """system + n_big oversized tool-result pairs + tail pairs + final user."""
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n_big):
        cid = f"big_{i}"
        msgs.append(_assistant_call(cid))
        msgs.append(_tool_msg(cid, chr(65 + (i % 26)) * big_chars))
    for i in range(tail_pairs):
        cid = f"tail_{i}"
        msgs.append(_assistant_call(cid))
        msgs.append(_tool_msg(cid, "ok"))
    msgs.append({"role": "user", "content": "final question"})
    return msgs


def _tool_by_id(msgs, cid):
    return [
        m for m in msgs
        if m.get("role") == "tool" and m.get("tool_call_id") == cid
    ][0]


class _FakeSessionDB:
    """Minimal session store recording archive_and_compact calls."""

    def __init__(self, raise_on_compact=False):
        self.calls = []
        self.raise_on_compact = raise_on_compact

    def archive_and_compact(self, session_id, messages, **kwargs):
        if self.raise_on_compact:
            raise RuntimeError("boom")
        self.calls.append((session_id, list(messages), kwargs))


# ---------------------------------------------------------------------------
# Pure function: prune_tool_result_content
# ---------------------------------------------------------------------------


def test_within_budget_returns_input_object():
    content = "x" * 100
    assert prune_tool_result_content(content, 8192, 4096, 1024) is content
    parts = [{"type": "text", "text": "y" * 50}]
    assert prune_tool_result_content(parts, 8192, 4096, 1024) is parts


def test_exact_threshold_unchanged():
    content = "x" * 8192
    assert prune_tool_result_content(content, 8192, 4096, 1024) is content


def test_head_tail_marker_shape():
    head, tail = "HEAD" * 1024, "TAIL" * 256
    content = head + ("M" * 10_000) + tail
    out = prune_tool_result_content(content, 8192, 4096, 1024)
    assert out.startswith(head)
    assert out.endswith(tail)
    assert out.count(PRUNE_MARKER) == 1
    # Marker is a standalone line between blank lines.
    assert PRUNE_MARKER.startswith("\n\n") and PRUNE_MARKER.endswith("\n\n")
    assert out[len(head):].startswith(PRUNE_MARKER)
    removed = "M" * 10_000
    assert removed not in out
    assert len(out) == 4096 + len(PRUNE_MARKER) + 1024


def test_unicode_astral_code_point_slicing():
    # Each emoji is ONE Python code point (no surrogate pairs in str).
    content = "😀" * 10_000
    out = prune_tool_result_content(content, 8192, 4096, 1024)
    assert out == ("😀" * 4096) + PRUNE_MARKER + ("😀" * 1024)
    # Slicing by code point can never split a surrogate pair: every retained
    # boundary is a full astral character.
    assert set(out.replace(PRUNE_MARKER, "")) == {"😀"}
    # Mixed CJK + ASCII keeps the same code-point semantics.
    mixed = "漢" * 5000 + "a" * 5000
    out2 = prune_tool_result_content(mixed, 8192, 4096, 1024)
    assert out2 == ("漢" * 4096) + PRUNE_MARKER + ("a" * 1024)


def test_zero_head_and_tail_budgets():
    content = "x" * 10_000
    out = prune_tool_result_content(content, 8192, 0, 0)
    assert out == PRUNE_MARKER
    out2 = prune_tool_result_content(content, 8192, 0, 1024)
    assert out2 == PRUNE_MARKER + ("x" * 1024)


def test_list_content_prunes_text_keeps_other_parts():
    parts = [
        {"type": "text", "text": "A" * 6000},
        {"type": "image_url", "image_url": {"url": "data:img"}},
        {"type": "text", "text": "B" * 6000},
        {"type": "text", "text": "C" * 6000},
    ]
    out = prune_tool_result_content(parts, 8192, 4096, 1024)
    # Non-text part survives byte-identical, in the same position.
    assert out[1] is parts[1]
    # Text total (18_000) > threshold → span [4096, 16_976) removed; the
    # marker lands in the first intersecting text part.
    total_text = "".join(
        p["text"] for p in out if isinstance(p, dict) and p.get("type") == "text"
    )
    assert total_text == ("A" * 4096) + PRUNE_MARKER + ("C" * 1024)
    # Pruned output is within budget and smaller than the input.
    text_after = sum(
        len(p["text"]) for p in out
        if isinstance(p, dict) and p.get("type") == "text"
    )
    assert text_after == 4096 + len(PRUNE_MARKER) + 1024 <= 8192


def test_list_content_within_budget_unchanged():
    parts = [{"type": "text", "text": "x" * 100}, {"type": "image_url", "image_url": {"url": "u"}}]
    assert prune_tool_result_content(parts, 8192, 4096, 1024) is parts


def test_unknown_shapes_never_pruned():
    for shape in (None, 42, {"_multimodal": True, "content": "x" * 20_000}):
        assert prune_tool_result_content(shape, 8192, 4096, 1024) is shape


def test_idempotent():
    content = "x" * 20_000
    once = prune_tool_result_content(content, 8192, 4096, 1024)
    twice = prune_tool_result_content(once, 8192, 4096, 1024)
    assert twice is once  # already within budget → byte-identical, no rewrite


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_config_defaults_disabled():
    assert resolve_tool_result_prune_config(None) == (
        False, 8192, 4096, 1024,
    )
    assert resolve_tool_result_prune_config({}) == (False, 8192, 4096, 1024)


def test_config_custom_values():
    assert resolve_tool_result_prune_config(
        {"enabled": True, "threshold_chars": 4096, "head_chars": 2048, "tail_chars": 512}
    ) == (True, 4096, 2048, 512)


def test_config_unsatisfiable_budget_disables():
    # head + marker + tail would exceed threshold → feature disabled.
    resolved = resolve_tool_result_prune_config(
        {"enabled": True, "threshold_chars": 100, "head_chars": 200, "tail_chars": 50}
    )
    assert resolved == (False, 8192, 4096, 1024)


def test_config_boolean_and_fractional_rejected():
    # bool subclasses int — YAML `threshold_chars: true` must not coerce to 1.
    assert resolve_tool_result_prune_config(
        {"enabled": True, "threshold_chars": True}
    )[1] == 8192
    # Fractional floats are rejected, not truncated.
    assert resolve_tool_result_prune_config(
        {"enabled": True, "head_chars": 1.5}
    )[2] == 4096
    # Integral floats and numeric strings are accepted.
    assert resolve_tool_result_prune_config(
        {"enabled": True, "threshold_chars": 4096.0, "head_chars": "2048"}
    )[1:3] == (4096, 2048)


def test_compressor_defaults_disabled():
    c = _compressor()
    assert c._tool_result_prune_enabled is False
    assert c._tool_result_prune_threshold_chars == _TOOL_RESULT_PRUNE_DEFAULTS["threshold_chars"]


def test_compressor_honors_config():
    c = _compressor(tool_result_prune={"enabled": True, "tail_chars": 256})
    assert c._tool_result_prune_enabled is True
    assert c._tool_result_prune_tail_chars == 256


def test_default_config_has_conservative_prune_defaults():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    trp = DEFAULT_CONFIG["compression"]["tool_result_prune"]
    assert trp["enabled"] is False
    assert trp["threshold_chars"] == 8192
    assert trp["head_chars"] == 4096
    assert trp["tail_chars"] == 1024


def test_agent_init_plumbs_tool_result_prune(monkeypatch, tmp_path):
    """End-to-end config seam: compression.tool_result_prune reaches the
    built-in compressor through agent_init (mirrors
    test_proactive_prune_config.py)."""
    import contextlib
    import io

    from hermes_cli import config as config_mod
    from hermes_state import SessionDB
    from run_agent import AIAgent

    compression = {
        "enabled": True,
        "threshold": 0.50,
        "target_ratio": 0.20,
        "protect_first_n": 3,
        "protect_last_n": 20,
        "tool_result_prune": {"enabled": True, "tail_chars": 256},
    }
    cfg = {
        "compression": compression,
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
    }
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: cfg)
    db = SessionDB(db_path=tmp_path / "state.db")
    with contextlib.redirect_stdout(io.StringIO()):
        agent = AIAgent(
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="test-key",
            provider="openai-codex",
            model="gpt-5.5",
            enabled_toolsets=[],
            disabled_toolsets=[],
            quiet_mode=True,
            skip_memory=True,
            session_db=db,
            session_id="tool-result-prune-config-test",
        )
    cc = agent.context_compressor
    assert cc._tool_result_prune_enabled is True
    assert cc._tool_result_prune_tail_chars == 256
    assert cc._tool_result_prune_threshold_chars == 8192


# ---------------------------------------------------------------------------
# Method: _prune_tool_results_in_place
# ---------------------------------------------------------------------------


def test_in_place_keeps_node_identity_and_tail_protection():
    c = _compressor(tool_result_prune={"enabled": True})
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(3):
        cid = f"old_{i}"
        msgs.append(_assistant_call(cid))
        msgs.append(_tool_msg(cid, chr(65 + i) * 9000))
    # Protected tail: a recent BIG result must stay verbatim.
    msgs.append(_assistant_call("recent"))
    msgs.append(_tool_msg("recent", "R" * 9000))
    msgs.append({"role": "user", "content": "final"})

    snapshot = [dict(m) for m in msgs]
    result, pruned = c._prune_tool_results_in_place(msgs, protect_tail_count=2)
    assert pruned == 3
    assert len(result) == len(msgs)
    for cid in ("old_0", "old_1", "old_2"):
        m = _tool_by_id(result, cid)
        assert m.get("role") == "tool"
        assert m.get("tool_call_id") == cid
        assert PRUNE_MARKER in m["content"]
        assert m["content"].startswith(chr(65 + int(cid[-1])) * 4096)
        assert m["content"].endswith(chr(65 + int(cid[-1])) * 1024)
    # Recent tail result untouched.
    assert _tool_by_id(result, "recent")["content"] == "R" * 9000
    # Non-tool messages untouched.
    assert result[0] == snapshot[0]
    assert result[1]["content"] == snapshot[1]["content"]
    assert result[-1] == snapshot[-1]
    # Input never mutated.
    assert msgs == snapshot


def test_in_place_drops_stale_api_content_sidecar():
    c = _compressor(tool_result_prune={"enabled": True})
    msgs = [
        {"role": "system", "content": "sys"},
        _assistant_call("c1"),
        {**_tool_msg("c1", "A" * 9000), "api_content": "pre-rewrite bytes"},
        _assistant_call("c2"),
        _tool_msg("c2", "ok"),
    ]
    result, pruned = c._prune_tool_results_in_place(msgs, protect_tail_count=2)
    assert pruned == 1
    pruned_msg = _tool_by_id(result, "c1")
    assert "api_content" not in pruned_msg


def test_in_place_disabled_noop_contract():
    c = _compressor()  # default: disabled
    msgs = _build(3)
    result, pruned = c._prune_tool_results_in_place(msgs, protect_tail_count=2)
    assert pruned == 0
    assert result is msgs


def test_in_place_persists_via_archive_and_compact():
    c = _compressor(tool_result_prune={"enabled": True})
    db = _FakeSessionDB()
    c.bind_session_state(session_db=db, session_id="s1")
    msgs = _build(3, tail_pairs=1)
    result, pruned = c._prune_tool_results_in_place(
        msgs, protect_tail_count=2, protect_tail_tokens=2000,
    )
    assert pruned >= 1
    assert len(db.calls) == 1
    sid, persisted, _ = db.calls[0]
    assert sid == "s1"
    assert any(
        isinstance(m.get("content"), str) and PRUNE_MARKER in m["content"]
        for m in persisted
    )
    # Persisted rows are stamped so the next append-only flush skips them.
    assert any(m.get(_DB_PERSISTED_MARKER) is True for m in result)


def test_in_place_persist_failure_rolls_back_to_input():
    c = _compressor(tool_result_prune={"enabled": True})
    db = _FakeSessionDB(raise_on_compact=True)
    c.bind_session_state(session_db=db, session_id="s1")
    msgs = _build(3, tail_pairs=1)
    result, pruned = c._prune_tool_results_in_place(
        msgs, protect_tail_count=2, protect_tail_tokens=10_000,
    )
    assert pruned == 0
    assert result is msgs  # never commit an in-memory prune the DB can't back


def test_in_place_store_without_capability_is_noop():
    c = _compressor(tool_result_prune={"enabled": True})
    c.bind_session_state(session_db=object(), session_id="s1")  # no archive_and_compact
    msgs = _build(3, tail_pairs=1)
    result, pruned = c._prune_tool_results_in_place(
        msgs, protect_tail_count=2, protect_tail_tokens=10_000,
    )
    assert pruned == 0
    assert result is msgs


def test_in_place_without_session_db_commits_in_memory():
    # No bound store → no flush exists to desync → the in-memory prune lands.
    c = _compressor(tool_result_prune={"enabled": True})
    msgs = _build(3, tail_pairs=1)
    result, pruned = c._prune_tool_results_in_place(
        msgs, protect_tail_count=2, protect_tail_tokens=2000,
    )
    assert pruned >= 1
    assert result is not msgs


# ---------------------------------------------------------------------------
# Integration: compress() runs the prune BEFORE region selection/summarization
# ---------------------------------------------------------------------------


def test_prune_runs_before_region_selection_and_summarizer():
    c = _compressor(tool_result_prune={"enabled": True})
    c.tail_token_budget = 12_000
    db = _FakeSessionDB()
    c.bind_session_state(session_db=db, session_id="s1")
    msgs = _build(22)

    region_inputs = []
    orig_tail_cut = c._find_tail_cut_by_tokens
    def spy_tail_cut(messages, head_end, token_budget=None):
        region_inputs.append(list(messages))
        return orig_tail_cut(messages, head_end, token_budget)
    c._find_tail_cut_by_tokens = spy_tail_cut

    summary_inputs = []
    def fake_summary(turns, focus_topic=None, memory_context=""):
        summary_inputs.append(list(turns))
        return "the summary"
    c._generate_summary = fake_summary

    out = c.compress(msgs, current_tokens=400_000, force=False)

    # Prune landed and persisted before anything else.
    assert db.calls, "prune must rewrite the persisted history"
    assert any(
        isinstance(m.get("content"), str) and PRUNE_MARKER in m["content"]
        for m in db.calls[0][1]
    )
    # Region selection saw the pruned transcript (prune BEFORE region selection).
    assert region_inputs, "region selection must run after the prune"
    assert any(
        isinstance(m.get("content"), str) and PRUNE_MARKER in m["content"]
        for m in region_inputs[0]
    )
    # Summarizer received the pruned turns (prune BEFORE summarization).
    assert len(summary_inputs) == 1
    assert any(
        isinstance(m.get("content"), str) and PRUNE_MARKER in m["content"]
        for m in summary_inputs[0]
    )
    # Summary present in the compressed output; a pruned row survives in the
    # protected tail with its node identity intact.
    assert any("the summary" in str(m.get("content")) for m in out)
    survivor = next(
        m for m in out
        if isinstance(m.get("content"), str) and PRUNE_MARKER in m["content"]
    )
    assert survivor.get("role") == "tool"
    assert survivor.get("tool_call_id", "").startswith("big_")
    # No persistence marker may leave compress() (the terminal sweep strips it).
    assert all(m.get(_DB_PERSISTED_MARKER) is not True for m in out)


def test_prune_can_skip_summarization_via_feasibility_guard():
    """With a prior ineffectiveness strike, a pruned middle below 10% of the
    threshold skips the LLM summarizer entirely (deterministic drop)."""
    c = _compressor(tool_result_prune={"enabled": True})
    c.tail_token_budget = 12_000
    db = _FakeSessionDB()
    c.bind_session_state(session_db=db, session_id="s1")
    # bind_session_state resets the strike counter; arm the guard AFTER it.
    c._ineffective_compression_count = 1
    msgs = _build(22)

    calls = []
    def fake_summary(turns, focus_topic=None, memory_context=""):
        calls.append(1)
        return "the summary"
    c._generate_summary = fake_summary

    out = c.compress(msgs, current_tokens=400_000, force=False)

    assert calls == []  # summarization avoided
    assert c._last_feasibility_skip is True
    assert db.calls  # the prune itself still persisted
    assert any(
        isinstance(m.get("content"), str) and PRUNE_MARKER in m["content"]
        for m in out
    )
    assert len(out) < len(msgs)  # deterministic drop still reclaimed


def test_prune_then_summarize_without_strike_still_runs():
    """No prior strike → feasibility guard off → summarization still runs,
    on the pruned transcript."""
    c = _compressor(tool_result_prune={"enabled": True})
    c.tail_token_budget = 12_000
    c._ineffective_compression_count = 0
    db = _FakeSessionDB()
    c.bind_session_state(session_db=db, session_id="s1")
    msgs = _build(22)

    calls = []
    def fake_summary(turns, focus_topic=None, memory_context=""):
        calls.append(1)
        return "the summary"
    c._generate_summary = fake_summary

    out = c.compress(msgs, current_tokens=400_000, force=False)

    assert len(calls) == 1
    assert c._last_feasibility_skip is False
    assert db.calls
    assert any("the summary" in str(m.get("content")) for m in out)


def test_default_off_compress_unchanged_and_no_persist():
    """Default-off pin: without tool_result_prune, compress() runs the
    historical path — no marker, no prune-originated archive_and_compact."""
    c = _compressor()  # nothing configured
    c.tail_token_budget = 12_000
    db = _FakeSessionDB()
    c.bind_session_state(session_db=db, session_id="s1")
    msgs = _build(22)

    calls = []
    def fake_summary(turns, focus_topic=None, memory_context=""):
        calls.append(1)
        return "the summary"
    c._generate_summary = fake_summary

    out = c.compress(msgs, current_tokens=400_000, force=False)

    assert len(calls) == 1
    assert db.calls == []  # the in-place prune never ran → no persist
    for m in out:
        if isinstance(m.get("content"), str):
            assert PRUNE_MARKER not in m["content"]
