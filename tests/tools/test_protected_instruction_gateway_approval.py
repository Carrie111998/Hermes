from tools import approval
from tools.file_tools import _request_protected_instruction_approval


def test_protected_instruction_write_uses_gateway_callback_and_accepts_approval():
    session_key = "agent:main:signal:dm:test-owner"
    prompts = []

    def notify(data):
        prompts.append(data)
        assert approval.resolve_gateway_approval(session_key, "once") == 1

    token = approval.set_current_session_key(session_key)
    approval.register_gateway_notify(session_key, notify)
    try:
        result = _request_protected_instruction_approval(["SOUL.md"])
    finally:
        approval.unregister_gateway_notify(session_key)
        approval.reset_current_session_key(token)

    assert result is None
    assert len(prompts) == 1
    assert prompts[0]["pattern_key"] == "protected_instruction_file"
    assert "SOUL.md" in prompts[0]["description"]


def test_protected_instruction_write_reports_gateway_delivery_failure():
    session_key = "agent:main:signal:dm:test-owner-failure"

    def notify(_data):
        raise RuntimeError("Signal approval delivery failed")

    token = approval.set_current_session_key(session_key)
    approval.register_gateway_notify(session_key, notify)
    try:
        result = _request_protected_instruction_approval(["SOUL.md"])
    finally:
        approval.unregister_gateway_notify(session_key)
        approval.reset_current_session_key(token)

    assert result is not None
    assert "could not be delivered" in result
    assert "timed out" not in result
