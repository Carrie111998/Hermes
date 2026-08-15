"""Webhook clarification is deterministic unless a real reply path exists."""

import json

from gateway.platforms.webhook_policy import (
    WebhookInteractionContext,
    reset_webhook_interaction_context,
    set_webhook_interaction_context,
)
from tools.clarify_tool import clarify_tool


def test_default_webhook_clarification_fails_without_prompting():
    called = False

    def callback(*args, **kwargs):
        nonlocal called
        called = True
        return "answer"

    token = set_webhook_interaction_context(
        WebhookInteractionContext("default", "route", "session", "deny", "fail")
    )
    try:
        result = clarify_tool("Question?", callback=callback)
    finally:
        reset_webhook_interaction_context(token)
    assert called is False
    assert "Clarification is disabled" in result


def test_delivery_target_clarification_requires_injected_gateway_callback():
    context = WebhookInteractionContext(
        "default", "route", "session", "deny", "delivery_target",
        clarification_delivery={"target": "discord", "chat_id": "123"},
    )
    token = set_webhook_interaction_context(context)
    try:
        blocked = clarify_tool("Question?", callback=None)
        answered = clarify_tool("Question?", callback=lambda *_args, **_kwargs: "yes")
    finally:
        reset_webhook_interaction_context(token)
    assert "target is unavailable" in blocked
    assert json.loads(answered)["user_response"] == "yes"
