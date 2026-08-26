"""``[[중요키워드]]`` — 이미 손으로 달아두신 표시를 최대한 활용한다.

변호사님이 소제목에 한 번만 표시하셨더라도 그 낱말은 그 문서 전체에서 중요한
낱말입니다. 그래서 문서 안에서 **한 번이라도** 링크된 낱말은 본문의 모든
등장 횟수를 세어 가중치로 삼고, 필요하면 나머지 등장에도 링크를 달아줍니다.

코드블록·URL·이미 링크된 자리는 건드리지 않습니다. 원문을 망가뜨리는 자동
치환은 되돌리기가 어렵기 때문입니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# [[대상]] · [[대상|보일 글자]] · [[대상#절]]
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#\n]{1,120})(?:#([^\[\]|\n]{1,80}))?(?:\|([^\[\]\n]{1,120}))?\]\]")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$", re.MULTILINE)
_CODE_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class WikiLink:
    target: str
    section: str = ""
    alias: str = ""
    in_heading: bool = False


def extract_wikilinks(text: str) -> list[WikiLink]:
    """``[[…]]`` 을 등장 순서대로. 같은 대상이 여러 번이면 여러 번 나옵니다."""
    if not text:
        return []
    heading_spans = [(match.start(), match.end()) for match in _HEADING_RE.finditer(text)]
    links: list[WikiLink] = []
    for match in _WIKILINK_RE.finditer(text):
        target = (match.group(1) or "").strip()
        if not target:
            continue
        in_heading = any(start <= match.start() < end for start, end in heading_spans)
        links.append(
            WikiLink(
                target=target,
                section=(match.group(2) or "").strip(),
                alias=(match.group(3) or "").strip(),
                in_heading=in_heading,
            )
        )
    return links


def linked_targets(text: str) -> list[str]:
    """중복 없이, 처음 나온 순서대로."""
    seen: dict[str, None] = {}
    for link in extract_wikilinks(text):
        seen.setdefault(link.target, None)
    return list(seen)


def _mask(text: str) -> str:
    """치환하면 안 되는 자리를 같은 길이의 공백으로 덮는다."""
    masked = list(text)

    def blank(pattern: re.Pattern[str]) -> None:
        for match in pattern.finditer(text):
            for index in range(match.start(), match.end()):
                masked[index] = "\x00"

    blank(_CODE_FENCE_RE)
    blank(_INLINE_CODE_RE)
    blank(_URL_RE)
    blank(_WIKILINK_RE)
    return "".join(masked)


def keyword_weights(text: str, extra: list[str] | None = None) -> dict[str, int]:
    """링크된 낱말이 이 문서에 **몇 번** 나오는지.

    소제목에만 ``[[ ]]`` 표시가 있어도 본문의 등장까지 모두 셉니다 — 그것이
    그 문서가 무엇에 관한 문서인지를 가장 정직하게 말해 줍니다. 제목·소제목에
    나온 낱말은 한 번을 세 번으로 칩니다(그 문서의 주제라는 뜻이므로).
    """
    if not text:
        return {}
    targets = linked_targets(text)
    for value in extra or []:
        value = (value or "").strip()
        if value and value not in targets:
            targets.append(value)
    if not targets:
        return {}

    headings = "\n".join(match.group(1) for match in _HEADING_RE.finditer(text))
    body = _mask(text)
    weights: dict[str, int] = {}
    for target in targets:
        if not target:
            continue
        # 링크 자체 1회 + 본문에서 맨 낱말로 등장한 횟수
        plain = body.count(target)
        linked = sum(1 for link in extract_wikilinks(text) if link.target == target)
        bonus = 2 if target in headings else 0
        weights[target] = max(1, plain + linked + bonus)
    return weights


def promote_links(text: str, targets: list[str] | None = None, limit_per_target: int = 0) -> str:
    """링크되지 않은 등장 자리에도 ``[[ ]]`` 를 달아 준다.

    ``limit_per_target`` 이 0이면 전부, 3이면 문서당 세 번까지만 답니다.
    옵시디언 그래프만 볼 것이라면 굳이 본문을 고칠 필요는 없습니다 — 가중치
    계산은 ``keyword_weights`` 만으로 충분합니다.
    """
    if not text:
        return text
    wanted = [value.strip() for value in (targets or linked_targets(text)) if value.strip()]
    if not wanted:
        return text
    # 긴 낱말부터 — '임대차보증금' 을 '임대차' 가 먼저 먹지 않도록.
    wanted.sort(key=len, reverse=True)

    result = text
    for target in wanted:
        masked = _mask(result)
        pieces: list[str] = []
        cursor = 0
        done = 0
        start = masked.find(target)
        while start != -1:
            if limit_per_target and done >= limit_per_target:
                break
            pieces.append(result[cursor:start])
            pieces.append(f"[[{target}]]")
            cursor = start + len(target)
            done += 1
            masked = masked[:start] + "\x00" * len(target) + masked[cursor:]
            start = masked.find(target, cursor)
        if not pieces:
            continue
        pieces.append(result[cursor:])
        result = "".join(pieces)
    return result
