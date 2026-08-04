"""KMDaily on-demand trigger plugin for Telegram and other Hermes gateway sessions.

The ``/kmdaily [ingest|digest|notion]`` command calls KMDaily's existing
``POST /api/v1/runs`` control-plane endpoint (T0051), which is independent
of and does not replace ``kmdaily.timer``'s own hourly schedule. When
``kmdaily.key_file`` is configured, the file's trimmed contents are sent as
an ``Authorization: Bearer <key>`` request header, mirroring the ``klib``
plugin's ``key_file`` convention.

The command is intentionally fail-closed and never raises to its caller:
missing or disabled configuration, authentication-file failures, HTTP
errors, and malformed responses all become user-facing messages instead.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 10.0

_NOT_CONFIGURED_REPLY = (
    "kmdaily: not configured or disabled. An admin must set "
    "kmdaily.enabled: true and kmdaily.base_url in config.yaml first."
)

_USAGE_REPLY = (
    "Usage: /kmdaily [ingest|digest|notion]\n"
    "Trigger an on-demand KMDaily run. Defaults to ingest when no argument "
    "is given. This does not change kmdaily.timer's own hourly schedule."
)

# Maps the command's user-facing argument to KMDaily's POST /api/v1/runs
# "action" value. "run_cycle" is intentionally not reachable from here --
# per T0051-spec Guard rail 6, on-demand triggers must pick a specific
# action rather than defaulting to the combined cycle for convenience.
_ACTION_MAP = {
    "": "ingest",
    "ingest": "ingest",
    "digest": "send_digest",
    "notion": "sync_notion",
}


def _load_kmdaily_config() -> dict[str, Any]:
    """Load the top-level kmdaily config block, failing closed on any error."""
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        block = config.get("kmdaily")
        return block if isinstance(block, dict) else {}
    except Exception:
        logger.warning("kmdaily: failed to load config; treating as disabled")
        return {}


def _idempotency_key(action: str) -> str:
    """Build a fresh <source>-<action>-<timestamp> key for one trigger intent.

    This must be called exactly once per user-initiated trigger and reused
    for any retry of that same intent -- callers must not regenerate a new
    key on retry, or KMDaily's idempotency guarantee is defeated.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "Z"
    return f"hermes-telegram-kmdaily-{action}-{timestamp}-{uuid.uuid4().hex[:8]}"


async def _trigger_kmdaily_run(action: str, cfg: dict[str, Any]) -> str:
    """POST one on-demand run and convert every expected failure to text."""
    base_url = cfg.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        return _NOT_CONFIGURED_REPLY
    base_url = base_url.strip()

    headers: dict[str, str] = {}
    key_file = cfg.get("key_file")
    if key_file:
        try:
            api_key = Path(str(key_file)).read_text(encoding="utf-8").strip()
            headers["Authorization"] = f"Bearer {api_key}"
        except Exception:
            return _static_reply(
                "kmdaily: could not read the configured API key file."
            )

    idempotency_key = _idempotency_key(action)
    headers["Idempotency-Key"] = idempotency_key

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/api/v1/runs",
                json={"action": action, "dry_run": False},
                headers=headers,
            )
    except httpx.TimeoutException:
        return _static_reply("kmdaily: trigger request timed out.")
    except httpx.RequestError:
        return _static_reply("kmdaily: could not reach the KMDaily service.")

    if response.status_code == 401:
        return _static_reply("kmdaily: unauthorized (check the configured API key).")
    if response.status_code == 422:
        return _static_reply("kmdaily: request rejected as invalid by the service.")
    if response.status_code == 409:
        run_id = _extract_run_id(response)
        return _static_reply(
            f"kmdaily: a run for this action is already in progress"
            + (f" (run_id={run_id})" if run_id else "") + "."
        )
    if not 200 <= response.status_code < 300:
        return _static_reply(
            f"kmdaily: service returned HTTP status {response.status_code}."
        )

    try:
        payload = response.json()
    except Exception:
        return _static_reply("kmdaily: received an invalid JSON response.")

    if not isinstance(payload, dict) or not payload.get("run_id"):
        return _static_reply("kmdaily: received an invalid response format.")

    run_id = payload["run_id"]
    status = payload.get("status", "unknown")

    # Single optional follow-up poll (not a wait-loop) -- if the run has
    # already resolved by the time this returns, report the final status
    # instead of the initial "running" snapshot. Any failure here still
    # falls back to reporting the original POST response.
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            poll = await client.get(
                f"{base_url.rstrip('/')}/api/v1/runs/{run_id}",
                headers=headers,
            )
        if poll.status_code == 200:
            poll_payload = poll.json()
            if isinstance(poll_payload, dict) and poll_payload.get("status"):
                status = poll_payload["status"]
    except Exception:
        pass

    return _static_reply(
        f"kmdaily: triggered action={action}, run_id={run_id}, status={status}."
    )


def _extract_run_id(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except Exception:
        return None
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict) and detail.get("run_id"):
            return str(detail["run_id"])
        if payload.get("run_id"):
            return str(payload["run_id"])
    return None


def _static_reply(value: str) -> str:
    """Return raw plugin text for the downstream platform formatter."""
    return value


async def _handle_kmdaily(
    raw_args: str,
    chat_id: int | str | None = None,
) -> str:
    """Handle ``/kmdaily`` and never propagate an exception to the gateway."""
    try:
        arg = raw_args.strip().lower()
        if arg not in _ACTION_MAP:
            return _USAGE_REPLY
        action = _ACTION_MAP[arg]

        cfg = _load_kmdaily_config()
        if not cfg.get("enabled") or not cfg.get("base_url"):
            return _NOT_CONFIGURED_REPLY
        return await _trigger_kmdaily_run(action, cfg)
    except Exception:
        logger.exception("kmdaily: unexpected command failure")
        return _static_reply(
            "kmdaily: an unexpected error occurred while processing the command."
        )


def register(ctx) -> None:
    ctx.register_command(
        "kmdaily",
        handler=_handle_kmdaily,
        description="Trigger an on-demand KMDaily run (does not change the hourly schedule).",
        args_hint="[ingest|digest|notion]",
    )
