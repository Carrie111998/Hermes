"""Cluster presence, handoff watcher helpers, and agent-facing API."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from hermes_storage.factory import get_storage

logger = logging.getLogger(__name__)

_HEARTBEAT_THREAD: Optional[threading.Thread] = None
_HEARTBEAT_STOP = threading.Event()


def format_cluster_prompt_block(status: Optional[dict[str, Any]] = None) -> str:
    """Compact system-prompt block describing the agent fleet."""
    storage = get_storage()
    if storage is None:
        return ""
    status = status or storage.cluster_status()
    state = status.get("state") or {}
    nodes = status.get("nodes") or []
    lines = [
        "CLUSTER (Hermes multi-PC fleet)",
        f"- This node: {status.get('this_node_id')} (machine {status.get('this_machine_id')})",
        f"- Active node: {state.get('active_node_id') or 'none'}",
        f"- Messaging owner: {state.get('messaging_owner') or 'none'}",
        f"- Handoff: {state.get('handoff_state') or 'idle'}",
        "- Online instances:",
    ]
    online = [n for n in nodes if n.get("online")]
    if not online:
        lines.append("  (none)")
    else:
        for n in online:
            caps = ",".join(n.get("capabilities") or []) or "-"
            marker = " *" if n.get("node_id") == state.get("active_node_id") else ""
            lines.append(
                f"  - {n.get('hostname') or n.get('machine_id')} "
                f"[{n.get('node_id')}] caps={caps}{marker}"
            )
    lines.append(
        "Use cluster_status / cluster_activate tools (or /cluster) to inspect "
        "or switch the active agent. Messaging gateway moves with a lease "
        "handoff so only one Telegram/Discord gateway is live."
    )
    return "\n".join(lines)


def start_heartbeat_loop(
    *,
    interval_s: float = 15.0,
    api_base: Optional[str] = None,
) -> None:
    """Background presence heartbeat for the current process."""
    global _HEARTBEAT_THREAD
    storage = get_storage()
    if storage is None:
        return
    _HEARTBEAT_STOP.clear()

    def _loop() -> None:
        while not _HEARTBEAT_STOP.is_set():
            try:
                storage.register_presence(api_base=api_base)
                # Also publish presence over mTLS orchestrator when configured
                try:
                    from hermes_storage.orchestrator_client import (
                        orch_heartbeat,
                        orchestrator_configured,
                    )
                    if orchestrator_configured():
                        orch_heartbeat({
                            "node_id": storage.node_id,
                            "machine_id": storage.machine_id,
                            "profile": storage.bootstrap.profile,
                            "api_base": api_base or "",
                            "status": "online",
                        })
                except Exception as orch_exc:
                    logger.debug("Orchestrator heartbeat skipped: %s", orch_exc)
                _maybe_handle_handoff(storage)
                _maybe_failover(storage)
            except Exception as exc:
                logger.warning("Cluster heartbeat failed: %s", exc)
            _HEARTBEAT_STOP.wait(interval_s)

    if _HEARTBEAT_THREAD and _HEARTBEAT_THREAD.is_alive():
        return
    _HEARTBEAT_THREAD = threading.Thread(target=_loop, name="hermes-cluster-hb", daemon=True)
    _HEARTBEAT_THREAD.start()


def stop_heartbeat_loop() -> None:
    _HEARTBEAT_STOP.set()


def _maybe_handle_handoff(storage: Any) -> None:
    """If we own messaging and handoff says releasing — stop adapters (hook).

    Actual gateway adapter stop/start is wired from gateway/run.py via
    ``on_release`` / ``on_acquire`` callbacks registered below.
    """
    state = storage.cluster.get_state()
    handoff = state.get("handoff_state")
    node_id = storage.node_id

    if handoff == "releasing" and state.get("handoff_from") == node_id:
        cb = _RELEASE_CB
        if cb:
            try:
                cb()
            except Exception as exc:
                logger.error("Messaging release callback failed: %s", exc)
                storage.cluster.rollback_messaging_handoff(reason=str(exc))
                return
        storage.cluster.mark_messaging_released(node_id)

    if handoff == "acquiring" and state.get("handoff_to") == node_id:
        cb = _ACQUIRE_CB
        ok = True
        err = None
        if cb:
            try:
                ok = bool(cb())
            except Exception as exc:
                ok = False
                err = str(exc)
        if ok:
            storage.cluster.complete_messaging_handoff(node_id)
            notify = _NOTIFY_CB
            if notify:
                try:
                    notify(
                        f"Active Hermes agent switched to this machine "
                        f"({storage.machine_id}). Messaging gateway is here now."
                    )
                except Exception:
                    pass
        else:
            storage.cluster.rollback_messaging_handoff(
                reason=err or "messaging health-check failed"
            )
            notify = _NOTIFY_CB
            if notify:
                try:
                    notify(
                        f"Failed to move messaging gateway here "
                        f"({err or 'health-check failed'}). Rolled back."
                    )
                except Exception:
                    pass


def _maybe_failover(storage: Any) -> None:
    state = storage.cluster.get_state()
    if (state.get("failover") or "auto") != "auto":
        return
    owner = state.get("messaging_owner")
    if not owner:
        return
    nodes = {n["node_id"]: n for n in storage.cluster.list_nodes(online_within_s=45.0)}
    owner_node = nodes.get(owner)
    if owner_node and owner_node.get("online"):
        return
    # Owner unhealthy — try next online node (prefer this node if online)
    candidates = [n for n in nodes.values() if n.get("online") and n["node_id"] != owner]
    if not candidates:
        return
    # Prefer self
    self_node = nodes.get(storage.node_id)
    target = self_node if self_node and self_node.get("online") else candidates[0]
    if target["node_id"] == storage.node_id:
        logger.warning(
            "Messaging owner %s unhealthy; initiating failover to %s",
            owner,
            target["node_id"],
        )
        storage.cluster.set_active(target["node_id"], reason="failover")


_RELEASE_CB: Optional[Callable[[], None]] = None
_ACQUIRE_CB: Optional[Callable[[], bool]] = None
_NOTIFY_CB: Optional[Callable[[str], None]] = None


def register_messaging_callbacks(
    *,
    on_release: Optional[Callable[[], None]] = None,
    on_acquire: Optional[Callable[[], bool]] = None,
    on_notify: Optional[Callable[[str], None]] = None,
) -> None:
    """Gateway registers stop/start/notify hooks for messaging handoff."""
    global _RELEASE_CB, _ACQUIRE_CB, _NOTIFY_CB
    if on_release is not None:
        _RELEASE_CB = on_release
    if on_acquire is not None:
        _ACQUIRE_CB = on_acquire
    if on_notify is not None:
        _NOTIFY_CB = on_notify
