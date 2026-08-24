"""Regression tests: null-valued "error"/"failed" keys are not tool failures."""

import json

from agent.display import _detect_tool_failure


class TestDetectToolFailureNullishErrorKeys:
    """Null-valued "error"/"failed" keys in success payloads are not failures."""

    def test_nested_null_error_key_not_flagged(self):
        # web_extract success shape: every entry carries "error": null even
        # when the extraction succeeded.
        result = json.dumps(
            {
                "results": [
                    {
                        "url": "https://example.com",
                        "title": "Example Domain",
                        "content": "# Example Domain",
                        "error": None,
                    }
                ]
            }
        )
        assert _detect_tool_failure("web_extract", result) == (False, "")

    def test_nested_empty_string_error_not_flagged(self):
        result = json.dumps({"results": [{"url": "https://example.com", "error": ""}]})
        assert _detect_tool_failure("web_extract", result) == (False, "")

    def test_failed_false_not_flagged(self):
        result = json.dumps({"attempts": 1, "failed": False, "content": "ok"})
        assert _detect_tool_failure("some_tool", result) == (False, "")

    def test_null_error_key_with_spaces_not_flagged(self):
        result = '{"results": [{"url": "https://example.com", "error" : null}]}'
        assert _detect_tool_failure("web_extract", result) == (False, "")

    def test_real_nested_error_string_still_flagged(self):
        result = json.dumps(
            {"results": [{"url": "https://blocked.example", "error": "HTTP 500"}]}
        )
        is_failure, suffix = _detect_tool_failure("web_extract", result)
        assert is_failure is True
        assert suffix == " [error]"

    def test_failed_true_still_flagged(self):
        result = json.dumps({"failed": True, "content": ""})
        assert _detect_tool_failure("some_tool", result) == (True, " [error]")

    def test_empty_error_array_not_flagged(self):
        result = json.dumps({"results": [{"url": "https://example.com", "error": []}]})
        assert _detect_tool_failure("web_extract", result) == (False, "")

    def test_empty_error_object_not_flagged(self):
        result = json.dumps({"results": [{"url": "https://example.com", "error": {}}]})
        assert _detect_tool_failure("web_extract", result) == (False, "")

    def test_nested_structured_error_object_still_flagged(self):
        result = json.dumps(
            {"results": [{"url": "https://x.example", "error": {"code": 500}}]}
        )
        is_failure, suffix = _detect_tool_failure("web_extract", result)
        assert is_failure is True
        assert suffix == " [error]"

    def test_nullish_prefix_word_continuation_not_masked(self):
        # Not valid JSON, but the heuristic must not strip a partial match:
        # "nullPointerException" is not the nullish "null" value.
        result = '{"results": [{"url": "u", "error": nullPointerException}]}'
        assert _detect_tool_failure("web_extract", result) == (True, " [error]")
