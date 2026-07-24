"""Ren markdown-chunkning för Obsidian-noter (ingen modell, ingen I/O)."""

from __future__ import annotations

import re
from collections import namedtuple

Chunk = namedtuple("Chunk", ["heading_trail", "content"])

_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def strip_frontmatter(text: str) -> str:
    """Return markdown without a leading YAML frontmatter block."""
    return _FRONTMATTER.sub("", text, count=1)


def chunk_markdown(text: str) -> "list[Chunk]":
    """Dela en not i chunks per rubrik. Frontmatter strippas."""
    body = strip_frontmatter(text or "")
    chunks: list[Chunk] = []
    cur_trail = ""
    cur_lines: list[str] = []

    def _flush() -> None:
        content = "\n".join(cur_lines).strip()
        if content:
            chunks.append(Chunk(heading_trail=cur_trail, content=content))

    for line in body.split("\n"):
        m = _HEADING.match(line)
        if m:
            _flush()
            cur_trail = m.group(2).strip()
            cur_lines = [line]
        else:
            cur_lines.append(line)
    _flush()
    return chunks
