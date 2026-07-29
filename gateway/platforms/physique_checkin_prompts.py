"""Value-free Korean prompt cards for the private physique check-in wizard."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

_EDITABLE_FIELDS = {
    "morning": (
        ("체중", "bodyweight"),
        ("수면 시간", "sleep_duration"),
        ("수면 질", "sleep_quality"),
        ("컨디션", "condition"),
        ("통증", "pain"),
        ("칼로리", "calories"),
        ("오늘 운동", "training_plan"),
        ("특이사항", "optional_note"),
    ),
    "workout": (
        ("운동 결과", "completion"),
        ("실제 훈련", "training_summary"),
        ("운동 질", "workout_quality"),
        ("통증", "pain"),
    ),
    "nutrition_daily": (
        ("체중", "bodyweight"),
        ("칼로리", "calories"),
        ("탄단지", "macros"),
        ("식사", "meals"),
        ("수분", "water"),
        ("수면 시간", "sleep_duration"),
        ("수면 질", "sleep_quality"),
        ("소화", "digestion"),
        ("컨디션", "condition"),
        ("식욕·스트레스", "appetite_stress"),
        ("운동", "training_summary"),
        ("특이사항", "optional_note"),
    ),
}


@dataclass(frozen=True, slots=True)
class WizardPrompt:
    """Rendered text and opaque callback addresses; never user-entered values."""

    text: str
    buttons: tuple[tuple[str, str], ...] = ()
    button_rows: tuple[tuple[tuple[str, str], ...], ...] = ()


def build_wizard_prompt(
    step: str,
    awaiting_text: bool,
    callback: Callable[[str], str],
    *,
    flow: str = "morning",
) -> WizardPrompt:
    """Render the exact field question or its bounded-choice keyboard."""
    if awaiting_text:
        return _typed_prompt(step)
    if step == "bodyweight": return WizardPrompt("① 기상 직후 체중(kg)을 숫자로 보내주세요. 예: 70.2")
    if step == "sleep_duration": return WizardPrompt("② 수면 시간을 숫자로 보내주세요. 예: 7.5")
    if step in {"sleep_quality", "condition", "workout_quality"}:
        labels = {
            "sleep_quality": "수면 질 점수 (1=매우 나쁨 · 3=보통 · 5=매우 좋음)를 골라주세요.",
            "condition": "③ 오늘 컨디션 점수 (1=매우 나쁨 · 3=보통 · 5=매우 좋음)를 골라주세요.",
            "workout_quality": "운동 수행 질 점수 (1=매우 나쁨 · 3=보통 · 5=매우 좋음)를 골라주세요.",
        }
        return WizardPrompt(labels[step], tuple((str(index), callback(f"a{index}")) for index in range(1, 6)))
    if step == "pain": return WizardPrompt("④ 통증·이상 신호가 있으면 알려주세요.", (("없음", callback("a0")), ("있음 · 입력", callback("a1"))))
    if step == "digestion":
        buttons = (
            ("1 · 딱딱한 알갱이", callback("a0")),
            ("2 · 울퉁불퉁한 소시지", callback("a1")),
            ("3 · 갈라진 소시지", callback("a2")),
            ("4 · 매끈하고 부드러움", callback("a3")),
            ("5 · 부드러운 덩어리", callback("a4")),
            ("6 · 묽고 풀어진 변", callback("a5")),
            ("7 · 완전한 물변", callback("a6")),
            ("가스·복부 팽만", callback("a7")),
            ("기타 · 직접 입력", callback("a8")),
        )
        return WizardPrompt(
            "⑧ 어제 배변 상태를 브리스톨 변 형태 1~7 중에서 골라주세요.\n"
            "가스가 차거나 배가 더부룩했다면 ‘가스·복부 팽만’을 누르세요.",
            buttons,
            tuple((button,) for button in buttons),
        )
    if step == "calories": return WizardPrompt("⑤ 어제 총칼로리를 숫자로 보내주세요. 예: 2350")
    if step == "training_plan": return WizardPrompt("⑥ 오늘 훈련 계획을 골라주세요.", (("휴식 예정", callback("a0")), ("미정", callback("a1")), ("운동 입력", callback("a2"))))
    if step == "optional_note": return WizardPrompt("특이사항이 있으면 적어주세요.", (("없음 · 계속", callback("a0")), ("입력", callback("a1"))))
    if step == "completion": return WizardPrompt("계획 대비 훈련 결과를 골라주세요.", (("완료", callback("a0")), ("부분 완료", callback("a1")), ("휴식으로 변경", callback("a2"))))
    if step == "training_summary" and flow == "nutrition_daily":
        return WizardPrompt(
            "⑪ 어제 트레이너가 운동 기록을 남겼다면 건너뛰어도 됩니다.",
            (("트레이너 기록 있음 · 건너뛰기", callback("a0")), ("직접 입력", callback("a1"))),
        )
    if step == "training_summary": return WizardPrompt("실제 훈련 내용을 짧게 적어주세요.")
    if step == "safety_ack": return WizardPrompt("운동 처방을 중단했습니다. 필요한 진료를 먼저 확인해 주세요.", (("확인", callback("a0")),))
    if step == "summary":
        buttons = (("저장", callback("a0")), ("수정", callback("a1")), ("임시저장", callback("a2")))
        return WizardPrompt("오늘 체크인을 저장할까요?", buttons, (buttons,))
    if step == "edit_menu":
        fields = _EDITABLE_FIELDS.get(flow, _EDITABLE_FIELDS["morning"])
        buttons = tuple((label, callback(f"e{index}")) for index, (label, _) in enumerate(fields))
        rows = tuple(buttons[index:index + 2] for index in range(0, len(buttons), 2))
        back = ("요약으로 돌아가기", callback("e12" if flow == "nutrition_daily" else "e11"))
        return WizardPrompt("어느 항목을 수정할까요?", buttons + (back,), rows + ((back,),))
    return WizardPrompt("체크인을 다시 시작해 주세요.")


def _typed_prompt(step: str) -> WizardPrompt:
    questions = {
        "bodyweight": "① 기상 직후 체중(kg)을 숫자로 보내주세요. 예: 70.2",
        "sleep_duration": "② 수면 시간을 숫자로 보내주세요. 예: 7.5",
        "pain": "통증 위치, 강도(0~10), 언제부터인지 적어주세요.",
        "training_plan": "오늘 할 운동 부위와 종목을 짧게 적어주세요.",
        "optional_note": "오늘 특이사항을 짧게 적어주세요.",
        "training_summary": "어제 별도로 한 운동이 있다면 부위·종목·세트 수·시간을 적어주세요.",
        "calories": "⑤ 어제 총칼로리를 숫자로 보내주세요. 예: 2350",
        "macros": "③ 어제 탄수화물·단백질·지방(g)을 ‘탄·단·지’ 순서대로 보내주세요. 예: 280 150 65",
        "meals": (
            "④ 어제 먹은 식사를 끼니별로 적어주세요. 끼니 수를 확인할 수 있도록 Meal 번호를 붙여주세요.\n"
            "예: Meal 1 달걀 3개·밥 / Meal 2 닭가슴살·밥 / Meal 3 외식(국밥)\n"
            "계획에서 벗어난 식사나 간식도 다음 Meal 번호로 함께 적어주세요."
        ),
        "water": "⑤ 어제 마신 물의 양(L)을 숫자로 보내주세요. 예: 2.5",
        "digestion": "⑧ ‘기타’를 선택했습니다. 배변 상태나 소화 불편을 편하게 적어주세요.",
        "appetite_stress": "⑩ 어제 하루의 평균 식욕과 스트레스를 각각 1~5로 적어주세요.\n1=거의 없음, 3=보통, 5=매우 높음\n예: 식욕 3, 스트레스 2"
    }
    return WizardPrompt(questions.get(step, "다음 답을 입력해 주세요."))
