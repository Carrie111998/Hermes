"""Visible-text masking for Group Chat mention routing."""

import re
import unicodedata
from html.parser import HTMLParser

_FENCE_START_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_BARE_URI_START_RE = re.compile(r"(?i)(?:https?://|ftp://|mailto:|www\.)")


def _masked_markdown_code(value: str) -> str:
    """Replace Markdown code with spaces while preserving mention boundaries."""

    chars = list(value)

    def mask(start: int, end: int) -> None:
        for index in range(start, end):
            if chars[index] not in "\r\n":
                chars[index] = " "

    offset = 0
    fence: tuple[str, int, int] | None = None
    for line in value.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        match = _FENCE_START_RE.match(body)
        if fence is None and match is not None:
            marker = match.group(1)
            fence = (marker[0], len(marker), offset)
        elif fence is not None and match is not None:
            marker = match.group(1)
            trailing = body[match.end() :]
            if (
                marker[0] == fence[0]
                and len(marker) >= fence[1]
                and not trailing.strip(" \t")
            ):
                mask(fence[2], offset + len(line))
                fence = None
        offset += len(line)
    if fence is not None:
        mask(fence[2], len(value))

    visible = "".join(chars)
    chars = list(visible)
    escaped = _escaped_positions(visible)
    index = 0
    while index < len(visible):
        if visible[index] != "`":
            index += 1
            continue
        if escaped[index]:
            index += 1
            continue
        run_end = index + 1
        while run_end < len(visible) and visible[run_end] == "`":
            run_end += 1
        run_length = run_end - index
        cursor = run_end
        closing_end = None
        while cursor < len(visible):
            if visible[cursor] != "`":
                cursor += 1
                continue
            candidate_end = cursor + 1
            while candidate_end < len(visible) and visible[candidate_end] == "`":
                candidate_end += 1
            if candidate_end - cursor == run_length:
                closing_end = candidate_end
                break
            cursor = candidate_end
        mask(index, closing_end if closing_end is not None else len(visible))
        index = closing_end if closing_end is not None else len(visible)
    return "".join(chars)


def _escaped_positions(value: str) -> tuple[bool, ...]:
    """Return whether each character has an odd preceding backslash run."""

    result: list[bool] = []
    backslashes = 0
    for char in value:
        result.append(backslashes % 2 == 1)
        backslashes = backslashes + 1 if char == "\\" else 0
    return tuple(result)


def _masked_markdown_destinations(value: str) -> str:
    """Hide inline link destinations while retaining their visible labels."""

    chars = list(value)
    escaped = _escaped_positions(value)
    brackets: list[int] = []
    index = 0
    while index < len(value):
        if escaped[index]:
            index += 1
            continue
        if value[index] == "[":
            brackets.append(index)
            index += 1
            continue
        if value[index] != "]" or not brackets:
            index += 1
            continue
        brackets.pop()
        if index + 1 >= len(value) or value[index + 1] != "(" or escaped[index + 1]:
            index += 1
            continue
        depth = 1
        cursor = index + 2
        angle = False
        quote = ""
        title_position = False
        while cursor < len(value):
            if escaped[cursor]:
                cursor += 1
                continue
            char = value[cursor]
            if angle:
                if char == ">":
                    angle = False
            elif quote:
                if char == quote:
                    quote = ""
            elif char == "<":
                angle = True
            elif char in {'"', "'"} and title_position:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    for masked in range(index + 1, cursor + 1):
                        if chars[masked] not in "\r\n":
                            chars[masked] = " "
                    index = cursor
                    break
            if not angle and not quote:
                title_position = depth == 1 and char in " \t\r\n"
            cursor += 1
        if depth:
            for masked in range(index + 1, len(value)):
                if chars[masked] not in "\r\n":
                    chars[masked] = " "
            break
        index += 1
    return "".join(chars)


def _masked_bare_uris(value: str) -> str:
    chars = list(value)
    cursor = 0
    while match := _BARE_URI_START_RE.search(value, cursor):
        end = match.end()
        parentheses = 0
        brackets = 0
        while end < len(value) and value[end] not in "\r\n\t <>{}":
            char = value[end]
            if char == "(":
                parentheses += 1
            elif char == ")":
                if end + 1 < len(value) and value[end + 1] == "@" and parentheses == 0:
                    break
                parentheses = max(0, parentheses - 1)
            elif char == "[":
                brackets += 1
            elif char == "]":
                if end + 1 < len(value) and value[end + 1] == "@" and brackets == 0:
                    break
                brackets = max(0, brackets - 1)
            elif char in {",", "!"} and end + 1 < len(value) and value[end + 1] == "@":
                break
            end += 1
        for index in range(match.start(), end):
            if chars[index] not in "\r\n":
                chars[index] = " "
        cursor = max(end, match.end())
    return "".join(chars)


class _VisibleHTMLParser(HTMLParser):
    """Collect only rendered text from Markdown's embedded HTML."""

    _HIDDEN = frozenset({"script", "style", "template"})
    _BREAKS = frozenset({
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "caption",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hgroup",
        "hr",
        "li",
        "legend",
        "main",
        "menu",
        "nav",
        "ol",
        "p",
        "pre",
        "search",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self._HIDDEN:
            self.hidden.append(tag)
        elif not self.hidden and tag in self._BREAKS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self._HIDDEN:
            self.hidden.append(tag)
            if tag in {"script", "style"}:
                self.set_cdata_mode(tag)
        elif not self.hidden and tag in self._BREAKS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if self.hidden[-1] == tag:
                self.hidden.pop()
            return
        if tag in self._BREAKS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _visible_html_text(value: str) -> str:
    parser = _VisibleHTMLParser()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


def _has_mention_boundary(value: str, index: int) -> bool:
    if index == 0:
        return True
    previous = value[index - 1]
    category = unicodedata.category(previous)
    return previous not in "._%+\\-/:?#=&" and category[0] not in {"L", "M", "N"}


def visible_mention_text(value: str) -> str:
    """Expose only visible prose to the mention resolver."""
    return _masked_bare_uris(
        _masked_markdown_destinations(_visible_html_text(_masked_markdown_code(value)))
    )
