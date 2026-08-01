import json

from gateway.run import (
    _find_bound_clawops_approval_args,
    _recover_bound_clawops_approval_args,
)


def _tool_call(call_id, arguments):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call_id,
                "function": {
                    "name": "clawops_delegate",
                    "arguments": json.dumps(arguments),
                },
            }
        ],
    }


def test_recovers_exact_contract_from_approval_required_result():
    token = "fe341e4c447cde20"
    contract = {
        "approved": False,
        "goal": {"objective": "建立草稿"},
        "scope": {"allowed": ["Facebook"]},
    }
    messages = [
        _tool_call("call-1", contract),
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": json.dumps(
                {
                    "status": "approval_required",
                    "approval_token": token,
                }
            ),
        },
    ]

    assert _find_bound_clawops_approval_args(messages, token) == contract


def test_recovery_ignores_unrelated_token():
    messages = [
        _tool_call(
            "call-old",
            {"approval_token": "aaaaaaaaaaaaaaaa", "approved": True},
        ),
        {
            "role": "tool",
            "tool_call_id": "call-old",
            "content": json.dumps(
                {"status": "rejected", "reason": "expired"}
            ),
        },
        {"role": "user", "content": "核准 fe341e4c447cde20"},
        _tool_call(
            "call-current",
            {"approval_token": "fe341e4c447cde20", "approved": True},
        ),
    ]

    assert (
        _find_bound_clawops_approval_args(
            messages, "fe341e4c447cde20"
        )
        is None
    )


def test_recovery_falls_back_to_durable_challenge(monkeypatch):
    token = "fe341e4c447cde20"
    contract = {
        "approved": False,
        "goal": {"objective": "唯讀檢查 Facebook Marketplace"},
        "scope": {"allowed": ["Facebook Marketplace 唯讀導覽"]},
    }
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate."
        "recover_clawops_approval_args",
        lambda candidate: contract if candidate == token else None,
    )

    assert _recover_bound_clawops_approval_args([], token) == contract
