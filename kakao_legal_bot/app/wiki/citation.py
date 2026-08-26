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


# "같은 법 제623조" · "법 제4조" 는 앞에서 말한 그 법입니다 — 새 법령명이 아닙니다.
# 실무서는 "이하 '도시정비법' 또는 '법'으로 약칭한다"라고 해 두고 그 뒤로는
# 줄곧 "법 제4조" 라고만 씁니다.
SAME_LAW = "\x00same"
_SAME_LAW_WORDS = {
    "법", "법률", "같은법", "같은법률", "동법", "위법", "본법", "이법", "당해법",
    "동조", "같은조", "같은시행령", "동시행령",
}

# 법령명으로 볼 수 있는 낱말 하나: 한글·가운뎃점만, 숫자 없음, 세 글자 이상.
_LAW_WORD = re.compile(r"^[가-힣][가-힣·]{1,30}(?:법률|법|령|규칙|조례|규정|헌법|협약|조약)$")
# 「국토의 계획 및 이용에 관한 법률」 처럼 여러 낱말짜리 이름. '관한'이 들어가고
# 법률/법으로 끝나는 것만 — 그래야 앞 문장의 꼬리를 물고 오지 않습니다.
_LONG_LAW = re.compile(r"^[가-힣][가-힣·\s]{4,44}(?:에|등에)\s*관한\s*(?:법률|법)$")
# 시행령·시행규칙은 홀로 서지 못합니다. 앞 낱말을 데려와야 이름이 됩니다.
_DEPENDENT = {"시행령", "시행규칙", "시행세칙", "시행규정"}
_BRACKETED = re.compile(r"[「『]([^「」『』]{2,60})[」』]\s*$")
# (이하 "○○법"이라 한다) 처럼 법령명 뒤에 붙는 곁말
_ASIDE = re.compile(r"[(（][^()（）]{0,80}?(?:이하|약칭|이라)[^()（）]{0,80}?[)）]\s*$")


# 문서가 스스로 정하는 약칭:
#   「고용보험 및 산업재해보상보험의 보험료징수 등에 관한 법률」(이하 "고용산재보험료징수법"이라 한다)
# 이 줄 하나를 읽어 두면 그 문서의 나머지 인용이 전부 제자리를 찾습니다.
_LOCAL_ALIAS = re.compile(
    r"[「『]?(?P<full>[가-힣][가-힣·\s]{3,50}?(?:법률|법|령|규칙))[」』]?\s*"
    r"\(\s*이하\s*[\"'“”‘’]?(?P<short>[가-힣][가-힣A-Za-z]{1,20})[\"'“”‘’]?\s*(?:이)?라\s*(?:고\s*)?한다"
)


def local_aliases(text: str) -> dict[str, str]:
    """그 문서 안에서만 쓰는 약칭 표를 뽑는다."""
    found: dict[str, str] = {}
    for match in _LOCAL_ALIAS.finditer(text or ""):
        short = match["short"].strip()
        full = re.sub(r"\s+", " ", match["full"]).strip()
        if short and full and short != full:
            found.setdefault(short.replace(" ", ""), full)
    return found


def _law_from_prefix(prefix: str, extra: dict[str, str] | None = None) -> tuple[str, bool]:
    """조문 바로 앞 글자들에서 법령명을 떼어낸다. ``(법령명, 구법인지)``.

    법률문서는 조문 앞에 문장을 길게 늘어놓습니다 —
    "…라고 볼 것이다 국제사법 제10조". 여기서 **가장 짧은 이름부터** 보는 것이
    핵심입니다. 길게 잡으면 "볼 것이다 국제사법" 이 통째로 법령명이 됩니다.
    """
    tail = prefix.rstrip()
    if not tail:
        return "", False
    # 「…법률」(이하 "○○법"이라 한다) 제5조 — 괄호 안은 곁말입니다. 떼어내고
    # 그 앞의 법령명을 봅니다. 이걸 안 하면 조문이 통째로 사라집니다.
    while True:
        stripped = _ASIDE.sub("", tail).rstrip()
        if stripped == tail:
            break
        tail = stripped
    if not tail:
        return "", False
    # '구 민법' — 폐지·개정 전 조문을 가리키는 인용
    historic = bool(re.search(r"(?:^|[^가-힣])구\s+[^\s]{1,20}$", tail))

    # ⓪ 「민법」 처럼 낫표로 묶은 것이 가장 정확합니다 — 경계가 분명하니까요.
    bracketed = _BRACKETED.search(tail)
    if bracketed is not None:
        name = bracketed.group(1).strip()
        if name.split()[-1] in _DEPENDENT and len(name.split()) == 1:
            before = tail[: bracketed.start()].split()
            if before:
                name = f"{before[-1]} {name}"
        return normalise_law_name(name), historic

    squeezed_tail = tail.replace(" ", "").rstrip("』\"'）)")
    table = alias_table()
    aliases = {**table.aliases, **(extra or {})}
    full_names = sorted(
        {*table.full_names, *aliases.values()}, key=len, reverse=True
    )

    # ① 정식 명칭이 통째로 붙어 있는 경우 (가장 긴 것 우선)
    for full in full_names:
        if squeezed_tail.endswith(full.replace(" ", "")):
            return full, historic

    # ② 약칭 (앞에 한글이 더 붙어 있으면 다른 낱말이다 — '이상28' ≠ '상 28')
    for alias in sorted(aliases, key=len, reverse=True):
        if squeezed_tail.endswith(alias):
            before = squeezed_tail[: -len(alias)]
            if not before or not _HANGUL.match(before[-1]):
                return aliases[alias], historic

    words = [word for word in re.split(r"[\s,·(){}\[\]「」『』]+", tail) if word]
    if not words:
        return "", historic

    # ③ "법 제4조" · "같은 법 제623조" — 앞에서 말한 그 법입니다.
    #    표에 있는 이름(①②)을 먼저 본 뒤라야 '상법'을 '법'으로 읽지 않습니다.
    if squeezed_tail.endswith(tuple(_SAME_LAW_WORDS)) and not _LAW_WORD.match(words[-1]):
        return SAME_LAW, historic

    # ④ 낱말 하나짜리 이름 — 이것이 대부분입니다.
    last = words[-1]
    if last in _DEPENDENT:
        name = f"{words[-2]} {last}" if len(words) >= 2 and _LAW_WORD.match(words[-2]) else last
        return normalise_law_name(name), historic
    if _LAW_WORD.match(last):
        return normalise_law_name(last), historic

    # ④ "…에 관한 법률" 처럼 여러 낱말짜리 이름만 여기까지 옵니다.
    for size in range(min(9, len(words)), 2, -1):
        candidate = " ".join(words[-size:]).strip()
        if _LONG_LAW.match(candidate):
            return normalise_law_name(candidate), historic
    return "", historic


def _last_word(text: str) -> str:
    words = [word for word in re.split(r"[\s,·(){}\[\]「」『』]+", text) if word]
    return words[-1] if words else ""


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


# 법령명이 이어지는 거리. "민법 제618조, 제623조" 는 8자 뒤지만, 두 문장
# 건너뛴 "제77조의2" 까지 앞의 법령명으로 읽으면 없는 조문이 생깁니다.
CARRY_WINDOW = 60


def parse_statutes(
    text: str,
    default_law: str = "",
    carry_window: int = CARRY_WINDOW,
    aliases: dict[str, str] | None = None,
) -> list[StatuteRef]:
    """본문에서 조문 인용을 모두 뽑는다.

    법령명은 **가까운 거리 안에서만** 이어집니다 — "민법 제618조, 제29조" 처럼
    두 번째 인용에 이름이 생략되는 것이 법률문서의 보통 문장이지만, 두 문장
    건너뛴 조문까지 앞의 법령명으로 읽으면 없는 조문이 생깁니다. 그 거리를
    넘어가면 ``default_law`` 로 돌아갑니다(주석서 한 권이 통째로 고용보험법이면
    그것을 기본값으로 두시면 됩니다).
    """
    if not text:
        return []
    default = normalise_law_name(default_law)
    # 문서가 스스로 정한 약칭이 있으면 그것부터 씁니다.
    extra = {**local_aliases(text), **(aliases or {})}
    found: list[StatuteRef] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        current = default
        carried_until = -1
        taken: list[tuple[int, int]] = []
        for match in _ARTICLE_RE.finditer(line):
            law, historic = _law_from_prefix(line[: match.start()], extra)
            if law and law != SAME_LAW:
                current = law
                carried_until = match.end() + carry_window
            elif law == SAME_LAW:
                carried_until = match.end() + carry_window
            elif carry_window and match.start() > carried_until:
                current = default  # 너무 멀어졌다 — 짐작하지 않는다
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
            law = (
                extra.get(alias)
                or alias_table().aliases.get(alias, "")
                or normalise_law_name(alias)
            )
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
    year: str  # 적힌 그대로 — '2017' 또는 '99'
    code: str  # 다 · 도 · 헌마 …
    number: str
    court: str = ""
    decided_on: str = ""  # 선고일 (ISO)
    en_banc: bool = False  # 전원합의체
    span: tuple[int, int] = (0, 0)

    @property
    def case_no(self) -> str:
        # 법조인이 쓰는 그대로가 곧 정규형입니다. '99구27275' 를 '1999구27275'
        # 로 고치면 오히려 아무도 못 찾습니다.
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
    """사건번호. 네 자리 연도와 **두 자리 연도**(80마158)를 함께 받는다.

    2000년 이전 사건은 ``99구27275`` 처럼 두 자리로 씁니다. 다만 두 자리
    쪽은 오탐이 쉬우므로 **표에 있는 사건부호일 때만** 인정합니다.
    """
    codes = alias_table().case_codes
    known = "|".join(re.escape(code) for code in codes) or r"(?!)"
    return re.compile(
        rf"(?:(?P<year>(?:19|20)\d{{2}})\s*(?P<code>{known}|[가-힣]{{1,3}})"
        rf"|(?P<year2>\d{{2}})\s*(?P<code2>{known}))"
        rf"\s*(?P<num>\d{{1,6}})(?P<more>(?:\s*,\s*\d{{1,6}})*)"
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
        code = (match["code"] or match["code2"] or "").strip()
        year = (match["year"] or match["year2"] or "").strip()
        if not code or not year:
            continue
        if code not in known and code in _NOT_A_CASE_CODE:
            continue
        before = text[: match.start()]
        if before and (before[-1].isdigit() or before[-1] == "."):
            continue  # 숫자 한복판
        if len(year) == 2 and before and before[-1] in "제조항호":
            continue  # '제99조' 같은 자리
        window = before[-60:]
        court_match = None
        for court_match in _COURT_RE.finditer(window):
            pass  # 가장 가까운(마지막) 법원명
        court = _COURT_ALIASES.get(court_match.group(1), court_match.group(1)) if court_match else ""
        after = text[match.end() : match.end() + 30]
        en_banc = "전원합의체" in window[-25:] or "전원합의체" in after
        ref = CaseRef(
            year=year,
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


def extract_citations(
    text: str, default_law: str = "", aliases: dict[str, str] | None = None
) -> Citations:
    return Citations(
        statutes=parse_statutes(text, default_law=default_law, aliases=aliases),
        cases=parse_cases(text),
    )


def entity_key(key: str) -> str:
    """그래프에서 같은 것으로 볼 형태.

    같은 법이 문서마다 띄어쓰기가 다릅니다 — 온주의 링크는
    ``고용보험및산업재해보상보험의보험료징수등에관한법률``, 본문은
    ``고용보험 및 산업재해보상보험의 보험료징수 등에 관한 법률``. 붙여 쓴
    쪽으로 맞춰 두 개가 한 마디가 되게 합니다. 보이는 이름은 띄어 쓴 쪽을
    씁니다.
    """
    return re.sub(r"\s+", "", key or "")
