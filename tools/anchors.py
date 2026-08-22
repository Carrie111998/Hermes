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

# The corruption markers the tool refuses in the new text: the hermes's own
# redaction («redacted:), the lean-ctx compression markers ([lean-ctx: ...]),
# and the common proxy forms. The marker forms are checked before any write —
# the same guard lean-ctx ships, extended to the proxy ecosystem.
CORRUPTION_MARKERS = ("«redacted:", "[lean-ctx:", "[REDACTED]", "<redacted>", "【REDACTED】")


def line_anchor_id(content: str) -> str:
    """A stable, content-derived line ID: unchanged lines keep the same ID
    when surrounding lines move (the Dirac contract — the ID carries no
    position)."""
    return hashlib.sha1(content.encode("utf-8")).hexdigest()[:10]


def render_anchored_lines(lines: list) -> list:
    """Render lines as ``ANCHOR<delimiter>CONTENT`` edit coordinates.

    The ID is the content digest, position-independent; duplicate contents
    get an occurrence suffix so the model can target each instance."""
    seen: dict = {}
    out = []
    for line in lines:
        anchor_id = line_anchor_id(line)
        seen[anchor_id] = seen.get(anchor_id, 0) + 1
        if seen[anchor_id] > 1:
            anchor_id = f"{anchor_id}-{seen[anchor_id]}"
        out.append(f"{ANCHOR_PREFIX}{anchor_id}{ANCHOR_DELIMITER}{line}")
    return out


def parse_anchored_line(anchored: str):
    """Split an anchored line into (anchor_id, content); the id is the
    line_anchor_id form (without the ANCHOR marker)."""
    marker = f"{ANCHOR_PREFIX}"
    if not anchored.startswith(marker):
        return None, anchored
    rest = anchored[len(marker):]
    if ANCHOR_DELIMITER not in rest:
        return None, anchored
    anchor_id, content = rest.split(ANCHOR_DELIMITER, 1)
    return anchor_id, content


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
    # Build the id -> (line_index, content) map from the CURRENT anchored state.
    # The caller passes the anchored lines (from an include_anchors read).
    out = list(lines)
    failures = []
    for edit in edits:
        if not isinstance(edit, dict) or not isinstance(edit.get("anchor"), str):
            failures.append("malformed edit: missing or invalid anchor")
            continue
        anchor = edit.get("anchor")
        end_anchor = edit.get("end_anchor", anchor)
        if not isinstance(edit.get("text"), str):
            failures.append("malformed edit: 'text' must be a string")
            continue
        if any(_m in edit["text"] for _m in CORRUPTION_MARKERS):
            failures.append(
                "the new text contains a compression/redaction marker "
                f"(one of {CORRUPTION_MARKERS}) — never write a marker back; "
                "reconstruct the real value or re-read"
            )
            continue
        text = edit["text"].split("\n")
        if text and text[-1] == "":
            text.pop()  # the trailing newline of the text is not a blank line
        # Re-locate in the CURRENT out on every edit: the earlier edits'
        # insertions/deletions shift the later lines, so the stored indices
        # from the original read are stale. The anchors are content-verified.
        id_index = {}
        for idx, line in enumerate(out):
            marker = f"{ANCHOR_PREFIX}"
            if not line.startswith(marker) or ANCHOR_DELIMITER not in line:
                continue
            rest = line[len(marker):]
            anchor_id, content = rest.split(ANCHOR_DELIMITER, 1)
            id_index[anchor_id] = (idx, content)
        start = id_index.get(_anchor_id_of(anchor))
        end = id_index.get(_anchor_id_of(end_anchor))
        if start is None or end is None:
            failures.append(f"anchor no longer resolves: {anchor}")
            continue
        start_idx, start_content = start
        end_idx, end_content = end
        if end_idx < start_idx:
            failures.append("end_anchor precedes anchor")
            continue
        # The verbatim contract, with the redaction carve-out: the hermes's
        # read redacts secret-bearing lines («redacted:...»), so the model's
        # copied anchor carries the redacted content while the current line
        # is raw — for those, the ID match is the contract (the ID is the
        # raw line's digest, so a stale line still fails).
        anchor_content = anchor.split(ANCHOR_DELIMITER, 1)[1]
        end_anchor_content = end_anchor.split(ANCHOR_DELIMITER, 1)[1]
        if start_content != anchor_content and "«redacted:" not in anchor_content:
            failures.append(f"stale anchor content: {anchor}")
            continue
        if end_content != end_anchor_content and "«redacted:" not in end_anchor_content:
            failures.append(f"stale end-anchor content: {end_anchor}")
            continue
        out[start_idx:end_idx + 1] = text
    return out, failures


def _anchor_id_of(anchored_line: str) -> str:
    anchor_id, _content = parse_anchored_line(anchored_line)
    return anchor_id if anchor_id is not None else anchored_line


ANCHORED_EDIT_GUIDANCE = (
    "# Anchored editing is the default\n"
    "After a read with include_anchors=true, ALWAYS edit with anchored_edit "
    "instead of the Cline patch: each source line is ANCHOR<id>≫CONTENT, and "
    "an edit is {anchor: <ANCHOR line>, end_anchor: <ANCHOR line>, "
    "text: <new lines>}. The tool locates the line by its ID and verifies "
    "the content EXACTLY — a wrong or stale coordinate is REFUSED (the "
    "model re-reads) instead of silently corrupting the file, and the old "
    "block never needs to be re-sent. Copy the complete anchored line "
    "verbatim; never retype it or combine an ID with another line's content. "
    "The Cline patch remains only as the fallback for edits without a fresh "
    "anchored read — prefer the anchors whenever they are available."
)
