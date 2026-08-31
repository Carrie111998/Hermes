"""Tests for pure helper functions in ``tui_gateway.server``.

These helpers are module-level and side-effect free (no ``HERMES_HOME``, no
global state, no timers), so they are exercised directly by importing the
module and calling them. Only ``_redact_tui_verbose_text`` needs a
monkeypatch — it imports the redactor locally on each call.
"""

import json

import tui_gateway.server as server


# ── _fuzzy_basename_rank ────────────────────────────────────────────


def test_fuzzy_rank_empty_query_returns_substring_tier_with_length():
    assert server._fuzzy_basename_rank("app.tsx", "") == (3, 7)


def test_fuzzy_rank_exact_basename_is_tier_zero():
    assert server._fuzzy_basename_rank("appChrome.tsx", "appchrome.tsx") == (0, 13)


def test_fuzzy_rank_prefix_is_tier_one():
    assert server._fuzzy_basename_rank("appChrome.tsx", "app") == (1, 13)


def test_fuzzy_rank_camel_case_word_boundary_is_tier_two():
    assert server._fuzzy_basename_rank("appChrome.tsx", "chrome") == (2, 13)


def test_fuzzy_rank_word_boundary_after_separator_is_tier_two():
    # Lowercase query hits a part split on a dot/dash/underscore boundary.
    assert server._fuzzy_basename_rank("foo-bar_baz.qux", "bar") == (2, 15)


def test_fuzzy_rank_substring_is_tier_three():
    assert server._fuzzy_basename_rank("appChrome.tsx", "ome") == (3, 13)


def test_fuzzy_rank_subsequence_is_tier_four():
    # 's s t' appear in order but not contiguously in "someStr".
    assert server._fuzzy_basename_rank("someStr", "sst") == (4, 7)


def test_fuzzy_rank_no_match_returns_none():
    assert server._fuzzy_basename_rank("app.tsx", "zzz") is None


def test_fuzzy_rank_query_longer_than_name_returns_none():
    assert server._fuzzy_basename_rank("abc", "abcdef") is None


def test_fuzzy_rank_secondary_key_is_name_length_for_ties():
    # Same tier, shorter name must sort first (tier 0, secondary len).
    assert server._fuzzy_basename_rank("b", "b") == (0, 1)
    assert server._fuzzy_basename_rank("bb", "bb") == (0, 2)


# ── _coerce_message_text ────────────────────────────────────────────


def test_coerce_message_text_none_is_empty_string():
    assert server._coerce_message_text(None) == ""


def test_coerce_message_text_str_passthrough():
    assert server._coerce_message_text("hi") == "hi"


def test_coerce_message_text_numbers_become_str():
    assert server._coerce_message_text(42) == "42"
    assert server._coerce_message_text(True) == "True"
    assert server._coerce_message_text(1.5) == "1.5"


def test_coerce_message_text_list_of_strs_concatenates():
    assert server._coerce_message_text(["a", "b"]) == "ab"


def test_coerce_message_text_list_mixed_str_and_text_dict():
    assert server._coerce_message_text(["a", {"text": "b"}]) == "ab"


def test_coerce_message_text_appends_image_url_from_list():
    content = [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "http://x"}},
    ]
    assert server._coerce_message_text(content) == "hi\nhttp://x"


def test_coerce_message_text_image_without_url_uses_placeholder():
    assert server._coerce_message_text([{"type": "image_url", "image_url": {}}]) == "\n[image]"


def test_coerce_message_text_audio_block_uses_placeholder():
    assert server._coerce_message_text([{"type": "input_audio", "input_audio": {}}]) == "\n[audio]"


def test_coerce_message_text_unknown_block_kind_is_bracketed():
    assert server._coerce_message_text([{"type": "weird"}]) == "\n[weird]"


def test_coerce_message_text_dict_text_key():
    assert server._coerce_message_text({"text": "hello"}) == "hello"


def test_coerce_message_text_dict_image_url_returns_url():
    assert (
        server._coerce_message_text({"type": "image_url", "image_url": {"url": "http://y"}})
        == "http://y"
    )


def test_coerce_message_text_dict_audio_is_placeholder():
    assert server._coerce_message_text({"type": "input_audio", "input_audio": {}}) == "[audio]"


def test_coerce_message_text_dict_unknown_kind_is_bracketed():
    assert server._coerce_message_text({"type": "weird"}) == "[weird]"


def test_coerce_message_text_dict_fallback_is_structured_placeholder():
    assert server._coerce_message_text({"foo": 1}) == "[structured content]"


# ── _cap_tui_verbose_text ───────────────────────────────────────────


def test_cap_short_text_is_unchanged():
    assert server._cap_tui_verbose_text("hello") == "hello"


def test_cap_long_single_line_truncates_with_marker():
    long_text = "x" * 1100
    out = server._cap_tui_verbose_text(long_text)
    assert out.startswith("[showing verbose tail; omitted 100 chars]\n")
    # Tail (after the label) must fit the render budget.
    assert len(out) - len("[showing verbose tail; omitted 100 chars]\n") <= (
        server._TUI_VERBOSE_TEXT_MAX_CHARS
    )
    assert out.endswith("x" * server._TUI_VERBOSE_TEXT_MAX_CHARS)


def test_cap_many_lines_truncates_line_count():
    text = "\n".join(f"line{i}" for i in range(30))
    out = server._cap_tui_verbose_text(text)
    assert "[showing verbose tail; omitted" in out
    # Label line + at most MAX_LINES shown lines.
    assert out.count("\n") <= server._TUI_VERBOSE_TEXT_MAX_LINES + 1


# ── _redact_tui_verbose_text ────────────────────────────────────────


def test_redact_plain_text_passes_through(monkeypatch):
    # The real redactor leaves non-secret text unchanged; force=True is set.
    assert server._redact_tui_verbose_text("hello world plain") == "hello world plain"


def test_redact_forces_redaction_and_passes_through(monkeypatch):
    calls = []

    def _fake(text, *, force=False, **kwargs):
        calls.append((text, force))
        return "redacted"

    monkeypatch.setattr("agent.redact.redact_sensitive_text", _fake)
    out = server._redact_tui_verbose_text("some secret text")
    assert out == "redacted"
    assert calls == [("some secret text", True)]


def test_redact_error_returns_empty_string(monkeypatch):
    def _boom(text, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("agent.redact.redact_sensitive_text", _boom)
    assert server._redact_tui_verbose_text("secret") == ""


# ── _fmt_tool_duration ──────────────────────────────────────────────


def test_tool_duration_none_is_empty():
    assert server._fmt_tool_duration(None) == ""


def test_tool_duration_zero_is_one_decimal():
    assert server._fmt_tool_duration(0) == "0.0s"


def test_tool_duration_sub_ten_is_one_decimal():
    assert server._fmt_tool_duration(2.5) == "2.5s"


def test_tool_duration_under_a_minute_is_rounded_seconds():
    assert server._fmt_tool_duration(30.0) == "30s"


def test_tool_duration_exactly_a_minute_is_whole_minutes():
    assert server._fmt_tool_duration(60) == "1m"


def test_tool_duration_minutes_and_seconds():
    assert server._fmt_tool_duration(90) == "1m 30s"
    assert server._fmt_tool_duration(125) == "2m 5s"


def test_tool_duration_whole_minutes_omits_seconds():
    assert server._fmt_tool_duration(120) == "2m"
    assert server._fmt_tool_duration(300) == "5m"


# ── _tool_summary ───────────────────────────────────────────────────


def test_tool_summary_web_search_counts_results():
    result = json.dumps({"data": {"web": [{}, {}, {}]}})
    assert server._tool_summary("web_search", result, 1.2) == "Did 3 searches in 1.2s"


def test_tool_summary_web_search_singular():
    result = json.dumps({"data": {"web": [{}]}})
    assert server._tool_summary("web_search", result, None) == "Did 1 search"


def test_tool_summary_web_extract_counts_results():
    result = json.dumps({"results": [{}, {}]})
    assert server._tool_summary("web_extract", result, 2.0) == "Extracted 2 pages in 2.0s"


def test_tool_summary_web_extract_nested_results():
    result = json.dumps({"data": {"results": [{}]}})
    assert server._tool_summary("web_extract", result, 2.0) == "Extracted 1 page in 2.0s"


def test_tool_summary_unknown_or_non_json_returns_none():
    assert server._tool_summary("foo", "not json", 5.0) is None
    assert server._tool_summary("web_search", json.dumps({"data": {}}), None) is None


def test_tool_summary_fallback_warning_wins_over_count():
    result = json.dumps({"fallback_warning": "Rate limited"})
    assert server._tool_summary("web_search", result, 2.0) == "Rate limited in 2.0s"


# ── _is_text_only_busy_payload ──────────────────────────────────────


def test_busy_payload_scalars():
    assert server._is_text_only_busy_payload("hi") is True
    assert server._is_text_only_busy_payload(42) is True


def test_busy_payload_none_is_false():
    assert server._is_text_only_busy_payload(None) is False


def test_busy_payload_empty_list_is_false():
    assert server._is_text_only_busy_payload([]) is False


def test_busy_payload_list_of_text_parts_is_true():
    assert server._is_text_only_busy_payload(["a", "b"]) is True
    assert server._is_text_only_busy_payload([{"type": "text", "text": "hi"}]) is True
    assert server._is_text_only_busy_payload([{"text": "hi"}]) is True


def test_busy_payload_list_with_media_is_false():
    assert (
        server._is_text_only_busy_payload([{"type": "image_url", "image_url": {}}])
        is False
    )
    assert (
        server._is_text_only_busy_payload(
            [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {}}]
        )
        is False
    )


def test_busy_payload_non_dict_part_is_false():
    assert server._is_text_only_busy_payload([123]) is False


def test_busy_payload_dict_variants():
    assert server._is_text_only_busy_payload({"type": "text", "text": "hi"}) is True
    assert server._is_text_only_busy_payload({"text": "hi"}) is True
    assert server._is_text_only_busy_payload({"type": "image_url", "image_url": {}}) is False
    assert server._is_text_only_busy_payload({"type": "weird"}) is False


def test_busy_payload_other_object_is_false():
    assert server._is_text_only_busy_payload(object()) is False


# ── _coerce_seed_history ────────────────────────────────────────────


def test_seed_history_non_list_returns_empty():
    assert server._coerce_seed_history(None) == []
    assert server._coerce_seed_history("x") == []
    assert server._coerce_seed_history({}) == []


def test_seed_history_empty_list_returns_empty():
    assert server._coerce_seed_history([]) == []


def test_seed_history_keeps_valid_role_content():
    assert server._coerce_seed_history([{"role": "user", "content": "hi"}]) == [
        {"role": "user", "content": "hi"}
    ]


def test_seed_history_falls_back_to_text_key():
    assert server._coerce_seed_history([{"role": "assistant", "text": "yes"}]) == [
        {"role": "assistant", "content": "yes"}
    ]
    assert server._coerce_seed_history([{"role": "system", "text": "sys"}]) == [
        {"role": "system", "content": "sys"}
    ]


def test_seed_history_drops_invalid_types_and_roles():
    assert server._coerce_seed_history([{"role": "tool", "content": "x"}]) == []
    assert server._coerce_seed_history([1, {"role": "user", "content": "hi"}]) == [
        {"role": "user", "content": "hi"}
    ]


def test_seed_history_drops_blank_or_missing_content():
    assert server._coerce_seed_history([{"role": "user", "content": "   "}]) == []
    assert server._coerce_seed_history([{"role": "user"}]) == []


def test_seed_history_filters_and_orders_valid_members():
    history = [
        {"role": "user", "content": "a"},
        {"role": "tool", "content": "drop"},
        {"role": "assistant", "content": "b"},
        {"role": "system", "content": "c"},
    ]
    assert server._coerce_seed_history(history) == [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "system", "content": "c"},
    ]
