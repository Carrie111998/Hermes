"""실제 자료 파일에서 노트를 만든다 — 온주 HTML · 주석서 · 단행본.

변환된 ``.md`` 보다 **원본 HTML 이 훨씬 많은 것을 담고 있습니다.** 온주 주석서를
예로 들면, 변환본은 소제목(1. 피보험자, 2. 이직 …)을 통째로 잃고 각주 번호가
본문에 섞여 들어가면서 ``제1조의2`` 가 ``제1조의{2)}`` 로 망가집니다. 조문 번호가
망가지면 이 데이터베이스에서 가장 중요한 연결이 끊깁니다.

그래서 HTML 이 있으면 HTML 을 읽습니다. 여기서 얻는 것:

* 법령명 · 조문 · 집필자 · 출판일 (숨은 입력값에 그대로 있습니다)
* ``[[시행일 2021.7.1]]`` · ``[개정 2008.12.31, …]`` → 시행일과 개정일
* 소제목 계층 (Ⅰ. / 1. ) 과 문단번호(방주번호)
* ``hyperlink('0','법률명','48X2')`` → **법령명과 조문이 짝지어진** 인용
  (본문 글자를 읽어 짐작하는 것보다 정확합니다)
* 각주와 그 안의 판례
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from .citation import extract_citations, normalise_law_name, parse_date
from .note import BOOK, CASE, COMMENTARY, STATUTE, WikiNote

# ── 숨은 입력값 ──────────────────────────────────────────────────────────
_HIDDEN = re.compile(
    r"""<input[^>]*id=["'](?P<id>hdn[A-Za-z]+)["'][^>]*value=["'](?P<value>[^"']*)["']""",
    re.IGNORECASE,
)
_HIDDEN_REVERSED = re.compile(
    r"""<input[^>]*value=["'](?P<value>[^"']*)["'][^>]*id=["'](?P<id>hdn[A-Za-z]+)["']""",
    re.IGNORECASE,
)

# 온주는 시행일을 위키링크처럼 [[ ]] 로 감싸 둡니다. 링크가 아니라 날짜입니다.
_EFFECTIVE = re.compile(r"\[\[\s*시행일\s*([0-9]{4}\s*[.\-][0-9.\-\s]+)\]\]")
_AMENDED = re.compile(r"\[\s*개정\s*([^\]]{4,200})\]")
_HYPERLINK = re.compile(
    r"""hyperlink\(\s*['"](?P<kind>[01])['"]\s*,\s*['"](?P<a>[^'"]+)['"]\s*,"""
    r"""\s*['"](?P<b>[^'"]+)['"]\s*\)"""
)


def _hidden_fields(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for pattern in (_HIDDEN, _HIDDEN_REVERSED):
        for match in pattern.finditer(text):
            found.setdefault(match["id"], html.unescape(match["value"]).strip())
    return found


def _iso(korean_date: str) -> str:
    """'2022. 3. 2' · '2021.7.1' → '2022-03-02'."""
    numbers = re.findall(r"\d+", korean_date or "")
    if len(numbers) < 3:
        return ""
    year, month, day = (int(value) for value in numbers[:3])
    if not (1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def _article_from_link(raw: str) -> str:
    """온주 링크의 조문 표기 — ``48X2`` 는 제48조의2 입니다."""
    match = re.fullmatch(r"(\d{1,4})(?:X(\d{1,3}))?", (raw or "").strip().upper())
    if match is None:
        return ""
    return f"제{match.group(1)}조의{match.group(2)}" if match.group(2) else f"제{match.group(1)}조"


# ── 온주 HTML ────────────────────────────────────────────────────────────
_BLOCK_CLASSES = {"title_1", "title_2", "title_3", "doc_content", "grayname"}


class _OnjuBody(HTMLParser):
    """주석 본문을 소제목·문단으로 되살린다.

    떠 있는 각주 상자(``miju_box``)는 본문에 또 한 번 인쇄되므로 건너뜁니다.
    그대로 두면 같은 판례가 두 번 세어져 그래프가 부풀어요.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self.footnotes: list[tuple[str, str]] = []
        self._stack: list[str] = []
        self._buffer: list[str] = []
        self._current = ""
        self._skip_depth = 0
        self._in_footnote_area = False
        self._footnote_no = ""

    # ── 도우미 ───────────────────────────────────────────────────────────
    def _classes(self, attrs: list[tuple[str, str | None]]) -> set[str]:
        for name, value in attrs:
            if name == "class" and value:
                return set(value.split())
        return set()

    def _flush(self) -> None:
        text = re.sub(r"[ \t]+", " ", "".join(self._buffer)).strip()
        self._buffer.clear()
        if not text:
            return
        if self._in_footnote_area:
            self.footnotes.append((self._footnote_no, text))
        elif self._current:
            self.blocks.append((self._current, text))

    # ── HTMLParser ───────────────────────────────────────────────────────
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if self._skip_depth:
            if tag == "div":
                self._skip_depth += 1
            return
        if tag == "div" and ("miju_box" in classes or "miju_num" in classes):
            # 떠 있는 각주 상자 — 아래 miju 영역에 같은 내용이 또 나옵니다.
            self._flush()
            self._skip_depth = 1
            return
        if tag == "div" and "miju" in classes:
            self._flush()
            self._in_footnote_area = True
            self._current = ""
            return
        if tag == "div" and "mi_content" in classes:
            self._flush()
            self._footnote_no = ""
            return
        if tag == "div" and (classes & _BLOCK_CLASSES):
            self._flush()
            self._current = next(iter(classes & _BLOCK_CLASSES))
            return
        if tag == "br":
            self._buffer.append("\n")
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value and "hyperlink(" in value:
                    self._buffer.append("")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            if tag == "div":
                self._skip_depth -= 1
            return
        if tag == "div":
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_footnote_area and not self._footnote_no:
            marker = re.match(r"\s*(\d{1,3})\)", data)
            if marker is not None:
                self._footnote_no = marker.group(1)
                data = data[marker.end() :]
        self._buffer.append(data)

    def close(self) -> None:  # noqa: D102
        super().close()
        self._flush()


@dataclass
class SourceMeta:
    title: str = ""
    kind: str = BOOK
    main_law: str = ""
    article: str = ""
    author: str = ""
    publisher: str = ""
    edition: str = ""
    written_on: str = ""
    effective_on: str = ""
    amended_on: str = ""
    statutes: list[str] = field(default_factory=list)
    cases: list[str] = field(default_factory=list)


def parse_onju_html(text: str, path: str = "") -> WikiNote:
    """온주 주석서 HTML 한 장 → 위키 노트."""
    fields = _hidden_fields(text)
    main_law = normalise_law_name(fields.get("hdnMainTitle", ""))
    article = (fields.get("hdnJoTitle") or fields.get("hdnJoNum") or "").strip()
    author = fields.get("hdnSubTitle", "")
    written_on = _iso(fields.get("hdnPubDate", ""))

    law_html = _section(text, "onju_preview_law")
    body_html = _section(text, "onju_preview_onju")

    effective = _EFFECTIVE.search(law_html or text)
    amended = _AMENDED.search(law_html or text)
    amend_dates = [_iso(part) for part in re.findall(r"\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}", amended.group(1))] if amended else []

    statute_text = _plain(law_html)
    parser = _OnjuBody()
    parser.feed(body_html)
    parser.close()

    lines: list[str] = []
    title = f"{main_law} {article}".strip() or fields.get("hdnTitle", "") or Path(path).stem
    lines.append(f"# {title}")
    if author:
        lines.append(f"\n집필: {author}" + (f" · 출판일 {written_on}" if written_on else ""))
    if statute_text:
        lines.append("\n## 조문\n")
        lines.append(statute_text)
    lines.append("\n## 주석\n")

    number = ""
    for kind, block in parser.blocks:
        if kind == "grayname":
            number = block.strip()
            continue
        if kind == "title_1":
            lines.append(f"\n### {block.strip()}")
        elif kind in {"title_2", "title_3"}:
            lines.append(f"\n#### {block.strip()}")
        else:
            prefix = f"[{number}] " if number else ""
            lines.append(f"\n{prefix}{block.strip()}")
            number = ""
    if parser.footnotes:
        lines.append("\n## 각주\n")
        for note_no, footnote in parser.footnotes:
            lines.append(f"{note_no or '·'}) {footnote}")

    body = "\n".join(lines).strip() + "\n"

    note = WikiNote(
        path=path,
        title=title,
        kind=COMMENTARY,
        source=path,
        body=body,
        written_on=written_on,
        effective_on=_iso(effective.group(1)) if effective else "",
        amended_on=max(date for date in amend_dates if date) if any(amend_dates) else "",
        extra={key: value for key, value in (("author", author), ("main_law", main_law)) if value},
    )
    # 링크에 적힌 법령명+조문이 본문 글자보다 정확합니다.
    note.statutes = _merge([f"{main_law} {article}".strip()], _linked_statutes(text))
    note.cases = _linked_cases(text)
    return note.enrich(default_law=main_law)


def _section(text: str, class_name: str) -> str:
    """``<div class="…">`` 한 덩어리를 잘라 온다 (중첩 div 를 세면서)."""
    start = text.find(f'class="{class_name}"')
    if start < 0:
        return ""
    open_tag = text.rfind("<div", 0, start)
    if open_tag < 0:
        return ""
    depth = 0
    index = open_tag
    while index < len(text):
        next_open = text.find("<div", index)
        next_close = text.find("</div", index)
        if next_close < 0:
            break
        if 0 <= next_open < next_close:
            depth += 1
            index = next_open + 4
            continue
        depth -= 1
        index = next_close + 5
        if depth == 0:
            return text[open_tag:index]
    return text[open_tag:]


def _plain(fragment: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>", " ", fragment or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    # 잘린 끝자락의 여는 꺾쇠 ("</div" 처럼) 도 지웁니다.
    text = re.sub(r"<[^>]*$", "", text)
    text = html.unescape(text)
    text = unicodedata.normalize("NFC", text)
    # 시행일은 이미 frontmatter 로 뽑았습니다. 본문에 [[ ]] 로 남겨 두면
    # 위키링크로 잘못 읽혀 '시행일 2021.7.1' 이라는 키워드가 생깁니다.
    text = _EFFECTIVE.sub(lambda m: f"(시행 {_iso(m.group(1)) or m.group(1).strip()})", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _linked_statutes(text: str) -> list[str]:
    found: list[str] = []
    for match in _HYPERLINK.finditer(text):
        if match["kind"] != "0":
            continue
        law = normalise_law_name(match["a"])
        article = _article_from_link(match["b"])
        if law and article:
            value = f"{law} {article}"
            if value not in found:
                found.append(value)
    return found


def _linked_cases(text: str) -> list[str]:
    found: list[str] = []
    for match in _HYPERLINK.finditer(text):
        if match["kind"] != "1":
            continue
        case_no = re.sub(r"\s+", "", match["b"])
        if case_no and case_no not in found:
            found.append(case_no)
    return found


def _merge(*groups: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for group in groups:
        for value in group:
            value = (value or "").strip()
            if value:
                seen.setdefault(value, None)
    return list(seen)


# ── 마크다운 자료 ────────────────────────────────────────────────────────
_LAW_IN_TITLE = re.compile(r"([가-힣][가-힣·\s]{1,30}?(?:법률|법))")
_PUB_DATE = re.compile(r"(?:출판일|발간연도|발행일|초판|발간)\s*[:：]?\s*([0-9]{4}[.\-][0-9.\-\s]*)")
_EDITION = re.compile(r"\(?(제\s*\d+\s*판)\)?")
_YEAR_MONTH = re.compile(r"((?:19|20)\d{2})\s*년\s*(\d{1,2})\s*월")

_KIND_WORDS: tuple[tuple[str, str], ...] = (
    ("온주", COMMENTARY),
    ("주석", COMMENTARY),
    ("조문해설", COMMENTARY),
    ("실무편람", BOOK),
    ("판례", CASE),
)


def parse_markdown_source(text: str, path: str = "") -> WikiNote:
    """이미 마크다운으로 바꾸신 자료 — 첫머리에서 서지사항을 읽어 온다."""
    head = "\n".join(text.splitlines()[:40])
    title = _first_heading(text) or Path(path).stem
    main_law = main_law_of(text, path)

    kind = BOOK
    for needle, guessed in _KIND_WORDS:
        if needle in head or needle in path:
            kind = guessed
            break

    written_on = ""
    pub = _PUB_DATE.search(head)
    if pub is not None:
        written_on = _iso(pub.group(1)) or _month_only(pub.group(1))
    if not written_on:
        ym = _YEAR_MONTH.search(head)
        if ym is not None:
            written_on = f"{int(ym.group(1)):04d}-{int(ym.group(2)):02d}-01"

    author = ""
    for pattern in (
        r"\*\*이름:\*\*\s*([^\n(（]+)",
        r"(?:편집대표|저자|집필)\s*[:：]\s*([^\n/]+)",
        r"#\s*변호사\s+([가-힣]{2,5})",
    ):
        match = re.search(pattern, head)
        if match is not None:
            author = match.group(1).strip()
            break

    edition = ""
    match = _EDITION.search(head)
    if match is not None:
        edition = re.sub(r"\s+", "", match.group(1))

    extra: dict[str, object] = {}
    if author:
        extra["author"] = author
    if edition:
        extra["edition"] = edition
    if main_law:
        extra["main_law"] = main_law

    note = WikiNote(
        path=path,
        title=title,
        kind=kind,
        source=path,
        body=text,
        written_on=written_on,
        extra=extra,
    )
    return note.enrich(default_law=main_law)


def _month_only(raw: str) -> str:
    numbers = re.findall(r"\d+", raw or "")
    if len(numbers) >= 2:
        year, month = int(numbers[0]), int(numbers[1])
        if 1900 <= year <= 2100 and 1 <= month <= 12:
            return f"{year:04d}-{month:02d}-01"
    return ""


def _first_heading(text: str) -> str:
    match = re.search(r"^\s{0,3}#{1,6}\s+(.+)$", text or "", re.MULTILINE)
    return re.sub(r"[*_`]", "", match.group(1)).strip() if match else ""


# 책 제목에 붙는 말들. '주석 민사소송법' 의 주된 법령은 민사소송법입니다.
_TITLE_PREFIX = re.compile(r"^(?:온주|주석|주해|조문해설|해설|신|판례|실무|알기\s*쉬운)\s*")


def _is_known_law(name: str) -> bool:
    """약칭 표에 있는 이름인가 — '민법' 처럼 짧아도 진짜 법령명인지."""
    from .citation import alias_table

    table = alias_table()
    squeezed = (name or "").replace(" ", "")
    return squeezed in table.aliases or any(
        squeezed == full.replace(" ", "") for full in table.full_names
    )


def _clean_law_name(raw: str) -> str:
    """'주석서 민법' → '민법'. 제목·경로에 붙은 말들을 앞에서부터 걷어낸다.

    가장 짧은 꼬리부터 봅니다 — 길게 잡으면 폴더 이름까지 법령명이 됩니다.
    """
    name = _TITLE_PREFIX.sub("", re.sub(r"\s+", " ", raw or "").strip())
    words = name.split()
    for start in range(len(words) - 1, -1, -1):
        # 낱말마다 다시 걷어냅니다 — '주석민사소송법' 처럼 붙여 쓴 것도 있어서.
        candidate = _TITLE_PREFIX.sub("", " ".join(words[start:]))
        squeezed = candidate.replace(" ", "")
        if candidate.startswith(("이 ", "그 ", "같은")) or squeezed in {"법", "법률"}:
            continue
        known = _is_known_law(candidate)
        if known or len(squeezed) >= 3:
            resolved = normalise_law_name(candidate)
            # 여러 낱말짜리는 '…에 관한 법률' 꼴일 때만 이름으로 봅니다.
            if len(candidate.split()) > 1 and not known and "관한" not in candidate:
                continue
            return resolved
    return ""


def main_law_of(text: str, path: str = "") -> str:
    """이 자료가 **어느 법에 관한 것인지**. 못 고르면 빈 문자열.

    이것이 정해져야 "제618조" 처럼 법령명을 생략한 인용을 읽을 수 있습니다.
    찾는 순서가 중요합니다 — 본문을 먼저 뒤지면 그 책이 **인용한** 다른 법을
    주된 법으로 잘못 잡습니다. 고용보험법 주석서가 고용산재보험료징수법에
    관한 책이 되어 버리는 식으로요.
    """
    hidden = _hidden_fields(text or "")
    if hidden.get("hdnMainTitle"):
        return normalise_law_name(hidden["hdnMainTitle"])

    # ① 파일 이름과 제목 — 사람이 붙인 것이라 가장 믿을 만합니다.
    from_path = re.sub(r"[/\\\-_.]+", " ", str(path or ""))
    for candidate in (from_path, _first_heading(text or "")):
        for raw in _LAW_IN_TITLE.findall(candidate or ""):
            name = _clean_law_name(raw)
            if name:
                return name

    # ② 그래도 모르면 **가장 많이 인용된 법**. 한 권짜리 교재에 맞는 짐작입니다.
    counts: dict[str, int] = {}
    for ref in extract_citations(text or "").statutes:
        counts[ref.law] = counts.get(ref.law, 0) + 1
    if counts:
        best = max(counts, key=lambda law: counts[law])
        if counts[best] >= 3:
            return best
    return ""


# ── 입구 ─────────────────────────────────────────────────────────────────
def read_source(path: Path | str) -> WikiNote:
    """파일 하나를 노트로. HTML 이면 HTML 로, 아니면 마크다운으로 읽습니다."""
    file = Path(path)
    text = file.read_text(encoding="utf-8", errors="replace")
    if file.suffix.lower() in {".html", ".htm"} or "onju_preview_law" in text[:4000]:
        return parse_onju_html(text, str(file))
    return parse_markdown_source(text, str(file))


def citations_of(text: str, default_law: str = "") -> tuple[list[str], list[str]]:
    citations = extract_citations(text, default_law=default_law)
    return citations.statute_displays(), citations.case_keys()


def source_dates(text: str) -> dict[str, str]:
    """본문에서 읽어낸 날짜들 — 진단용."""
    effective = _EFFECTIVE.search(text or "")
    return {
        "effective_on": _iso(effective.group(1)) if effective else "",
        "first_date": parse_date(text or ""),
    }


# 인식할 수 있는 자료 종류 — build 가 쓰는 목록
KINDS_BY_SOURCE = (COMMENTARY, BOOK, CASE, STATUTE)
