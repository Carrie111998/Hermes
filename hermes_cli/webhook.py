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
from pathlib import Path
from typing import Dict

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


def webhook_command(args):
    """Entry point for 'hermes webhook' subcommand."""
    sub = getattr(args, "webhook_action", None)

    if not sub:
        print(
            "Usage: hermes webhook {subscribe|list|remove|test"
            "|orca-register|orca-runs|orca-sweep|orca-notify}"
        )
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
    elif sub == "orca-register":
        _cmd_orca_register(args)
    elif sub == "orca-runs":
        _cmd_orca_runs(args)
    elif sub == "orca-sweep":
        _cmd_orca_sweep(args)
    elif sub == "orca-notify":
        _cmd_orca_notify(args)


# ---------------------------------------------------------------------------
# Orca completion bridge
# ---------------------------------------------------------------------------

_ORCA_DYNAMIC_SEGMENT = "orca"


def _build_destination_path(path: str, event_type: str) -> str:
    """Normalise an Orca notification path back to the bridge's base route.

    Orca's notifier builds a DYNAMIC route by appending ``/orca/<event>`` to
    whatever target URL it was handed, so one configured endpoint fans out
    into a path per event type. Hermes' bridge is deliberately a SINGLE route:
    the event kind is read out of the signed JSON body, because the request
    path is not covered by the HMAC and therefore must never select
    behaviour.

    So both dynamic segments are stripped and the POST goes to the base route.
    Dropping only the event name leaves ``/orca`` — a route the adapter does
    not serve, which 404s at runtime while still passing any check that merely
    asserts "the event name is gone".
    """
    parts = [p for p in path.split("/") if p]
    if (
        event_type
        and len(parts) >= 2
        and parts[-1] == event_type
        and parts[-2] == _ORCA_DYNAMIC_SEGMENT
    ):
        parts = parts[:-2]
    return "/" + "/".join(parts)


def _cmd_orca_register(args):
    """Record an Orca run so its completion can be routed back here.

    Registration is the ONLY point at which the originating conversation is
    still known, so it is also the only point at which the follow-up can be
    made routable. Pass ``--session-key`` when calling from outside a session;
    inside one, the live ``HERMES_SESSION_KEY`` is used and the completion
    lands in the same thread that asked for the work.
    """
    from tools import orca_bridge

    run_id = (getattr(args, "run_id", "") or "").strip()
    if not orca_bridge.is_valid_run_id(run_id):
        print(f"Error: Invalid Orca run id '{run_id}'.")
        return

    terminal = (getattr(args, "terminal", "") or "").strip()
    if terminal and not orca_bridge.is_valid_terminal_id(terminal):
        print(f"Error: Invalid Orca terminal handle '{terminal}'.")
        return

    run = orca_bridge.register_run(
        run_id,
        goal=(getattr(args, "goal", "") or "").strip(),
        session_key=(getattr(args, "session_key", "") or "").strip() or None,
        worktree=(getattr(args, "worktree", "") or "").strip(),
        terminal=terminal,
    )
    target = run.get("session_key") or "(none — completion will not be routed)"
    print(f"Registered Orca run {run_id}")
    print(f"  goal:       {run.get('goal') or '(none)'}")
    print(f"  reports to: {target}")


def _cmd_orca_runs(args):
    from tools import orca_bridge

    state = (getattr(args, "state", "") or "").strip() or None
    runs = orca_bridge.list_runs(state)
    if not runs:
        print("No Orca runs registered.")
        return
    for run in runs:
        print(
            f"{run['run_id']}  [{run['state']}]  "
            f"{run.get('goal') or '(no goal)'}"
        )
        print(f"    reports to: {run.get('session_key') or '(unrouted)'}")


def _cmd_orca_sweep(args):
    """Force a reconcile of every open run against Orca's own ledger."""
    from tools import orca_bridge

    orca_bridge.start()
    try:
        published = orca_bridge.sweep()
    except Exception as exc:  # noqa: BLE001 — surface the reason, don't trace
        print(f"Error: could not reach Orca ({type(exc).__name__}).")
        return
    print(f"Reconciled Orca runs; {published} newly delivered.")


def _cmd_orca_notify(args):
    """Send a signed completion notification to the local bridge route.

    This is what an Orca hook calls. The signature is the replay-protected
    generic V2 scheme (HMAC-SHA256 over ``"<timestamp>.<body>"``) because the
    bridge refuses the body-only schemes.
    """
    import hashlib
    import hmac
    import urllib.parse
    import urllib.request

    route = (getattr(args, "route", "") or "orca").strip().lower()
    run_id = (getattr(args, "run_id", "") or "").strip()
    event = (getattr(args, "event", "") or "worker_done").strip()
    secret = (getattr(args, "secret", "") or "").strip()

    if not secret:
        wh_extra = _get_webhook_config().get("extra", {})
        route_cfg = (wh_extra.get("routes", {}) or {}).get(route, {}) or {}
        secret = route_cfg.get("secret", "") or wh_extra.get("secret", "") or ""
    if not secret:
        print(
            f"Error: no HMAC secret for route '{route}'. Pass --secret or set "
            f"platforms.webhook.extra.routes.{route}.secret."
        )
        return

    base_url = _get_webhook_base_url()
    parsed = urllib.parse.urlsplit(f"{base_url}/webhooks/{route}")
    path = _build_destination_path(parsed.path, event)
    url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, "", "")
    )

    body = json.dumps(
        {
            "run_id": run_id,
            "kind": event,
            "event_id": (getattr(args, "event_id", "") or "").strip() or None,
            "sequence": getattr(args, "sequence", -1),
        },
        separators=(",", ":"),
    ).encode()
    timestamp = str(int(time.time()))
    sig = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()

    print(f"  POST {url}  (event={event})")
    try:
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature-V2": sig,
                "X-Webhook-Timestamp": timestamp,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"  Response ({resp.status}): {resp.read().decode()}")
    except Exception as e:
        print(f"  Error: {e}")
        print("  Is the gateway running? (hermes gateway run)")


def _cmd_subscribe(args):
    name = args.name.strip().lower().replace(" ", "-")
    if not re.match(r'^[a-z0-9][a-z0-9_-]*$', name):
        print(f"Error: Invalid name '{name}'. Use lowercase alphanumeric with hyphens/underscores.")
        return

    subs = _load_subscriptions()
    is_update = name in subs

    secret = args.secret or secrets.token_urlsafe(32)
    events = [e.strip() for e in args.events.split(",")] if args.events else []

    route = {
        "description": args.description or f"Agent-created subscription: {name}",
        "events": events,
        "secret": secret,
        "prompt": args.prompt or "",
        "skills": [s.strip() for s in args.skills.split(",")] if args.skills else [],
        "deliver": args.deliver or "log",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

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

    print(f"\n  {status} webhook subscription: {name}")
    print(f"  URL:    {base_url}/webhooks/{name}")
    print(f"  Secret: {secret}")
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
    secret = route.get("secret", "")
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
