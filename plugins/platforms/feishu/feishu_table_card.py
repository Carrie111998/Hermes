"""Markdown table → Feishu card table-component converter.

Why this module exists
----------------------
Feishu renders markdown tables inside ``post``-type ``md`` elements with
columns squeezed to content width; long CJK/Latin cells wrap into unreadable
narrow strips and the column widths are not controllable. Feishu's
interactive-card ``table`` component (schema 2.0) solves this: columns accept
``width: "auto"`` (size-to-content) and rows accept ``row_height: "auto"``
(expand to fit).

This module converts the pipe tables in an outbound markdown chunk into a
single JSON 2.0 interactive-card payload:

    prose segment  ->  {"tag": "markdown", "content": ...}
    table block    ->  {"tag": "table", "columns": [...], "rows": [...]}

Interleaved order is preserved. When the content exceeds card component
limits (>5 tables, >50 columns, >100 rows per table) or contains no
convertible table, ``build_table_card_payload`` returns ``None`` and the
caller falls back to the historical post path.

Docs: https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-json-v2-components/content-components/table
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Limits (Feishu card component constraints + defensive caps)
# ---------------------------------------------------------------------------

_MAX_TABLES_PER_CARD = 5          # Feishu hard limit per card
_MAX_COLUMNS_PER_TABLE = 50       # Feishu hard limit per table
_MAX_ROWS_PER_TABLE = 100         # Defensive: longer tables are unreadable in chat
_MAX_PAGE_SIZE = 10               # Feishu max rows per page
_ROW_MAX_HEIGHT = "999px"         # row_height auto cap; avoids clipping tall cells

# A table row line: starts (after optional indent) with a pipe.
_TABLE_ROW_LINE_RE = re.compile(r"^\s*\|")
# The separator line under the header: cells of colons/dashes/spaces only.
_TABLE_SEP_CELL_RE = re.compile(r"^:?-+:?$")
# Escaped pipe inside a cell.
_ESCAPED_PIPE = "\\|"


def _split_table_row(line: str) -> List[str]:
    """Split a markdown table line into raw cell strings.

    Handles optional leading/trailing pipes and escaped pipes (``\\|``).
    """
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith(_ESCAPED_PIPE):
        text = text[:-1]

    cells: List[str] = []
    buf: List[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] == "|":
            buf.append("\\|")  # keep escape for now; unescape below
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf))
    return [c.strip().replace(_ESCAPED_PIPE, "|") for c in cells]


def _is_separator_row(cells: List[str]) -> bool:
    if not cells:
        return False
    return all(_TABLE_SEP_CELL_RE.match(c) for c in cells if c != "") and any(
        c for c in cells
    )


def _alignment_for(sep_cell: str) -> str:
    stripped = sep_cell.strip()
    if stripped.startswith(":") and stripped.endswith(":"):
        return "center"
    if stripped.endswith(":"):
        return "right"
    return "left"


def _clean_cell(text: str) -> str:
    """Normalize a cell for lark_md rendering."""
    # Internal newlines cannot occur in GFM tables; collapse stray whitespace.
    text = re.sub(r"\s*\n\s*", " ", text)
    # lark_md escapes: backslashes before markdown punctuation are literal.
    return text.strip()


def _extract_table_blocks(lines: List[str]) -> List[Dict[str, Any]]:
    """Extract contiguous markdown table blocks from a line list.

    Returns a list of blocks, each ``{"start": i, "end": j (exclusive),
    "header": [...], "aligns": [...], "rows": [[...], ...]}``.
    """
    blocks: List[Dict[str, Any]] = []
    i = 0
    n = len(lines)
    while i < n - 1:
        if not _TABLE_ROW_LINE_RE.match(lines[i]):
            i += 1
            continue
        header = _split_table_row(lines[i])
        sep = _split_table_row(lines[i + 1]) if i + 1 < n else []
        if not header or not _is_separator_row(sep):
            i += 1
            continue
        # Valid table start: header + separator.
        aligns = [_alignment_for(c) for c in sep]
        rows: List[List[str]] = []
        j = i + 2
        while j < n and _TABLE_ROW_LINE_RE.match(lines[j]):
            cells = _split_table_row(lines[j])
            rows.append(cells)
            j += 1
        blocks.append(
            {"start": i, "end": j, "header": header, "aligns": aligns, "rows": rows}
        )
        i = j
    return blocks


# Feishu column width bounds (docs): custom width is [80, 600] px.
_MIN_COL_PX = 80
_MAX_COL_PX = 600
# Approximate rendered character widths at 14px body font.
_CJK_CHAR_PX = 14.0
_ASCII_CHAR_PX = 7.5
# Padding/borders allowance per cell.
_CELL_PADDING_PX = 24
# Columns estimated no wider than this are pinned in px; wider columns stay
# "auto" and share the remaining card width. Pinning only applies when the
# table has at least one wide (auto) column to absorb the leftover space —
# an all-pinned table would total far less than the card width and Feishu
# would re-stretch it anyway.
_PINNED_COL_MAX_PX = 220


def _display_width(text: str) -> float:
    """Estimated rendered width of *text* in pixels at 14px font.

    CJK/fullwidth characters count double the ASCII width. Markdown markers
    (**, `, links) are discounted since they render as styling, not glyphs.
    """
    # Strip markdown markers that do not render as visible characters.
    visible = re.sub(r"\*\*|`", "", text)
    visible = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", visible)  # [label](url) -> label
    width = 0.0
    for ch in visible:
        width += _CJK_CHAR_PX if unicodedata.east_asian_width(ch) in ("W", "F") else _ASCII_CHAR_PX
    return width


def _column_width_px(header: str, rows: List[List[str]], idx: int) -> int:
    """Estimate the content-driven width for column *idx* in pixels."""
    widths = [_display_width(header[idx])]
    for row in rows:
        cell = row[idx] if idx < len(row) else ""
        # Only measure the longest line of a multi-line cell.
        widths.append(max((_display_width(part) for part in cell.split("\n")), default=0.0))
    content_px = max(widths, default=0.0) + _CELL_PADDING_PX
    return max(_MIN_COL_PX, min(_MAX_COL_PX, int(content_px)))


def _build_table_element(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a Feishu table component from one parsed table block."""
    header = block["header"]
    aligns = block["aligns"]
    rows = block["rows"]

    n_cols = len(header)
    if n_cols == 0 or n_cols > _MAX_COLUMNS_PER_TABLE:
        return None
    if len(rows) > _MAX_ROWS_PER_TABLE:
        return None

    columns: List[Dict[str, str]] = []
    # Two-pass: estimate every column's content width first, then pin only
    # narrow columns to px so wide "auto" columns absorb the remaining card
    # width. All-narrow tables keep everything "auto" (Feishu stretches the
    # table to full card width regardless, so pinning gains nothing there).
    estimated = [_column_width_px(header, rows, i) for i in range(n_cols)]
    n_pinned = sum(1 for w in estimated if w <= _PINNED_COL_MAX_PX)
    pin_narrow = 0 < n_pinned < n_cols
    for idx in range(n_cols):
        if pin_narrow and estimated[idx] <= _PINNED_COL_MAX_PX:
            width = f"{estimated[idx]}px"
        else:
            width = "auto"
        col: Dict[str, Any] = {
            "name": f"c{idx}",
            "display_name": _clean_cell(header[idx]),
            "data_type": "lark_md",
            "width": width,
            "horizontal_align": aligns[idx] if idx < len(aligns) else "left",
            "vertical_align": "top",
        }
        columns.append(col)

    row_dicts: List[Dict[str, str]] = []
    for row in rows:
        row_obj: Dict[str, str] = {}
        for idx in range(n_cols):
            cell = _clean_cell(row[idx]) if idx < len(row) else ""
            row_obj[f"c{idx}"] = cell
        row_dicts.append(row_obj)

    return {
        "tag": "table",
        "page_size": _MAX_PAGE_SIZE,
        "row_height": "auto",
        "row_max_height": _ROW_MAX_HEIGHT,
        "freeze_first_column": False,
        "header_style": {
            "text_align": "left",
            "text_size": "normal",
            "background_style": "grey",
            "text_color": "default",
            "bold": True,
            "lines": 1,
        },
        "columns": columns,
        "rows": row_dicts,
    }


_FENCE_OPEN_RE = re.compile(r"^```([^\n`]*)\s*$")
_FENCE_CLOSE_RE = re.compile(r"^```\s*$")


def _prose_elements(segment: str) -> List[Dict[str, str]]:
    """Build markdown elements for a prose segment, splitting at code fences.

    Splitting mirrors the post-path fence isolation: a fenced block gets its
    own markdown element so a renderer quirk in one element cannot swallow the
    content that follows it.
    """
    if not segment.strip():
        return []
    elements: List[Dict[str, str]] = []
    current: List[str] = []
    in_code = False

    def _flush() -> None:
        text = "\n".join(current).strip("\n")
        if text.strip():
            elements.append({"tag": "markdown", "content": text})
        current.clear()

    for line in segment.splitlines():
        stripped = line.strip()
        is_fence = bool(
            _FENCE_CLOSE_RE.match(stripped) if in_code else _FENCE_OPEN_RE.match(stripped)
        )
        if is_fence:
            if not in_code:
                _flush()
            current.append(line)
            in_code = not in_code
            if not in_code:
                _flush()
            continue
        current.append(line)
    _flush()
    return elements


def build_table_card_payload(content: str) -> Optional[str]:
    """Convert a markdown chunk containing pipe tables into a card JSON string.

    Returns ``None`` when the content has no convertible table or exceeds the
    Feishu card component limits — the caller then uses the historical post
    path unchanged.
    """
    if not content or "|" not in content:
        return None

    lines = content.replace("\r\n", "\n").split("\n")
    blocks = _extract_table_blocks(lines)
    if not blocks:
        return None
    if len(blocks) > _MAX_TABLES_PER_CARD:
        return None

    table_payloads: List[Optional[Dict[str, Any]]] = [
        _build_table_element(b) for b in blocks
    ]
    if any(t is None for t in table_payloads):
        return None

    elements: List[Dict[str, Any]] = []
    cursor = 0
    for block, table in zip(blocks, table_payloads):
        prose = "\n".join(lines[cursor : block["start"]])
        elements.extend(_prose_elements(prose))
        assert table is not None
        elements.append(table)
        cursor = block["end"]
    elements.extend(_prose_elements("\n".join(lines[cursor:])))

    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": elements},
    }
    return json.dumps(card, ensure_ascii=False)
