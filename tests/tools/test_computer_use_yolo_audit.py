"""Audit regressions for computer-use approval bypasses."""


def test_yolo_request_bypass_emits_escalation_warning(monkeypatch):
    from tools import approval
    from tools.computer_use import tool as cu

    warnings = []
    approval_calls = []

    def approval_callback(action, args, summary):
        approval_calls.append(action)
        return "deny"

    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", True)
    monkeypatch.setattr(cu, "_warn_bypass_escalation", warnings.append)
    cu.set_approval_callback(approval_callback)
    try:
        assert cu._request_approval(
            "type", {"delivery_mode": "foreground"}, "audit-yolo"
        ) is None
        assert warnings == ["audit-yolo"]
        assert approval_calls == []
    finally:
        cu.set_approval_callback(None)
