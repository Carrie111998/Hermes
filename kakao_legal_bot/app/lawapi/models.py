"""Normalised shapes for everything the Korean law open APIs return.

Each upstream service uses its own Korean field names (법령명한글, 사건명,
판례일련번호…). Mapping them onto one dataclass keeps the prompt builder
and the MCP layer from having to know which endpoint a hit came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Upstream key → our field. Longest/most specific keys first; the resolver
# takes the first key present on the record.
TITLE_KEYS = (
    "법령명한글",
    "법령명",
    "사건명",
    "제목",
    "자치법규명",
    "행정규칙명",
    "안건명",
    "별표명",
    "생활법령명",
    "title",
)
ID_KEYS = (
    "판례일련번호",
    "법령일련번호",
    "자치법규일련번호",
    "행정규칙일련번호",
    "별표일련번호",
    "헌재결정례일련번호",
    "해석례일련번호",
    "id",
    "ID",
    "prcdntSn",
    "seq",
)
DATE_KEYS = (
    "선고일자",
    "시행일자",
    "공포일자",
    "발령일자",
    "종국일자",
    "결정일자",
    "제개정일자",
    "date",
    "dcsnDe",
)
ACTOR_KEYS = (
    "법원명",
    "소관부처명",
    "제개정구분명",
    "기관명",
    "지자체기관명",
    "부처명",
    "court",
    "courtName",
)
NUMBER_KEYS = ("사건번호", "법령구분명", "공포번호", "발령번호", "번호", "caseNo", "csNo")
LINK_KEYS = (
    "판례상세링크",
    "법령상세링크",
    "자치법규상세링크",
    "행정규칙상세링크",
    "별표서식파일링크",
    "상세링크",
    "link",
    "url",
)
BODY_KEYS = (
    "판례내용",
    "판결요지",
    "판시사항",
    "조문내용",
    "본문",
    "내용",
    "요지",
    "결정요지",
    "content",
    "text",
)


def _pick(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, dict):
            # law.go.kr XML→dict conversion sometimes wraps text nodes.
            value = value.get("#text") or value.get("value")
        if isinstance(value, (list, tuple)):
            value = " ".join(str(item) for item in value if item)
        if value not in (None, "", [], {}):
            return str(value).strip()
    return ""


def _format_date(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 8:
        return f"{digits[:4]}. {int(digits[4:6])}. {int(digits[6:8])}."
    return raw


@dataclass(frozen=True)
class LawDoc:
    kind: str  # law | prec | admrul | ordin | detc | expc | byeolpyo | life | cc_prec
    doc_id: str
    title: str
    number: str = ""
    date: str = ""
    actor: str = ""  # 법원명 / 소관부처
    link: str = ""
    body: str = ""
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_record(cls, kind: str, record: dict[str, Any]) -> LawDoc:
        return cls(
            kind=kind,
            doc_id=_pick(record, ID_KEYS),
            title=_pick(record, TITLE_KEYS),
            number=_pick(record, NUMBER_KEYS),
            date=_format_date(_pick(record, DATE_KEYS)),
            actor=_pick(record, ACTOR_KEYS),
            link=_pick(record, LINK_KEYS),
            body=_pick(record, BODY_KEYS),
            extra=record,
        )

    @property
    def citation(self) -> str:
        """How a Korean lawyer would actually cite this in a sentence."""
        if self.kind in {"prec", "detc", "cc_prec"}:
            parts = [part for part in (self.actor, self.date, self.number) if part]
            head = " ".join(parts)
            if head and self.title:
                return f"{head} 판결 ({self.title})" if self.kind == "prec" else f"{head} ({self.title})"
            return head or self.title
        parts = [self.title]
        if self.date:
            parts.append(f"[시행 {self.date}]")
        return " ".join(part for part in parts if part)

    def to_prompt_block(self, max_body: int = 1200) -> str:
        lines = [f"■ {self.citation}"]
        if self.link:
            lines.append(f"  링크: {self.link}")
        if self.body:
            body = " ".join(self.body.split())
            lines.append(f"  {body[:max_body]}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.doc_id,
            "title": self.title,
            "number": self.number,
            "date": self.date,
            "actor": self.actor,
            "link": self.link,
            "body": self.body[:4000],
            "citation": self.citation,
        }
