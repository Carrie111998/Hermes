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
        # Truncated in the middle of a string value — deliberately NOT closed.
        # Inventing a terminator would hand the tool a silently incomplete
        # argument, so this stays unrepairable and routes to the partial-stream
        # path instead (#62948).
        assert _repair_tool_call_arguments('{"truncated": "val', "t") == "{}"

    # -- Valid JSON passthrough (this path is via except, but still works) --


    # -- Combined repairs --



    # -- Stage 0: strict=False (literal control chars in strings) --
    # llama.cpp backends sometimes emit literal tabs/newlines inside JSON
    # string values. strict=False accepts these; we re-serialise to the
    # canonical wire form (#12068).




    # -- Stage 4: control-char escape fallback --


    # -- Nesting-aware closing (#35151) --
    # Closing by delimiter count appended every missing '}' before every
    # missing ']', so a payload whose innermost open structure was an array
    # got its closers in the wrong order, stayed invalid, and fell through to
    # the "{}" last resort.

    def test_unclosed_array_inside_object_closes_innermost_first(self):
        result = _repair_tool_call_arguments('{"items": [1, 2, 3', "t")
        assert json.loads(result) == {"items": [1, 2, 3]}

    def test_deeply_nested_mixed_truncation(self):
        raw = '{"a": {"b": [{"c": [1, 2'
        result = _repair_tool_call_arguments(raw, "t")
        assert json.loads(result) == {"a": {"b": [{"c": [1, 2]}]}}

    def test_object_inside_array_closes_innermost_first(self):
        result = _repair_tool_call_arguments('{"edits": [{"line": 1', "t")
        assert json.loads(result) == {"edits": [{"line": 1}]}

    def test_delimiters_inside_string_values_are_not_counted(self):
        """Braces in a *completed* string value must not skew the deficit.

        Counting raw '{' characters saw braces belonging to the content, so
        the computed deficit was wrong in either direction — here it hid the
        still-open array and produced '...[1, 2}]'.
        """
        raw = '{"content": "if x: {y}", "items": [1, 2'
        result = _repair_tool_call_arguments(raw, "write_file")
        assert json.loads(result) == {"content": "if x: {y}", "items": [1, 2]}

    def test_truncation_inside_string_is_left_unrepairable(self):
        """Structure may be closed; a cut-off *value* may not.

        Closing the quote here would produce well-formed JSON carrying a
        truncated value, which the caller would then execute as if complete.
        """
        assert _repair_tool_call_arguments('{"path": "x.txt", "content": "hel', "write_file") == "{}"

    def test_truncation_inside_nested_string_is_left_unrepairable(self):
        assert _repair_tool_call_arguments('{"a": [1, {"b": "partial', "t") == "{}"

    def test_truncation_after_a_comma_is_left_unrepairable(self):
        """A dangling comma is the model promising an element that never came.

        Appending the closer would present a short list as a complete one, so
        this stays unparseable and falls through to "{}" (and the caller's
        truncation path) rather than executing.
        """
        assert _repair_tool_call_arguments('{"a": [1, 2,', "t") == "{}"

    def test_json_illegal_whitespace_is_not_treated_as_whitespace(self):
        """Only space/tab/LF/CR are JSON whitespace (RFC 8259 s2).

        Python's str.isspace()/rstrip() also match U+001C-U+001F, U+0085,
        U+00A0, U+2028 -- all illegal outside a JSON string.  Skipping them
        would delete them and hand back a payload that parses, turning input
        JSON rightly rejects into an executed tool call.
        """
        for cp in (0x1C, 0x1F, 0x85, 0xA0, 0x2028, 0x0B):
            raw = '{"command":"echo X",' + chr(cp) + "}"
            assert _repair_tool_call_arguments(raw, "t") == "{}", f"U+{cp:04X}"

    def test_balanced_payload_is_left_alone(self):
        """The closing pass must be a no-op when nothing is open."""
        raw = '{"a": [1, 2], "b": {"c": 3},}'
        result = _repair_tool_call_arguments(raw, "t")
        assert json.loads(result) == {"a": [1, 2], "b": {"c": 3}}

    # -- String-aware trailing-comma strip --
    # The comma strip used a regex with no notion of string boundaries, so a
    # comma inside a string value followed by ']' or '}' was rewritten too.
    # Alone that produced JSON that still failed to parse (-> "{}"); combined
    # with a nesting-aware closer it parses cleanly and the corrupted value
    # reaches the tool.

    def test_comma_and_bracket_inside_string_value_survive(self):
        result = _repair_tool_call_arguments('{"sep": ", ]", "files": ["a"', "t")
        assert json.loads(result) == {"sep": ", ]", "files": ["a"]}

    def test_comma_and_brace_inside_string_value_survive(self):
        result = _repair_tool_call_arguments('{"msg": "end ,}", "n": [1', "t")
        assert json.loads(result) == {"msg": "end ,}", "n": [1]}

    def test_trailing_comma_strip_still_applies_outside_strings(self):
        result = _repair_tool_call_arguments('{"a": [1, 2,], "b": {"c": 3,},}', "t")
        assert json.loads(result) == {"a": [1, 2], "b": {"c": 3}}

    # -- Excess-closer trimming is string-aware too --

    def test_excess_trailing_closer_with_delimiter_inside_string(self):
        """A brace inside a value made raw counts look balanced.

        The excess `}` was therefore never removed and a payload one character
        from valid degraded to "{}".
        """
        result = _repair_tool_call_arguments('{"s":"{","x":[1]}}', "t")
        assert json.loads(result) == {"s": "{", "x": [1]}

    def test_excess_trailing_closer_with_closing_delimiter_in_string(self):
        result = _repair_tool_call_arguments('{"s":"}", "x":[1]}}', "t")
        assert json.loads(result) == {"s": "}", "x": [1]}

    def test_excess_closer_mid_payload_is_left_unrepairable(self):
        """Only a trailing excess closer is dropped.

        In `{"a": [1]]}` the stray `]` is not the last token, so trimming the
        tail would remove the legitimate `}` and mangle the payload.  It stays
        unparseable and falls through to "{}" instead.
        """
        assert _repair_tool_call_arguments('{"a": [1]]}', "t") == "{}"
        assert _repair_tool_call_arguments('{"a": [1, 2]], "b": 3}', "t") == "{}"

    # -- Never-raises contract --

    def test_pathological_nesting_does_not_raise(self):
        """RecursionError is not a ValueError.

        json.loads recurses per nesting level, so a repetition-loop payload
        raises RecursionError out of a function documented as never raising.
        """
        assert _repair_tool_call_arguments("[" * 100_000, "t") == "{}"

    def test_overflowing_float_is_not_laundered_into_infinity(self):
        """json.loads turns 1e999 into inf; a default dumps re-emits the bare
        token ``Infinity``, which is not valid JSON."""
        result = _repair_tool_call_arguments('{"n": 1e999}', "t")
        assert "Infinity" not in result
        json.loads(result)


    def test_long_numeric_literal_does_not_raise(self):
        """CPython >= 3.11 raises a bare ValueError, not JSONDecodeError.

        ``int`` parsing past ``sys.get_int_max_str_digits()`` (4300 by default)
        raises ValueError from inside ``json.loads``.  Two handlers here caught
        only JSONDecodeError, so a digit run-on escaped a function the callers
        rely on never raising.
        """
        result = _repair_tool_call_arguments('{"n": ' + "9" * 5000, "t")
        json.loads(result)  # must not raise

    def test_long_numeric_literal_in_closed_object_does_not_raise(self):
        result = _repair_tool_call_arguments('{"n": ' + "9" * 5000 + ",}", "t")
        json.loads(result)

