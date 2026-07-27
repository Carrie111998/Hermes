"""Tests for _split_concatenated_json_objects — detects models emitting N
complete JSON objects back-to-back in a single tool_call's arguments
instead of N separate tool_calls (observed with Gemini 3.5 Flash on both
built-in tools like search_files and MCP tools with single-item schemas)."""

import json

from run_agent import _split_concatenated_json_objects


class TestSplitConcatenatedJsonObjects:
    def test_two_simple_objects(self):
        raw = '{"path": "a.json"}{"path": "b.json"}'
        result = _split_concatenated_json_objects(raw)
        assert result == ['{"path": "a.json"}', '{"path": "b.json"}']

    def test_three_objects(self):
        raw = '{"id": "111"}{"id": "222"}{"id": "333"}'
        result = _split_concatenated_json_objects(raw)
        assert len(result) == 3
        assert [json.loads(r)["id"] for r in result] == ["111", "222", "333"]

    def test_whitespace_between_objects(self):
        raw = '{"a": 1}\n  {"b": 2}'
        result = _split_concatenated_json_objects(raw)
        assert result is not None
        assert len(result) == 2

    def test_nested_objects_within_each_item(self):
        raw = '{"data": {"nested": true}}{"data": {"nested": false}}'
        result = _split_concatenated_json_objects(raw)
        assert len(result) == 2
        assert json.loads(result[0])["data"]["nested"] is True
        assert json.loads(result[1])["data"]["nested"] is False

    # -- Must NOT match: single valid object --

    def test_single_object_returns_none(self):
        assert _split_concatenated_json_objects('{"a": 1}') is None

    # -- Must NOT match: genuinely truncated single object --

    def test_genuinely_truncated_object_returns_none(self):
        raw = (
            '{"dataPoints": [{"type": "Tech Stack"}, '
            '{"type": "Competitors"}],'
        )
        assert _split_concatenated_json_objects(raw) is None

    def test_empty_string_returns_none(self):
        assert _split_concatenated_json_objects("") is None

    def test_non_json_garbage_returns_none(self):
        assert _split_concatenated_json_objects("not json at all") is None

    def test_trailing_garbage_after_last_object_returns_none(self):
        assert _split_concatenated_json_objects('{"a": 1} trailing garbage') is None

    def test_unbalanced_extra_closing_brace_returns_none(self):
        assert _split_concatenated_json_objects('{"a": 1}}') is None

    # -- String-aware scanning: `}{ ` inside a string value must not be
    #    mistaken for an object boundary --

    def test_braces_inside_string_value_not_mistaken_for_boundary(self):
        raw = '{"a": "text with }{ inside a string"}{"b": 2}'
        result = _split_concatenated_json_objects(raw)
        assert result is not None
        assert len(result) == 2
        assert json.loads(result[0])["a"] == "text with }{ inside a string"
        assert json.loads(result[1])["b"] == 2

    def test_escaped_quote_inside_string_does_not_break_scanning(self):
        raw = r'{"a": "she said \"hi\""}{"b": 2}'
        result = _split_concatenated_json_objects(raw)
        assert result is not None
        assert len(result) == 2

    # -- Real-world production case: a file-read tool with two paths --

    def test_real_world_read_file_two_paths(self):
        raw = (
            '{"path": "./report_a.json"}'
            '{"path": "./report_b.json"}'
        )
        result = _split_concatenated_json_objects(raw)
        assert len(result) == 2
        paths = [json.loads(r)["path"] for r in result]
        assert paths == ["./report_a.json", "./report_b.json"]
