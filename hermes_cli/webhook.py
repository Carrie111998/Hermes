"""hermes webhook — manage dynamic webhook subscriptions from the CLI.

Usage:
    hermes webhook subscribe <name> [options]
    hermes webhook list [--json]
    hermes webhook show <name> [--json]
    hermes webhook update <name> [options]
    hermes webhook enable|disable|rotate-secret <name>
    hermes webhook remove <name>
    hermes webhook test <name> [--payload '{"key": "value"}']

Subscriptions persist in each profile's canonical route store and are
hot-reloaded by the sharded webhook adapter without a gateway restart.
"""

import base64
import copy
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping
from urllib.parse import urlsplit

from gateway.platforms.webhook_models import (
    WebhookRouteDocument,
    from_persisted_route,
    to_persisted_route,
)
from gateway.platforms.webhook_store import (
    WebhookRouteStore,
    WebhookRouteStoreError,
)
from hermes_constants import display_hermes_home
from hermes_cli.config import cfg_get


_MAX_SECRET_BYTES = 4096
_LEGACY_CLI_DESCRIPTION_PREFIX = "Agent-created subscription:"


class WebhookCommandError(RuntimeError):
    """A webhook management command cannot proceed safely."""


class ConcurrentWebhookUpdateError(WebhookCommandError):
    """A route changed divergently after a caller loaded its snapshot."""


class _SubscriptionSnapshot(dict[str, dict]):
    """Compatibility view retaining a baseline for an atomic three-way save."""

    def __init__(self, value: Mapping[str, dict], *, profile: str):
        super().__init__(copy.deepcopy(dict(value)))
        self._baseline = copy.deepcopy(dict(value))
        self._profile = profile


def _merge_subscription_snapshot(
    baseline: Mapping[str, dict],
    desired: Mapping[str, dict],
    current: Mapping[str, dict],
) -> dict[str, dict]:
    """Preserve unrelated writes and reject divergent same-route changes."""

    missing = object()
    merged: dict[str, dict] = {}
    conflicts: list[str] = []
    for name in sorted(set(baseline) | set(desired) | set(current)):
        before = baseline.get(name, missing)
        wanted = desired.get(name, missing)
        live = current.get(name, missing)
        if wanted == before:
            chosen = live
        elif live == before or live == wanted:
            chosen = wanted
        else:
            conflicts.append(name)
            continue
        if chosen is not missing:
            merged[name] = copy.deepcopy(chosen)
    if conflicts:
        raise ConcurrentWebhookUpdateError(
            "Concurrent webhook route update conflict: " + ", ".join(conflicts)
        )
    return merged


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _profile_root(profile: str | None = None) -> Path:
    """Return the exact sharded root owned by a subscription profile."""

    return _route_store(profile).profile_root


def _route_store(profile: str | None = None) -> WebhookRouteStore:
    """Build the canonical locked store for an explicit or active profile."""

    from hermes_constants import get_default_hermes_root

    return WebhookRouteStore(
        get_default_hermes_root(),
        profile=_storage_profile_name(profile),
    )


def _subscriptions_path(profile: str | None = None) -> Path:
    return _route_store(profile).path


def _load_subscriptions(profile: str | None = None) -> Dict[str, dict]:
    """Return lossless canonical projections for compatibility callers."""

    store = _route_store(profile)
    current = {name: to_persisted_route(route) for name, route in store.load().items()}
    return _SubscriptionSnapshot(current, profile=store.profile)


def _save_subscriptions(
    subs: Dict[str, dict],
    profile: str | None = None,
) -> None:
    requested_profile = _storage_profile_name(profile)
    if isinstance(subs, _SubscriptionSnapshot):
        if profile is not None and requested_profile != subs._profile:
            raise ValueError("webhook subscription snapshot belongs to another profile")
        store = _route_store(subs._profile)
        baseline = copy.deepcopy(subs._baseline)
        desired = copy.deepcopy(dict(subs))

        def _mutate(
            current_documents: Dict[str, WebhookRouteDocument],
        ) -> Dict[str, WebhookRouteDocument]:
            current = {
                name: to_persisted_route(route)
                for name, route in current_documents.items()
            }
            merged = _merge_subscription_snapshot(baseline, desired, current)
            return {
                name: _new_route_document(name, route, profile=store.profile)
                for name, route in merged.items()
            }

        saved = store.update(_mutate)
        refreshed = {name: to_persisted_route(route) for name, route in saved.items()}
        subs.clear()
        subs.update(copy.deepcopy(refreshed))
        subs._baseline = copy.deepcopy(refreshed)
        return

    store = _route_store(profile)
    documents = {
        name: _new_route_document(name, route, profile=store.profile)
        for name, route in subs.items()
    }
    store.save(documents)


def _new_route_document(
    name: str,
    route: Mapping[str, Any],
    *,
    profile: str,
) -> WebhookRouteDocument:
    """Validate a newly authored mapping through the canonical document."""

    raw = dict(route)
    raw.pop("name", None)
    embedded_profile = raw.get("profile", profile)
    if embedded_profile != profile:
        raise ValueError("webhook route belongs to a different profile store")
    raw["profile"] = profile
    if "deliveries" not in raw:
        delivery: dict[str, Any] = {}
        if raw.get("deliver") is not None:
            delivery["target"] = raw["deliver"]
        extra = raw.get("deliver_extra")
        if isinstance(extra, Mapping):
            delivery.update(extra)
        elif extra is not None:
            delivery["extra"] = extra
        raw["deliveries"] = [delivery] if delivery else []
    return WebhookRouteDocument.model_validate({"name": name, **raw})


def _get_webhook_config() -> dict:
    """Load webhook platform config. Returns {} if not configured."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        return cfg_get(cfg, "platforms", "webhook", default={})
    except Exception:
        return {}


def _is_webhook_enabled() -> bool:
    return bool(_get_webhook_config().get("enabled"))


def _get_webhook_base_url() -> str:
    wh = _get_webhook_config().get("extra", {})
    host = wh.get("host")
    port = wh.get("port", 8644)
    display_host = "localhost" if not host or host in {"0.0.0.0", "::"} else host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}"


def _is_loopback_webhook_url(url: str) -> bool:
    """Return whether a displayed webhook URL is local to this host."""

    hostname = (urlsplit(url).hostname or "").strip().lower()
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _normalize_subscription_profile(value: Any) -> str:
    """Return one canonical, URL-safe gateway profile binding."""

    from hermes_cli.profiles import normalize_profile_name, validate_profile_name

    profile = normalize_profile_name(str(value or "default"))
    validate_profile_name(profile)
    return profile


def _subscription_url(base_url: str, name: str, profile: Any) -> str:
    """Render the route URL that selects its bound execution profile."""

    normalized_profile = _normalize_subscription_profile(profile)
    if normalized_profile == "default":
        return f"{base_url}/webhooks/{name}"
    return f"{base_url}/p/{normalized_profile}/webhooks/{name}"


def _setup_hint() -> str:
    _dhh = display_hermes_home()
    return f"""
  Webhook platform is not enabled. To set it up:

  1. Run the gateway setup wizard:
     hermes gateway setup

  2. Or manually add to {_dhh}/config.yaml:
     platforms:
       webhook:
         enabled: true
         extra:
           port: 8644
           secret: "your-global-hmac-secret"

  3. Or set environment variables in {_dhh}/.env:
     WEBHOOK_ENABLED=true
     WEBHOOK_PORT=8644
     WEBHOOK_SECRET=your-global-secret

  Then start the gateway: hermes gateway run
"""


def _require_webhook_enabled() -> bool:
    """Check webhook is enabled. Print setup guide and return False if not."""
    if _is_webhook_enabled():
        return True
    print(_setup_hint())
    return False


def _args_profile(args) -> str | None:
    """Return the optional profile store selected by a management command."""

    raw = getattr(args, "profile", "") or ""
    if not isinstance(raw, str):
        raise WebhookCommandError("Invalid webhook profile selector.")
    if not raw.strip():
        return None
    try:
        return _normalize_subscription_profile(raw)
    except ValueError as exc:
        raise WebhookCommandError(f"Invalid webhook profile: {exc}") from exc


def _storage_profile_name(profile: str | None) -> str:
    """Return the profile authority represented by one subscription store."""

    if profile:
        return _normalize_subscription_profile(profile)
    try:
        from hermes_cli.profiles import get_active_profile_name

        active = get_active_profile_name()
    except Exception:
        active = "default"
    # A custom HERMES_HOME is the root/default profile for this installation.
    if not active or active == "custom":
        return "default"
    return _normalize_subscription_profile(active)


def _route_for_json(name: str, route: Mapping[str, Any], base_url: str) -> dict:
    """Build a stable read model that never contains a plaintext secret."""

    try:
        route_url = _subscription_url(
            base_url,
            name,
            route.get("profile", "default"),
        )
    except ValueError:
        route_url = None
    secret_set = bool(
        route.get("secret") or route.get("secret_value") or route.get("secret_ref")
    )
    return {
        "name": name,
        "description": route.get("description", ""),
        "profile": route.get("profile", "default"),
        "provider": route.get("provider"),
        "signature_mode": route.get("signature_mode"),
        "enabled": route.get("enabled", True) is not False,
        "events": list(route.get("events") or []),
        "deliver": route.get("deliver", "log"),
        "deliver_only": bool(route.get("deliver_only")),
        "prompt": route.get("prompt", ""),
        "script": route.get("script"),
        "skills": list(route.get("skills") or []),
        "created_at": route.get("created_at"),
        "updated_at": route.get("updated_at"),
        "url": route_url,
        "secret_set": secret_set,
        "secret_masked": "***" if secret_set else "",
    }


def _read_secret_fd(fd: int) -> str | None:
    """Read bounded UTF-8 from *fd* without closing or echoing it."""

    data = bytearray()
    try:
        while len(data) <= _MAX_SECRET_BYTES:
            chunk = os.read(fd, min(4096, _MAX_SECRET_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
    except (OSError, OverflowError):
        print("Error: Could not read --secret-fd.")
        return None

    if len(data) > _MAX_SECRET_BYTES:
        print(f"Error: --secret-fd input exceeds {_MAX_SECRET_BYTES} bytes.")
        return None
    try:
        secret = data.decode("utf-8").rstrip()
    except UnicodeDecodeError:
        print("Error: --secret-fd input must be valid UTF-8.")
        return None
    if not secret:
        print("Error: --secret-fd input is empty after trimming trailing whitespace.")
        return None
    return secret


def _bind_subscription(name: str, route: Mapping[str, Any]):
    """Bind a saved CLI subscription to the canonical webhook contract.

    New subscriptions always persist an explicit provider and signature mode.
    The narrow description check preserves ``hermes webhook test`` for routes
    written by older CLI versions; the gateway uses the same one-time legacy
    classification when it loads dynamic subscriptions.
    """

    from gateway.platforms.webhook_contract import WebhookRouteConfig

    candidate = dict(route)
    if not candidate.get("provider") and not candidate.get("signature_mode"):
        description = str(candidate.get("description") or "")
        if description.startswith(_LEGACY_CLI_DESCRIPTION_PREFIX):
            candidate["provider"] = "github"
    request_profile = candidate.get("profile", "default")
    return WebhookRouteConfig.bind(
        name,
        candidate,
        headers={},
        request_profile=request_profile,
    )


def _selected_test_event(bound_route) -> str:
    return bound_route.events[0] if bound_route.events else "test"


def _default_test_payload(bound_route) -> Dict[str, Any]:
    """Return a provider-valid default payload for ``hermes webhook test``."""

    event_type = _selected_test_event(bound_route)
    if bound_route.provider == "github" and bound_route.events:
        test_id = f"test_{secrets.token_hex(12)}"
        repository = {"id": 801, "full_name": "example/hermes-webhook-test"}
        sender = {"id": 901, "login": "hermes-webhook-test"}
        if event_type == "check_run":
            return {
                "action": "completed",
                "check_run": {
                    "id": 401,
                    "name": "Hermes webhook test",
                    "status": "completed",
                    "conclusion": "success",
                    "details_url": "https://example.invalid/checks/401",
                    "pull_requests": [],
                },
                "repository": repository,
                "sender": sender,
                "test": True,
                "test_id": test_id,
            }
        if event_type == "pull_request":
            return {
                "action": "opened",
                "number": 1,
                "pull_request": {
                    "id": 701,
                    "number": 1,
                    "state": "open",
                    "title": "Hermes webhook test",
                    "head": {"ref": "hermes-webhook-test"},
                    "base": {"ref": "main"},
                },
                "repository": repository,
                "sender": sender,
                "test": True,
                "test_id": test_id,
            }
        if event_type == "push":
            return {
                "ref": "refs/heads/main",
                "before": "0" * 40,
                "after": "1" * 40,
                "created": False,
                "deleted": False,
                "forced": False,
                "commits": [],
                "head_commit": None,
                "repository": repository,
                "pusher": {"name": "hermes-webhook-test"},
                "sender": sender,
                "test": True,
                "test_id": test_id,
            }
        if event_type == "issues":
            return {
                "action": "opened",
                "issue": {"id": 601, "number": 1, "title": "Webhook test"},
                "repository": repository,
                "sender": sender,
                "test": True,
                "test_id": test_id,
            }
        if event_type == "ping":
            return {
                "zen": "Keep it logically awesome.",
                "hook_id": 501,
                "hook": {"id": 501, "type": "Repository", "name": "web"},
                "repository": repository,
                "sender": sender,
                "test": True,
                "test_id": test_id,
            }
        raise ValueError(f"Unsupported authenticated GitHub event: {event_type}")
    if bound_route.provider == "hermes":
        return {
            "test": True,
            "message": "Hello from hermes webhook test",
            "delivery_id": f"test_{secrets.token_hex(12)}",
            "hook_event_name": event_type,
            "timestamp": datetime
            .now(tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
    if bound_route.provider == "stripe":
        return {
            "id": f"evt_test_{secrets.token_hex(12)}",
            "type": event_type,
            "message": "Hello from hermes webhook test",
        }
    if bound_route.provider == "linear":
        return {
            "test": True,
            "test_id": f"test_{secrets.token_hex(12)}",
            "type": event_type,
            "webhookTimestamp": int(time.time() * 1000),
            "message": "Hello from hermes webhook test",
        }
    if bound_route.provider == "chatwoot":
        payload = {
            "test": True,
            "event": event_type,
            "message": "Hello from hermes webhook test",
        }
    else:
        payload = {
            "test": True,
            "event_type": event_type,
            "message": "Hello from hermes webhook test",
        }

    # Body- or credential-only contracts have no authenticated provider ID.
    # Give each generated test a fresh value inside the covered body so two
    # deliberate CLI tests cannot collapse onto the same durable replay key.
    # Svix/Standard Webhooks already authenticate the fresh message ID that
    # _test_headers emits; Hermes and Stripe carry their native IDs above.
    if not bound_route.provider_spec.authenticated_delivery_id_headers:
        payload["test_id"] = f"test_{secrets.token_hex(12)}"
    return payload


def _svix_signature(secret: str, message: bytes) -> str:
    key = secret.encode()
    if secret.startswith("whsec_"):
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def _test_headers(
    bound_route,
    secret: str,
    payload: bytes,
    payload_object: Mapping[str, Any],
) -> Dict[str, str]:
    """Build one request for the verifier selected by the saved route."""

    mode = bound_route.signature_mode
    event_type = _selected_test_event(bound_route)
    headers = {"Content-Type": "application/json"}
    body_digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    if mode == "github":
        headers["X-Hub-Signature-256"] = f"sha256={body_digest}"
        headers["X-GitHub-Event"] = event_type
    elif mode == "gitlab":
        headers["X-Gitlab-Token"] = secret
        headers["X-GitLab-Event"] = event_type
    elif mode in {"svix", "standard_webhooks"}:
        message_id = f"msg_{secrets.token_hex(12)}"
        timestamp = str(int(time.time()))
        prefix = "svix" if mode == "svix" else "webhook"
        headers[f"{prefix}-id"] = message_id
        headers[f"{prefix}-timestamp"] = timestamp
        headers[f"{prefix}-signature"] = _svix_signature(
            secret,
            message_id.encode() + b"." + timestamp.encode() + b"." + payload,
        )
    elif mode == "hindsight":
        headers["X-Hindsight-Signature"] = f"sha256={body_digest}"
    elif mode == "hermes":
        delivery_id = str(payload_object.get("delivery_id") or "").strip()
        hermes_event = str(payload_object.get("hook_event_name") or "").strip()
        if not delivery_id or not hermes_event:
            raise ValueError(
                "Hermes test payload requires delivery_id and hook_event_name"
            )
        headers["X-Hermes-Signature-256"] = f"sha256={body_digest}"
        headers["X-Hermes-Delivery"] = delivery_id
        headers["X-Hermes-Event"] = hermes_event
    elif mode == "linear":
        headers["linear-signature"] = body_digest
        timestamp_ms = payload_object.get("webhookTimestamp")
        if not isinstance(timestamp_ms, int) or isinstance(timestamp_ms, bool):
            raise ValueError("Linear test payload requires an integer webhookTimestamp")
        headers["linear-timestamp"] = str(timestamp_ms)
    elif mode == "stripe":
        timestamp = str(int(time.time()))
        signed = timestamp.encode() + b"." + payload
        digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        headers["Stripe-Signature"] = f"t={timestamp},v1={digest}"
    elif mode == "generic_v1":
        headers["X-Webhook-Signature"] = body_digest
    elif mode == "generic_v2":
        timestamp = str(int(time.time()))
        signed = timestamp.encode() + b"." + payload
        digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        headers["X-Webhook-Timestamp"] = timestamp
        headers["X-Webhook-Signature-V2"] = digest
    else:  # WebhookRouteConfig currently makes this unreachable.
        raise ValueError(f"Unsupported webhook signature mode: {mode}")
    return headers


def webhook_command(args):
    """Entry point for 'hermes webhook' subcommand."""
    sub = getattr(args, "webhook_action", None)

    if not sub:
        print(
            "Usage: hermes webhook "
            "{subscribe|list|show|update|enable|disable|rotate-secret|remove|test}"
        )
        print("Run 'hermes webhook --help' for details.")
        return

    if not _require_webhook_enabled():
        return

    try:
        if sub in {"subscribe", "add", "create"}:
            _cmd_subscribe(args)
        elif sub in {"list", "ls"}:
            _cmd_list(args)
        elif sub == "show":
            _cmd_show(args)
        elif sub == "update":
            _cmd_update(args)
        elif sub == "enable":
            _cmd_set_enabled(args, True)
        elif sub == "disable":
            _cmd_set_enabled(args, False)
        elif sub == "rotate-secret":
            _cmd_rotate_secret(args)
        elif sub in {"remove", "rm"}:
            _cmd_remove(args)
        elif sub == "test":
            _cmd_test(args)
        else:
            print(f"Error: Unknown webhook action '{sub}'.")
    except WebhookCommandError as exc:
        print(f"Error: {exc}")
    except (WebhookRouteStoreError, TimeoutError) as exc:
        print(
            "Error: Cannot safely access the webhook subscription store "
            f"({exc}). No changes were made."
        )
    except OSError:
        print("Error: Could not safely update the webhook subscription store.")


def _cmd_subscribe(args):
    name = args.name.strip().lower().replace(" ", "-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", name):
        print(
            f"Error: Invalid name '{name}'. Use at most 128 lowercase "
            "alphanumeric, hyphen, or underscore characters."
        )
        return

    store_profile = _args_profile(args)
    route_profile_arg = getattr(args, "route_profile", "") or ""
    try:
        requested_route_profile = (
            _normalize_subscription_profile(route_profile_arg)
            if route_profile_arg
            else None
        )
    except ValueError as exc:
        print(f"Error: Invalid webhook profile: {exc}")
        return
    # ``--route-profile`` predates sharded storage.  When it is the only
    # selector, preserve the spelling as an alias for the owning shard.  A
    # document can never claim an authority different from its physical store.
    if store_profile is None and requested_route_profile is not None:
        store_profile = requested_route_profile
    store = _route_store(store_profile)
    profile = store.profile
    if requested_route_profile not in {None, profile}:
        print(
            "Error: --route-profile must match the selected --profile store; "
            "webhook route authority is profile-sharded."
        )
        return

    is_update = name in store.load()
    if is_update and not getattr(args, "replace", False):
        print(
            f"Error: A subscription named '{name}' already exists. "
            f"Use 'hermes webhook update {name}' to patch fields, or pass "
            "--replace to overwrite."
        )
        return

    secret_arg = getattr(args, "secret", "") or ""
    secret_fd = getattr(args, "secret_fd", None)
    if secret_arg and secret_fd is not None:
        print("Error: --secret and --secret-fd are mutually exclusive.")
        return
    if secret_fd is not None:
        if (
            not isinstance(secret_fd, int)
            or isinstance(secret_fd, bool)
            or secret_fd < 0
        ):
            print("Error: --secret-fd must be a non-negative integer.")
            return
        secret = _read_secret_fd(secret_fd)
        if secret is None:
            return
    else:
        secret = secret_arg or secrets.token_urlsafe(32)

    events = (
        [
            event
            for item in (getattr(args, "events", "") or "").split(",")
            if (event := item.strip())
        ]
        if getattr(args, "events", "")
        else []
    )

    route = {
        "description": getattr(args, "description", "")
        or f"Agent-created subscription: {name}",
        "profile": profile,
        "provider": getattr(args, "provider", "github") or "github",
        "events": events,
        "secret": secret,
        "prompt": getattr(args, "prompt", "") or "",
        "skills": [
            skill.strip()
            for skill in (getattr(args, "skills", "") or "").split(",")
            if skill.strip()
        ],
        "deliver": getattr(args, "deliver", "") or "log",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    signature_mode = getattr(args, "signature_mode", "") or ""
    if signature_mode.strip():
        route["signature_mode"] = signature_mode.strip()

    try:
        bound_route = _bind_subscription(name, route)
    except (TypeError, ValueError) as exc:
        print(f"Error: Invalid webhook provider contract: {exc}")
        return
    # Persist canonical values so hot reload and ``hermes webhook test`` use
    # exactly the verifier that was validated here.
    route["provider"] = bound_route.provider
    route["signature_mode"] = bound_route.signature_mode
    route["events"] = list(bound_route.events)
    events = route["events"]

    if getattr(args, "deliver_only", False):
        if route["deliver"] == "log":
            print(
                "Error: --deliver-only requires --deliver to be a real target "
                "(telegram, discord, slack, github_comment, etc.) — not 'log'."
            )
            return
        route["deliver_only"] = True

    script = getattr(args, "script", "") or ""
    if script.strip():
        route["script"] = script.strip()

    deliver_chat_id = getattr(args, "deliver_chat_id", "") or ""
    if deliver_chat_id:
        route["deliver_extra"] = {"chat_id": deliver_chat_id}

    try:
        document = _new_route_document(name, route, profile=profile)
    except Exception:
        # Pydantic may include input values in its error rendering. Never print
        # an exception that was constructed from a plaintext secret.
        print("Error: Invalid webhook route document. No changes were made.")
        return

    replace = bool(getattr(args, "replace", False))
    mutation = {"replaced": False}

    def _insert(current: Dict[str, WebhookRouteDocument]):
        exists = name in current
        if exists and not replace:
            raise WebhookCommandError(
                f"A subscription named '{name}' already exists. "
                f"Use 'hermes webhook update {name}' to patch fields, or pass "
                "--replace to overwrite."
            )
        mutation["replaced"] = exists
        current[name] = document
        return current

    saved = store.update(_insert)
    document = saved[name]
    route = to_persisted_route(document)
    is_update = mutation["replaced"]

    base_url = _get_webhook_base_url()
    status = "Updated" if is_update else "Created"
    subscription_url = _subscription_url(base_url, name, bound_route.profile)
    local_test_url = _is_loopback_webhook_url(subscription_url)

    if getattr(args, "json", False):
        result = _route_for_json(name, route, base_url)
        result["operation"] = status.lower()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    print(f"\n  {status} webhook subscription: {name}")
    url_label = "Local test URL" if local_test_url else "Callback URL"
    print(f"  {url_label}: {subscription_url}")
    print(f"  HMAC secret stored in {store.path} (mode 0600); value not displayed.")
    print(f"  Provider: {bound_route.provider} ({bound_route.signature_mode})")
    if events:
        print(f"  Events: {', '.join(events)}")
    else:
        print("  Events: (all)")
    print(f"  Deliver: {route['deliver']}")
    if route.get("deliver_only"):
        print("  Mode: direct delivery (no agent, zero LLM cost)")
    if route.get("prompt"):
        prompt_preview = route["prompt"][:80] + (
            "..." if len(route["prompt"]) > 80 else ""
        )
        label = "Message" if route.get("deliver_only") else "Prompt"
        print(f"  {label}: {prompt_preview}")
    if route.get("script"):
        print(f"  Script: {route['script']}")
    if local_test_url:
        callback_path = urlsplit(subscription_url).path
        print(
            "\n  External callback: a public HTTPS reverse proxy is required; "
            f"use https://<public-host>{callback_path} and preserve the exact path."
        )
        print("  Do not configure an external service with the local test URL.")
    else:
        print("\n  Configure your service to POST to the callback URL above.")
    authentication_hints = {
        "github": "GitHub webhook secret (X-Hub-Signature-256 HMAC-SHA256)",
        "gitlab": "GitLab Secret token (X-Gitlab-Token exact string match)",
        "generic_v1": "generic_v1 secret (X-Webhook-Signature HMAC-SHA256)",
        "generic_v2": (
            "generic_v2 secret (X-Webhook-Timestamp + "
            "X-Webhook-Signature-V2 HMAC-SHA256)"
        ),
    }
    authentication_hint = authentication_hints.get(
        bound_route.signature_mode,
        f"{bound_route.provider}/{bound_route.signature_mode} verifier secret",
    )
    print(f"  Authentication: {authentication_hint}.")
    print("  The gateway must be running to receive events (hermes gateway run).\n")


def _delivery_label(route: Mapping[str, Any]) -> str:
    target = route.get("deliver")
    if not target:
        deliveries = route.get("deliveries")
        if isinstance(deliveries, list) and deliveries:
            first = deliveries[0]
            if isinstance(first, Mapping):
                target = first.get("target")
    result = str(target or "log")
    if route.get("deliver_only"):
        result = f"{result} (direct — no agent)"
    return result


def _cmd_show(args):
    name = args.name.strip().lower()
    store = _route_store(_args_profile(args))
    document = store.load().get(name)
    if document is None:
        print(f"  No subscription named '{name}'.")
        return

    route = to_persisted_route(document)
    base_url = _get_webhook_base_url()
    if getattr(args, "json", False):
        print(
            json.dumps(
                _route_for_json(name, route, base_url),
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    summary = _route_for_json(name, route, base_url)
    events = ", ".join(summary["events"]) or "(all)"
    provider = summary["provider"] or "legacy github"
    if summary["signature_mode"]:
        provider = f"{provider} ({summary['signature_mode']})"
    print(f"\n  ◆ {name}")
    if summary["description"]:
        print(f"    {summary['description']}")
    print(f"    URL:      {summary['url']}")
    print(f"    Enabled:  {'yes' if summary['enabled'] else 'no'}")
    print(f"    Provider: {provider}")
    print(f"    Events:   {events}")
    print(f"    Deliver:  {_delivery_label(route)}")
    secret_display = "***" if summary["secret_set"] else "(none)"
    print(f"    Secret:   {secret_display}")
    if summary["script"]:
        print(f"    Script:   {summary['script']}")
    if summary["skills"]:
        print(f"    Skills:   {', '.join(summary['skills'])}")
    if summary["prompt"]:
        print(f"    Prompt:   {summary['prompt'][:80]}")
    print()


def _cmd_update(args):
    name = args.name.strip().lower()
    store = _route_store(_args_profile(args))
    values = {
        "prompt": getattr(args, "prompt", "") or "",
        "events": getattr(args, "events", "") or "",
        "description": getattr(args, "description", "") or "",
        "skills": getattr(args, "skills", "") or "",
        "deliver": getattr(args, "deliver", "") or "",
        "deliver_chat_id": getattr(args, "deliver_chat_id", "") or "",
    }
    if not any(values.values()):
        print("Error: No update fields were specified. No changes were made.")
        return

    def _patch(current: Dict[str, WebhookRouteDocument]):
        document = current.get(name)
        if document is None:
            raise WebhookCommandError(f"No subscription named '{name}'.")
        route = to_persisted_route(document)
        if values["prompt"]:
            route["prompt"] = values["prompt"]
        if values["events"]:
            route["events"] = [
                item.strip() for item in values["events"].split(",") if item.strip()
            ]
        if values["description"]:
            route["description"] = values["description"]
        if values["skills"]:
            route["skills"] = [
                item.strip() for item in values["skills"].split(",") if item.strip()
            ]

        if values["deliver"] or values["deliver_chat_id"]:
            deliveries = [
                dict(item)
                for item in route.get("deliveries", [])
                if isinstance(item, Mapping)
            ]
            primary = deliveries[0] if deliveries else {}
            if values["deliver"]:
                route["deliver"] = values["deliver"]
                primary["target"] = values["deliver"]
            if values["deliver_chat_id"]:
                extra = route.get("deliver_extra")
                extra = dict(extra) if isinstance(extra, Mapping) else {}
                extra["chat_id"] = values["deliver_chat_id"]
                route["deliver_extra"] = extra
                primary["chat_id"] = values["deliver_chat_id"]
            if primary:
                if deliveries:
                    deliveries[0] = primary
                else:
                    deliveries.append(primary)
            route["deliveries"] = deliveries

        route["updated_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        )
        current[name] = from_persisted_route(
            name,
            route,
            profile=store.profile,
        )
        return current

    try:
        store.update(_patch)
    except ValueError:
        # ValidationError formatting can include the plaintext legacy secret.
        print("Error: Invalid webhook update. No changes were made.")
        return
    print(f"  Updated webhook subscription: {name}")


def _cmd_set_enabled(args, enabled: bool):
    name = args.name.strip().lower()
    store = _route_store(_args_profile(args))

    def _toggle(current: Dict[str, WebhookRouteDocument]):
        document = current.get(name)
        if document is None:
            raise WebhookCommandError(f"No subscription named '{name}'.")
        route = to_persisted_route(document)
        route["enabled"] = enabled
        route["updated_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        )
        current[name] = from_persisted_route(
            name,
            route,
            profile=store.profile,
        )
        return current

    store.update(_toggle)
    action = "Enabled" if enabled else "Disabled"
    print(f"  {action} webhook subscription: {name}")


def _cmd_rotate_secret(args):
    name = args.name.strip().lower()
    store = _route_store(_args_profile(args))
    new_secret = secrets.token_urlsafe(32)

    def _rotate(current: Dict[str, WebhookRouteDocument]):
        document = current.get(name)
        if document is None:
            raise WebhookCommandError(f"No subscription named '{name}'.")
        if document.secret_ref:
            raise WebhookCommandError(
                "Referenced webhook secrets must be rotated in their secret backend."
            )
        route = to_persisted_route(document)
        route.pop("secret_value", None)
        route["secret"] = new_secret
        route["updated_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        )
        current[name] = from_persisted_route(
            name,
            route,
            profile=store.profile,
        )
        return current

    store.update(_rotate)
    print(f"  Rotated secret for {name}.")
    print(f"  New secret (shown once): {new_secret}")
    print("  Store this in your provider's webhook configuration.")


def _cmd_list(args):
    store = _route_store(_args_profile(args))
    documents = store.load()
    base_url = _get_webhook_base_url()
    rows = [
        _route_for_json(name, to_persisted_route(document), base_url)
        for name, document in documents.items()
    ]
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not documents:
        print("  No dynamic webhook subscriptions.")
        print("  Create one with: hermes webhook subscribe <name>")
        return

    print(f"\n  {len(documents)} webhook subscription(s):\n")
    for (name, document), summary in zip(documents.items(), rows):
        route = to_persisted_route(document)
        events = ", ".join(summary["events"]) or "(all)"
        status = "enabled" if summary["enabled"] else "disabled"
        print(f"  ◆ {name} ({status})")
        if summary["description"]:
            print(f"    {summary['description']}")
        print(f"    URL:      {summary['url']}")
        provider = summary["provider"] or "legacy github"
        if summary["signature_mode"]:
            provider = f"{provider} ({summary['signature_mode']})"
        print(f"    Provider: {provider}")
        print(f"    Events:   {events}")
        print(f"    Deliver:  {_delivery_label(route)}")
        if summary["script"]:
            print(f"    Script:   {summary['script']}")
        print()


def _cmd_remove(args):
    name = args.name.strip().lower()
    store = _route_store(_args_profile(args))

    def _remove(current: Dict[str, WebhookRouteDocument]):
        if name not in current:
            raise WebhookCommandError(
                f"No subscription named '{name}'. "
                "Static routes from config.yaml cannot be removed here."
            )
        del current[name]
        return current

    store.update(_remove)
    print(f"  Removed webhook subscription: {name}")


def _cmd_test(args):
    """Send a test POST to a webhook route."""
    name = args.name.strip().lower()
    store = _route_store(_args_profile(args))
    document = store.load().get(name)

    if document is None:
        print(f"  No subscription named '{name}'.")
        return
    if document.enabled is False:
        print(f"  Error: Subscription '{name}' is disabled.")
        return

    try:
        bound_route = document.contract
    except (TypeError, ValueError) as exc:
        print(f"  Error: Invalid webhook provider contract: {exc}")
        return
    if document.legacy_secret is not None:
        secret = document.legacy_secret
    elif document.legacy_secret_value is not None:
        secret = document.legacy_secret_value
    elif document.secret_ref:
        secret = os.environ.get(document.secret_ref, "")
    else:
        secret = ""
    if not isinstance(secret, str) or not secret:
        print("  Error: Subscription has no usable webhook secret.")
        return
    base_url = _get_webhook_base_url()
    url = _subscription_url(base_url, name, bound_route.profile)

    supplied_payload = getattr(args, "payload", "") or ""
    if supplied_payload:
        payload = supplied_payload
        try:
            payload_object = json.loads(payload)
        except (json.JSONDecodeError, RecursionError):
            print("  Error: --payload must be valid JSON.")
            return
        if not isinstance(payload_object, dict):
            print("  Error: --payload must be a JSON object.")
            return
    else:
        payload_object = _default_test_payload(bound_route)
        payload = json.dumps(payload_object, ensure_ascii=False)

    payload_bytes = payload.encode()
    try:
        headers = _test_headers(
            bound_route,
            secret,
            payload_bytes,
            payload_object,
        )
    except (ValueError, TypeError) as exc:
        print(f"  Error: Cannot build provider test request: {exc}")
        return

    print(
        f"  Sending {bound_route.provider}/{bound_route.signature_mode} "
        f"test POST to {url}"
    )
    try:
        import urllib.request

        req = urllib.request.Request(
            url,
            data=payload_bytes,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            print(f"  Response ({resp.status}): {body}")
    except Exception as e:
        print(f"  Error: {e}")
        print("  Is the gateway running? (hermes gateway run)")
