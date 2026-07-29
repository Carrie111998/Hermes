import json
import re
from dataclasses import asdict, dataclass, replace

import pytest

from gateway.platforms.korean_humanizer import (
    AdaptiveGroundingInput,
    CoachingGrounding,
    apply_response,
    coach_and_polish,
    humanize,
    prepare_document,
)
from gateway.platforms.nutrition_coaching import AdaptiveCoachingFacts


DAILY = """오늘 체크인 완료

- 체중: 78.10kg
- 섭취: 2,310kcal

저장된 오늘 기록을 확인했습니다.

오늘 할 일

- 현재 식사량 유지
""".rstrip()

WEEKLY = """이번 주 린매스업 리포트

- 최근 7일 평균 체중: 78.12kg
- 이전 7일 평균 체중: 78.08kg
- 주간 변화: +0.05%
- 목표 범위: +0.10~+0.25%

체중은 증가하고 있지만 목표 범위보다 느립니다.

이번 주 판단: 조정 검토

- 현재 섭취량 유지

다음 주에도 증가율이 낮으면 소폭 조정을 검토합니다.
""".rstrip()

ADAPTIVE = """적응형 영양 검토 · 린매스업

가상 테스트 고객 · 2026-07-28
현재 판단: 순응도 근거 충돌로 조정 보류

권장안

- 칼로리와 매크로를 변경하지 않음

검토 필요

같은 날짜의 수정 전·후 기록이 함께 감지되어 확인이 필요합니다.
최신 확정 기록을 기준으로 다시 계산해야 합니다.

고객에게는 아직 전달되지 않았습니다.
""".rstrip()


def _response(document, replacements):
    return json.dumps(
        {
            "slots": [
                {"id": slot.slot_id, "text": replacements[slot.slot_id]}
                for slot in document.slots
            ]
        },
        ensure_ascii=False,
    )


def test_daily_accepts_natural_prose_and_preserves_locked_copy():
    document = prepare_document("daily", DAILY)
    result = apply_response(
        document,
        _response(document, {"slot_0": "오늘 기록은 빠짐없이 확인했어요."}),
    )

    assert "오늘 기록은 빠짐없이 확인했어요." in result
    assert "- 체중: 78.10kg" in result
    assert "- 섭취: 2,310kcal" in result
    assert "오늘 할 일\n\n- 현재 식사량 유지" in result


def test_weekly_rewrites_only_interpretation_and_rationale():
    document = prepare_document("weekly", WEEKLY)
    result = apply_response(
        document,
        _response(
            document,
            {
                "slot_0": "이번 주 체중은 올랐지만 목표 속도에는 조금 못 미쳤어요.",
                "slot_1": "다음 주 초반 기록까지 본 뒤 필요한 만큼만 조정하겠습니다.",
            },
        ),
    )

    assert "이번 주 체중은 올랐지만" in result
    assert "이번 주 판단: 조정 검토" in result
    assert "+0.10~+0.25%" in result
    assert "- 현재 섭취량 유지" in result


def test_adaptive_rewrites_review_copy_but_not_decision_or_delivery_state():
    document = prepare_document("adaptive_operator", ADAPTIVE)
    result = apply_response(
        document,
        _response(
            document,
            {"slot_0": "같은 날의 수정 전후 기록이 겹쳐 있어요.\n최신 확정 기록부터 다시 확인하겠습니다."},
        ),
    )

    assert "현재 판단: 순응도 근거 충돌로 조정 보류" in result
    assert "고객에게는 아직 전달되지 않았습니다." in result
    assert "최신 확정 기록부터 다시 확인하겠습니다." in result


def test_new_number_or_internal_code_rejects_the_whole_response():
    document = prepare_document("weekly", WEEKLY)
    response = _response(
        document,
        {
            "slot_0": "2주 동안 더 확인하겠습니다.",
            "slot_1": "reason_code를 확인합니다.",
        },
    )

    assert apply_response(document, response) == WEEKLY


def test_malformed_or_partial_response_falls_back_exactly():
    document = prepare_document("weekly", WEEKLY)

    assert apply_response(document, "not-json") == WEEKLY
    assert apply_response(document, '{"slots":[]}') == WEEKLY


def test_humanize_makes_one_request_and_provider_failure_is_canonical():
    calls = []

    def request(system, user):
        calls.append((system, user))
        raise TimeoutError

    assert humanize("daily", DAILY, request) == DAILY
    assert len(calls) == 1


_APPROVED_PRINCIPLES = (
    (
        "choi_01",
        "원인부터 설명합니다. 문제를 동작·관절·부하·회복 요소로 나누고, 조정 뒤 관찰할 결과까지 연결합니다.",
    ),
    (
        "choi_02",
        "몸통 안정성과 부하 경로를 우선합니다. 목표 근육의 느낌 하나보다 자세, 관절 움직임, 보상 동작, 반복 재현성을 함께 봅니다.",
    ),
    (
        "choi_03",
        "약점은 인접 움직임에서 찾되 진단하지 않습니다. 견갑·전거근·전완·발목 등 인접 패턴을 검토할 수 있지만, 보조운동은 선택적 수단입니다.",
    ),
    (
        "choi_04",
        "작고 누적 가능한 수행을 중시합니다. 부하·반복·운동순서·속도는 실제 수행, 기술, 관절 내성, 회복을 보고 점진적으로 조정합니다.",
    ),
    (
        "choi_05",
        "감량 전 훈련 수용능력을 중시합니다. 공격적 감량보다 훈련을 감당할 준비를 갖추고, 단기 절식보다 식사 이행·소화·훈련 수행·회복을 함께 봅니다.",
    ),
    (
        "choi_06",
        "개인 반응을 기록으로 확인합니다. 한 번에 적은 수의 가설만 바꾸고 결과와 반례를 기록합니다.",
    ),
)
COACHED_DAILY = DAILY.replace(
    "저장된 오늘 기록을 확인했습니다.",
    "오늘 기록된 내용을 기준으로 현재 상태를 차분히 확인했어요.",
)
POLISHED_DAILY = DAILY.replace(
    "저장된 오늘 기록을 확인했습니다.",
    "오늘 기록을 기준으로 현재 상태를 차분히 확인했어요.",
)


def _grounding(principle_count=3, *, memory_value="lean_mass_gain"):
    return CoachingGrounding(
        approved_principles=_APPROVED_PRINCIPLES[:principle_count],
        verified_memory=(("goal_mode", memory_value),),
        playbook_id="daily_checkin_v1",
        playbook_version="1",
        source_cluster_ids=("daily-checkin",),
        excluded_risk_ids=("medical", "unsafe_nutrition"),
        decision_id="decision_daily",
        revision_binding_digest="a" * 64,
    )


def _bound_coach_and_polish(*args, **kwargs):
    kwargs.setdefault("revision_binding_digest", "a" * 64)
    return coach_and_polish(*args, **kwargs)


def _finite_request(
    calls,
    *,
    forged_stage=None,
    forged_field=None,
    oversized_stage=None,
    raise_stage=None,
):
    def request(_system, user):
        payload = json.loads(user)
        calls.append(payload)
        stage = payload["stage"]
        if stage == "coach":
            if raise_stage == stage:
                raise RuntimeError("stage one failed")
            response_slots = []
            for slot in payload["slots"]:
                selection = {
                    "id": slot["id"],
                    "hard_identity": slot["hard_identity"],
                    "principle_ids": [payload["approved_principles"][0]["id"]],
                    "atom_ids": [slot["offered_atom_ids"][0]],
                }
                if forged_stage == stage and forged_field == "principle_ids":
                    selection["principle_ids"] = ["forged_principle"]
                if forged_stage == stage and forged_field == "atom_ids":
                    selection["atom_ids"] = ["forged_atom"]
                response_slots.append(selection)
        elif stage == "polish":
            if raise_stage == stage:
                raise RuntimeError("stage two failed")
            response_slots = [
                {
                    "id": slot["id"],
                    "variant_id": slot["offered_variant_ids"][0],
                }
                for slot in payload["slots"]
            ]
            if forged_stage == stage and forged_field == "variant_id":
                response_slots[0]["variant_id"] = "forged_variant"
        else:
            raise AssertionError(f"unexpected stage: {stage}")
        encoded = json.dumps({"slots": response_slots}, ensure_ascii=False)
        return encoded + (" " * 1025 if oversized_stage == stage else "")

    return request


@pytest.mark.parametrize("principle_count", (3, 4, 5))
def test_finite_pipeline_allows_three_to_five_approved_principles(
    principle_count,
):
    calls = []
    result, receipt = _bound_coach_and_polish(
        "daily",
        DAILY,
        _grounding(principle_count),
        _finite_request(calls),
    )

    assert result == POLISHED_DAILY
    assert [payload["stage"] for payload in calls] == ["coach", "polish"]
    assert receipt.outcome == "coached_and_polished"
    assert receipt.coach_valid is True
    assert receipt.polish_valid is True


@pytest.mark.parametrize("principle_count", (2, 6))
def test_finite_pipeline_rejects_principle_counts_outside_bounds(principle_count):
    calls = []
    result, receipt = _bound_coach_and_polish(
        "daily",
        DAILY,
        _grounding(principle_count),
        _finite_request(calls),
    )

    assert result == DAILY
    assert calls == []
    assert receipt.outcome == "canonical"
    assert receipt.coach_valid is False
    assert receipt.polish_valid is False


@pytest.mark.parametrize(
    ("forged_stage", "forged_field"),
    (("coach", "atom_ids"), ("coach", "principle_ids"), ("polish", "variant_id")),
)
def test_forged_finite_ids_fail_closed_without_retry(forged_stage, forged_field):
    calls = []
    result, receipt = _bound_coach_and_polish(
        "daily",
        DAILY,
        _grounding(),
        _finite_request(
            calls,
            forged_stage=forged_stage,
            forged_field=forged_field,
        ),
    )

    assert "forged_" not in result
    if forged_stage == "coach":
        assert result == DAILY
        assert receipt.outcome == "canonical"
        assert receipt.coach_valid is False
        assert receipt.polish_valid is False
        assert [payload["stage"] for payload in calls] == ["coach"]
    else:
        assert result == COACHED_DAILY
        assert receipt.outcome == "coached"
        assert receipt.coach_valid is True
        assert receipt.polish_valid is False
        assert [payload["stage"] for payload in calls] == ["coach", "polish"]


@pytest.mark.parametrize("oversized_stage", ("coach", "polish"))
def test_oversized_stage_json_fails_closed(oversized_stage):
    calls = []
    result, receipt = _bound_coach_and_polish(
        "daily",
        DAILY,
        _grounding(),
        _finite_request(calls, oversized_stage=oversized_stage),
    )

    if oversized_stage == "coach":
        assert result == DAILY
        assert receipt.outcome == "canonical"
        assert [payload["stage"] for payload in calls] == ["coach"]
    else:
        assert result == COACHED_DAILY
        assert receipt.outcome == "coached"
        assert receipt.coach_valid is True
        assert receipt.polish_valid is False
        assert [payload["stage"] for payload in calls] == ["coach", "polish"]


def test_stage_one_exception_returns_canonical_without_retry():
    calls = []
    result, receipt = _bound_coach_and_polish(
        "daily",
        DAILY,
        _grounding(),
        _finite_request(calls, raise_stage="coach"),
    )

    assert result == DAILY
    assert [payload["stage"] for payload in calls] == ["coach"]
    assert receipt.outcome == "canonical"
    assert receipt.coach_valid is False
    assert receipt.polish_valid is False


def test_stage_two_exception_returns_validated_coach_without_retry():
    calls = []
    result, receipt = _bound_coach_and_polish(
        "daily",
        DAILY,
        _grounding(),
        _finite_request(calls, raise_stage="polish"),
    )

    assert result == COACHED_DAILY
    assert [payload["stage"] for payload in calls] == ["coach", "polish"]
    assert receipt.outcome == "coached"
    assert receipt.coach_valid is True
    assert receipt.polish_valid is False


def test_processing_gate_revocation_between_stages_returns_canonical():
    calls = []
    checks = iter((True, False))
    result, receipt = _bound_coach_and_polish(
        "daily",
        DAILY,
        _grounding(),
        _finite_request(calls),
        processing_allowed=lambda: next(checks),
    )

    assert result == DAILY
    assert [payload["stage"] for payload in calls] == ["coach"]
    assert receipt.outcome == "canonical"


def test_processing_gate_revocation_before_final_output_returns_canonical():
    calls = []
    checks = iter((True, True, False))
    result, receipt = _bound_coach_and_polish(
        "daily",
        DAILY,
        _grounding(),
        _finite_request(calls),
        processing_allowed=lambda: next(checks),
    )

    assert result == DAILY
    assert [payload["stage"] for payload in calls] == ["coach", "polish"]
    assert receipt.outcome == "canonical"


def test_pipeline_receipt_contains_only_opaque_ids_and_digests():
    sensitive = "가상 고객 001 · 체중 78.10kg · 상담 메모"
    calls = []
    result, receipt = _bound_coach_and_polish(
        "daily",
        DAILY,
        _grounding(memory_value="lean_mass_gain"),
        _finite_request(calls),
    )
    encoded = json.dumps(asdict(receipt), ensure_ascii=False, sort_keys=True)

    assert result == POLISHED_DAILY
    for value in (
        DAILY,
        "오늘 체크인 완료",
        "저장된 오늘 기록을 확인했습니다.",
        "오늘 할 일",
        "78.10kg",
        "2,310kcal",
        "린매스업",
        sensitive,
    ):
        assert value not in encoded
    opaque_id = re.compile(r"[a-z][a-z0-9_.-]{1,63}")
    for value in (
        *receipt.principle_ids,
        *receipt.memory_keys,
        receipt.playbook_id,
        *receipt.atom_ids,
        *receipt.variant_ids,
        *receipt.source_cluster_ids,
        *receipt.excluded_risk_ids,
        receipt.decision_id,
    ):
        assert opaque_id.fullmatch(value)
    for value in (
        receipt.output_sha256,
        receipt.canonical_sha256,
        receipt.revision_binding_digest,
    ):
        assert re.fullmatch(r"[0-9a-f]{64}", value)


def test_stage_one_response_template_requires_exact_hard_identity_copy():
    calls = []

    def request(_system, user):
        payload = json.loads(user)
        calls.append(payload)
        if payload["stage"] == "coach":
            assert payload["response_schema"]["slots"] == [
                {
                    "id": slot["id"],
                    "hard_identity": slot["hard_identity"],
                    "principle_ids": ["approved_id"],
                    "atom_ids": ["offered_id"],
                }
                for slot in payload["slots"]
            ]
        return _finite_request([])(_system, user)

    result, receipt = _bound_coach_and_polish(
        "daily",
        DAILY,
        _grounding(),
        request,
    )

    assert result == POLISHED_DAILY
    assert receipt.outcome == "coached_and_polished"
    assert len(calls) == 2

@pytest.mark.parametrize("mutation", ("missing", "alias"))
def test_stage_one_requires_exact_hard_identity_field(mutation):
    calls = []

    def request(_system, user):
        payload = json.loads(user)
        calls.append(payload)
        slot = payload["slots"][0]
        item = {
            "id": slot["id"],
            "hard_identity": slot["hard_identity"],
            "principle_ids": [payload["approved_principles"][0]["id"]],
            "atom_ids": [slot["offered_atom_ids"][0]],
        }
        if mutation == "missing":
            item.pop("hard_identity")
        else:
            item["hard_id"] = item.pop("hard_identity")
        return json.dumps({"slots": [item]})

    result, receipt = _bound_coach_and_polish("daily", DAILY, _grounding(), request)

    assert result == DAILY
    assert receipt.outcome == "canonical"
    assert len(calls) == 1


def test_stage_two_exception_rechecks_final_processing_gate():
    calls = []
    checks = iter((True, True, False))
    result, receipt = _bound_coach_and_polish(
        "daily",
        DAILY,
        _grounding(),
        _finite_request(calls, raise_stage="polish"),
        processing_allowed=lambda: next(checks),
    )

    assert result == DAILY
    assert receipt.outcome == "canonical"
    assert [payload["stage"] for payload in calls] == ["coach", "polish"]


def test_unbounded_receipt_ids_fail_before_model_calls():
    calls = []
    grounding = _grounding()
    grounding = CoachingGrounding(
        approved_principles=grounding.approved_principles,
        verified_memory=grounding.verified_memory,
        playbook_id=grounding.playbook_id,
        playbook_version=grounding.playbook_version,
        source_cluster_ids=tuple(f"cluster-{index}" for index in range(9)),
        excluded_risk_ids=grounding.excluded_risk_ids,
        decision_id=grounding.decision_id,
        revision_binding_digest=grounding.revision_binding_digest,
    )

    result, receipt = _bound_coach_and_polish(
        "daily",
        DAILY,
        grounding,
        _finite_request(calls),
    )

    assert result == DAILY
    assert receipt.outcome == "canonical"
    assert calls == []
    assert receipt.source_cluster_ids == ()


def test_extra_stage_slot_is_rejected():
    calls = []

    def request(_system, user):
        payload = json.loads(user)
        calls.append(payload)
        slot = payload["slots"][0]
        item = {
            "id": slot["id"],
            "hard_identity": slot["hard_identity"],
            "principle_ids": [payload["approved_principles"][0]["id"]],
            "atom_ids": [slot["offered_atom_ids"][0]],
        }
        return json.dumps({"slots": [item, item]})

    result, _receipt = _bound_coach_and_polish("daily", DAILY, _grounding(), request)
    assert result == DAILY
    assert len(calls) == 1


@pytest.mark.parametrize("mutation", ("surface", "revision"))
def test_mismatched_playbook_or_revision_is_zero_call_canonical(mutation):
    calls = []
    grounding = _grounding()
    kwargs = asdict(grounding)
    if mutation == "surface":
        kwargs["playbook_id"] = "weekly_report_v1"
    else:
        kwargs["revision_binding_digest"] = ""
    result, receipt = coach_and_polish(
        "daily",
        DAILY,
        CoachingGrounding(**kwargs),
        _finite_request(calls),
        revision_binding_digest="b" * 64 if mutation == "revision" else None,
    )
    assert result == DAILY
    assert receipt.outcome == "canonical"
    assert calls == []


def test_malformed_surrogate_response_falls_back_without_raising():
    calls = []

    def request(_system, user):
        calls.append(json.loads(user))
        return "\ud800"

    result, receipt = _bound_coach_and_polish("daily", DAILY, _grounding(), request)
    assert result == DAILY
    assert receipt.outcome == "canonical"
    assert len(calls) == 1


def _adaptive_facts(**updates):
    values = {
        "evaluation_day": "2026-07-28",
        "goal_mode": "lean_mass_gain",
        "goal_range": ("+0.10", "+0.25"),
        "current_mean_kg": None,
        "prior_mean_kg": None,
        "weekly_rate_percent": None,
        "decision": "observe",
        "reason_category_ids": ("insufficient_data",),
        "target_macros": (("calories", 2500), ("protein_g", 160)),
        "carb_category_targets": (("high", (("calories", 2700),)),),
        "safety_held": False,
        "approval_state": "pending",
        "delivery_state": "not_delivered",
        "proposal_digest": "b" * 64,
        "revision": 1,
        "revision_binding_digest": "c" * 64,
        "source_cluster_ids": ("adaptive-proposal",),
        "excluded_risk_ids": ("medical", "unsafe_nutrition"),
    }
    values.update(updates)
    return AdaptiveCoachingFacts(**values)


@pytest.mark.parametrize("external_binding", (None, "", [], 1, "b" * 64))
def test_external_revision_binding_is_explicit_and_exact(external_binding):
    calls = []
    result, receipt = coach_and_polish(
        "daily",
        DAILY,
        _grounding(),
        _finite_request(calls),
        revision_binding_digest=external_binding,
    )

    assert result == DAILY
    assert receipt.outcome == "canonical"
    assert calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("approved_principles", None),
        ("verified_memory", None),
        ("source_cluster_ids", ["daily-checkin"]),
        ("excluded_risk_ids", None),
        ("revision_binding_digest", None),
        ("decision_id", "customer_001"),
        ("playbook_id", None),
    ),
)
def test_malformed_grounding_fields_fail_closed_without_hashing_or_calls(field, value):
    calls = []
    kwargs = asdict(_grounding())
    kwargs[field] = value
    result, receipt = coach_and_polish(
        "daily",
        DAILY,
        CoachingGrounding(**kwargs),
        _finite_request(calls),
        revision_binding_digest="a" * 64,
    )

    assert result == DAILY
    assert receipt.outcome == "canonical"
    assert calls == []
    assert receipt.decision_id == ""
    assert receipt.output_sha256 == ""
    assert "customer_001" not in json.dumps(asdict(receipt), ensure_ascii=False)


@pytest.mark.parametrize(
    "memory",
    (
        (("evaluation_day", "2026-02-30"),),
        (("evaluation_day", "2026-7-28"),),
        (("next_check_time", "2026-07-28T09:00"),),
        (("next_check_time", "2026-07-28T25:00+09:00"),),
        (("next_check_time", "2026-07-28T09:00+99:00"),),
    ),
)
def test_verified_memory_temporal_grammar_requires_real_iso_values(memory):
    calls = []
    kwargs = asdict(_grounding())
    kwargs["verified_memory"] = memory
    result, receipt = coach_and_polish(
        "daily",
        DAILY,
        CoachingGrounding(**kwargs),
        _finite_request(calls),
        revision_binding_digest="a" * 64,
    )

    assert result == DAILY
    assert receipt.outcome == "canonical"
    assert calls == []


def test_adaptive_projection_requires_the_exact_frozen_class_identity():
    @dataclass(frozen=True, slots=True)
    class AdaptiveCoachingFacts:
        forged: str = "forged"

    AdaptiveCoachingFacts.__name__ = "AdaptiveCoachingFacts"
    AdaptiveCoachingFacts.__module__ = "gateway.platforms.nutrition_coaching"

    with pytest.raises(TypeError):
        AdaptiveGroundingInput.from_card(AdaptiveCoachingFacts())


def test_adaptive_projection_allows_optional_averages_and_rate_to_be_none():
    projected = AdaptiveGroundingInput.from_card(_adaptive_facts())

    assert projected.revision_binding_digest == "c" * 64
    assert all(
        key not in {"current_mean_kg", "prior_mean_kg", "weekly_rate_percent"}
        for key, _value in projected.facts
    )


@pytest.mark.parametrize(
    "updates",
    (
        {"revision": 0},
        {"proposal_digest": "FORGED"},
        {"target_macros": [("calories", 2500)]},
        {"carb_category_targets": [("high", (("calories", 2700),))]},
        {"reason_category_ids": ["insufficient_data"]},
        {"safety_held": 1},
        {"source_cluster_ids": ["adaptive-proposal"]},
    ),
)
def test_adaptive_projection_rejects_forged_field_types_and_revision(updates):
    with pytest.raises((TypeError, ValueError)):
        AdaptiveGroundingInput.from_card(_adaptive_facts(**updates))


@pytest.mark.parametrize(
    ("surface", "canonical"),
    (
        (None, DAILY),
        ("daily", None),
        ("daily", []),
        ("unknown", DAILY),
    ),
)
def test_invalid_surface_or_canonical_is_canonical_with_zero_calls(surface, canonical):
    calls = []
    result, receipt = coach_and_polish(
        surface,
        canonical,
        _grounding(),
        _finite_request(calls),
        revision_binding_digest="a" * 64,
    )

    assert result == canonical
    assert receipt.outcome == "canonical"
    assert calls == []
