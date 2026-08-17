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
    """Split an anchored line into (anchor_id, content)."""
    marker = f"{ANCHOR_PREFIX}"
    if not anchored.startswith(marker):
        return None, anchored
    rest = anchored[len(marker):]
    if ANCHOR_DELIMITER not in rest:
        return None, anchored
    anchor_id, content = rest.split(ANCHOR_DELIMITER, 1)
    return f"{ANCHOR_PREFIX}{anchor_id}", content
