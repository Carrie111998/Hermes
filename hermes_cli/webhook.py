"""hermes webhook — manage dynamic webhook subscriptions from the CLI.

Usage:
    hermes webhook subscribe <name> [options]
    hermes webhook list
    hermes webhook remove <name>
    hermes webhook test <name> [--payload '{"key": "value"}']

Subscriptions persist to ~/.hermes/webhook_subscriptions.json and are
hot-reloaded by the webhook adapter without a gateway restart.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping
from urllib.parse import urlsplit

from hermes_constants import display_hermes_home
from utils import atomic_replace
from hermes_cli.config import cfg_get


_SUBSCRIPTIONS_FILENAME = "webhook_subscriptions.json"
_SUBSCRIPTIONS_FILE_MODE = 0o600
_LEGACY_CLI_DESCRIPTION_PREFIX = "Agent-created subscription:"


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def _subscriptions_path() -> Path:
    return _hermes_home() / _SUBSCRIPTIONS_FILENAME


def _load_subscriptions() -> Dict[str, dict]:
    path = _subscriptions_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_subscriptions(subs: Dict[str, dict]) -> None:
    path = _subscriptions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # webhook_subscriptions.json contains per-route HMAC secrets — write
    # via tempfile + chmod 0o600 before the atomic rename so a permissive
    # umask cannot leave the secrets readable to other local users in the
    # window between create and rename.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(subs, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, _SUBSCRIPTIONS_FILE_MODE)
        atomic_replace(tmp_path, path)
        # Re-assert after rename in case the destination existed with a
        # broader mode and atomic_replace preserved it.
        os.chmod(path, _SUBSCRIPTIONS_FILE_MODE)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
        print("Usage: hermes webhook {subscribe|list|remove|test}")
        print("Run 'hermes webhook --help' for details.")
        return

    if not _require_webhook_enabled():
        return

    if sub in {"subscribe", "add"}:
        _cmd_subscribe(args)
    elif sub in {"list", "ls"}:
        _cmd_list(args)
    elif sub in {"remove", "rm"}:
        _cmd_remove(args)
    elif sub == "test":
        _cmd_test(args)


def _cmd_subscribe(args):
    name = args.name.strip().lower().replace(" ", "-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", name):
        print(
            f"Error: Invalid name '{name}'. Use at most 128 lowercase "
            "alphanumeric, hyphen, or underscore characters."
        )
        return

    try:
        profile = _normalize_subscription_profile(
            getattr(args, "route_profile", "default")
        )
    except ValueError as exc:
        print(f"Error: Invalid webhook profile: {exc}")
        return

    subs = _load_subscriptions()
    is_update = name in subs

    secret = args.secret or secrets.token_urlsafe(32)
    events = (
        [event for item in args.events.split(",") if (event := item.strip())]
        if args.events
        else []
    )

    route = {
        "description": args.description or f"Agent-created subscription: {name}",
        "profile": profile,
        "provider": getattr(args, "provider", "github") or "github",
        "events": events,
        "secret": secret,
        "prompt": args.prompt or "",
        "skills": [s.strip() for s in args.skills.split(",")] if args.skills else [],
        "deliver": args.deliver or "log",
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

    if args.deliver_chat_id:
        route["deliver_extra"] = {"chat_id": args.deliver_chat_id}

    subs[name] = route
    _save_subscriptions(subs)

    base_url = _get_webhook_base_url()
    status = "Updated" if is_update else "Created"
    subscription_url = _subscription_url(base_url, name, bound_route.profile)
    local_test_url = _is_loopback_webhook_url(subscription_url)

    print(f"\n  {status} webhook subscription: {name}")
    url_label = "Local test URL" if local_test_url else "Callback URL"
    print(f"  {url_label}: {subscription_url}")
    print(f"  Secret: {secret}")
    print(f"  Provider: {bound_route.provider} ({bound_route.signature_mode})")
    if events:
        print(f"  Events: {', '.join(events)}")
    else:
        print("  Events: (all)")
    print(f"  Deliver: {route['deliver']}")
    if route.get("deliver_only"):
        print("  Mode: direct delivery (no agent, zero LLM cost)")
    if route.get("prompt"):
        prompt_preview = route["prompt"][:80] + ("..." if len(route["prompt"]) > 80 else "")
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


def _cmd_list(args):
    subs = _load_subscriptions()
    if not subs:
        print("  No dynamic webhook subscriptions.")
        print("  Create one with: hermes webhook subscribe <name>")
        return

    base_url = _get_webhook_base_url()
    print(f"\n  {len(subs)} webhook subscription(s):\n")
    for name, route in subs.items():
        events = ", ".join(route.get("events", [])) or "(all)"
        deliver = route.get("deliver", "log")
        if route.get("deliver_only"):
            deliver = f"{deliver} (direct — no agent)"
        desc = route.get("description", "")
        print(f"  ◆ {name}")
        if desc:
            print(f"    {desc}")
        try:
            route_url = _subscription_url(
                base_url,
                name,
                route.get("profile", "default"),
            )
        except ValueError:
            route_url = "(invalid profile binding)"
        print(f"    URL:     {route_url}")
        provider = route.get("provider") or "legacy github"
        signature_mode = route.get("signature_mode")
        if signature_mode:
            provider = f"{provider} ({signature_mode})"
        print(f"    Provider: {provider}")
        print(f"    Events:  {events}")
        print(f"    Deliver: {deliver}")
        if route.get("script"):
            print(f"    Script:  {route['script']}")
        print()


def _cmd_remove(args):
    name = args.name.strip().lower()
    subs = _load_subscriptions()

    if name not in subs:
        print(f"  No subscription named '{name}'.")
        print("  Note: Static routes from config.yaml cannot be removed here.")
        return

    del subs[name]
    _save_subscriptions(subs)
    print(f"  Removed webhook subscription: {name}")


def _cmd_test(args):
    """Send a test POST to a webhook route."""
    name = args.name.strip().lower()
    subs = _load_subscriptions()

    if name not in subs:
        print(f"  No subscription named '{name}'.")
        return

    route = subs[name]
    try:
        bound_route = _bind_subscription(name, route)
    except (TypeError, ValueError) as exc:
        print(f"  Error: Invalid webhook provider contract: {exc}")
        return
    secret = route.get("secret", "")
    if not isinstance(secret, str) or not secret:
        print("  Error: Subscription has no usable webhook secret.")
        return
    base_url = _get_webhook_base_url()
    url = _subscription_url(base_url, name, bound_route.profile)

    if args.payload:
        payload = args.payload
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
