"""Webhook approval policy must outrank global yolo and fail closed."""

from gateway.platforms.webhook_policy import (
    WebhookInteractionContext,
    reset_webhook_interaction_context,
    set_webhook_interaction_context,
)
from tools import approval


def _run_gate():
    return approval._run_approval_gate(
        pattern_key="test-pattern",
        description="test approval",
        display_target="echo test",
        cron_deny_message="cron denied",
        autoapprove_log_prefix="test",
    )


def test_webhook_default_deny_precedes_process_yolo(monkeypatch):
    token = set_webhook_interaction_context(
        WebhookInteractionContext("default", "route", "session", "deny", "fail")
    )
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", True)
    try:
        result = _run_gate()
    finally:
        reset_webhook_interaction_context(token)
    assert result["approved"] is False
    assert result["user_consent"] is False
    assert "deny interactive approvals" in result["message"]


def test_delivery_target_without_registered_notifier_never_queues_hidden_approval(monkeypatch):
    token = set_webhook_interaction_context(
        WebhookInteractionContext(
            "default", "route", "session", "delivery_target", "fail",
            approval_delivery={"target": "discord", "chat_id": "123"},
        )
    )
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval, "is_current_session_yolo_enabled", lambda: False)
    monkeypatch.setattr(approval, "get_current_session_key", lambda: "session")
    monkeypatch.setattr(approval, "is_approved", lambda *_: False)
    monkeypatch.setattr(approval, "_resolve_cli_approval_callback", lambda value: value)
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: False)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: True)
    with approval._lock:
        approval._gateway_notify_cbs.pop("session", None)
    try:
        result = _run_gate()
    finally:
        reset_webhook_interaction_context(token)
    assert result["approved"] is False
    assert "could not be reached" in result["message"]
    assert result.get("status") != "approval_required"
