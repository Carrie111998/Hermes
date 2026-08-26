"""법령·판례 인용을 하나의 키로 모은다.

같은 조문이 문서마다 다르게 적힙니다.

    민28 · 민28조 · 민법28 · 민법28조 · 민법 제28조 · 민법제28조 제1항 · 민법 제28조①

이것들이 서로 다른 낱말로 남아 있으면 백링크도 허브노트도 그래프 검색도
전부 헛돕니다. 그래서 인용 해석은 **LLM에게 맡기지 않고** 여기서 결정적으로
처리합니다 — 같은 입력이면 언제나 같은 키가 나와야 하고, 틀리면 테스트가
잡아야 하기 때문입니다.

약칭 표는 ``knowledge/law_aliases.json`` 이고, 코드를 고치지 않고 늘릴 수
있습니다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

ALIAS_FILE = Path(__file__).resolve().parent.parent.parent / "knowledge" / "law_aliases.json"

# 동그라미 숫자는 항을 뜻합니다 (민법 제28조① = 제28조 제1항).
_CIRCLED = {chr(0x2460 + index): index + 1 for index in range(20)}

# 연도 뒤에 오지만 사건부호가 아닌 글자들. "2018년 12월" 을 사건번호로 읽으면
# 없는 판례가 그래프에 생깁니다.
_NOT_A_CASE_CODE = {
    "년", "월", "일", "원", "명", "개", "회", "번", "차", "쪽", "면", "항", "조",
    "호", "권", "판", "년도", "년대", "여", "경", "억", "만",
}

_HANGUL = re.compile(r"[가-힣]")


# ── 약칭 표 ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AliasTable:
    aliases: dict[str, str]
    full_names: tuple[str, ...]
    case_codes: tuple[str, ...]


@lru_cache(maxsize=1)
def alias_table() -> AliasTable:
    try:
        raw = json.loads(ALIAS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    aliases = {
        str(key).replace(" ", ""): str(value).strip()
        for key, value in (raw.get("약칭") or {}).items()
        if str(key).strip() and str(value).strip()
    }
    # 정식 명칭 자체도 그대로 인식되어야 합니다.
    full = sorted({value for value in aliases.values()}, key=len, reverse=True)
    codes = tuple(
        sorted(
            {str(code).strip() for code in (raw.get("사건부호") or []) if str(code).strip()},
            key=len,
            reverse=True,
        )
    )
    return AliasTable(aliases=aliases, full_names=tuple(full), case_codes=codes)


def normalise_law_name(raw: str) -> str:
    """'민' · '민법전' · '민법' → '민법'. 모르는 이름은 공백만 정리해 돌려준다."""
    text = re.sub(r"\s+", " ", (raw or "")).strip().strip("「」『』\"'")
    if not text:
        return ""
    table = alias_table()
    squeezed = text.replace(" ", "")
    if squeezed in table.aliases:
        return table.aliases[squeezed]
    for full in table.full_names:
        if squeezed == full.replace(" ", ""):
            return full
    return text


# ── 조문 ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class StatuteRef:
    law: str
    article: int
    branch: int = 0  # 제148조의2 의 '2'
    paragraph: int = 0
    item: int = 0  # 호
    subitem: str = ""  # 목
    historic: bool = False  # '구 민법' 처럼 옛 법으로 인용된 것
    span: tuple[int, int] = (0, 0)

    @property
    def article_label(self) -> str:
        return f"제{self.article}조의{self.branch}" if self.branch else f"제{self.article}조"

    @property
    def key(self) -> str:
        """그래프 노드 키 — **조 단위**로 묶는다.

        허브노트는 '민법 제618조' 하나로 모여야 쓸모가 있습니다. 항·호까지
        쪼개면 같은 조문 이야기가 열 조각으로 흩어집니다.
        """
        return f"{self.law} {self.article_label}"

    @property
    def display(self) -> str:
        parts = [self.key]
        if self.paragraph:
            parts.append(f"제{self.paragraph}항")
        if self.item:
            parts.append(f"제{self.item}호")
        if self.subitem:
            parts.append(f"{self.subitem}목")
        return " ".join(parts)

    @property
    def sort_key(self) -> tuple:
        return (self.law, self.article, self.branch, self.paragraph, self.item, self.subitem)


_ARTICLE_RE = re.compile(
    r"(?:제\s*)?(?P<article>\d{1,4})\s*조"
    r"(?:\s*의\s*(?P<branch>\d{1,3}))?"
    r"(?:\s*(?:제\s*)?(?P<para>\d{1,3})\s*항|\s*(?P<circled>[①-⑳]))?"
    r"(?:\s*(?:제\s*)?(?P<item>\d{1,3})\s*호)?"
    r"(?:\s*(?:제\s*)?(?P<sub>[가-힣])\s*목)?"
)

# '민28', '형355' — 조 자를 생략한 실무 약기. 약칭 바로 뒤에 숫자가 붙을 때만.
_SHORT_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _short_alias_re() -> re.Pattern[str]:
    table = alias_table()
    if "pattern" not in _SHORT_RE_CACHE:
        # 정식 명칭도 넣습니다 — '민법28' 처럼 조 자를 뺀 표기가 흔합니다.
        names = sorted({*table.aliases, *table.full_names}, key=len, reverse=True)
        joined = "|".join(re.escape(name) for name in names) or r"(?!)"
        _SHORT_RE_CACHE["pattern"] = re.compile(
            rf"(?P<alias>{joined})\s*(?:제\s*)?(?P<article>\d{{1,4}})"
            rf"(?:\s*의\s*(?P<branch>\d{{1,3}}))?"
            rf"(?:\s*(?:제\s*)?(?P<para>\d{{1,3}})\s*항|\s*(?P<circled>[①-⑳]))?"
            rf"(?:\s*(?:제\s*)?(?P<item>\d{{1,3}})\s*호)?"
        )
    return _SHORT_RE_CACHE["pattern"]


# "같은 법 제623조" 는 앞에서 말한 그 법입니다 — 새 법령명이 아닙니다.
SAME_LAW = "\x00same"
_SAME_LAW_WORDS = {"같은법", "동법", "위법", "본법", "이법", "당해법", "같은법률", "동조", "같은조"}

# 법령명으로 볼 수 있는 모양: 한글·가운뎃점·괄호만, 숫자 없음.
_PLAUSIBLE_LAW = re.compile(r"^[가-힣][가-힣·()「」\s]{1,38}(?:법률|법|령|규칙|조례|규정|헌법)$")


def _law_from_prefix(prefix: str) -> tuple[str, bool]:
    """조문 바로 앞 글자들에서 법령명을 떼어낸다. ``(법령명, 구법인지)``."""
    tail = prefix.rstrip()
    if not tail:
        return "", False
    # '구 민법' — 폐지·개정 전 조문을 가리키는 인용
    historic = bool(re.search(r"(?:^|[^가-힣])구\s+[^\s]{1,20}$", tail))
    # 「민법」 처럼 법령명을 낫표로 감싸는 표기가 흔합니다.
    squeezed_tail = tail.replace(" ", "").rstrip("」』\"'）)")
    table = alias_table()

    for word in _SAME_LAW_WORDS:
        if squeezed_tail.endswith(word):
            return SAME_LAW, historic

    # ① 정식 명칭이 통째로 붙어 있는 경우 (가장 긴 것 우선)
    for full in table.full_names:
        if squeezed_tail.endswith(full.replace(" ", "")):
            return full, historic

    # ② 약칭 (앞에 한글이 더 붙어 있으면 다른 낱말이다 — '이상28' ≠ '상 28')
    for alias in sorted(table.aliases, key=len, reverse=True):
        if squeezed_tail.endswith(alias):
            before = squeezed_tail[: -len(alias)]
            if not before or not _HANGUL.match(before[-1]):
                return table.aliases[alias], historic

    # ③ 표에 없는 법 — 이름처럼 생긴 마지막 낱말들을 쓴다. 숫자가 섞이면
    #    법령명이 아니라 앞 문장의 꼬리이므로 버립니다.
    words = [word for word in re.split(r"[\s,·(){}\[\]「」]+", tail) if word]
    for size in (6, 5, 4, 3, 2, 1):
        if len(words) < size:
            continue
        candidate = " ".join(words[-size:]).strip()
        if candidate.replace(" ", "") in _SAME_LAW_WORDS:
            return SAME_LAW, historic
        if _PLAUSIBLE_LAW.match(candidate) and candidate not in {"법", "법률"}:
            return normalise_law_name(candidate), historic
    return "", historic


def _build_ref(
    law: str, groups: dict, historic: bool, span: tuple[int, int]
) -> StatuteRef | None:
    if not law:
        return None
    try:
        article = int(groups["article"])
    except (TypeError, ValueError):
        return None
    if article <= 0:
        return None
    paragraph = int(groups["para"]) if groups.get("para") else 0
    if not paragraph and groups.get("circled"):
        paragraph = _CIRCLED.get(groups["circled"], 0)
    return StatuteRef(
        law=law,
        article=article,
        branch=int(groups["branch"]) if groups.get("branch") else 0,
        paragraph=paragraph,
        item=int(groups["item"]) if groups.get("item") else 0,
        subitem=(groups.get("sub") or ""),
        historic=historic,
        span=span,
    )


def parse_statutes(text: str, default_law: str = "") -> list[StatuteRef]:
    """본문에서 조문 인용을 모두 뽑는다.

    법령명은 같은 줄 안에서 이어집니다 — "민법 제28조, 제29조" 처럼 두 번째
    인용에 법령명이 생략되는 것이 법률문서의 보통 문장이기 때문입니다. 줄이
    바뀌면 다시 ``default_law`` 로 돌아갑니다(주석서 한 권이 통째로 민법이면
    그것을 기본값으로 두시면 됩니다).
    """
    if not text:
        return []
    default = normalise_law_name(default_law)
    found: list[StatuteRef] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        current = default
        taken: list[tuple[int, int]] = []
        for match in _ARTICLE_RE.finditer(line):
            law, historic = _law_from_prefix(line[: match.start()])
            if law and law != SAME_LAW:
                current = law
            ref = _build_ref(
                current, match.groupdict(), historic, (offset + match.start(), offset + match.end())
            )
            if ref is not None:
                found.append(ref)
            taken.append((match.start(), match.end()))
        # '민28' 처럼 조 자가 없는 약기는 위에서 안 잡히므로 따로 훑습니다.
        for match in _short_alias_re().finditer(line):
            if any(match.start() < end and start < match.end() for start, end in taken):
                continue  # 이미 위에서 잡은 인용
            before = line[: match.start()]
            if before and _HANGUL.match(before[-1]):
                continue  # 다른 낱말의 꼬리
            alias = match.group("alias")
            law = alias_table().aliases.get(alias, "") or normalise_law_name(alias)
            ref = _build_ref(
                law, match.groupdict(), False, (offset + match.start(), offset + match.end())
            )
            if ref is not None:
                found.append(ref)
        offset += len(line)
    return found


def parse_statute(text: str, default_law: str = "") -> StatuteRef | None:
    """인용 하나만 들어 있는 짧은 문자열용."""
    refs = parse_statutes(text, default_law)
    return refs[0] if refs else None


# ── 판례 ─────────────────────────────────────────────────────────────────
_COURT_ALIASES = {
    "대판": "대법원",
    "대법": "대법원",
    "대법원": "대법원",
    "헌재": "헌법재판소",
    "헌법재판소": "헌법재판소",
}

_COURT_RE = re.compile(
    r"(대법원|대법|대판|헌법재판소|헌재|특허법원|서울고등법원|[가-힣]{2,6}고등법원"
    r"|서울중앙지방법원|서울행정법원|서울가정법원|서울회생법원|[가-힣]{2,8}지방법원"
    r"|[가-힣]{2,8}지원|[가-힣]{2,8}가정법원)"
)

_DATE_RE = re.compile(
    r"(?P<y>\d{4})\s*[.년]\s*(?P<m>\d{1,2})\s*[.월]\s*(?P<d>\d{1,2})\s*[.일]?"
)


def parse_date(text: str) -> str:
    """'2018. 3. 15.' · '2018년 3월 15일' → '2018-03-15'. 못 읽으면 빈 문자열."""
    match = _DATE_RE.search(text or "")
    if match is None:
        return ""
    year, month, day = int(match["y"]), int(match["m"]), int(match["d"])
    if not (1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


@dataclass(frozen=True)
class CaseRef:
    year: int
    code: str  # 다 · 도 · 헌마 …
    number: str
    court: str = ""
    decided_on: str = ""  # 선고일 (ISO)
    en_banc: bool = False  # 전원합의체
    span: tuple[int, int] = (0, 0)

    @property
    def case_no(self) -> str:
        return f"{self.year}{self.code}{self.number}"

    @property
    def key(self) -> str:
        """사건번호만으로 충분히 유일합니다 — 법원명 표기가 문서마다 다릅니다."""
        return self.case_no

    @property
    def display(self) -> str:
        parts = [part for part in (self.court, self.case_no) if part]
        text = " ".join(parts)
        return f"{text} 전원합의체" if self.en_banc else text


def _case_pattern() -> re.Pattern[str]:
    codes = alias_table().case_codes
    known = "|".join(re.escape(code) for code in codes) or r"(?!)"
    return re.compile(
        rf"(?P<year>(?:19|20)\d{{2}})\s*(?P<code>{known}|[가-힣]{{1,3}})\s*(?P<num>\d{{1,6}})"
        rf"(?P<more>(?:\s*,\s*\d{{1,6}})*)"
    )


@lru_cache(maxsize=1)
def _cases_re() -> re.Pattern[str]:
    return _case_pattern()


def parse_cases(text: str) -> list[CaseRef]:
    """본문에서 판례 인용을 모두 뽑는다 (법원명·선고일·전원합의체 포함)."""
    if not text:
        return []
    known = set(alias_table().case_codes)
    found: list[CaseRef] = []
    for match in _cases_re().finditer(text):
        code = (match["code"] or "").strip()
        if not code or (code not in known and code in _NOT_A_CASE_CODE):
            continue
        before = text[: match.start()]
        if before and (before[-1].isdigit() or before[-1] == "."):
            continue  # 숫자 한복판
        window = before[-60:]
        court_match = None
        for court_match in _COURT_RE.finditer(window):
            pass  # 가장 가까운(마지막) 법원명
        court = _COURT_ALIASES.get(court_match.group(1), court_match.group(1)) if court_match else ""
        after = text[match.end() : match.end() + 30]
        en_banc = "전원합의체" in window[-25:] or "전원합의체" in after
        ref = CaseRef(
            year=int(match["year"]),
            code=code,
            number=match["num"],
            court=court,
            decided_on=parse_date(window),
            en_banc=en_banc,
            span=(match.start(), match.end()),
        )
        found.append(ref)
        # '2017다12345, 12346' — 뒤따르는 번호는 같은 연도·부호입니다.
        for extra in re.findall(r"\d{1,6}", match["more"] or ""):
            found.append(
                CaseRef(
                    year=ref.year,
                    code=ref.code,
                    number=extra,
                    court=court,
                    decided_on=ref.decided_on,
                    en_banc=en_banc,
                    span=ref.span,
                )
            )
    return found


def parse_case(text: str) -> CaseRef | None:
    refs = parse_cases(text)
    return refs[0] if refs else None


# ── 한꺼번에 ─────────────────────────────────────────────────────────────
@dataclass
class Citations:
    statutes: list[StatuteRef] = field(default_factory=list)
    cases: list[CaseRef] = field(default_factory=list)

    def statute_keys(self) -> list[str]:
        return _unique(ref.key for ref in self.statutes)

    def case_keys(self) -> list[str]:
        return _unique(ref.key for ref in self.cases)

    def statute_displays(self) -> list[str]:
        """가장 자세하게 적힌 형태를 조문마다 하나씩 (제1항까지 나온 것 우선)."""
        best: dict[str, StatuteRef] = {}
        for ref in self.statutes:
            current = best.get(ref.key)
            if current is None or ref.sort_key > current.sort_key:
                best[ref.key] = ref
        return [best[key].display for key in sorted(best, key=lambda k: best[k].sort_key)]

    def case_displays(self) -> list[str]:
        best: dict[str, CaseRef] = {}
        for ref in self.cases:
            current = best.get(ref.key)
            if current is None or (not current.court and ref.court):
                best[ref.key] = ref
        return [best[key].display for key in sorted(best)]


def _unique(values) -> list[str]:  # noqa: ANN001
    seen: dict[str, None] = {}
    for value in values:
        if value not in seen:
            seen[value] = None
    return list(seen)


def extract_citations(text: str, default_law: str = "") -> Citations:
    return Citations(
        statutes=parse_statutes(text, default_law=default_law), cases=parse_cases(text)
    )
