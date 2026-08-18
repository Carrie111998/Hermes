"""Tests for tools.mcp_tool._annotation_read_only_hint (regression for #88858).

The MCP Python SDK materialises the wire field ``readOnlyHint`` as the
Python attribute ``read_only_hint`` (snake_case), so live-discovered
SDK annotation objects expose the hint on ``annotations.read_only_hint``,
not ``annotations.readOnlyHint``. Without falling back to the
snake_case spelling, the trust gate classifies every read-only tool on
an untrusted MCP server as write-capable and prompts for approval on
every call.

The function is also called on plain dicts (schema-cache JSON), where
both spellings are accepted so a server that serialised the snake_case
form (older code paths, third-party caches) still gates correctly.
"""

from types import SimpleNamespace

from tools import mcp_tool


def test_sdk_object_snake_case_is_read_only():
    """SDK objects expose readOnlyHint on read_only_hint (snake_case).

    This is the live-discovery case that was broken before #88858.
    """
    annotations = SimpleNamespace(read_only_hint=True)
    tool = SimpleNamespace(annotations=annotations)
    assert mcp_tool._annotation_read_only_hint(tool) is True


def test_sdk_object_camel_case_is_read_only():
    """Older SDK / unusual objects may use the camelCase attribute name.

    The fix must remain backwards-compatible with anything that already
    produced the camelCase spelling.
    """
    annotations = SimpleNamespace(readOnlyHint=True)
    tool = SimpleNamespace(annotations=annotations)
    assert mcp_tool._annotation_read_only_hint(tool) is True


def test_sdk_object_both_spellings_true_returns_true():
    """Both spellings set to True → True (no double-counting, no XOR)."""
    annotations = SimpleNamespace(read_only_hint=True, readOnlyHint=True)
    tool = SimpleNamespace(annotations=annotations)
    assert mcp_tool._annotation_read_only_hint(tool) is True


def test_sdk_object_false_returns_false():
    """Explicit False on either spelling must be respected (not coerced)."""
    assert mcp_tool._annotation_read_only_hint(
        SimpleNamespace(annotations=SimpleNamespace(read_only_hint=False))
    ) is False
    assert mcp_tool._annotation_read_only_hint(
        SimpleNamespace(annotations=SimpleNamespace(readOnlyHint=False))
    ) is False


def test_sdk_object_missing_both_spellings_returns_false():
    """An SDK object with no read-only hint on either spelling → write-capable.

    This is the fail-closed path: a server that does not annotate its
    tools at all is treated as write-capable regardless of trust tier.
    """
    annotations = SimpleNamespace(title="noop")
    tool = SimpleNamespace(annotations=annotations)
    assert mcp_tool._annotation_read_only_hint(tool) is False


def test_sdk_object_no_annotations_attribute_returns_false():
    """Object with no ``annotations`` attribute at all → write-capable."""
    assert mcp_tool._annotation_read_only_hint(SimpleNamespace()) is False


def test_sdk_object_annotations_none_returns_false():
    """Object with ``annotations=None`` → write-capable."""
    assert mcp_tool._annotation_read_only_hint(
        SimpleNamespace(annotations=None)
    ) is False


def test_dict_camel_case_is_read_only():
    """Schema-cache JSON (dict) with the wire-format camelCase key."""
    assert mcp_tool._annotation_read_only_hint(
        SimpleNamespace(annotations={"readOnlyHint": True})
    ) is True


def test_dict_snake_case_is_read_only():
    """Schema-cache JSON (dict) with snake_case key (older serialisations)."""
    assert mcp_tool._annotation_read_only_hint(
        SimpleNamespace(annotations={"read_only_hint": True})
    ) is True


def test_dict_both_spellings_true_returns_true():
    """Dict with both spellings True → True."""
    assert mcp_tool._annotation_read_only_hint(
        SimpleNamespace(
            annotations={"readOnlyHint": True, "read_only_hint": True}
        )
    ) is True


def test_dict_truthy_non_bool_string_is_not_read_only():
    """A non-bool truthy value (e.g. ``"yes"``) must NOT be treated as True.

    The hint is an opt-in signal: only an explicit boolean ``True`` from
    the server can mark a tool as read-only. Anything else is write-capable.
    """
    assert mcp_tool._annotation_read_only_hint(
        SimpleNamespace(annotations={"readOnlyHint": "yes"})
    ) is False
    assert mcp_tool._annotation_read_only_hint(
        SimpleNamespace(annotations={"read_only_hint": "yes"})
    ) is False


def test_dict_missing_key_returns_false():
    """Dict with no read-only key on either spelling → write-capable."""
    assert mcp_tool._annotation_read_only_hint(
        SimpleNamespace(annotations={"title": "noop"})
    ) is False
