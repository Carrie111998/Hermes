from __future__ import annotations

import pytest

from proactive.loop_contract import LoopContractError, validate_loop_contract
from proactive.grace_task_compiler import render_execution_body


def _contract():
    return {
        "identity": {
            "project": "ingrids_marketing",
            "topic_name": "ingrids.app",
            "thread_id": "270",
            "request_instance_id": "gri_test",
        },
        "original_request": "請執行下一步",
        "grace_interpretation": "完成已定義的 Lighthouse 下一步，不跨到其他專案",
        "trigger": "KJ 明確要求執行",
        "completion_mode": "terminal",
        "goal": {"objective": "完成 Lighthouse 文件核對", "deliverables": ["核對報告"], "non_goals": ["不發布"]},
        "scope": {"allowed": ["Project Lighthouse 文件"], "forbidden": ["二手拍賣"]},
        "verification": {"checks": ["逐檔核對"], "evidence_required": ["檔案路徑"], "acceptance_criteria": ["無跨專案內容"]},
        "stop_rules": {"success": ["證據齊全"], "blocked": ["需要批准"], "no_progress": ["同錯誤兩次"], "max_iterations": 6, "max_runtime_seconds": 1800},
        "memory": {"namespace": "topic:270/ingrids", "working": ["本次核對狀態"], "promote_on_acceptance": ["已驗證結論"]},
    }


def test_complete_loop_contract_is_accepted():
    assert validate_loop_contract(_contract())["contract_version"] == "1.0"


def test_non_topic_lane_accepts_empty_thread_id():
    contract = _contract()
    contract["identity"]["thread_id"] = ""

    assert validate_loop_contract(contract)["identity"]["thread_id"] == ""


def test_missing_verification_and_stop_rules_fail_closed():
    contract = _contract()
    contract["verification"]["checks"] = []
    contract["stop_rules"]["max_iterations"] = 0
    with pytest.raises(LoopContractError) as exc:
        validate_loop_contract(contract)
    assert "verification.checks" in str(exc.value)
    assert "max_iterations" in str(exc.value)


def test_completion_mode_is_required_and_explicit():
    contract = _contract()
    contract.pop("completion_mode")
    with pytest.raises(LoopContractError, match="completion_mode"):
        validate_loop_contract(contract)

    contract["completion_mode"] = "sometimes"
    with pytest.raises(LoopContractError, match="terminal or intermediate"):
        validate_loop_contract(contract)


@pytest.mark.parametrize("invalid_item", [{}, 0, ["nested"], ""])
def test_required_contract_lists_accept_only_nonempty_strings(invalid_item):
    contract = _contract()
    contract["goal"]["deliverables"] = [invalid_item]

    with pytest.raises(LoopContractError, match="only non-empty strings"):
        validate_loop_contract(contract)


def test_execution_body_fails_closed_for_malformed_scoped_authorization():
    contract = _contract()
    contract["authorization"] = {
        "mode": "single_loop_contract",
        "risk_level": "high",
        "worker_risk_level_limit": "medium",
        "contract_risk_level_limit": "high",
        "reusable": False,
    }

    body = render_execution_body(contract)

    assert "Scoped authorization is malformed" in body
    assert "effective_risk_level_limit=high" not in body


def test_execution_body_makes_scoped_effective_limit_authoritative():
    contract = _contract()
    contract["authorization"] = {
        "mode": "single_loop_contract",
        "issued_by": "Hermes",
        "contract_fingerprint": "sha256:test-contract",
        "risk_level": "high",
        "human_approved": True,
        "worker_risk_level_limit": "medium",
        "contract_risk_level_limit": "high",
        "effective_risk_level_limit": "high",
        "reusable": False,
    }

    body = render_execution_body(contract)

    assert "Scoped authorization decision (authoritative)" in body
    assert "effective_risk_level_limit=high" in body
    assert "does not override the scoped effective limit" in body
