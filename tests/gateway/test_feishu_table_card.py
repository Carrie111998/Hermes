"""Tests for the Feishu card-table conversion module.

``feishu_table_card.build_table_card_payload`` converts markdown pipe tables
into Feishu interactive-card ``table`` components (schema 2.0) so columns get
real width control (``auto`` or pinned px) instead of the post/md renderer's
equal-width squeeze. Conversion is opt-in via ``tables_as_cards`` and must
fall back to ``None`` (caller keeps the historical post path) whenever the
content does not fit Feishu's card component limits.

These are behavior-contract tests: they assert relationships that must hold
(width estimates, fallback decisions, round-trip of cell content), not frozen
snapshot values.
"""

from __future__ import annotations

import json

from plugins.platforms.feishu.feishu_table_card import (
    _MAX_COLUMNS_PER_TABLE,
    _MAX_ROWS_PER_TABLE,
    _MAX_TABLES_PER_CARD,
    _column_width_px,
    _display_width,
    build_table_card_payload,
)

_SIMPLE_TABLE = (
    "| col A | col B |\n"
    "| ----- | ----- |\n"
    "| 1     | 2     |"
)


def _tables_from_card(payload_str: str) -> list[dict]:
    """Pull every ``{tag: 'table'}`` element out of a card payload."""
    card = json.loads(payload_str)
    assert card["schema"] == "2.0"
    return [el for el in card["body"]["elements"] if el.get("tag") == "table"]


# ---------------------------------------------------------------------------
# Fallback contract: return None → caller keeps the post path
# ---------------------------------------------------------------------------


def test_returns_none_without_table():
    assert build_table_card_payload("") is None
    assert build_table_card_payload("plain prose, no pipes") is None
    # Pipe-looking prose that never forms header+separator is not a table.
    assert build_table_card_payload("a | b | c\njust text") is None


def test_returns_none_above_table_limit():
    line = _SIMPLE_TABLE
    too_many = ("\n\nprose\n\n".join([line] * (_MAX_TABLES_PER_CARD + 1)))
    assert build_table_card_payload(too_many) is None


def test_returns_none_above_row_limit():
    header = "| h |\n| - |\n"
    rows = "".join(f"| r{i} |\n" for i in range(_MAX_ROWS_PER_TABLE + 1))
    assert build_table_card_payload(header + rows) is None


def test_returns_none_above_column_limit():
    n = _MAX_COLUMNS_PER_TABLE + 1
    header = "| " + " | ".join(f"c{i}" for i in range(n)) + " |\n"
    sep = "| " + " | ".join("-" for _ in range(n)) + " |\n"
    row = "| " + " | ".join("x" for _ in range(n)) + " |\n"
    assert build_table_card_payload(header + sep + row) is None


# ---------------------------------------------------------------------------
# Card structure contract
# ---------------------------------------------------------------------------


def test_simple_table_produces_card_table_component():
    payload = build_table_card_payload(_SIMPLE_TABLE)
    assert payload is not None
    tables = _tables_from_card(payload)
    assert len(tables) == 1
    table = tables[0]
    assert [c["display_name"] for c in table["columns"]] == ["col A", "col B"]
    assert table["rows"] == [{"c0": "1", "c1": "2"}]
    # Every column carries a Feishu-legal width value.
    for col in table["columns"]:
        w = col["width"]
        assert w == "auto" or (w.endswith("px") and 80 <= int(w[:-2]) <= 600)


def test_prose_around_table_is_preserved():
    content = "intro paragraph\n\n" + _SIMPLE_TABLE + "\n\noutro paragraph"
    payload = build_table_card_payload(content)
    assert payload is not None
    card = json.loads(payload)
    contents = [
        el.get("content", "")
        for el in card["body"]["elements"]
        if el.get("tag") == "markdown"
    ]
    joined = "\n".join(contents)
    assert "intro paragraph" in joined
    assert "outro paragraph" in joined


def test_alignment_survives_separator_syntax():
    content = (
        "| left | center | right |\n"
        "| ----- | :----: | ----: |\n"
        "| a | b | c |"
    )
    payload = build_table_card_payload(content)
    assert payload is not None
    table = _tables_from_card(payload)[0]
    aligns = [c["horizontal_align"] for c in table["columns"]]
    assert aligns == ["left", "center", "right"]


def test_escaped_pipe_renders_as_literal():
    content = (
        "| expr |\n"
        "| ---- |\n"
        "| a \\| b |"
    )
    payload = build_table_card_payload(content)
    assert payload is not None
    table = _tables_from_card(payload)[0]
    assert table["rows"][0]["c0"] == "a | b"


def test_multiple_tables_interleave_with_prose():
    content = (
        "before\n\n"
        + _SIMPLE_TABLE
        + "\n\nmiddle\n\n"
        + _SIMPLE_TABLE
        + "\n\nafter"
    )
    payload = build_table_card_payload(content)
    assert payload is not None
    card = json.loads(payload)
    tags = [el.get("tag") for el in card["body"]["elements"]]
    assert tags.count("table") == 2
    # Order preserved: prose segments on both sides of both tables.
    assert tags[0] == "markdown" and tags[-1] == "markdown"
    joined = "\n".join(
        el.get("content", "") for el in card["body"]["elements"] if el.get("tag") == "markdown"
    )
    for marker in ("before", "middle", "after"):
        assert marker in joined


# ---------------------------------------------------------------------------
# Column width contract (the reason this module exists)
# ---------------------------------------------------------------------------


def test_cjk_content_measures_wider_than_ascii():
    assert _display_width("中文内容") > _display_width("ascii")
    # CJK chars count roughly double per glyph.
    assert _display_width("四个汉字") == pytest.approx(_display_width("abcdefgh"), rel=0.1)


def test_column_width_clamped_to_feishu_bounds():
    header = ["x"]
    rows = [["y"]]
    assert _column_width_px(header, rows, 0) >= 80
    huge = ["非常" * 400]  # far beyond 600px even at CJK width
    rows_huge = [[huge[0]]]
    assert _column_width_px(["h"], rows_huge, 0) <= 600


def test_all_narrow_table_keeps_auto_widths():
    payload = build_table_card_payload(_SIMPLE_TABLE)
    assert payload is not None
    table = _tables_from_card(payload)[0]
    assert all(c["width"] == "auto" for c in table["columns"])


def test_mixed_width_table_pins_narrow_columns():
    # One very wide CJK column forces pin_narrow on for the short ones.
    wide_cell = "这一列包含一段相当长的中文内容用来撑宽自适应列宽" * 4
    content = (
        "| short | wide |\n"
        "| ----- | ---- |\n"
        f"| ok | {wide_cell} |"
    )
    payload = build_table_card_payload(content)
    assert payload is not None
    table = _tables_from_card(payload)[0]
    widths = [c["width"] for c in table["columns"]]
    assert widths[0] != "auto" and widths[0].endswith("px")  # narrow column pinned
    assert widths[1] == "auto"  # wide column stays auto


# ---------------------------------------------------------------------------
# Adapter integration: opt-in routing and update-path guard
# ---------------------------------------------------------------------------


def _bare_adapter():
    from tests.gateway._plugin_adapter_loader import load_plugin_adapter

    adapter = load_plugin_adapter("feishu")
    return object.__new__(adapter.FeishuAdapter)


def test_opt_in_routes_table_to_interactive_card():
    inst = _bare_adapter()
    inst._tables_as_cards = True
    msg_type, payload = inst._build_outbound_payload(_SIMPLE_TABLE)
    assert msg_type == "interactive"
    assert json.loads(payload)["schema"] == "2.0"


def test_default_off_keeps_post_path():
    inst = _bare_adapter()
    inst._tables_as_cards = False
    msg_type, _ = inst._build_outbound_payload(_SIMPLE_TABLE)
    assert msg_type == "post"


def test_message_update_path_never_emits_card():
    # The im/v1 update API rejects msg_type changes, so allow_card_table=False.
    inst = _bare_adapter()
    inst._tables_as_cards = True
    msg_type, _ = inst._build_outbound_payload(_SIMPLE_TABLE, allow_card_table=False)
    assert msg_type == "post"


def test_non_table_content_unaffected_by_opt_in():
    inst = _bare_adapter()
    inst._tables_as_cards = True
    msg_type, payload = inst._build_outbound_payload("just **prose**, no table")
    assert msg_type == "post"
    assert "prose" in payload


import pytest  # noqa: E402  (kept at bottom: only used by width-approx test)
