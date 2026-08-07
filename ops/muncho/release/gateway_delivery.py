"""Live-gateway delivery edge for verified Muncho release announcements.

Release coordination writes a strict request into the gateway's private state
directory.  This module dispatches those exact bytes through either the
privileged Relay connector or the already-connected native Discord adapter.
Both paths are explicitly idempotent and read back an exact bot-authored guild
receipt.
This module never reads a bot token and never falls back to standalone Discord
REST.
"""

from __future__ import annotations

import hashlib
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
_DISCORD_NONCE_MAX = (1 << 64) - 1


def _native_discord_nonce(release_idempotency_key: str) -> int:
    """Return a stable 64-bit Discord nonce for one immutable release request."""

    digest = hashlib.sha256(
        f"muncho-release:{release_idempotency_key}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big")


def _supports_verified_native_discord_delivery(adapter: Any) -> bool:
    """Require the live adapter's explicit idempotent guild-receipt contract."""

    return (
        getattr(adapter, "supports_verified_idempotent_guild_delivery", False)
        is True
        and callable(getattr(adapter, "find_guild_message_ids_by_exact_nonce", None))
        and callable(getattr(adapter, "verify_guild_message_receipt", None))
    )


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
    """Dispatch pending summaries through one authenticated live transport.

    Relay replays carry the same connector idempotency key.  Native Discord
    replays carry a deterministic 64-bit nonce which discord.py sends with
    ``enforce_nonce``.  The native adapter is admitted only when it explicitly
    implements the verified, idempotent guild-delivery contract; standalone
    senders and token access are never consulted.
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
        if transport is None:
            outcomes.append({**identity, "state": "blocked_live_transport_unavailable"})
            continue
        connector_key = f"muncho-release:{request['release_idempotency_key']}"
        is_native = not transport.is_relay
        native_nonce: int | None = None
        if is_native:
            if not _supports_verified_native_discord_delivery(transport.adapter):
                outcomes.append(
                    {**identity, "state": "blocked_live_transport_unsupported"}
                )
                continue
            native_nonce = _native_discord_nonce(request["release_idempotency_key"])
            if not 0 <= native_nonce <= _DISCORD_NONCE_MAX:  # pragma: no cover
                outcomes.append({**identity, "state": "blocked_native_nonce_invalid"})
                continue
            delivery_metadata = {
                "scope_id": request["guild_id"],
                "discord_enforced_nonce": native_nonce,
                "non_conversational": True,
                "require_exact_content": True,
                "require_single_public_receipt": True,
            }
        else:
            delivery_metadata = {
                "scope_id": request["guild_id"],
                "connector_idempotency_key": connector_key,
                "non_conversational": True,
            }
        try:
            if is_native:
                queued_at = datetime.fromisoformat(
                    str(request["queued_at_utc"]).replace("Z", "+00:00")
                )
                existing_ids = (
                    await transport.adapter.find_guild_message_ids_by_exact_nonce(
                        expected_guild_id=request["guild_id"],
                        channel_id=request["channel_id"],
                        nonce=native_nonce,
                        expected_content_sha256=request["summary_sha256"],
                        after_utc=queued_at,
                    )
                )
                if len(existing_ids) > 1:
                    raise RuntimeError("native Discord nonce receipt is ambiguous")
                if existing_ids:
                    result = {
                        "success": True,
                        "message_id": existing_ids[0],
                    }
                else:
                    result = await transport.send(
                        Platform.DISCORD,
                        request["channel_id"],
                        request["summary"],
                        metadata=delivery_metadata,
                    )
            else:
                result = await transport.send(
                    Platform.DISCORD,
                    request["channel_id"],
                    request["summary"],
                    metadata=delivery_metadata,
                )
        except Exception:
            uncertain = {**identity, "state": "dispatch_uncertain"}
            if is_native:
                uncertain["discord_enforced_nonce"] = native_nonce
            else:
                uncertain["connector_idempotency_key"] = connector_key
            outcomes.append(uncertain)
            continue
        success = _result_field(result, "success") is True
        message_id = str(_result_field(result, "message_id", "") or "")
        if success and _SNOWFLAKE.fullmatch(message_id) is not None:
            if is_native:
                try:
                    guild_receipt = (
                        await transport.adapter.verify_guild_message_receipt(
                            expected_guild_id=request["guild_id"],
                            channel_id=request["channel_id"],
                            message_id=message_id,
                            expected_content_sha256=request["summary_sha256"],
                        )
                    )
                    if (
                        guild_receipt.get("verified") is not True
                        or guild_receipt.get("platform") != "discord"
                        or str(guild_receipt.get("guild_id", ""))
                        != request["guild_id"]
                        or str(guild_receipt.get("channel_id", ""))
                        != request["channel_id"]
                        or str(guild_receipt.get("message_id", "")) != message_id
                        or guild_receipt.get("content_sha256")
                        != request["summary_sha256"]
                    ):
                        raise RuntimeError("native Discord receipt identity mismatch")
                except Exception:
                    outcomes.append(
                        {
                            **identity,
                            "state": "dispatch_uncertain",
                            "discord_enforced_nonce": native_nonce,
                        }
                    )
                    continue
            receipt = record_gateway_discord_delivery(
                state_dir,
                request,
                message_id=message_id,
                published_at=published_at,
            )
            delivered = {
                **identity,
                "state": "delivered",
                "message_id": message_id,
                "delivery_receipt_sha256": receipt["receipt_sha256"],
            }
            if is_native:
                delivered["discord_enforced_nonce"] = native_nonce
            else:
                delivered["connector_idempotency_key"] = connector_key
            outcomes.append(delivered)
            continue
        error_kind = str(_result_field(result, "error_kind", "") or "")
        failed = {
            **identity,
            "state": (
                "dispatch_uncertain"
                if error_kind == "dispatch_uncertain"
                else "delivery_failed"
            ),
        }
        if is_native:
            failed["discord_enforced_nonce"] = native_nonce
        else:
            failed["connector_idempotency_key"] = connector_key
        outcomes.append(failed)
    return tuple(outcomes)


__all__ = ["dispatch_pending_gateway_discord_deliveries"]
