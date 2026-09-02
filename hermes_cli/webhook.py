"""hermes webhook — manage dynamic webhook subscriptions from the CLI.

Usage:
    hermes webhook subscribe <name> [options]
    hermes webhook list
    hermes webhook remove <name>
    hermes webhook test <name> [--payload '{"key": "value"}']

Subscriptions persist to ~/.hermes/webhook_subscriptions.json and are
hot-reloaded by the webhook adapter without a gateway restart.
"""

import json
import os
import re
import secrets
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator

from hermes_constants import display_hermes_home
from utils import atomic_replace
from hermes_cli.config import cfg_get


_SUBSCRIPTIONS_FILENAME = "webhook_subscriptions.json"
_SUBSCRIPTIONS_FILE_MODE = 0o600


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
    for name, route in subs.items():
        if not isinstance(route, dict):
            raise ValueError(f"Webhook route {name!r} must be an object")
        if any(key in route for key in ("secret", "secret_value")):
            raise ValueError(
                f"Refusing to persist plaintext webhook secret for route {name!r}; "
                "run 'hermes webhook migrate-secrets' first"
            )
        from hermes_cli.webhook_secrets import validate_webhook_secret_ref

        try:
            validate_webhook_secret_ref(route.get("secret_ref"))
        except ValueError:
            raise ValueError(
                f"Webhook route {name!r} must contain a canonical secret_ref"
            ) from None
    path = _subscriptions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The route store is reference-only, but still contains operational
    # metadata. Write via tempfile + chmod 0o600 before the atomic rename so
    # a permissive umask cannot expose it between create and rename.
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


@contextmanager
def _subscription_write_transaction() -> Iterator[None]:
    """Migrate legacy state, then serialize the complete route-store update."""
    path = _subscriptions_path()
    if path.exists():
        from hermes_cli.migrations.webhook_secret_refs import migrate_webhook_routes

        backups = tuple(path.parent.glob(path.name + ".bak*"))
        migrate_webhook_routes(path, backup_paths=backups)

    from hermes_cli.webhook_secrets import webhook_secret_write_lock

    with webhook_secret_write_lock():
        yield


def _store_route_secret_unlocked(name: str, value: str) -> str:
    """Persist one new credential version while the route writer lock is held."""
    from hermes_cli.webhook_secrets import (
        store_webhook_secret_unlocked,
        webhook_route_secret_ref,
    )

    ref = webhook_route_secret_ref(name, versioned=True)
    store_webhook_secret_unlocked(ref, value)
    return ref


def _store_route_secret(name: str, value: str) -> str:
    """Persist one route secret and return its opaque reference."""
    from hermes_cli.webhook_secrets import webhook_secret_write_lock

    with webhook_secret_write_lock():
        return _store_route_secret_unlocked(name, value)


def _resolve_route_secret(route: dict) -> str:
    """Resolve a reference; plaintext is accepted only for migration fallback."""
    ref = route.get("secret_ref")
    if ref not in (None, ""):
        from hermes_cli.webhook_secrets import resolve_webhook_secret

        return resolve_webhook_secret(ref)
    value = route.get("secret")
    return value if isinstance(value, str) else ""


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
           secret_ref: WEBHOOK_SECRET

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


def webhook_command(args):
    """Entry point for 'hermes webhook' subcommand."""
    sub = getattr(args, "webhook_action", None)

    if not sub:
        print("Usage: hermes webhook {subscribe|list|remove|test|migrate-secrets}")
        print("Run 'hermes webhook --help' for details.")
        return

    if sub == "migrate-secrets":
        _cmd_migrate_secrets(args)
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
    if not re.match(r'^[a-z0-9][a-z0-9_-]*$', name):
        print(f"Error: Invalid name '{name}'. Use lowercase alphanumeric with hyphens/underscores.")
        return

    supplied_secret = bool(args.secret)
    events = [e.strip() for e in args.events.split(",")] if args.events else []

    if getattr(args, "deliver_only", False) and (args.deliver or "log") == "log":
        print(
            "Error: --deliver-only requires --deliver to be a real target "
            "(telegram, discord, slack, github_comment, etc.) — not 'log'."
        )
        return

    with _subscription_write_transaction():
        subs = _load_subscriptions()
        is_update = name in subs
        existing_route = subs.get(name) if is_update else None
        existing_ref = (
            existing_route.get("secret_ref")
            if isinstance(existing_route, dict)
            else None
        )
        disclose_secret = not is_update or supplied_secret or not existing_ref
        secret = args.secret or (
            secrets.token_urlsafe(32) if disclose_secret else ""
        )
        secret_ref = (
            _store_route_secret_unlocked(name, secret)
            if secret
            else str(existing_ref)
        )

        route = {
            "description": args.description or f"Agent-created subscription: {name}",
            "events": events,
            "secret_ref": secret_ref,
            "prompt": args.prompt or "",
            "skills": [s.strip() for s in args.skills.split(",")] if args.skills else [],
            "deliver": args.deliver or "log",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if getattr(args, "deliver_only", False):
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

    print(f"\n  {status} webhook subscription: {name}")
    print(f"  URL:    {base_url}/webhooks/{name}")
    if disclose_secret:
        print(f"  Secret: {secret}")
    else:
        print("  Secret: (unchanged; not displayed)")
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
    print("\n  Configure your service to POST to the URL above.")
    print("  Use the secret for HMAC-SHA256 signature validation.")
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
        print(f"    URL:     {base_url}/webhooks/{name}")
        print(f"    Events:  {events}")
        print(f"    Deliver: {deliver}")
        if route.get("script"):
            print(f"    Script:  {route['script']}")
        print()


def _cmd_remove(args):
    name = args.name.strip().lower()
    with _subscription_write_transaction():
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
    secret = _resolve_route_secret(route)
    if not secret:
        print("  Error: webhook secret reference could not be resolved")
        return
    base_url = _get_webhook_base_url()
    url = f"{base_url}/webhooks/{name}"

    payload = args.payload or '{"test": true, "event_type": "test", "message": "Hello from hermes webhook test"}'

    import hmac
    import hashlib
    sig = "sha256=" + hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()

    print(f"  Sending test POST to {url}")
    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            data=payload.encode(),
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "test",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            print(f"  Response ({resp.status}): {body}")
    except Exception as e:
        print(f"  Error: {e}")
        print("  Is the gateway running? (hermes gateway run)")


def _cmd_migrate_secrets(args):
    """Migrate every legacy webhook secret with value-free receipts."""
    from hermes_cli.migrations.webhook_secret_refs import (
        migrate_webhook_config,
        migrate_webhook_routes,
    )

    route_path = _subscriptions_path()
    route_result = {
        "migrated_routes": [],
        "receipts": [],
        "scrubbed_backups": [],
    }
    if route_path.exists():
        route_backups = tuple(route_path.parent.glob(route_path.name + ".bak*"))
        route_result = migrate_webhook_routes(
            route_path,
            backup_paths=route_backups,
        )

    config_path = _hermes_home() / "config.yaml"
    config_result = {
        "migrated": False,
        "receipts": [],
        "scrubbed_backups": [],
    }
    if config_path.exists():
        config_backups = tuple(config_path.parent.glob(config_path.name + ".bak*"))
        config_result = migrate_webhook_config(
            config_path,
            backup_paths=config_backups,
        )

    result = {"routes": route_result, "config": config_result}
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            "Webhook secret migration complete: "
            f"{len(route_result.get('migrated_routes', []))} route(s), "
            f"config={'migrated' if config_result.get('migrated') else 'unchanged'}."
        )
    return result
