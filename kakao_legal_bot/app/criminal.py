"""범죄구성요건 지식 — 죄명을 먼저 확정하고, 그 죄의 요건으로 질문한다.

민사의 ``knowledge.py`` 와 같은 구조입니다. 시스템 프롬프트에는 **죄명 색인만**
상주하고, 구성요건 본문은 죄명이 정해지는 순간 ``get_crime_elements`` 로 꺼냅니다.

민사와 다른 점이 하나 있습니다. 미수·예비음모·상습범·과실범의 처벌 여부는
**죄마다 다르고, 처벌규정이 없으면 처벌할 수 없습니다.** 그래서 이것들을 서술이
아니라 필드로 두었고, 필드가 비어 있으면(``null``) 모델에게 "확인 필요"라고
돌려줍니다. 데이터에 없는 것을 일반론으로 메우면 고소장에 없는 죄가 들어갑니다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

CRIMINAL_DIR = Path(__file__).resolve().parent.parent / "knowledge" / "criminal"

VERIFIED = "검증완료"

# 구성요건 본문 앞에 늘 붙는 경고. 모델이 데이터 밖으로 나가지 않게 하는 울타리.
_RULE_NOTE = (
    "미수·예비음모·상습범·과실범은 **그 죄에 처벌규정이 있는 경우에만** 문제된다. "
    "아래에 근거 조문이 적힌 것만 인정하고, '확인 필요'로 표시된 항목은 "
    "처벌된다고도 안 된다고도 말하지 말고 변호사 확인이 필요하다고 안내하라."
)


# ── 처벌규정 유무 ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Punishability:
    """처벌규정이 있는지 — 그리고 그것을 우리가 알고 있는지.

    ``known=False`` 는 "처벌 안 된다"가 아니라 "데이터에 없다"입니다. 둘을
    같은 값으로 뭉개면 봇이 없는 근거로 단정하게 됩니다.
    """

    known: bool
    punishable: bool = False
    basis: str = ""

    @property
    def text(self) -> str:
        if not self.known:
            return "확인 필요 (데이터에 없음 — 단정하지 말 것)"
        if not self.punishable:
            return "처벌규정 없음 → 처벌 불가"
        return f"처벌 ({self.basis})" if self.basis else "처벌 (근거 조문 미기재 — 확인 필요)"


def _punishability(raw: object) -> Punishability:
    if isinstance(raw, bool):
        return Punishability(known=True, punishable=raw)
    if isinstance(raw, dict):
        value = raw.get("처벌")
        basis = str(raw.get("근거") or "").strip()
        if value is None:
            return Punishability(known=False, basis=basis)
        return Punishability(known=True, punishable=bool(value), basis=basis)
    return Punishability(known=False)


def _strings(raw: object) -> tuple[str, ...]:
    if isinstance(raw, str):
        return (raw.strip(),) if raw.strip() else ()
    if isinstance(raw, list):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    return ()


def _flag(raw: object) -> bool | None:
    return bool(raw) if isinstance(raw, bool) else None


# ── 죄명 ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Crime:
    name: str
    statute: str = "형법"
    article: str = ""
    penalty: str = ""
    interest: str = ""
    objective: tuple[str, ...] = ()
    subjective: tuple[str, ...] = ()
    attempt: Punishability = field(default_factory=lambda: Punishability(known=False))
    preparation: Punishability = field(default_factory=lambda: Punishability(known=False))
    habitual: Punishability = field(default_factory=lambda: Punishability(known=False))
    negligent: Punishability = field(default_factory=lambda: Punishability(known=False))
    complaint_required: bool | None = None  # 친고죄
    victim_veto: bool | None = None  # 반의사불벌죄
    limitation: str = ""  # 공소시효
    questions: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    verified: bool = False
    source: str = ""

    @property
    def key(self) -> str:
        return self.name

    @property
    def label(self) -> str:
        head = f"{self.statute} {self.article}".strip()
        return f"{self.name} ({head})" if head else self.name

    def strength(self, text: str) -> int:
        """이 죄를 가리키는 표현 중 문장에 등장한 가장 긴 것의 길이 (없으면 0).

        긴 표현일수록 구체적입니다 — "인터넷에 글"이 "글"보다 많은 것을 말해줍니다.
        """
        haystack = (text or "").replace(" ", "")
        if not haystack:
            return 0
        lengths = [
            len(needle)
            for needle in (self.name, *self.aliases)
            if needle and needle.replace(" ", "") in haystack
        ]
        return max(lengths, default=0)

    def matches(self, text: str) -> bool:
        return self.strength(text) > 0


def _crime_from(row: dict, source: str) -> Crime | None:
    name = str(row.get("죄명") or "").strip()
    if not name:
        return None
    return Crime(
        name=name,
        statute=str(row.get("법률") or "형법").strip(),
        article=str(row.get("조문") or "").strip(),
        penalty=str(row.get("법정형") or "").strip(),
        interest=str(row.get("보호법익") or "").strip(),
        objective=_strings(row.get("객관적_구성요건")),
        subjective=_strings(row.get("주관적_구성요건")),
        attempt=_punishability(row.get("미수")),
        preparation=_punishability(row.get("예비음모")),
        habitual=_punishability(row.get("상습범")),
        negligent=_punishability(row.get("과실범")),
        complaint_required=_flag(row.get("친고죄")),
        victim_veto=_flag(row.get("반의사불벌죄")),
        limitation=str(row.get("공소시효") or "").strip(),
        questions=_strings(row.get("질문항목")),
        aliases=_strings(row.get("별칭")),
        verified=str(row.get("검증") or "").strip() == VERIFIED,
        source=source,
    )


# ── 적재 ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class LoadReport:
    crimes: tuple[Crime, ...]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


@lru_cache(maxsize=1)
def _load() -> LoadReport:
    """``knowledge/criminal/*.jsonl`` 을 전부 읽는다 (형법각론·특별형법 파일 분리 가능)."""
    crimes: list[Crime] = []
    problems: list[str] = []
    seen: dict[str, str] = {}

    if not CRIMINAL_DIR.is_dir():
        return LoadReport((), (f"{CRIMINAL_DIR} 디렉터리가 없습니다.",))

    for path in sorted(CRIMINAL_DIR.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            problems.append(f"{path.name}: 읽지 못했습니다 ({exc})")
            continue
        for number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "//")):
                continue
            where = f"{path.name}:{number}"
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as exc:
                problems.append(f"{where}: JSON 오류 — {exc.msg}")
                continue
            if not isinstance(row, dict):
                problems.append(f"{where}: 객체가 아닙니다.")
                continue
            crime = _crime_from(row, where)
            if crime is None:
                problems.append(f"{where}: '죄명' 이 비어 있습니다.")
                continue
            if crime.name in seen:
                problems.append(f"{where}: '{crime.name}' 이 {seen[crime.name]} 에 이미 있습니다.")
                continue
            if not crime.objective:
                problems.append(f"{where}: '{crime.name}' 에 객관적_구성요건이 없습니다.")
            if not crime.article:
                problems.append(f"{where}: '{crime.name}' 에 조문이 없습니다.")
            seen[crime.name] = where
            crimes.append(crime)

    return LoadReport(tuple(crimes), tuple(problems))


def all_crimes() -> tuple[Crime, ...]:
    return _load().crimes


def load_problems() -> tuple[str, ...]:
    return _load().problems


def reload_crimes() -> None:
    """파일을 고쳤을 때 캐시를 비운다 (운영 중에는 재배포가 정석)."""
    _load.cache_clear()
    crime_index.cache_clear()
    crime_name_index.cache_clear()
    crime_elements_for.cache_clear()


# ── 조회 ─────────────────────────────────────────────────────────────────
def find_crime(text: str) -> Crime | None:
    """죄명 하나로 좁혀질 때만 그 죄를 돌려준다.

    좁혀지지 않으면 ``None`` 입니다. 어림짐작으로 하나를 고르면 그 죄의
    구성요건으로 질문이 흘러가고, 상담자는 자기 사건과 다른 것을 묻는
    이유를 모른 채 답하게 됩니다. 갈릴 때는 한 가지를 더 묻는 편이 낫습니다.
    """
    if not text:
        return None
    needle = text.strip()
    crimes = all_crimes()
    exact = next((c for c in crimes if c.name == needle), None)
    if exact is not None:
        return exact
    ranked = find_crimes(needle)
    if not ranked:
        return None
    best = ranked[0].strength(needle)
    contenders = [c for c in ranked if c.strength(needle) == best]
    return contenders[0] if len(contenders) == 1 else None


def find_crimes(text: str, limit: int = 5) -> list[Crime]:
    """걸리는 죄를 구체적인 순서로 늘어놓는다 — 죄명 확정은 상담자에게 묻는다."""
    if not text:
        return []
    scored = [(c.strength(text), c) for c in all_crimes()]
    matched = sorted(
        ((score, crime) for score, crime in scored if score > 0),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return [crime for _score, crime in matched[:limit]]


@lru_cache(maxsize=1)
def crime_index() -> str:
    """조문까지 붙인 죄명 목록 — 점검용."""
    return "\n".join(f"- {c.name} ({c.statute} {c.article})".rstrip(" (") for c in all_crimes())


@lru_cache(maxsize=1)
def crime_name_index() -> str:
    """시스템 프롬프트에 상주하는 죄명 색인.

    죄명이 수백 개로 늘어도 프롬프트가 부풀지 않도록 이름만 이어 붙입니다.
    구성요건 본문은 죄명이 정해진 뒤 ``get_crime_elements`` 로 꺼냅니다.
    """
    by_statute: dict[str, list[str]] = {}
    for crime in all_crimes():
        by_statute.setdefault(crime.statute or "형법", []).append(crime.name)
    lines = []
    for statute in sorted(by_statute, key=lambda s: (s != "형법", s)):
        lines.append(f"- {statute}: {', '.join(by_statute[statute])}")
    return "\n".join(lines)


def available_crime_names() -> list[str]:
    return [c.name for c in all_crimes()]


def _punish_lines(crime: Crime) -> list[str]:
    return [
        f"- 미수: {crime.attempt.text}",
        f"- 예비·음모: {crime.preparation.text}",
        f"- 상습범 가중: {crime.habitual.text}",
        f"- 과실범: {crime.negligent.text}",
    ]


def _procedure_lines(crime: Crime) -> list[str]:
    def flag(value: bool | None, yes: str, no: str) -> str:
        if value is None:
            return "확인 필요 (데이터에 없음)"
        return yes if value else no

    return [
        f"- 친고죄: {flag(crime.complaint_required, '친고죄 — 고소기간 6개월 확인 필요', '친고죄 아님')}",
        f"- 반의사불벌죄: {flag(crime.victim_veto, '반의사불벌 — 처벌불원 의사가 있으면 처벌 못함', '반의사불벌 아님')}",
        f"- 공소시효: {crime.limitation or '확인 필요 (데이터에 없음)'}",
    ]


@lru_cache(maxsize=64)
def crime_elements_for(name: str) -> str:
    """해당 죄의 구성요건·처벌규정·질문항목을 프롬프트용으로 모아 돌려준다."""
    crime = find_crime(name)
    if crime is None:
        return ""

    blocks: list[str] = [f"# [{crime.name}] {crime.statute} {crime.article} — 범죄구성요건".rstrip()]
    if not crime.verified:
        blocks.append(
            "⚠ 이 항목은 **미검증** 데이터다. 조문 번호와 법정형은 그대로 인용하지 말고, "
            "답변에 '담당 변호사 확인이 필요하다'는 취지를 함께 적어라."
        )
    blocks.append(_RULE_NOTE)

    if crime.penalty:
        blocks.append(f"## 법정형\n{crime.penalty}")
    if crime.interest:
        blocks.append(f"## 보호법익\n{crime.interest}")

    if crime.objective:
        blocks.append(
            "## 객관적 구성요건 (하나라도 빠지면 죄가 되지 않는다 — 질문의 기준)\n"
            + "\n".join(f"- {item}" for item in crime.objective)
        )
    if crime.subjective:
        blocks.append(
            "## 주관적 구성요건\n" + "\n".join(f"- {item}" for item in crime.subjective)
        )

    blocks.append("## 미수·예비음모·상습·과실 처벌규정\n" + "\n".join(_punish_lines(crime)))
    blocks.append("## 절차\n" + "\n".join(_procedure_lines(crime)))

    if crime.questions:
        blocks.append(
            "## 반드시 물어야 할 것 (6하원칙으로 풀어서 묻되, 한 번에 3개까지)\n"
            + "\n".join(f"- {item}" for item in crime.questions)
        )
    return "\n\n".join(blocks).strip()


def criminal_stats() -> dict[str, int]:
    crimes = all_crimes()
    return {
        "crimes": len(crimes),
        "verified": sum(1 for c in crimes if c.verified),
        "special_statutes": len({c.statute for c in crimes if c.statute != "형법"}),
        "problems": len(load_problems()),
    }


# ── CLI ──────────────────────────────────────────────────────────────────
def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m kakao_legal_bot.app.criminal",
        description="범죄구성요건 데이터 점검",
    )
    parser.add_argument("--check", action="store_true", help="스키마 검사")
    parser.add_argument("--list", action="store_true", help="죄명 색인 출력")
    parser.add_argument("--show", metavar="죄명", help="한 죄명의 구성요건 출력")
    parser.add_argument("--match", metavar="문장", help="상담자의 말에서 죄명 찾기")
    args = parser.parse_args(argv)

    if args.show:
        text = crime_elements_for(args.show)
        if not text:
            print(f"'{args.show}' 을(를) 찾지 못했습니다.")
            return 1
        print(text)
        return 0

    if args.match:
        candidates = find_crimes(args.match)
        if not candidates:
            print("해당하는 죄명을 찾지 못했습니다.")
            return 1
        for crime in candidates:
            print(f"{crime.label}{'' if crime.verified else '  [미검증]'}")
        return 0

    if args.list:
        print(crime_index() or "(비어 있습니다)")
        return 0

    stats = criminal_stats()
    print(
        f"죄명 {stats['crimes']}개 · 검증완료 {stats['verified']}개 · "
        f"특별형법 {stats['special_statutes']}종"
    )
    problems = load_problems()
    for problem in problems:
        print(f"  ✗ {problem}")
    unverified = [c.name for c in all_crimes() if not c.verified]
    if unverified:
        print(f"  ! 미검증 {len(unverified)}개: {', '.join(unverified[:20])}")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
