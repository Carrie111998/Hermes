"""Profile-scoped session and unattended interaction policy for webhooks."""

from __future__ import annotations

import contextvars
import dataclasses
import hashlib
import json
import re
from typing import Any, Mapping


class WebhookPolicyError(ValueError):
    """A route cannot satisfy the unattended webhook contract."""


# Only targets with a real bidirectional approval/clarification implementation
# are eligible. A plain outbound sender is not enough: the response has to
# resolve the SAME pending session operation rather than create a second chat.
_APPROVAL_TARGETS = frozenset({
    "telegram", "discord", "slack", "matrix", "feishu", "teams",
    "qqbot", "whatsapp_cloud", "relay",
})
_CLARIFICATION_TARGETS = frozenset({
    "telegram", "discord", "slack", "matrix", "feishu", "teams",
    "qqbot", "whatsapp", "whatsapp_cloud", "google_chat", "photon", "relay",
})
_ADDRESS_KEYS = (
    "chat_id", "channel_id", "room_id", "conversation_id", "recipient", "to",
)
_METADATA_KEYS = (
    "thread_id", "message_thread_id", "slack_team_id", "workspace_id", "reply_to",
)
_TOKEN = re.compile(r"\{([a-zA-Z0-9_.-]+)\}")


@dataclasses.dataclass(frozen=True)
class WebhookInteractionContext:
    profile: str
    route: str
    session_key: str
    approval_mode: str
    clarification_mode: str
    session_mode: str = "event"
    approval_delivery: Mapping[str, Any] | None = None
    clarification_delivery: Mapping[str, Any] | None = None

    @property
    def delivery_target(self) -> str | None:
        delivery = self.approval_delivery or self.clarification_delivery
        if not isinstance(delivery, Mapping):
            return None
        value = delivery.get("target") or delivery.get("deliver")
        return str(value).strip().lower() if value else None


_CURRENT: contextvars.ContextVar[WebhookInteractionContext | None] = contextvars.ContextVar(
    "webhook_interaction_context", default=None
)


def set_webhook_interaction_context(context: WebhookInteractionContext):
    return _CURRENT.set(context)


def reset_webhook_interaction_context(token) -> None:
    _CURRENT.reset(token)


def get_webhook_interaction_context() -> WebhookInteractionContext | None:
    return _CURRENT.get()


def _resolve_token(payload: Mapping[str, Any], dotted: str) -> str:
    value: Any = payload
    for part in dotted.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise WebhookPolicyError(f"session key token {{{dotted}}} is missing")
        value = value[part]
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def _deliveries(route: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = route.get("deliveries")
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    legacy = route.get("deliver")
    if not legacy:
        return []
    item: dict[str, Any] = {"target": legacy}
    extra = route.get("deliver_extra")
    if isinstance(extra, Mapping):
        item.update(extra)
    return [item]


def _target(delivery: Mapping[str, Any]) -> str:
    return str(delivery.get("target") or delivery.get("deliver") or "").strip().lower()


def _select_delivery(
    route: Mapping[str, Any], purpose: str
) -> Mapping[str, Any] | None:
    allowed = _APPROVAL_TARGETS if purpose == "approval" else _CLARIFICATION_TARGETS
    for delivery in _deliveries(route):
        if _target(delivery) in allowed:
            return delivery
    return None


def validate_webhook_route_policy(name: str, route: Mapping[str, Any]) -> None:
    mode = str(route.get("session_mode", "event"))
    template = route.get("session_key_template")
    if mode not in {"event", "thread", "keyed"}:
        raise WebhookPolicyError(f"route {name!r}: unsupported session_mode {mode!r}")
    if mode in {"thread", "keyed"} and (
        not isinstance(template, str) or not template.strip()
    ):
        raise WebhookPolicyError(
            f"route {name!r}: {mode} session_mode requires session_key_template"
        )
    if mode == "event" and template not in {None, ""}:
        raise WebhookPolicyError(
            f"route {name!r}: event mode cannot carry session_key_template"
        )

    approval = str(route.get("approval_mode", "deny"))
    if approval not in {"deny", "delivery_target"}:
        raise WebhookPolicyError(f"route {name!r}: unsupported approval_mode {approval!r}")
    if approval == "delivery_target" and _select_delivery(route, "approval") is None:
        raise WebhookPolicyError(
            f"route {name!r}: delivery_target approvals require a bidirectional target"
        )

    # Completion callbacks are one-way notifications, not an interactive reply
    # protocol. Until a signed callback-response contract exists, clarification
    # supports only deterministic fail-closed or a bidirectional delivery target.
    clarification = str(route.get("clarification_mode", "fail"))
    if clarification not in {"fail", "delivery_target"}:
        raise WebhookPolicyError(
            f"route {name!r}: unsupported clarification_mode {clarification!r}"
        )
    if (
        clarification == "delivery_target"
        and _select_delivery(route, "clarification") is None
    ):
        raise WebhookPolicyError(
            f"route {name!r}: delivery_target clarification requires a bidirectional target"
        )


def resolve_webhook_session_key(
    *,
    profile: str,
    route_name: str,
    route: Mapping[str, Any],
    payload: Mapping[str, Any],
    delivery_id: str,
) -> str:
    """Return a stable profile/route-scoped event, thread, or keyed session."""
    mode = str(route.get("session_mode", "event"))
    if mode == "event":
        raw = delivery_id
    else:
        template = str(route.get("session_key_template") or "")
        raw = _TOKEN.sub(lambda match: _resolve_token(payload, match.group(1)), template)
        if not raw.strip():
            raise WebhookPolicyError("resolved session key is empty")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"webhook:{profile}:{route_name}:{mode}:{digest}"


def session_is_one_shot(route: Mapping[str, Any]) -> bool:
    return str(route.get("session_mode", "event")) == "event"


def interaction_context(
    *, profile: str, route_name: str, session_key: str, route: Mapping[str, Any]
) -> WebhookInteractionContext:
    return WebhookInteractionContext(
        profile=profile,
        route=route_name,
        session_key=session_key,
        approval_mode=str(route.get("approval_mode", "deny")),
        clarification_mode=str(route.get("clarification_mode", "fail")),
        session_mode=str(route.get("session_mode", "event")),
        approval_delivery=_select_delivery(route, "approval"),
        clarification_delivery=_select_delivery(route, "clarification"),
    )


def _mapping_get_by_platform(mapping: Any, platform: Any, target: str):
    if not isinstance(mapping, Mapping):
        return None
    if platform in mapping:
        return mapping[platform]
    if target in mapping:
        return mapping[target]
    for key, value in mapping.items():
        if str(getattr(key, "value", key)).strip().lower() == target:
            return value
    return None


def _routing_details(delivery: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
    merged: dict[str, Any] = {}
    extra = delivery.get("extra") or delivery.get("deliver_extra")
    if isinstance(extra, Mapping):
        merged.update(extra)
    merged.update({key: value for key, value in delivery.items() if key not in {"extra", "deliver_extra"}})
    chat_id = None
    for key in _ADDRESS_KEYS:
        value = merged.get(key)
        if value not in {None, ""}:
            chat_id = str(value)
            break
    metadata = {
        key: merged[key]
        for key in _METADATA_KEYS
        if merged.get(key) not in {None, ""}
    }
    return chat_id, metadata


def _home_channel(runner: Any, profile: str, platform: Any):
    config = None
    if profile != "default":
        configs = getattr(runner, "_profile_configs", None)
        if isinstance(configs, Mapping):
            config = configs.get(profile)
    if config is None:
        config = getattr(runner, "config", None)
    getter = getattr(config, "get_home_channel", None)
    if not callable(getter):
        return None
    try:
        return getter(platform)
    except Exception:
        return None


def resolve_webhook_interaction_delivery(
    runner: Any,
    context: WebhookInteractionContext,
    *,
    purpose: str,
):
    """Resolve one same-profile adapter/address for an interactive prompt."""
    if purpose not in {"approval", "clarification"}:
        raise WebhookPolicyError(f"unsupported interaction purpose {purpose!r}")
    delivery = (
        context.approval_delivery
        if purpose == "approval"
        else context.clarification_delivery
    )
    if not isinstance(delivery, Mapping):
        raise WebhookPolicyError(
            f"route {context.route!r}: {purpose} has no bidirectional delivery target"
        )
    target = _target(delivery)
    allowed = _APPROVAL_TARGETS if purpose == "approval" else _CLARIFICATION_TARGETS
    if target not in allowed:
        raise WebhookPolicyError(
            f"route {context.route!r}: target {target!r} cannot answer {purpose} prompts"
        )
    try:
        from gateway.config import Platform

        platform = Platform(target)
    except Exception as exc:
        raise WebhookPolicyError(
            f"route {context.route!r}: target {target!r} is not registered"
        ) from exc

    if runner is None:
        raise WebhookPolicyError(
            f"route {context.route!r}: gateway runner is unavailable for {purpose}"
        )
    if context.profile == "default":
        adapter_map = getattr(runner, "adapters", None)
    else:
        profiles = getattr(runner, "_profile_adapters", None)
        adapter_map = profiles.get(context.profile) if isinstance(profiles, Mapping) else None
    adapter = _mapping_get_by_platform(adapter_map, platform, target)
    if adapter is None:
        raise WebhookPolicyError(
            f"route {context.route!r}: {target!r} is not connected in profile {context.profile!r}"
        )

    method_name = "send_exec_approval" if purpose == "approval" else "send_clarify"
    if not callable(getattr(adapter, method_name, None)):
        raise WebhookPolicyError(
            f"route {context.route!r}: target {target!r} lacks {method_name}"
        )

    chat_id, metadata = _routing_details(delivery)
    if not chat_id:
        home = _home_channel(runner, context.profile, platform)
        chat_id = str(getattr(home, "chat_id", "") or "") if home is not None else ""
        if home is not None:
            thread_id = getattr(home, "thread_id", None)
            if thread_id not in {None, ""}:
                metadata.setdefault("thread_id", thread_id)
    if not chat_id:
        raise WebhookPolicyError(
            f"route {context.route!r}: target {target!r} has no explicit address or same-profile home channel"
        )
    return adapter, chat_id, metadata


def validate_webhook_route_runtime(
    name: str, route: Mapping[str, Any], runner: Any, *, profile: str
) -> None:
    """Fail startup when an admitted interactive route cannot really answer."""
    context = interaction_context(
        profile=profile,
        route_name=name,
        session_key=f"webhook:{profile}:{name}:validation",
        route=route,
    )
    if context.approval_mode == "delivery_target":
        resolve_webhook_interaction_delivery(runner, context, purpose="approval")
    if context.clarification_mode == "delivery_target":
        resolve_webhook_interaction_delivery(runner, context, purpose="clarification")
