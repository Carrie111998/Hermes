"""Deduplicated operational alerts for the gateway Kanban dispatcher."""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("gateway.run")

_MAX_STATE_ENTRIES = 512


@dataclass(frozen=True)
class KanbanAlertSettings:
    """Resolved ``kanban.alerts`` configuration."""

    enabled: bool = False
    platform: str = "buzz"
    automation_channel: str = ""
    blockers_channel: str = ""
    profile: Optional[str] = None
    cooldown_seconds: float = 900.0
    retry_seconds: float = 60.0
    max_items_per_message: int = 10
    health_window_ticks: int = 6

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "KanbanAlertSettings":
        kanban = config.get("kanban")
        alerts = kanban.get("alerts") if isinstance(kanban, Mapping) else None
        if not isinstance(alerts, Mapping):
            return cls()

        def number(name: str, default: float, *, minimum: float = 0.0) -> float:
            try:
                return max(minimum, float(alerts.get(name, default)))
            except (TypeError, ValueError):
                return default

        try:
            max_items = int(alerts.get("max_items_per_message", 10))
        except (TypeError, ValueError):
            max_items = 10
        try:
            health_window = int(alerts.get("health_window_ticks", 6))
        except (TypeError, ValueError):
            health_window = 6
        profile = str(alerts.get("profile") or "").strip() or None
        return cls(
            enabled=alerts.get("enabled") is True,
            platform=str(alerts.get("platform") or "buzz").strip().lower(),
            automation_channel=str(alerts.get("automation_channel") or "").strip(),
            blockers_channel=str(alerts.get("blockers_channel") or "").strip(),
            profile=profile,
            cooldown_seconds=number("cooldown_seconds", 900.0),
            retry_seconds=number("retry_seconds", 60.0, minimum=1.0),
            max_items_per_message=max(1, min(max_items, 50)),
            health_window_ticks=max(1, min(health_window, 120)),
        )


@dataclass(frozen=True)
class KanbanAlertIncident:
    """One active condition and the text to send when it clears."""

    key: str
    route: str
    message: str
    recovery_message: str = ""
    allow_dm: bool = False
    board: str = ""
    task_id: str = ""


@dataclass(frozen=True)
class KanbanAlertIntake:
    """One normalized source event offered to the authoritative alert plane."""

    source: str
    root_cause: str
    state: str
    route: str
    message: str
    recovery_message: str = ""
    actionable: bool = False


_ALLOWED_INTAKE_SOURCES = frozenset({"kanban", "dispatcher", "gateway", "notifier"})


def intake_incident(event: KanbanAlertIntake) -> Optional[KanbanAlertIncident]:
    """Filter raw sources and normalize state into a root-cause incident key."""
    source = event.source.strip().lower()
    if source not in _ALLOWED_INTAKE_SOURCES:
        return None
    root = _one_line(event.root_cause, limit=180).lower()
    if not root:
        return None
    state = event.state.strip().lower()
    if state not in {"open", "recovered", "closed"}:
        return None
    recovery = (
        event.recovery_message or event.message
        if state != "open"
        else event.recovery_message
    )
    return KanbanAlertIncident(
        key=f"intake:{source}:{root}",
        route=event.route,
        message=event.message if state == "open" else "",
        recovery_message=recovery,
        allow_dm=bool(event.actionable and state == "open"),
    )


_ACTIONABLE_BLOCK_KINDS = frozenset({"needs_input", "capability"})
_ROUTED_BLOCK_KINDS = _ACTIONABLE_BLOCK_KINDS | {"dependency"}


def _one_line(value: Any, *, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def collect_routed_blocker_incidents(
    kb_module: Any,
    *,
    boards: Iterable[str],
    failed_boards: Optional[set[str]] = None,
) -> list[KanbanAlertIncident]:
    """Snapshot human-input and explicit task-dependency blockers."""
    incidents: list[KanbanAlertIncident] = []
    for board in boards:
        conn = None
        try:
            conn = kb_module.connect(board=board)
            tasks = [
                *kb_module.list_tasks(conn, status="blocked"),
                *kb_module.list_tasks(conn, status="triage"),
                *kb_module.list_tasks(conn, status="todo"),
            ]
            for task in tasks:
                if task.block_kind not in _ROUTED_BLOCK_KINDS:
                    continue
                reason = ""
                for event in reversed(kb_module.list_events(conn, task.id)):
                    if event.kind not in {
                        "blocked",
                        "block_loop_detected",
                        "dependency_wait",
                    }:
                        continue
                    payload = event.payload if isinstance(event.payload, dict) else {}
                    reason = _one_line(payload.get("reason"))
                    if reason:
                        break
                owner = _one_line(task.assignee or "unassigned", limit=80)
                title = _one_line(task.title, limit=120)
                kind = str(task.block_kind)
                details = f" — {reason}" if reason else ""
                action = (
                    "Action: review the task and unblock it when resolved."
                    if kind in _ACTIONABLE_BLOCK_KINDS
                    else "Action: monitor the prerequisite; intervene only if it stops progressing."
                )
                incidents.append(
                    KanbanAlertIncident(
                        key=f"blocker:{board}:{task.id}",
                        route="blockers",
                        message=(
                            f"🛑 Kanban {kind} blocker: {board}/{task.id} "
                            f"{title} (owner: {owner}){details}\n"
                            f"{action}"
                        ),
                        recovery_message=(
                            f"✅ Kanban blocker cleared: {board}/{task.id} {title}."
                        ),
                        allow_dm=kind in _ACTIONABLE_BLOCK_KINDS,
                        board=board,
                        task_id=task.id,
                    )
                )
        except Exception as exc:
            if failed_boards is not None:
                failed_boards.add(board)
            logger.warning(
                "kanban alerts: cannot inspect blockers on board %s: %s",
                board,
                exc,
            )
        finally:
            if conn is not None:
                conn.close()
    return incidents


def record_dispatch_alerts(
    notifier: "KanbanAlertNotifier",
    dispatch_results: Iterable[tuple[str, Any]],
    *,
    ready_stalled: bool,
    ready_healthy: bool,
    health_window: int,
) -> None:
    """Translate one dispatcher tick into bounded alert state transitions."""
    ready_incidents = []
    if ready_stalled:
        ready_incidents.append(
            KanbanAlertIncident(
                key="dispatcher:ready-unspawned",
                route="automation",
                message=(
                    "⚠️ Kanban dispatcher stalled: spawnable ready work remains, "
                    f"but no workers launched for {health_window} consecutive ticks."
                ),
                recovery_message=(
                    "✅ Kanban dispatcher recovered: ready work is launching again."
                ),
            )
        )
    if ready_stalled or ready_healthy:
        notifier.sync_scope("dispatcher-ready", ready_incidents)

    for board, result in dispatch_results:
        stale = {str(task_id) for task_id in (getattr(result, "stale", None) or [])}
        spawned = {
            str(item[0] if isinstance(item, (list, tuple)) and item else item)
            for item in (getattr(result, "spawned", None) or [])
        }
        for task_id in stale:
            key = f"dispatcher:stale:{board}:{task_id}"
            if task_id in spawned:
                notifier.resolve_incident(key)
                notifier.queue_transient(
                    KanbanAlertIncident(
                        key=f"dispatcher:stale-auto-recovered:{board}:{task_id}",
                        route="automation",
                        message=(
                            f"⚠️ Kanban stale worker auto-recovered: {board}/{task_id} "
                            "stopped producing evidenced progress, was reclaimed, "
                            "and was respawned."
                        ),
                    )
                )
                continue
            notifier.open_incident(
                "dispatcher-stale",
                KanbanAlertIncident(
                    key=key,
                    route="automation",
                    message=(
                        f"⚠️ Kanban stale worker: {board}/{task_id} stopped "
                        "producing evidenced progress and was reclaimed; it has "
                        "not respawned yet."
                    ),
                    recovery_message=(
                        f"✅ Kanban stale-worker incident recovered: {board}/{task_id} "
                        "launched again."
                    ),
                    board=board,
                    task_id=task_id,
                ),
            )
        for task_id in spawned - stale:
            notifier.resolve_incident(f"dispatcher:stale:{board}:{task_id}")


def reconcile_stale_task_incidents(
    notifier: "KanbanAlertNotifier",
    kb_module: Any,
) -> None:
    """Close stale-worker incidents whose task left the ready queue."""
    by_board: dict[str, list[tuple[str, str]]] = {}
    for key, entry in notifier.active_incidents("dispatcher-stale"):
        board = str(entry.get("board") or "")
        task_id = str(entry.get("task_id") or "")
        if board and task_id:
            by_board.setdefault(board, []).append((key, task_id))

    for board, targets in by_board.items():
        conn = None
        try:
            conn = kb_module.connect(board=board)
            for key, task_id in targets:
                task = kb_module.get_task(conn, task_id)
                status = str(task.status) if task is not None else "missing"
                if status == "ready":
                    continue
                if status == "running":
                    message = (
                        f"✅ Kanban stale-worker incident recovered: {board}/{task_id} "
                        "launched again."
                    )
                else:
                    message = (
                        f"✅ Kanban stale-worker incident closed: {board}/{task_id} "
                        f"is now {status}."
                    )
                notifier.resolve_incident(key, recovery_message=message)
        except Exception as exc:
            logger.warning(
                "kanban alerts: cannot reconcile stale tasks on board %s: %s",
                board,
                exc,
            )
        finally:
            if conn is not None:
                conn.close()


class KanbanAlertNotifier:
    """Persist incident state and deliver only openings and recoveries."""

    def __init__(
        self,
        settings: KanbanAlertSettings,
        *,
        state_path: Path,
        adapter_lookup: Callable[[str, Optional[str]], Any],
        resolve_channel: Callable[[str, str], Optional[str]],
        lookup_channel_type: Callable[[str, str], Optional[str]],
        list_known_channels: Optional[Callable[[str], list[dict[str, Any]]]] = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.state_path = Path(state_path)
        self._adapter_lookup = adapter_lookup
        self._resolve_channel = resolve_channel
        self._lookup_channel_type = lookup_channel_type
        self._list_known_channels = list_known_channels
        self._now = now
        self._state_write_failed = False
        self._destination_fingerprint = json.dumps(
            [
                settings.platform,
                settings.profile,
                settings.automation_channel,
                settings.blockers_channel,
            ],
            separators=(",", ":"),
        )
        self._state = self._load_state()
        if self._state.get("destination") != self._destination_fingerprint:
            self._state = {
                "version": 1,
                "destination": self._destination_fingerprint,
                "active": {},
                "recent": {},
                "pending_transients": {},
            }

    def _load_state(self) -> dict[str, Any]:
        empty = {
            "version": 1,
            "destination": None,
            "active": {},
            "recent": {},
            "pending_transients": {},
        }
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("active"), dict):
                empty["destination"] = raw.get("destination")
                for key in ("active", "recent", "pending_transients"):
                    value = raw.get(key)
                    empty[key] = value if isinstance(value, dict) else {}
                empty["active"] = dict(
                    list(empty["active"].items())[-_MAX_STATE_ENTRIES:]
                )
                empty["pending_transients"] = dict(
                    list(empty["pending_transients"].items())[-_MAX_STATE_ENTRIES:]
                )
                return empty
        except (OSError, ValueError, TypeError):
            pass
        return empty

    def _save_state(self) -> None:
        pending = self._state.get("pending_transients", {})
        while len(pending) > _MAX_STATE_ENTRIES:
            pending.pop(next(iter(pending)), None)
        recent = self._state.get("recent", {})
        if len(recent) > _MAX_STATE_ENTRIES:
            keep = sorted(
                recent.items(), key=lambda item: float(item[1] or 0.0), reverse=True
            )[:_MAX_STATE_ENTRIES]
            self._state["recent"] = dict(keep)
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            from utils import atomic_json_write

            atomic_json_write(self.state_path, self._state)
        except OSError as exc:
            if not self._state_write_failed:
                logger.warning(
                    "kanban alerts: cannot persist dedupe state at %s: %s",
                    self.state_path,
                    exc,
                )
            else:
                logger.debug("kanban alerts: dedupe state is still unwritable: %s", exc)
            self._state_write_failed = True
        else:
            self._state_write_failed = False

    def sync_scope(
        self,
        scope: str,
        incidents: Iterable[KanbanAlertIncident],
    ) -> None:
        """Make one snapshot scope match ``incidents`` without touching others."""
        current = {incident.key: incident for incident in incidents}
        active = self._state["active"]
        for key, entry in list(active.items()):
            if entry.get("scope") != scope or key in current:
                continue
            if entry.get("announced") and entry.get("recovery_message"):
                entry["pending_recovery"] = True
            else:
                active.pop(key, None)
        for key, incident in current.items():
            entry = active.get(key)
            if entry is None:
                if len(active) >= _MAX_STATE_ENTRIES:
                    logger.warning(
                        "kanban alerts: active incident cap reached; dropping %s", key
                    )
                    continue
                last_sent = float(self._state["recent"].get(key) or 0.0)
                active[key] = {
                    **asdict(incident),
                    "scope": scope,
                    "announced": False,
                    "pending_recovery": False,
                    "opened_at": self._now(),
                    "last_attempt_at": 0.0,
                    "suppress_until": (
                        last_sent + self.settings.cooldown_seconds if last_sent else 0.0
                    ),
                }
            else:
                entry.update(asdict(incident))
                entry["scope"] = scope
                entry["pending_recovery"] = False
        self._save_state()

    def retire_missing_scopes(self, prefix: str, keep: set[str]) -> None:
        """Forget scopes authoritatively removed from the current board set."""
        active = self._state["active"]
        retired = [
            key
            for key, entry in active.items()
            if str(entry.get("scope") or "").startswith(prefix)
            and entry.get("scope") not in keep
        ]
        for key in retired:
            active.pop(key, None)
        if retired:
            self._save_state()

    def open_incident(self, scope: str, incident: KanbanAlertIncident) -> None:
        """Open or refresh an event-driven incident without closing scope peers."""
        entry = self._state["active"].get(incident.key)
        if entry is None:
            if len(self._state["active"]) >= _MAX_STATE_ENTRIES:
                logger.warning(
                    "kanban alerts: active incident cap reached; dropping %s",
                    incident.key,
                )
                return
            last_sent = float(self._state["recent"].get(incident.key) or 0.0)
            self._state["active"][incident.key] = {
                **asdict(incident),
                "scope": scope,
                "announced": False,
                "pending_recovery": False,
                "opened_at": self._now(),
                "last_attempt_at": 0.0,
                "suppress_until": (
                    last_sent + self.settings.cooldown_seconds if last_sent else 0.0
                ),
            }
        else:
            entry.update(asdict(incident))
            entry["scope"] = scope
            entry["pending_recovery"] = False
        self._save_state()

    def active_incidents(self, scope: str) -> list[tuple[str, dict[str, Any]]]:
        """Return a defensive snapshot of active incidents in ``scope``."""
        return [
            (key, dict(entry))
            for key, entry in self._state["active"].items()
            if entry.get("scope") == scope
        ]

    def resolve_incident(
        self,
        key: str,
        *,
        recovery_message: Optional[str] = None,
    ) -> None:
        """Close one event-driven incident, recovering only if it was announced."""
        entry = self._state["active"].get(key)
        if entry is None:
            return
        if recovery_message is not None:
            entry["recovery_message"] = recovery_message
        if entry.get("announced") and entry.get("recovery_message"):
            entry["pending_recovery"] = True
        else:
            self._state["active"].pop(key, None)
        self._save_state()

    def queue_transient(self, incident: KanbanAlertIncident) -> None:
        """Queue a one-shot alert unless the same key is inside its cooldown."""
        now = self._now()
        last_sent = float(self._state["recent"].get(incident.key) or 0.0)
        if last_sent and now - last_sent < self.settings.cooldown_seconds:
            return
        self._state["pending_transients"].setdefault(
            incident.key,
            {**asdict(incident), "last_attempt_at": 0.0},
        )
        self._save_state()

    async def flush(self) -> None:
        """Deliver pending openings/recoveries and persist successful acks."""
        active = self._state["active"]
        groups: dict[tuple[str, str, bool], list[tuple[str, dict[str, Any]]]] = {}
        for key, entry in list(active.items()):
            recovering = bool(entry.get("pending_recovery"))
            if recovering:
                message = str(entry.get("recovery_message") or "")
            elif not entry.get("announced"):
                message = str(entry.get("message") or "")
            else:
                continue
            if not recovering and self._now() < float(
                entry.get("suppress_until") or 0.0
            ):
                continue
            last_attempt = float(entry.get("last_attempt_at") or 0.0)
            if (
                last_attempt
                and self._now() - last_attempt < self.settings.retry_seconds
            ):
                continue
            if not message:
                if recovering:
                    active.pop(key, None)
                continue
            route = str(entry.get("route") or "automation")
            transition = "recovery" if recovering else "opening"
            # Recovery is informational, never actionable enough to justify a DM.
            allow_dm = bool(entry.get("allow_dm")) and not recovering
            groups.setdefault((route, transition, allow_dm), []).append((key, entry))

        for (route, transition, allow_dm), entries in groups.items():
            messages = [
                str(
                    entry.get("recovery_message")
                    if transition == "recovery"
                    else entry.get("message")
                )
                for _, entry in entries
            ]
            attempted_at = self._now()
            for _key, entry in entries:
                entry["last_attempt_at"] = attempted_at
            delivery = await self._send(
                route,
                self._render_batch(route, transition, messages),
                allow_dm=allow_dm,
            )
            if delivery is None:
                if transition == "recovery":
                    for key, _entry in entries:
                        active.pop(key, None)
                continue
            if not delivery:
                continue
            now = self._now()
            for key, entry in entries:
                self._state["recent"][key] = now
                if transition == "recovery":
                    active.pop(key, None)
                else:
                    entry["announced"] = True
                    entry["last_sent_at"] = now
                    entry["last_attempt_at"] = 0.0

        pending = self._state["pending_transients"]
        transient_groups: dict[tuple[str, bool], list[tuple[str, dict[str, Any]]]] = {}
        for key, entry in pending.items():
            last_attempt = float(entry.get("last_attempt_at") or 0.0)
            if (
                last_attempt
                and self._now() - last_attempt < self.settings.retry_seconds
            ):
                continue
            route = str(entry.get("route") or "automation")
            allow_dm = bool(entry.get("allow_dm"))
            transient_groups.setdefault((route, allow_dm), []).append((key, entry))
        for (route, allow_dm), entries in transient_groups.items():
            messages = [str(entry.get("message") or "") for _, entry in entries]
            attempted_at = self._now()
            for _key, entry in entries:
                entry["last_attempt_at"] = attempted_at
            delivery = await self._send(
                route,
                self._render_batch(route, "alert", messages),
                allow_dm=allow_dm,
            )
            if delivery is None:
                for key, _entry in entries:
                    pending.pop(key, None)
                continue
            if not delivery:
                continue
            now = self._now()
            for key, _entry in entries:
                self._state["recent"][key] = now
                pending.pop(key, None)
        self._save_state()

    def _render_batch(self, route: str, transition: str, messages: list[str]) -> str:
        if len(messages) == 1:
            return messages[0]
        limit = max(1, int(self.settings.max_items_per_message))
        label = "blocker" if route == "blockers" else "automation"
        noun = "recoveries" if transition == "recovery" else "alerts"
        lines = [f"Kanban {label} {noun} ({len(messages)}):"]
        lines.extend(f"• {message}" for message in messages[:limit])
        omitted = len(messages) - limit
        if omitted > 0:
            lines.append(f"… +{omitted} more; review the Kanban board for details.")
        return "\n".join(lines)

    async def _send(
        self, route: str, message: str, *, allow_dm: bool
    ) -> Optional[bool]:
        channel_ref = (
            self.settings.blockers_channel
            if route == "blockers"
            else self.settings.automation_channel
        )
        if not self.settings.enabled or not channel_ref:
            return False
        adapter = self._adapter_lookup(self.settings.platform, self.settings.profile)
        if adapter is None:
            return False
        chat_id, channel_type = await self._resolve_destination(adapter, channel_ref)
        if not chat_id:
            logger.warning(
                "kanban alerts: unresolved %s channel %r on %s; not guessing an id",
                route,
                channel_ref,
                self.settings.platform,
            )
            return False
        if not allow_dm and channel_type in {"dm", "direct", "user"}:
            logger.warning(
                "kanban alerts: refusing non-actionable alert to DM %s/%s",
                self.settings.platform,
                chat_id,
            )
            return None
        if not allow_dm and not channel_type:
            logger.warning(
                "kanban alerts: refusing non-actionable alert to unclassified %s/%s",
                self.settings.platform,
                chat_id,
            )
            return None
        try:
            result = adapter.send(
                chat_id, message, metadata={"chat_type": channel_type}
            )
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            logger.warning("kanban alerts: delivery failed", exc_info=True)
            return False
        return getattr(result, "success", True) is not False

    async def _resolve_destination(
        self,
        adapter: Any,
        channel_ref: str,
    ) -> tuple[Optional[str], str]:
        """Resolve a configured name/id against grounded directory or adapter data."""
        chat_id = self._resolve_channel(self.settings.platform, channel_ref)
        channel_type = (
            self._lookup_channel_type(self.settings.platform, chat_id) or ""
            if chat_id
            else ""
        ).lower()
        list_channels = getattr(adapter, "list_channels", None)
        listed: Any = None
        if callable(list_channels):
            try:
                listed = list_channels()
                if inspect.isawaitable(listed):
                    listed = await listed
            except Exception:
                logger.debug(
                    "kanban alerts: adapter channel listing failed", exc_info=True
                )
        if not isinstance(listed, list):
            if self.settings.profile:
                return None, ""
            if self._list_known_channels is not None:
                try:
                    listed = self._list_known_channels(self.settings.platform)
                except Exception:
                    logger.debug(
                        "kanban alerts: process channel listing failed", exc_info=True
                    )
            if not isinstance(listed, list):
                if channel_ref.startswith("#"):
                    return None, ""
                return chat_id, channel_type

        query = channel_ref.lstrip("#").strip().lower()
        exact_id_candidates: dict[str, str] = {}
        name_candidates: dict[str, str] = {}
        for item in listed:
            if not isinstance(item, Mapping):
                continue
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                continue
            item_name = str(item.get("name") or "").lstrip("#").strip().lower()
            item_type = str(item.get("type") or "").strip().lower()
            if item_id == channel_ref:
                exact_id_candidates[item_id] = item_type
            if item_name == query:
                name_candidates[item_id] = item_type
        if len(exact_id_candidates) == 1:
            return next(iter(exact_id_candidates.items()))
        if len(name_candidates) != 1:
            return None, ""
        resolved_id, resolved_type = next(iter(name_candidates.items()))
        return resolved_id, resolved_type
