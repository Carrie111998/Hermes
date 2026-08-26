"""요건사실 지식 — 사건유형별로 필요한 것만 꺼내 쓰기.

세 파일을 합치면 22,000자, 2만~3만 토큰입니다. 매 메시지에 실어 보내면
상담 한 건마다 요건사실 값만 20원이 붙고 응답도 느려집니다. 그래서:

* **시스템 프롬프트에는 사건유형 색인만** — 짧고 항상 같아서 캐시가 걸립니다.
* **요건사실 본문은 사건유형이 정해지는 순간** ``get_requisite_facts`` 로
  꺼내 그 상담 동안 고정합니다.

덕분에 일반 상담은 싸게, 문서 인테이크는 빠짐없이 갑니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"

CLAIMS_FILE = "02-청구원인-요건사실.md"
DEFENSES_FILE = "03-항변-요건사실.md"
CIVIL_FILE = "01-민법-요건사실.md"


# ── 파일 파싱 ────────────────────────────────────────────────────────────
def _read(name: str) -> str:
    path = KNOWLEDGE_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


@lru_cache(maxsize=8)
def _sections(name: str) -> dict[str, str]:
    """``## I. 매매계약…`` 단위로 잘라 로마숫자를 키로 돌려준다."""
    text = _read(name)
    if not text:
        return {}
    out: dict[str, str] = {}
    parts = re.split(r"\n(?=## )", text)
    for part in parts:
        match = re.match(r"##\s*([IVXL]+)\.\s*(.+)", part)
        if match:
            out[match.group(1)] = part.strip()
    return out


@lru_cache(maxsize=2)
def _civil_items() -> dict[int, str]:
    """민법 요건사실 40개를 번호로 색인한다 (``16,`` 처럼 쉼표인 것도 있다)."""
    text = _read(CIVIL_FILE)
    if not text:
        return {}
    out: dict[int, str] = {}
    parts = re.split(r"\n(?=\d{1,2}[.,]\s)", text)
    for part in parts:
        match = re.match(r"(\d{1,2})[.,]\s*(.+)", part.strip())
        if match:
            out[int(match.group(1))] = part.strip()
    return out


# ── 사건유형 표 ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CaseType:
    key: str
    label: str
    claim: str  # 02 파일의 로마숫자 절
    defenses: tuple[str, ...] = ()  # 03 파일의 로마숫자 절
    civil: tuple[int, ...] = ()  # 01 파일의 항목 번호
    aliases: tuple[str, ...] = field(default=())

    def matches(self, text: str) -> bool:
        haystack = (text or "").replace(" ", "")
        if not haystack:
            return False
        for needle in (self.key, self.label, *self.aliases):
            if needle.replace(" ", "") in haystack:
                return True
        return False


# 소멸시효·변제·상계·동시이행은 어느 사건에서나 나오므로 대부분에 붙였습니다.
_COMMON = ("VII", "VIII", "X", "XII")

CASE_TYPES: tuple[CaseType, ...] = (
    CaseType("대여금", "대여금 반환 청구", "III", _COMMON, (5, 14, 15, 18),
             ("빌려준 돈", "차용", "소비대차", "돈을 안 갚")),
    CaseType("매매대금", "매매대금 지급 청구", "I", (*_COMMON, "XIII"), (27, 28, 29),
             ("물품대금", "잔금", "매매")),
    CaseType("소유권이전등기", "매매에 기한 소유권이전등기 청구", "I", ("XII", "XIII", "VII"),
             (26, 27), ("이전등기", "등기이전")),
    CaseType("임대차보증금반환", "임대차보증금 반환 청구", "II", ("X", "XII"), (30, 31, 32),
             ("전세금", "보증금", "전세보증금", "임대차")),
    CaseType("건물명도", "임대차목적물 반환·건물 인도 청구", "XII", ("XII", "VII"),
             (6, 12, 10, 11), ("명도", "인도청구", "퇴거", "점유")),
    CaseType("공사대금", "수급인의 공사대금 청구", "IV", (*_COMMON, "XIII"), (33, 34),
             ("도급", "공사비", "인테리어")),
    CaseType("부당이득", "부당이득 반환 청구", "V", ("VII", "VIII"), (35,),
             ("착오송금", "잘못 보낸 돈", "부당이득반환")),
    CaseType("손해배상_불법행위", "불법행위 손해배상 청구", "VI", ("VII",),
             (36, 37, 38, 39, 40, 1), ("교통사고", "폭행", "명예훼손", "의료과실", "불법행위")),
    CaseType("손해배상_채무불이행", "채무불이행 손해배상 청구", "VII", ("VII", "VIII", "XII"),
             (19, 24, 25, 26), ("이행지체", "이행불능", "계약위반")),
    CaseType("채권자대위", "채권자대위권에 기한 청구", "VIII", ("VII",), (20,), ("대위",)),
    CaseType("사해행위취소", "채권자취소권에 기한 청구", "IX", (), (21,),
             ("채권자취소", "사해행위")),
    CaseType("보증채무", "연대채무·보증계약에 기한 청구", "X", _COMMON, (15,),
             ("연대보증", "보증인")),
    CaseType("양수금", "채권양도에 기한 양수금 청구", "XI", _COMMON, (22, 23),
             ("채권양도",)),
    CaseType("토지인도_건물철거", "대지 인도 및 건물 철거 청구", "XIII", ("VII", "XII"),
             (6, 10, 11, 12), ("철거", "지료")),
    CaseType("말소등기", "소유권에 기한 말소등기 청구", "XIV", ("VII",), (7, 4, 2, 3),
             ("근저당말소", "등기말소")),
    CaseType("진정명의회복", "진정명의회복을 원인으로 한 이전등기 청구", "XV", (), (7,)),
    CaseType("점유취득시효", "점유취득시효 완성에 기한 이전등기 청구", "XVI", (), (9,),
             ("취득시효", "20년 점유")),
    CaseType("등기부취득시효", "등기부취득시효에 기한 청구", "XVII", (), (9, 8)),
    CaseType("법정지상권", "법정지상권 관련 청구", "XVIII", (), (10, 11, 13)),
    CaseType("유치권", "유치권 관련 주장", "XIX", (), (12,)),
    CaseType("전부금_추심금", "전부금·추심금 청구", "XX", _COMMON, (),
             ("압류", "추심명령", "전부명령")),
    CaseType("기타약정", "그 밖의 약정채권(증여·교환·위임·임치·조합 등)", "XXI", _COMMON, (),
             ("증여", "교환", "사용대차", "고용", "위임", "임치", "조합", "화해", "현상광고")),
)


def find_case_type(text: str) -> CaseType | None:
    """상담자의 말이나 사건유형 이름에서 가장 그럴듯한 유형을 고른다."""
    if not text:
        return None
    exact = next((c for c in CASE_TYPES if c.key == text.strip()), None)
    if exact is not None:
        return exact
    matched = [c for c in CASE_TYPES if c.matches(text)]
    if not matched:
        return None
    # 여러 개 걸리면 더 구체적인 쪽(별칭이 긴 쪽)을 고릅니다.
    return max(matched, key=lambda c: max(len(n) for n in (c.key, c.label, *c.aliases)))


@lru_cache(maxsize=1)
def case_type_index() -> str:
    """시스템 프롬프트에 상주하는 짧은 색인 (약 700자)."""
    lines = [f"- {c.key}: {c.label}" for c in CASE_TYPES]
    return "\n".join(lines)


@lru_cache(maxsize=32)
def requisite_facts_for(case_key: str) -> str:
    """해당 사건유형의 청구원인·항변·관련 민법 요건사실을 모아 돌려준다."""
    case = find_case_type(case_key)
    if case is None:
        return ""

    claims = _sections(CLAIMS_FILE)
    defenses = _sections(DEFENSES_FILE)
    civil = _civil_items()

    blocks: list[str] = [f"# [{case.key}] {case.label} — 요건사실"]

    claim_text = claims.get(case.claim, "")
    if claim_text:
        blocks.append("## 청구원인 요건사실 (이것이 빠지면 청구가 인용되지 않습니다)")
        blocks.append(claim_text)

    defence_blocks = [defenses[key] for key in case.defenses if key in defenses]
    if defence_blocks:
        blocks.append("## 자주 나오는 항변 (상대방이 이렇게 나올 수 있습니다)")
        blocks.extend(defence_blocks)

    civil_blocks = [civil[num] for num in case.civil if num in civil]
    if civil_blocks:
        blocks.append("## 관련 민법 요건사실")
        blocks.extend(civil_blocks)

    return "\n\n".join(blocks).strip()


def available_case_keys() -> list[str]:
    return [c.key for c in CASE_TYPES]


def knowledge_stats() -> dict[str, int]:
    return {
        "case_types": len(CASE_TYPES),
        "claim_sections": len(_sections(CLAIMS_FILE)),
        "defense_sections": len(_sections(DEFENSES_FILE)),
        "civil_items": len(_civil_items()),
    }
