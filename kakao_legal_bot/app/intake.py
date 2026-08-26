"""문서작성 인테이크 — 폼 → 요건사실 문답 → 상담보고서 → 견적.

상담자가 "내용증명 써주세요"라고 하면 곧바로 초안을 만들지 않습니다.
빠진 사실관계 위에 쓴 문서는 변호사가 처음부터 다시 써야 하기 때문입니다.

    ① 정보입력폼을 드려 상담자가 스스로 채우게 하고
    ② 그 사건유형의 **요건사실**을 기준으로 빠진 것만 되묻고
    ③ 상담보고서로 정리해 상담자에게 확인받은 뒤
    ④ 소요기간과 비용을 안내합니다.

②가 이 흐름의 값어치입니다. 6하원칙만으로는 "무엇이 빠졌는지"를 알 수
없고, 청구원인·항변의 요건사실을 대조해야 비로소 알 수 있습니다.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── 문서 등급과 비용 ─────────────────────────────────────────────────────
SIMPLE, MEDIUM, COMPLEX = "simple", "medium", "complex"


@dataclass(frozen=True)
class Tier:
    key: str
    label: str
    price_krw: int
    lead_time: str
    examples: tuple[str, ...]


TIERS: dict[str, Tier] = {
    SIMPLE: Tier(
        SIMPLE,
        "간단",
        100_000,
        "2~3 영업일",
        ("내용증명", "통고서", "합의서", "각서", "지급명령신청서", "answer letter"),
    ),
    MEDIUM: Tier(
        MEDIUM,
        "중간",
        200_000,
        "3~5 영업일",
        ("문서송부촉탁서", "증인신청서", "사실조회신청서", "기일변경신청서", "석명준비명령 답변서"),
    ),
    COMPLEX: Tier(
        COMPLEX,
        "복잡",
        300_000,
        "5~7 영업일",
        ("소장", "답변서", "준비서면", "참고서면", "항소이유서", "청구취지변경신청서"),
    ),
}

# 문서 이름 → 등급. 부분 문자열로 맞춥니다("반박 준비서면" → complex).
# 긴 이름이 먼저 걸리도록 아래 순서를 유지하세요.
_DOC_TIER_RULES: tuple[tuple[str, str], ...] = (
    ("항소이유서", COMPLEX),
    ("상고이유서", COMPLEX),
    ("청구취지변경", COMPLEX),
    ("준비서면", COMPLEX),
    ("참고서면", COMPLEX),
    ("답변서", COMPLEX),
    ("소장", COMPLEX),
    ("반소장", COMPLEX),
    ("고소장", COMPLEX),
    ("문서송부촉탁", MEDIUM),
    ("사실조회", MEDIUM),
    ("증인신청", MEDIUM),
    ("증거신청", MEDIUM),
    ("기일변경", MEDIUM),
    ("보정서", MEDIUM),
    ("신청서", MEDIUM),
    ("내용증명", SIMPLE),
    ("통고서", SIMPLE),
    ("합의서", SIMPLE),
    ("각서", SIMPLE),
    ("확인서", SIMPLE),
)


def tier_for(doc_kind: str) -> Tier:
    """문서 이름에서 등급을 고른다. 모르면 가장 낮은 등급으로 잡는다.

    잘못 잡아 비싸게 부르는 것보다 싸게 부르고 변호사가 조정하는 편이
    상담자에게 낫습니다.
    """
    text = (doc_kind or "").replace(" ", "")
    for needle, tier_key in _DOC_TIER_RULES:
        if needle in text:
            return TIERS[tier_key]
    return TIERS[SIMPLE]


def quote_text(doc_kind: str, lawyer_name: str) -> str:
    tier = tier_for(doc_kind)
    return (
        f"[{doc_kind} 작성 안내]\n"
        f"· 예상 비용: {tier.price_krw:,}원 (부가세 별도)\n"
        f"· 예상 소요: {tier.lead_time}\n\n"
        f"{lawyer_name}님이 직접 검토·수정한 최종본을 이메일로 보내드립니다.\n"
        f"진행을 원하시면 '진행할게요'라고 답해주시고, "
        f"이메일 주소를 아직 안 알려주셨다면 함께 적어주세요.\n"
        f"(사건 내용에 따라 비용은 조정될 수 있으며, 확정 금액은 {lawyer_name}님이 안내드립니다.)"
    )


# ── 정보입력폼 ───────────────────────────────────────────────────────────
INTAKE_FORM = """[문서작성 신청서]
아래를 그대로 복사해서 채워 보내주세요. 모르는 항목은 '모름'이라고 적으시면 됩니다.

1) 신청인(본인) 성함 / 연락처
2) 상대방 성함(상호) / 아는 주소·연락처
3) 사건 개요 — 언제, 어디서, 누가, 무엇을, 어떻게, 왜
4) 금액 — 청구하거나 다투는 금액
5) 주요 날짜 — 계약일, 변제기, 사고일, 소장 받은 날 등
6) 가지고 계신 증거 — 계약서, 이체내역, 문자·카톡, 녹취 등
7) 상대방이 하는 말 (있다면)
8) 원하시는 결과

한 번에 다 적기 어려우시면 아는 것만 먼저 보내주셔도 됩니다."""


# ── 상태 ─────────────────────────────────────────────────────────────────
FORM_SENT = "form_sent"
COLLECTING = "collecting"
REPORT_REVIEW = "report_review"
QUOTED = "quoted"
CONFIRMED = "confirmed"
CANCELLED = "cancelled"

OPEN_STATES = (FORM_SENT, COLLECTING, REPORT_REVIEW, QUOTED)


def report_template(doc_kind: str, case_label: str) -> str:
    """상담보고서에 반드시 들어가야 할 뼈대 — 모델에게 주는 지시입니다."""
    return f"""상담보고서는 아래 순서로 씁니다.

# 상담보고서 — {doc_kind}
## 1. 당사자
   신청인 / 상대방. 확인된 것만. 모르는 것은 [미확인]으로.
## 2. 사건 개요
   시간 순서대로. 6하원칙이 드러나게.
## 3. 요건사실 정리 ({case_label})
   청구원인 요건사실을 항목별로 적고, 각 항목마다
   ✅ 확인됨 / ⚠️ 미확인 / ❌ 불리 를 표시합니다.
## 4. 예상되는 상대방 항변
   해당 사건유형에서 자주 나오는 항변과, 그에 대한 우리 쪽 사정.
## 5. 증거
   상담자가 가지고 있다고 한 것 / 추가로 필요한 것.
## 6. 추가 확인이 필요한 사항
   아직 못 받은 정보. 없으면 '없음'.

⚠️ 상담자가 말하지 않은 사실을 채워 넣지 마세요. 모르는 것은 [미확인]입니다."""
