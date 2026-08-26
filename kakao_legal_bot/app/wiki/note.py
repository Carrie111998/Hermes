"""WIKI 노트 — 원문 위에 얹는 얇고 엄격한 층.

노트 하나는 frontmatter(기계가 읽는 부분)와 본문(사람과 LLM이 읽는 부분)으로
이루어집니다. frontmatter가 이 데이터베이스의 계약서입니다.

    ---
    title: 임대차보증금 반환청구
    kind: 판례
    source: raw/판례/2017다12345.md
    decided_on: 2018-03-15
    statutes: [민법 제618조, 주택임대차보호법 제3조]
    cases: [2016다212524]
    keywords: [임대차보증금, 대항력]
    ---

날짜를 필수로 둔 이유가 있습니다. 개정 전 조문은 **연혁조문으로만 의미가
있고 지금은 적용되지 않습니다.** 날짜가 없으면 낡은 설명과 현행 규정을
구별할 방법이 없고, 그러면 이 데이터베이스는 틀린 답을 자신 있게 내놓습니다.

YAML 전체를 지원하지는 않습니다 — 우리가 쓰는 형태(문자열·목록)만 읽고 씁니다.
의존성을 하나 더 얹는 것보다 이 편이 낫고, 못 읽는 형태는 조용히 넘기는 대신
lint가 잡아냅니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .citation import entity_key, extract_citations
from .links import defined_terms, heading_terms, keyword_weights, linked_targets

FENCE = "---"

# 자료의 성격. 무엇을 반드시 적어야 하는지가 여기서 갈립니다.
CASE = "판례"
STATUTE = "법령"
COMMENTARY = "주석서"
BOOK = "서적"
PRACTICE = "실무편람"
FORM = "서식"
OTHER = "기타"
KINDS = (CASE, STATUTE, COMMENTARY, BOOK, PRACTICE, FORM, OTHER)

_LIST_FIELDS = ("statutes", "cases", "keywords", "tags", "sources")
_DATE_FIELDS = ("written_on", "effective_on", "promulgated_on", "decided_on", "amended_on")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ── frontmatter ──────────────────────────────────────────────────────────
def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _split_flow_list(value: str) -> list[str]:
    inner = value.strip()[1:-1]
    return [_unquote(part) for part in inner.split(",") if _unquote(part)]


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """``(필드, 본문)``. frontmatter가 없으면 ``({}, 원문)``."""
    if not text:
        return {}, ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FENCE:
        return {}, text
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == FENCE)
    except StopIteration:
        return {}, text  # 닫히지 않은 frontmatter — 본문으로 본다

    fields: dict[str, object] = {}
    key = ""
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and key:
            fields.setdefault(key, [])
            current = fields[key]
            if isinstance(current, list):
                current.append(_unquote(line.lstrip()[2:]))
            continue
        head, separator, value = line.partition(":")
        if not separator:
            continue
        key = head.strip()
        value = value.strip()
        if not key:
            continue
        if value.startswith("[") and value.endswith("]"):
            fields[key] = _split_flow_list(value)
        elif value == "":
            fields[key] = []  # 아래 '- ' 줄들이 채웁니다
        else:
            fields[key] = _unquote(value)
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return fields, body


def _needs_quoting(value: str) -> bool:
    return bool(value) and (
        value[0] in "[]{}#&*!|>%@`\"'" or ": " in value or value.strip() != value
    )


def dump_frontmatter(fields: dict[str, object]) -> str:
    lines = [FENCE]
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            items = [str(item).strip() for item in value if str(item).strip()]
            rendered = ", ".join(f'"{item}"' if _needs_quoting(item) else item for item in items)
            lines.append(f"{key}: [{rendered}]")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        else:
            text = "" if value is None else str(value)
            lines.append(f'{key}: "{text}"' if _needs_quoting(text) else f"{key}: {text}")
    lines.append(FENCE)
    return "\n".join(lines)


# ── 노트 ─────────────────────────────────────────────────────────────────
@dataclass
class WikiNote:
    path: str = ""
    title: str = ""
    kind: str = OTHER
    source: str = ""  # 원문 raw 파일
    collection: str = ""
    body: str = ""

    written_on: str = ""  # 자료 작성·발행일
    effective_on: str = ""  # 법령 시행일
    promulgated_on: str = ""  # 공포일
    amended_on: str = ""  # 개정일
    decided_on: str = ""  # 판례 선고일

    court: str = ""
    case_no: str = ""
    case_name: str = ""

    statutes: list[str] = field(default_factory=list)
    cases: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    weights: dict[str, int] = field(default_factory=dict)

    superseded_by: str = ""
    verified: bool = False
    extra: dict[str, object] = field(default_factory=dict)

    # ── 읽기 ─────────────────────────────────────────────────────────────
    @classmethod
    def from_markdown(cls, text: str, path: str = "") -> WikiNote:
        fields, body = parse_frontmatter(text)

        def one(key: str) -> str:
            value = fields.pop(key, "")
            return str(value).strip() if not isinstance(value, list) else ""

        def many(key: str) -> list[str]:
            value = fields.pop(key, [])
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
            return [part.strip() for part in str(value).split(",") if part.strip()]

        note = cls(
            path=path,
            title=one("title"),
            kind=one("kind") or OTHER,
            source=one("source"),
            collection=one("collection"),
            body=body,
            written_on=one("written_on"),
            effective_on=one("effective_on"),
            promulgated_on=one("promulgated_on"),
            amended_on=one("amended_on"),
            decided_on=one("decided_on"),
            court=one("court"),
            case_no=one("case_no"),
            case_name=one("case_name"),
            statutes=many("statutes"),
            cases=many("cases"),
            keywords=many("keywords"),
            superseded_by=one("superseded_by"),
            verified=str(fields.pop("verified", "")).strip().lower() in {"true", "yes", "1"},
            extra=fields,
        )
        if not note.title:
            note.title = _first_heading(body) or Path(path).stem
        return note

    @classmethod
    def load(cls, path: Path | str) -> WikiNote:
        file = Path(path)
        return cls.from_markdown(file.read_text(encoding="utf-8"), str(file))

    # ── 쓰기 ─────────────────────────────────────────────────────────────
    def to_markdown(self) -> str:
        fields: dict[str, object] = {"title": self.title, "kind": self.kind}
        for key in ("source", "collection"):
            if getattr(self, key):
                fields[key] = getattr(self, key)
        for key in _DATE_FIELDS:
            if getattr(self, key):
                fields[key] = getattr(self, key)
        for key in ("court", "case_no", "case_name"):
            if getattr(self, key):
                fields[key] = getattr(self, key)
        for key in ("statutes", "cases", "keywords"):
            if getattr(self, key):
                fields[key] = getattr(self, key)
        if self.superseded_by:
            fields["superseded_by"] = self.superseded_by
        fields["verified"] = self.verified
        for key, value in self.extra.items():
            fields.setdefault(key, value)
        return f"{dump_frontmatter(fields)}\n\n{self.body.strip()}\n"

    # ── 본문에서 채우기 ──────────────────────────────────────────────────
    def enrich(self, default_law: str = "") -> WikiNote:
        """본문을 읽어 조문·판례·키워드를 채운다 (이미 적힌 것은 지우지 않음).

        사람이 적은 frontmatter가 언제나 우선입니다. 여기서 하는 일은 빠뜨린
        것을 더해 주는 것뿐입니다.
        """
        citations = extract_citations(self.body, default_law=default_law)
        self.statutes = _prefer_spaced(_merge(self.statutes, citations.statute_displays()))
        self.cases = _merge(self.cases, citations.case_keys())

        marked = linked_targets(self.body)
        if marked:
            self.keywords = _merge(self.keywords, marked)
        else:
            # ``[[ ]]`` 표시가 없는 자료도 많습니다. 그런 자료에서는 문서가
            # 스스로 정의한 용어와 소제목이 가장 정직한 주제어입니다.
            self.keywords = _merge(
                self.keywords, [*defined_terms(self.body), *heading_terms(self.body)]
            )
        self.weights = keyword_weights(self.body, extra=self.keywords)

        if self.kind == CASE:
            if not self.case_no and citations.cases:
                first = citations.cases[0]
                self.case_no = first.key
                self.court = self.court or first.court
                self.decided_on = self.decided_on or first.decided_on
            # 그 판례 자신은 '인용한 판례'가 아닙니다.
            self.cases = [key for key in self.cases if key != self.case_no]
        return self

    # ── 편의 ─────────────────────────────────────────────────────────────
    @property
    def as_of(self) -> str:
        """이 노트가 '언제 기준'인지. 최신 자료를 고를 때 쓰는 값입니다."""
        for value in (self.effective_on, self.decided_on, self.amended_on, self.written_on):
            if value:
                return value
        return ""

    @property
    def entity_keys(self) -> list[str]:
        from .citation import parse_statute

        keys: list[str] = []
        for written in self.statutes:
            ref = parse_statute(written)
            keys.append(ref.key if ref else written)
        keys.extend(self.cases)
        if self.case_no:
            keys.append(self.case_no)
        keys.extend(self.keywords)
        return _merge([], keys)

    def missing_required(self) -> list[str]:
        """이 종류의 자료라면 반드시 있어야 하는데 빠진 것들."""
        missing: list[str] = []
        if not self.title:
            missing.append("title")
        if self.kind not in KINDS:
            missing.append(f"kind (알 수 없는 값: {self.kind})")
        if self.kind == CASE:
            if not self.case_no:
                missing.append("case_no")
            if not self.decided_on:
                missing.append("decided_on (선고일)")
        elif self.kind == STATUTE:
            if not self.effective_on:
                missing.append("effective_on (시행일)")
            if not self.statutes:
                missing.append("statutes (조문)")
        elif not self.written_on:
            missing.append("written_on (자료 작성·발행일)")
        for key in _DATE_FIELDS:
            value = getattr(self, key)
            if value and not _ISO_DATE.match(value):
                missing.append(f"{key} 는 YYYY-MM-DD 여야 합니다 (지금: {value})")
        return missing


def _prefer_spaced(values: list[str]) -> list[str]:
    """같은 조문이 띄어쓰기만 다르게 두 번 실리지 않게.

    온주의 링크는 ``고용보험및산업재해보상보험의…`` 처럼 붙여 쓰고 본문은
    띄어 씁니다. 둘은 같은 조문이므로 읽기 좋은 쪽 하나만 남깁니다.
    """
    from .citation import parse_statute

    def group_of(value: str) -> str:
        ref = parse_statute(value)
        return entity_key(ref.key if ref is not None else value)

    def better(candidate: str, current: str) -> bool:
        # 자세한 쪽(제1항까지 적힌 것)을, 같은 자세함이면 띄어 쓴 쪽을.
        if len(candidate) != len(current):
            return len(candidate) > len(current)
        return candidate.count(" ") > current.count(" ")

    best: dict[str, str] = {}
    for value in values:
        key = group_of(value)
        current = best.get(key)
        if current is None or better(value, current):
            best[key] = value
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(best[group_of(value)], None)
    return list(seen)


def _merge(existing: list[str], additions: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in [*existing, *additions]:
        value = (value or "").strip()
        if value:
            seen.setdefault(value, None)
    return list(seen)


def _first_heading(body: str) -> str:
    match = re.search(r"^\s{0,3}#{1,6}\s+(.+)$", body or "", re.MULTILINE)
    return match.group(1).strip() if match else ""


def today() -> str:
    return date.today().isoformat()
