"""Task 12 ContextVar and same-profile interaction routing regressions."""

import contextvars

from gateway.config import Platform
from gateway.platforms.webhook_policy import (
    WebhookInteractionContext,
    get_webhook_interaction_context,
    interaction_context,
    reset_webhook_interaction_context,
    resolve_webhook_interaction_delivery,
    set_webhook_interaction_context,
)


class _Adapter:
    async def send_exec_approval(self, **kwargs):
        return kwargs

    async def send_clarify(self, **kwargs):
        return kwargs


class _Home:
    chat_id = "home-chat"
    thread_id = "home-thread"


class _Config:
    def get_home_channel(self, platform):
        return _Home()


class _Runner:
    def __init__(self):
        self.adapters = {Platform.DISCORD: _Adapter()}
        self._profile_adapters = {"worker": {Platform.DISCORD: _Adapter()}}
        self.config = _Config()
        self._profile_configs = {"worker": _Config()}


def test_context_is_reset_and_does_not_leak_between_profiles():
    alpha = WebhookInteractionContext("alpha", "route", "key-a", "deny", "fail")
    token = set_webhook_interaction_context(alpha)
    assert get_webhook_interaction_context().profile == "alpha"
    reset_webhook_interaction_context(token)
    assert get_webhook_interaction_context() is None


def test_copy_context_keeps_the_dispatch_profile():
    beta = WebhookInteractionContext("beta", "route", "key-b", "deny", "fail")
    token = set_webhook_interaction_context(beta)
    copied = contextvars.copy_context()
    reset_webhook_interaction_context(token)
    assert copied.run(get_webhook_interaction_context).profile == "beta"
    assert get_webhook_interaction_context() is None


def test_named_profile_interaction_uses_only_named_profile_adapter_and_route_address():
    context = interaction_context(
        profile="worker",
        route_name="route",
        session_key="key",
        route={
            "approval_mode": "delivery_target",
            "deliveries": [{"target": "discord", "chat_id": "worker-chat", "thread_id": "42"}],
        },
    )
    runner = _Runner()
    adapter, chat_id, metadata = resolve_webhook_interaction_delivery(
        runner, context, purpose="approval"
    )
    assert adapter is runner._profile_adapters["worker"][Platform.DISCORD]
    assert chat_id == "worker-chat"
    assert metadata["thread_id"] == "42"


def test_missing_explicit_address_uses_same_profile_home_channel():
    context = interaction_context(
        profile="worker",
        route_name="route",
        session_key="key",
        route={
            "clarification_mode": "delivery_target",
            "deliveries": [{"target": "discord"}],
        },
    )
    _adapter, chat_id, metadata = resolve_webhook_interaction_delivery(
        _Runner(), context, purpose="clarification"
    )
    assert chat_id == "home-chat"
    assert metadata["thread_id"] == "home-thread"
