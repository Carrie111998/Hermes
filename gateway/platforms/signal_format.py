"""Shared Signal formatting helpers.

Keep markdown → Signal native formatting conversion in one place so both the
live Signal adapter and standalone send paths emit the same bodyRanges.
"""

from __future__ import annotations

import re


def markdown_to_signal(text: str) -> tuple[str, list[str]]:
    """Convert markdown to plain text + Signal textStyles list.

    Signal doesn't render markdown. Instead it uses ``bodyRanges`` (exposed by
    signal-cli as ``textStyle`` / ``textStyles`` params) with the format
    ``start:length:STYLE``.

    Positions are measured in UTF-16 code units because that's what the Signal
    protocol uses.

    Supported styles: BOLD, ITALIC, STRIKETHROUGH, MONOSPACE.
    """

    def _utf16_len(s: str) -> int:
        """Length of *s* in UTF-16 code units."""
        return len(s.encode("utf-16-le")) // 2

    def _normalize_bullet_markers(source: str) -> str:
        """Replace Markdown bullet markers with plain Unicode bullets.

        Signal does not render Markdown list syntax, so ``- item`` and
        ``* item`` otherwise arrive as literal Markdown markers. Preserve
        fenced code blocks byte-for-byte; list-looking lines inside code are
        code, not prose bullets.
        """
        parts = re.split(r"(```.*?```)", source, flags=re.DOTALL)
        for idx, part in enumerate(parts):
            if idx % 2 == 1:
                continue
            parts[idx] = re.sub(r"(?m)^([ \t]{0,3})[-*+]\s+", r"\1• ", part)
        return "".join(parts)

    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    text = _normalize_bullet_markers(text)

    styles: list[tuple[int, int, str]] = []

    code_block = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
    code_ranges: list[tuple[int, int]] = []
    while match := code_block.search(text):
        inner = match.group(1).rstrip("\n")
        start = match.start()
        text = text[: match.start()] + inner + text[match.end() :]
        styles.append((start, len(inner), "MONOSPACE"))
        code_ranges.append((start, start + len(inner)))

    # Once de-fenced, a code block's own text (e.g. a Python "# comment" or
    # "**kwargs") is indistinguishable from prose to the regexes below.
    # Keep it out of the heading pass and seed `occupied` (below) with it so
    # the inline patterns skip it too — otherwise code content gets mangled
    # (its "#"/"**"/"_" markers stripped) and double-styled on top of MONOSPACE.
    heading = re.compile(r"^#{1,6}\s+", re.MULTILINE)
    new_text = ""
    last_end = 0
    num_code_styles = len(styles)
    heading_removals: list[tuple[int, int]] = []
    for match in heading.finditer(text):
        if any(cs <= match.start() < ce for cs, ce in code_ranges):
            continue
        new_text += text[last_end : match.start()]
        last_end = match.end()
        eol = text.find("\n", match.end())
        if eol == -1:
            eol = len(text)
        heading_text = text[match.end() : eol]
        start = len(new_text)
        new_text += heading_text
        styles.append((start, len(heading_text), "BOLD"))
        last_end = eol
        heading_removals.append((match.start(), match.end() - match.start()))
    new_text += text[last_end:]
    text = new_text

    # The heading pass above shortens `text` by stripping each "#+ " marker,
    # which invalidates the absolute positions of the code-block MONOSPACE
    # styles recorded earlier (a heading before a code block shifted the
    # block's start left, silently dropping the style once positions ran
    # past the new, shorter `len(text)`). Shift them by however much was
    # removed ahead of each one.
    for _i in range(num_code_styles):
        _start, _length, _style = styles[_i]
        _shift = sum(l for p, l in heading_removals if p < _start)
        styles[_i] = (_start - _shift, _length, _style)
    # Re-derive code_ranges (now in post-heading `text` coordinates) so the
    # inline pass below can be seeded with them too.
    code_ranges = [(s, s + l) for s, l, _ in styles[:num_code_styles]]

    patterns = [
        (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), "BOLD"),
        (re.compile(r"__(.+?)__", re.DOTALL), "BOLD"),
        (re.compile(r"~~(.+?)~~", re.DOTALL), "STRIKETHROUGH"),
        (re.compile(r"`(.+?)`"), "MONOSPACE"),
        (re.compile(r"(?<!\*)\*(?!\*| )(.+?)(?<!\*)\*(?!\*)"), "ITALIC"),
        (re.compile(r"(?<!\w)_(?!_)(.+?)(?<!_)_(?!\w)"), "ITALIC"),
    ]

    all_matches: list[tuple[int, int, int, int, str]] = []
    occupied: list[tuple[int, int]] = list(code_ranges)
    for pattern, style in patterns:
        for match in pattern.finditer(text):
            ms, me = match.start(), match.end()
            if not any(ms < oe and me > os for os, oe in occupied):
                all_matches.append((ms, me, match.start(1), match.end(1), style))
                occupied.append((ms, me))
    all_matches.sort()

    removals: list[tuple[int, int]] = []
    for ms, me, g1s, g1e, _ in all_matches:
        if g1s > ms:
            removals.append((ms, g1s - ms))
        if me > g1e:
            removals.append((g1e, me - g1e))
    removals.sort()

    def _adjust(pos: int) -> int:
        shift = 0
        for remove_pos, remove_len in removals:
            if remove_pos < pos:
                shift += min(remove_len, pos - remove_pos)
            else:
                break
        return pos - shift

    adjusted_prior: list[tuple[int, int, str]] = []
    for start, length, style in styles:
        new_start = _adjust(start)
        new_end = _adjust(start + length)
        if new_end > new_start:
            adjusted_prior.append((new_start, new_end - new_start, style))

    result = ""
    last_end = 0
    inline_styles: list[tuple[int, int, str]] = []
    for ms, me, g1s, g1e, style in all_matches:
        result += text[last_end:ms]
        pos = len(result)
        inner = text[g1s:g1e]
        result += inner
        inline_styles.append((pos, len(inner), style))
        last_end = me
    result += text[last_end:]
    text = result

    styles = adjusted_prior + inline_styles

    style_strings: list[str] = []
    for cp_start, cp_len, style_type in sorted(styles):
        if cp_start < 0 or cp_start + cp_len > len(text):
            continue
        u16_start = _utf16_len(text[:cp_start])
        u16_len = _utf16_len(text[cp_start : cp_start + cp_len])
        style_strings.append(f"{u16_start}:{u16_len}:{style_type}")

    return text, style_strings
