"""Live-gateway delivery edge for verified Muncho release announcements.

The production gateway already owns the credential-free, idempotent relay to
the privileged Discord connector.  Release coordination writes a strict
request into its private state directory; this module dispatches those exact
bytes only through that live relay and records the connector's exact message
ID.  It never reads a bot token and never falls back to direct Discord REST.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from gateway.config import Platform
from gateway.delivery import resolve_delivery_transport

from .completion import (
    pending_gateway_discord_deliveries,
    record_gateway_discord_delivery,
    resolve_discord_destination,
)
from .metadata import require_exact_release_sha


_SNOWFLAKE = re.compile(r"^[1-9][0-9]{0,24}$")
_SYSTEMD_INVOCATION_ID = re.compile(r"^[0-9a-f]{32}$")


def _result_field(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, Mapping):
        return result.get(name, default)
    return getattr(result, name, default)


async def dispatch_pending_gateway_discord_deliveries(
    *,
    state_dir: Path,
    gateway_config: Any,
    adapters: Mapping[Any, Any],
    production_config: Mapping[str, Any],
    deployed_release_sha: str,
    active_service_invocation_id: str | None,
    published_at: datetime | None = None,
) -> tuple[dict[str, Any], ...]:
    """Dispatch pending summaries through the authenticated relay connector.

    A direct Discord adapter is deliberately rejected: Muncho production pins
    Discord token ownership to the privileged connector.  Replays carry the
    same connector idempotency key, so a gateway crash after Discord accepted
    the message can reconcile without a second mutation.
    """

    deployed_release_sha = require_exact_release_sha(deployed_release_sha)
    expected_destination = resolve_discord_destination(production_config)
    requests = pending_gateway_discord_deliveries(state_dir)
    if not requests:
        return ()
    active_service_invocation_id = str(active_service_invocation_id or "")
    transport = resolve_delivery_transport(
        Platform.DISCORD,
        gateway_config,
        dict(adapters),
    )
    outcomes: list[dict[str, Any]] = []
    for request in requests:
        identity = {
            "muncho_version": request["muncho_version"],
            "release_sha": request["release_sha"],
            "summary_sha256": request["summary_sha256"],
        }
        if request["release_sha"] != deployed_release_sha:
            outcomes.append({**identity, "state": "blocked_identity_mismatch"})
            continue
        if (
            _SYSTEMD_INVOCATION_ID.fullmatch(active_service_invocation_id) is None
            or request["after_invocation_id"] != active_service_invocation_id
        ):
            outcomes.append(
                {**identity, "state": "blocked_restart_identity_mismatch"}
            )
            continue
        if any(
            request[name] != expected_destination[name]
            for name in ("guild_id", "channel_id", "target_type")
        ):
            outcomes.append({**identity, "state": "blocked_destination_mismatch"})
            continue
        if transport is None or not transport.is_relay:
            outcomes.append({**identity, "state": "blocked_live_relay_unavailable"})
            continue
        connector_key = f"muncho-release:{request['release_idempotency_key']}"
        try:
            result = await transport.send(
                Platform.DISCORD,
                request["channel_id"],
                request["summary"],
                metadata={
                    "scope_id": request["guild_id"],
                    "connector_idempotency_key": connector_key,
                    "non_conversational": True,
                },
            )
        except Exception:
            outcomes.append(
                {
                    **identity,
                    "state": "dispatch_uncertain",
                    "connector_idempotency_key": connector_key,
                }
            )
            continue
        success = _result_field(result, "success") is True
        message_id = str(_result_field(result, "message_id", "") or "")
        if success and _SNOWFLAKE.fullmatch(message_id) is not None:
            receipt = record_gateway_discord_delivery(
                state_dir,
                request,
                message_id=message_id,
                published_at=published_at,
            )
            outcomes.append(
                {
                    **identity,
                    "state": "delivered",
                    "message_id": message_id,
                    "delivery_receipt_sha256": receipt["receipt_sha256"],
                    "connector_idempotency_key": connector_key,
                }
            )
            continue
        error_kind = str(_result_field(result, "error_kind", "") or "")
        outcomes.append(
            {
                **identity,
                "state": (
                    "dispatch_uncertain"
                    if error_kind == "dispatch_uncertain"
                    else "delivery_failed"
                ),
                "connector_idempotency_key": connector_key,
            }
        )
    return tuple(outcomes)


__all__ = ["dispatch_pending_gateway_discord_deliveries"]
