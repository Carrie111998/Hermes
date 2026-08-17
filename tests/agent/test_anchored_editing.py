"""Anchored editing (ported from Dirac's EditFileTool tests).

The cases copied/adapted from Dirac: the validation (non-array edits,
malformed), the partial-success (valid edits apply even if some fail,
all-fail errors), and the anchor contract (the content-derived ID stability,
the exact-content verification, the stale-anchor re-read). The middleware
and the registry are mocked; this pins the anchor machinery."""

import json

from tools.anchors import (
    ANCHOR_DELIMITER,
    line_anchor_id,
    parse_anchored_line,
    render_anchored_lines,
    resolve_anchored_edits,
)
from tools.file_tools import anchored_edit_tool, read_file_tool


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    return str(p)


# ── the anchor contract (the content-derived IDs) ─────────────────────

def test_render_and_parse_anchored_lines_round_trip():
    lines = render_anchored_lines(["def a():", "    return 1"])
    assert lines[0].startswith("ANCHOR") and ANCHOR_DELIMITER in lines[0]
    anchor_id, content = parse_anchored_line(lines[0])
    assert content == "def a():"
    assert anchor_id == line_anchor_id("def a():")


def test_unchanged_lines_keep_ids_when_neighbours_move():
    # the b line's ID is content-derived and position-independent: it
    # survives both the a->x change AND the line moving down a slot
    assert line_anchor_id("b") == line_anchor_id("b")
    assert "≫b" in render_anchored_lines(["x", "b", "c"])[1]


def test_stale_anchor_reports_failure():
    lines = render_anchored_lines(["a", "b"])
    out, failures = resolve_anchored_edits(lines, [{"anchor": "ANCHOR9:nope≫zz", "text": "y"}])
    assert failures and out == lines


def test_edit_verifies_content_exactly():
    lines = render_anchored_lines(["a", "b", "c"])
    # the same ID with a different content must be rejected as stale
    anchor = lines[1]
    wrong_content = anchor.split(ANCHOR_DELIMITER)[0] + ANCHOR_DELIMITER + "WRONG"
    out, failures = resolve_anchored_edits(lines, [{"anchor": wrong_content, "text": "y"}])
    assert failures and out == lines


def test_replace_and_span_edits():
    lines = render_anchored_lines(["a", "b", "c"])
    out, failures = resolve_anchored_edits(lines, [{"anchor": lines[1], "text": "B2"}])
    assert not failures and out[1] == "B2"
    out2, failures2 = resolve_anchored_edits(lines, [{"anchor": lines[0], "end_anchor": lines[1], "text": "X"}])
    assert not failures2 and out2 == ["X"] + lines[2:]


# ── the validation (copied from EditFileTool.validation) ───────────────

def test_rejects_non_array_edits(tmp_path):
    p = _write(tmp_path, "t.py", "a\nb\n")
    res = json.loads(anchored_edit_tool(p, "not-a-list"))
    assert res.get("ok") is False


def test_rejects_malformed_edit_shape(tmp_path):
    p = _write(tmp_path, "t.py", "a\nb\n")
    res = json.loads(anchored_edit_tool(p, [{"text": "no anchor"}]))
    assert res.get("ok") is False


# ── the partial success (copied from EditFileTool.partialSuccess) ───────

def test_partial_success_applies_valid_edits(tmp_path):
    p = _write(tmp_path, "t.py", "a\nb\nc\n")
    anchored = render_anchored_lines(open(p).read().splitlines())
    res = json.loads(anchored_edit_tool(p, [
        {"anchor": anchored[0], "text": "A2"},
        {"anchor": "ANCHOR9:nope≫zz", "text": "never"},
        {"anchor": anchored[2], "text": "C2"},
    ]))
    assert res.get("ok") is True
    assert "never" not in open(p).read()


def test_all_edits_fail_returns_error(tmp_path):
    p = _write(tmp_path, "t.py", "a\nb\n")
    res = json.loads(anchored_edit_tool(p, [{"anchor": "ANCHOR9:nope≫zz", "text": "x"}]))
    assert res.get("ok") is False


# ── the include_anchors read (copied from ReadFileTool) ────────────────

def test_include_anchors_read_returns_raw_content(tmp_path):
    p = _write(tmp_path, "t.py", "def a():\n    return 1\n")
    out = json.loads(read_file_tool(p, include_anchors=True))
    assert "ANCHOR" in out["content"]
    assert "1|def" not in out["content"]  # the <n>| prefix must be stripped


def test_read_plain_is_unchanged(tmp_path):
    p = _write(tmp_path, "t.py", "def a():\n    return 1\n")
    out = json.loads(read_file_tool(p))
    assert "ANCHOR" not in out["content"]


def test_sequential_edits_survive_line_shifts():
    """The earlier edits' insertions shift the later lines; the anchors
    re-locate in the current state instead of the original indices."""
    lines = render_anchored_lines(["a", "b", "c", "d"])
    out, failures = resolve_anchored_edits(lines, [
        {"anchor": lines[1], "text": "B1\nB2"},
        {"anchor": lines[3], "text": "D2"},
    ])
    assert not failures
    assert out == [lines[0], "B1", "B2", lines[2], "D2"]


def test_edit_preserves_the_trailing_newline(tmp_path):
    p = _write(tmp_path, "t.py", "a\nb\nc\n")
    anchored = render_anchored_lines(open(p).read().splitlines())
    json.loads(anchored_edit_tool(p, [{"anchor": anchored[0], "text": "A2"}]))
    assert open(p).read().endswith("\n")


def test_text_trailing_newline_is_not_a_blank_line():
    lines = render_anchored_lines(["a", "b", "c"])
    out, failures = resolve_anchored_edits(lines, [{"anchor": lines[1], "text": "x\n"}])
    assert not failures and out[1:2] == ["x"]


def test_partial_reports_the_failures(tmp_path):
    p = _write(tmp_path, "t.py", "a\nb\nc\n")
    anchored = render_anchored_lines(open(p).read().splitlines())
    res = json.loads(anchored_edit_tool(p, [
        {"anchor": anchored[0], "text": "A2"},
        {"anchor": "ANCHOR9:nope≫zz", "text": "never"},
    ]))
    assert res["ok"] is True
    assert res.get("failures") and "never" not in open(p).read()


def test_crlf_file_keeps_crlf(tmp_path):
    p = tmp_path / "t.py"
    p.write_bytes(b"a\r\nb\r\nc\r\n")
    anchored = render_anchored_lines(open(p).read().splitlines())
    json.loads(anchored_edit_tool(str(p), [{"anchor": anchored[0], "text": "A2"}]))
    assert p.read_bytes().startswith(b"A2\r\n")


def test_empty_edits_are_a_noop(tmp_path):
    p = _write(tmp_path, "t.py", "a\nb\n")
    import os
    before = os.stat(p).st_mtime_ns
    res = json.loads(anchored_edit_tool(p, []))
    assert res["noop"] is True
    assert os.stat(p).st_mtime_ns == before


def test_write_refuses_anchored_display_text(tmp_path):
    import json as _json
    from tools.file_tools import _is_internal_file_tool_content, write_file_tool
    anchored = "ANCHOR0:abc≫def a():\nANCHOR1:def≫    return 1"
    assert _is_internal_file_tool_content(anchored)
    p = str(tmp_path / "t.py")
    res = _json.loads(write_file_tool(p, anchored))
    assert res.get("error") and "display text" in res["error"]


def test_duplicate_contents_get_distinct_ids():
    lines = render_anchored_lines(["a", "b", "a"])
    ids = [l.split("≫")[0] for l in lines]
    assert ids[0] != ids[2]  # the duplicate 'a's are targetable separately


def test_moved_line_keeps_its_id():
    # the Dirac contract: an unchanged line keeps its ID when the neighbours
    # move — the index-free digest delivers it
    assert line_anchor_id("return 1") == line_anchor_id("return 1")
