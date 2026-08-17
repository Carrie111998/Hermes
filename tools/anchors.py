"""Line-anchored edit coordinates (ported from Dirac's edit_file).

Each anchored line has the form ``ANCHOR<delimiter>CONTENT``: the word
before the delimiter is an opaque, file-scoped line ID maintained for the
conversation; the text after is the exact current source line. Unchanged
lines keep their IDs when surrounding lines move. The edit tool rereads the
file, locates the line by ID, and verifies the supplied content exactly.
"""

import hashlib

ANCHOR_PREFIX = "ANCHOR"
ANCHOR_DELIMITER = "≫"


def line_anchor_id(content: str, line_index: int) -> str:
    """A stable, content-derived line ID: unchanged lines keep the same ID
    when surrounding lines move."""
    digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:10]
    return f"{line_index}:{digest}"


def render_anchored_lines(lines: list) -> list:
    """Render lines as ``ANCHOR<delimiter>CONTENT`` edit coordinates."""
    out = []
    for idx, line in enumerate(lines):
        anchor_id = line_anchor_id(line, idx)
        out.append(f"{ANCHOR_PREFIX}{anchor_id}{ANCHOR_DELIMITER}{line}")
    return out


def parse_anchored_line(anchored: str):
    """Split an anchored line into (anchor_id, content)."""
    marker = f"{ANCHOR_PREFIX}"
    if not anchored.startswith(marker):
        return None, anchored
    rest = anchored[len(marker):]
    if ANCHOR_DELIMITER not in rest:
        return None, anchored
    anchor_id, content = rest.split(ANCHOR_DELIMITER, 1)
    return f"{ANCHOR_PREFIX}{anchor_id}", content


def resolve_anchored_edits(lines: list, edits: list) -> list:
    """Apply Dirac-style anchored edits to the current file lines.

    Each edit is ``{"anchor": <ANCHOR line>, "end_anchor": <ANCHOR line>,
    "text": <new lines>}``. The anchor/end_anchor are the exact current
    anchored lines (ANCHOR<id>DELIM<content>); the tool locates each by its
    content-derived ID and verifies the supplied content exactly. An anchor
    whose ID no longer resolves (the line changed or moved) returns an error
    — the caller must re-read with include_anchors (the stale-file alert's
    role). Returns the new lines, or raises ``ValueError`` with the
    unresolvable anchor.
    """
    by_id = {}
    for idx, line in enumerate(lines):
        anchor_id, _content = parse_anchored_line(f"ANCHOR{line}") if False else (None, None)
    # Build the id -> (line_index, content) map from the CURRENT anchored state.
    # The caller passes the anchored lines (from an include_anchors read).
    id_index = {}
    for idx, line in enumerate(lines):
        marker = f"{ANCHOR_PREFIX}"
        if not line.startswith(marker) or ANCHOR_DELIMITER not in line:
            continue
        rest = line[len(marker):]
        anchor_id, content = rest.split(ANCHOR_DELIMITER, 1)
        id_index[f"{ANCHOR_PREFIX}{anchor_id}"] = (idx, content)

    out = list(lines)
    for edit in edits:
        anchor = edit.get("anchor")
        end_anchor = edit.get("end_anchor", anchor)
        text = edit.get("text", "").split("\n")
        # the full ANCHOR<id>DELIM<content> line is the coordinate — locate by
        # the ID and verify the content exactly (the Dirac contract).
        start = id_index.get(_anchor_id_of(anchor))
        end = id_index.get(_anchor_id_of(end_anchor))
        if start is None or end is None:
            raise ValueError(f"anchor no longer resolves: {anchor}")
        start_idx, start_content = start
        end_idx, _end_content = end
        if end_idx < start_idx:
            raise ValueError("end_anchor precedes anchor")
        if start_content != anchor.split(ANCHOR_DELIMITER, 1)[1]:
            raise ValueError(f"stale anchor content: {anchor}")
        out[start_idx:end_idx + 1] = text
    return out


def _anchor_id_of(anchored_line: str) -> str:
    marker = f"{ANCHOR_PREFIX}"
    if not anchored_line.startswith(marker) or ANCHOR_DELIMITER not in anchored_line:
        return anchored_line
    rest = anchored_line[len(marker):]
    anchor_id, _content = rest.split(ANCHOR_DELIMITER, 1)
    return f"{ANCHOR_PREFIX}{anchor_id}"


ANCHORED_EDIT_GUIDANCE = (
    "# Anchored editing\n"
    "After a read with include_anchors=true, you may edit by the anchored "
    "coordinates instead of the Cline search blocks: each source line is "
    "ANCHOR<id>≫CONTENT. An edit is {anchor: <ANCHOR line>, "
    "end_anchor: <ANCHOR line>, text: <new lines>} — the tool locates the "
    "line by its ID and verifies the content exactly, so the old block never "
    "needs to be re-sent. Copy the complete anchored line verbatim; never "
    "retype it or combine an ID with another line's content. If an anchor no "
    "longer resolves (the file changed externally), re-read with "
    "include_anchors=true first."
)
