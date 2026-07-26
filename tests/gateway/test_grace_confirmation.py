from gateway.grace_confirmation import (
    MAX_CONFIRMATION_CHARS,
    build_delegation_confirmation,
)


def _contract(*, approved=True):
    return {
        "original_request": "RAW_SENTINEL must never be shown",
        "grace_interpretation": "重建望遠鏡刊登，只投放到 KJ 已挑選並加入的相關群組。",
        "goal": {
            "objective": "完成 Celestron 130EQ 的刊登重建與指定群組投放。",
            "deliverables": ["完整刊登內容", "群組投放紀錄", "失敗原因清單"],
            "non_goals": ["尋找新群組"],
        },
        "scope": {
            "allowed": ["使用已核對照片", "投放已加入的指定群組"],
            "forbidden": ["不相關群組", "未加入或仍待審核的群組"],
        },
        "verification": {
            "checks": ["核對商品、價格與照片", "逐一確認群組投放結果"],
            "evidence_required": ["網址或截圖"],
            "acceptance_criteria": ["刊登正確", "只出現在允許群組"],
        },
        "approved": approved,
    }


def test_confirmation_restates_compiled_contract_without_raw_request():
    rendered = build_delegation_confirmation("clawops_delegate", _contract())

    assert rendered is not None
    assert "重建望遠鏡刊登" in rendered
    assert "你已挑選並加入" in rendered
    assert "KJ" not in rendered
    assert "execution task ID" in rendered
    assert "任務確實建立後" in rendered
    assert "我先檢查這份委派" in rendered
    assert "RAW_SENTINEL" not in rendered
    assert "• 目標：" not in rendered
    assert "• 可執行範圍：" not in rendered


def test_confirmation_does_not_treat_model_approved_flag_as_authorization():
    rendered = build_delegation_confirmation("clawops_delegate", _contract(approved=False))
    approved_rendered = build_delegation_confirmation(
        "clawops_delegate", _contract(approved=True),
    )

    assert rendered is not None
    assert approved_rendered is not None
    assert rendered == approved_rendered
    assert "若需要對外操作" in rendered
    assert "先給你精確的核准範圍" in rendered
    assert "我現在會把這項工作交給 ClawOps" not in rendered


def test_confirmation_turns_third_person_contract_into_direct_conversation():
    contract = _contract()
    contract["grace_interpretation"] = (
        "KJ 選擇由他本人手動申請加入相關 Facebook 社團。"
        "Grace／ClawOps 僅需整理值得 KJ 人工申請加入的精確社團連結；"
        "不得代為加入、填問卷、互動或發布。"
        "並需修正 Carimali 候選的資格標示。"
    )

    rendered = build_delegation_confirmation("clawops_delegate", contract)

    assert rendered is not None
    assert "你會自己手動申請加入" in rendered
    assert "我只需要整理值得你手動申請加入" in rendered
    assert "我不會代你加入、填問卷、互動或發布" in rendered
    assert "另外，我也會修正 Carimali" in rendered
    assert "KJ" not in rendered
    assert "他本人" not in rendered


def test_confirmation_ignores_other_tools_and_incomplete_contracts():
    assert build_delegation_confirmation("browser", _contract()) is None
    assert build_delegation_confirmation("clawops_delegate", {}) is None
    assert build_delegation_confirmation("clawops_delegate", {"goal": {}}) is None


def test_confirmation_is_bounded_for_chat_delivery():
    contract = _contract()
    contract["scope"]["allowed"] = ["A" * 1000] * 20
    contract["verification"]["checks"] = ["B" * 1000] * 20

    rendered = build_delegation_confirmation("clawops_delegate", contract)

    assert rendered is not None
    assert len(rendered) <= MAX_CONFIRMATION_CHARS
