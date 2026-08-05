"""Tests for _repair_tool_call_arguments — malformed JSON repair pipeline."""

import json

from run_agent import _repair_tool_call_arguments


class TestRepairToolCallArguments:
    """Verify each repair stage in the pipeline."""

    # -- Stage 1: empty / whitespace-only --

    def test_empty_string_returns_empty_object(self):
        assert _repair_tool_call_arguments("", "t") == "{}"



    # -- Stage 2: Python None literal --



    # -- Stage 3: trailing comma repair --


    def test_trailing_comma_in_array(self):
        result = _repair_tool_call_arguments('{"a": [1, 2,]}', "t")
        parsed = json.loads(result)
        assert parsed == {"a": [1, 2]}


    # -- Stage 4: unclosed brackets --



    # -- Stage 5: excess closing delimiters --



    # -- Stage 6: last resort --


    def test_unrepairable_partial_returns_empty_object(self):
        # Truncated in the middle of a string key — bracket closing won't help
        assert _repair_tool_call_arguments('{"truncated": "val', "t") == "{}"

    # -- Valid JSON passthrough (this path is via except, but still works) --


    # -- Combined repairs --



    # -- Stage 0: strict=False (literal control chars in strings) --
    # llama.cpp backends sometimes emit literal tabs/newlines inside JSON
    # string values. strict=False accepts these; we re-serialise to the
    # canonical wire form (#12068).




    # -- Stage 4: control-char escape fallback --




class TestWrappedArgumentRecovery:
    """A complete JSON payload that merely arrived wrapped must be recovered.

    Both shapes below are ordinary model behaviour and carry the arguments the
    model actually chose; falling through to "{}" runs the tool with no
    arguments instead of the requested ones.
    """

    @staticmethod
    def _repair(raw):
        from agent.message_sanitization import _repair_tool_call_arguments

        return _repair_tool_call_arguments(raw, "write_file")

    def test_markdown_fenced_payload_is_recovered(self):
        out = self._repair('```json\n{"path": "a.txt", "content": "hi"}\n```')
        assert json.loads(out) == {"path": "a.txt", "content": "hi"}

    def test_bare_fence_without_language_is_recovered(self):
        out = self._repair('```\n{"path": "a.txt"}\n```')
        assert json.loads(out) == {"path": "a.txt"}

    def test_trailing_prose_after_the_object_is_dropped(self):
        out = self._repair('{"path": "a.txt"} — writing that file now')
        assert json.loads(out) == {"path": "a.txt"}

    def test_leading_and_trailing_whitespace_only(self):
        out = self._repair('  \n {"path": "a.txt"}  \n ')
        assert json.loads(out) == {"path": "a.txt"}

    def test_wellformed_payload_is_unchanged_semantically(self):
        payload = {
            "path": r"C:\Users\me\a.txt",
            "content": "line1\nline2 🌍",
        }
        out = self._repair(json.dumps(payload))
        assert json.loads(out) == payload

    def test_still_empty_object_when_nothing_parses(self):
        assert json.loads(self._repair("{path: 'a.txt'")) == {}
