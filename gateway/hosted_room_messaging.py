"""Messaging-facing controls for gateway-hosted Bot rooms.

The handlers in :mod:`gateway.slash_commands` deliberately stay thin.  This
module owns parsing, room lookup, bounded presentation, and server-owned actor
identity so Telegram, Signal, WhatsApp, and the other gateway transports share
one behavior contract.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_driver as driver
from gateway import hosted_rooms


MAX_ROOM_CHOICES = 8
MAX_RECENT_MESSAGES = 5
MAX_PREVIEW_CHARS = 180
GROUP_CHAT_SYNC_META_KEY = "hermes-bots-groups"


def _projected_desktop_rooms() -> list[dict[str, Any]]:
    """Read the bounded classic-room projection shared by Desktop clients."""

    try:
        import yaml
        from hermes_cli.profiles import get_profile_dir

        profile_meta = Path(get_profile_dir("default")) / "profile.yaml"
        if not profile_meta.is_file():
            return []
        raw = yaml.safe_load(profile_meta.read_text(encoding="utf-8")) or {}
        ui_meta = raw.get("ui_meta") if isinstance(raw, Mapping) else None
        snapshot = (
            ui_meta.get(GROUP_CHAT_SYNC_META_KEY)
            if isinstance(ui_meta, Mapping)
            else None
        )
        raw_rooms = snapshot.get("rooms") if isinstance(snapshot, Mapping) else None
    except Exception:
        return []
    if not isinstance(raw_rooms, Mapping):
        return []
    try:
        snapshot_version = int(snapshot.get("version") or 0)
    except (TypeError, ValueError):
        snapshot_version = 0

    rooms: list[dict[str, Any]] = []
    for key, raw_room in raw_rooms.items():
        if not isinstance(raw_room, Mapping):
            continue
        hosted = raw_room.get("hosted")
        if isinstance(hosted, str) and hosted.strip():
            continue
        name = _clean_line(raw_room.get("name") or key, limit=200)
        explicit_room_id = str(raw_room.get("roomId") or "").strip()
        room_id = (
            explicit_room_id
            or (str(key).strip() if snapshot_version >= 3 else f"name:{name}")
        )
        if (
            not name
            or not room_id
            or len(room_id) > 200
            or any(char in room_id for char in ("\x00", "\r", "\n"))
        ):
            continue
        authority_hash = str(raw_room.get("desktopAuthorityHash") or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", authority_hash):
            authority_hash = ""
        raw_log = raw_room.get("log")
        log = [dict(item) for item in raw_log if isinstance(item, Mapping)] if isinstance(raw_log, list) else []
        raw_members = raw_room.get("members")
        members = (
            [dict(item) for item in raw_members if isinstance(item, Mapping)]
            if isinstance(raw_members, list)
            else []
        )
        updated_at = max(
            (float(item.get("at") or 0) for item in log),
            default=float(snapshot.get("updatedAt") or 0) / 1000,
        )
        rooms.append(
            {
                "room_id": room_id,
                "name": name,
                "members": members,
                "log": log,
                "created_at": min(
                    (float(item.get("at") or 0) for item in log),
                    default=updated_at,
                ),
                "updated_at": updated_at,
                "desktop_authority_hash": authority_hash,
                "_room_mode": "desktop",
            }
        )
    return rooms


def list_messaging_rooms(service: Any) -> list[dict[str, Any]]:
    """Return hosted and reachable classic rooms with stable short numbers."""

    hosted = [
        {**room, "_room_mode": "hosted"}
        for room in hosted_rooms.list_rooms(service.db_path)
    ]
    hosted_ids = {str(room["room_id"]) for room in hosted}
    desktop = [
        room
        for room in _projected_desktop_rooms()
        if str(room["room_id"]) not in hosted_ids
    ]
    rooms = hosted + desktop
    if not rooms:
        return []

    from gateway.desktop_room_mailbox import (
        MAX_ROOM_IDS,
        available_room_ids,
        default_db_path,
        latest_command_states,
        register_projected_authorities,
    )

    mailbox_db = default_db_path()
    desktop_ids = [room["room_id"] for room in desktop]
    commitments = [
        {
            "room_id": room["room_id"],
            "authority_hash": room["desktop_authority_hash"],
        }
        for room in desktop
        if room.get("desktop_authority_hash")
    ]
    for index in range(0, len(commitments), MAX_ROOM_IDS):
        register_projected_authorities(
            mailbox_db,
            commitments[index : index + MAX_ROOM_IDS],
        )
    available = available_room_ids(mailbox_db, desktop_ids)
    command_states = latest_command_states(mailbox_db, desktop_ids)
    rooms = [
        {
            **room,
            **(
                {
                    "desktop_available": str(room["room_id"]) in available,
                    "desktop_command": command_states.get(str(room["room_id"])),
                }
                if room.get("_room_mode") == "desktop"
                else {}
            ),
        }
        for room in rooms
    ]

    # Keep the numeric reference ledger where #96274 first created it. Moving
    # it to the compatibility mailbox would silently renumber existing group
    # chats after upgrade, making `/group 2 send ...` target the wrong chat.
    db_path = Path(service.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(conn, db_label="state.db (room messaging refs)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS hosted_room_messaging_refs (
                room_ref INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL UNIQUE
            )"""
        )
        # Existing rooms receive deterministic first-use numbers. AUTOINCREMENT
        # keeps those numbers stable and prevents a disbanded room's reference
        # from being reassigned to unrelated work later.
        for room in sorted(
            rooms,
            key=lambda item: (
                float(item.get("created_at") or 0),
                str(item.get("room_id") or ""),
            ),
        ):
            conn.execute(
                "INSERT OR IGNORE INTO hosted_room_messaging_refs (room_id) VALUES (?)",
                (str(room["room_id"]),),
            )
        refs = {
            str(row["room_id"]): int(row["room_ref"])
            for row in conn.execute(
                "SELECT room_id, room_ref FROM hosted_room_messaging_refs"
            )
        }
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return [
        {**room, "messaging_ref": refs[str(room["room_id"])]}
        for room in rooms
    ]


def room_reference(room: Mapping[str, Any]) -> str:
    """Return the short messaging reference, with an internal-id fallback."""

    reference = room.get("messaging_ref")
    if isinstance(reference, int) and reference > 0:
        return str(reference)
    return str(room.get("room_id") or "")


class RoomControlError(ValueError):
    """A user-actionable hosted-room command error."""


@dataclass(frozen=True)
class RoomCommand:
    """Parsed mutating ``/group`` subcommand."""

    action: str
    room_query: str
    message: str = ""


class MessagingRoomBackend:
    """Cross-process hosted-room access through the shared durable stores."""

    def __init__(self, *, db_path: Any, service: Any = None) -> None:
        self.db_path = db_path
        self.service = service

    def status(self, room_id: str) -> dict[str, Any]:
        if self.service is not None:
            return self.service.status(room_id)
        tasks = driver.list_tasks(self.db_path, room_id=room_id)
        counts: dict[str, int] = {}
        for task in tasks:
            status = str(task.get("status") or "")
            counts[status] = counts.get(status, 0) + 1
        return {
            "working": any(counts.get(status) for status in ("queued", "running", "stopping")),
            "blocked": bool(counts.get("indeterminate")),
            "counts": counts,
        }

    def send(
        self,
        *,
        room_id: str,
        event_id: str,
        payload: Any,
        actor: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self.service is not None:
            return self.service.send(
                room_id=room_id,
                event_id=event_id,
                payload=payload,
                actor=actor,
            )
        normalized = discussion.validate_user_payload(payload)
        return hosted_rooms.append_event(
            self.db_path,
            room_id=room_id,
            event_id=event_id,
            kind="message.user",
            actor=dict(actor),
            payload=normalized,
        )

    def stop_room(self, room_id: str, *, cancel_id: str) -> int:
        if self.service is not None:
            return self.service.stop_room(room_id, cancel_id=cancel_id)
        hosted_rooms.request_room_stop(
            self.db_path,
            room_id=room_id,
            cancel_id=cancel_id,
        )
        requested = 0
        for task in driver.list_tasks(self.db_path, room_id=room_id):
            for _attempt in range(3):
                current = driver.get_task(self.db_path, task["identity"])
                status = str(current.get("status") or "")
                try:
                    if status == "queued":
                        driver.cancel_task(
                            self.db_path,
                            current["identity"],
                            cancel_id=cancel_id,
                            expected_cancel_generation=int(
                                current["cancel_generation"]
                            ),
                            clock=time.time,
                        )
                        requested += 1
                    elif status in {"running", "indeterminate"}:
                        driver.begin_task_cancel(
                            self.db_path,
                            current["identity"],
                            cancel_id=cancel_id,
                            expected_cancel_generation=int(
                                current["cancel_generation"]
                            ),
                            clock=time.time,
                        )
                        requested += 1
                    elif status == "stopping":
                        requested += 1
                    elif (
                        status == "cancelled"
                        and current.get("cancel_id") == cancel_id
                    ):
                        requested += 1
                    break
                except (driver.InvalidTaskTransitionError, driver.StaleTaskError):
                    # Queued work can become running between the list and the
                    # write. Reload and retry under the new generation.
                    continue
        return requested


def current_room_backend() -> MessagingRoomBackend:
    """Resolve in-process service access or the shared cross-process store."""

    from tui_gateway.methods_groups import get_hosted_room_service

    service = get_hosted_room_service()
    return MessagingRoomBackend(
        db_path=service.db_path if service is not None else hosted_rooms.default_db_path(),
        service=service,
    )


def _clean_line(value: Any, *, limit: int = MAX_PREVIEW_CHARS) -> str:
    """Collapse untrusted text to one bounded display line."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def command_form(event: Any) -> str:
    """Return the channel-valid hosted-room command prefix."""

    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", "")
    if platform == "slack":
        return "/hermes group"
    if platform == "matrix":
        return "!group"
    return "/group"


def parse_room_command(args: str, *, command_root: str = "/group") -> RoomCommand:
    """Parse the number-first send/stop grammar used by messaging clients."""

    raw = str(args or "").strip()
    entity_first = raw.split(maxsplit=2)
    if len(entity_first) < 2 or not entity_first[0].isdecimal():
        raise RoomControlError(
            f"Use `{command_root} <number> send <message>` or "
            f"`{command_root} <number> stop`."
        )
    room_query = entity_first[0]
    action = entity_first[1].casefold()
    remainder = entity_first[2].strip() if len(entity_first) == 3 else ""
    if action == "send":
        message = remainder.removeprefix("--").strip()
        if len(message) >= 2 and message[0] == message[-1] and message[0] in {'"', "'"}:
            message = message[1:-1].strip()
        if not message:
            raise RoomControlError(
                f"Use `{command_root} <number> send <message>`."
            )
        return RoomCommand("send", room_query, message)
    if action == "stop":
        if remainder:
            raise RoomControlError(f"Use `{command_root} <number> stop`.")
        return RoomCommand("stop", room_query)
    raise RoomControlError(
        f"Use `{command_root} <number> send <message>` or "
        f"`{command_root} <number> stop`."
    )


def resolve_room(rooms: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """Resolve by stable messaging number, then id/name convenience matches."""

    needle = _clean_line(query, limit=hosted_rooms.MAX_ROOM_NAME_CHARS).casefold()
    if not needle:
        raise RoomControlError("Enter a room number or name.")

    if needle.isdecimal():
        numeric_ref = int(needle)
        matches = [
            room for room in rooms if room.get("messaging_ref") == numeric_ref
        ]
        if len(matches) == 1:
            return matches[0]
        raise RoomControlError(f"No hosted Bot room is numbered {numeric_ref}.")

    if needle.startswith("id:"):
        internal_id = needle.removeprefix("id:")
        matches = [
            room
            for room in rooms
            if str(room.get("room_id") or "").casefold() == internal_id
        ]
        if len(matches) == 1:
            return matches[0]
        raise RoomControlError("No hosted Bot room matches that internal ID.")

    def _keys(room: Mapping[str, Any]) -> tuple[str, str]:
        return (
            str(room.get("room_id") or "").casefold(),
            str(room.get("name") or "").casefold(),
        )

    for match_kind in ("exact", "prefix", "contains"):
        matches: list[dict[str, Any]] = []
        for room in rooms:
            room_id, name = _keys(room)
            if match_kind == "exact" and needle in {room_id, name}:
                matches.append(room)
            elif match_kind == "prefix" and (
                room_id.startswith(needle) or name.startswith(needle)
            ):
                matches.append(room)
            elif match_kind == "contains" and (needle in room_id or needle in name):
                matches.append(room)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = ", ".join(
                (
                    f"{_clean_line(room.get('name') or room.get('room_id'), limit=48)} "
                    f"[{room_reference(room)}]"
                )
                for room in matches[:MAX_ROOM_CHOICES]
            )
            suffix = "…" if len(matches) > MAX_ROOM_CHOICES else ""
            raise RoomControlError(
                f"That matches several group chats: {names}{suffix}. Enter more of the name."
            )
    raise RoomControlError(f"No group chat matches “{_clean_line(query)}”.")


def _room_status(service: Any, room: Mapping[str, Any]) -> str:
    if room.get("_room_mode") == "desktop":
        command = room.get("desktop_command")
        state = str(command.get("state") or "") if isinstance(command, Mapping) else ""
        if state == "failed":
            return "needs attention"
        if state in {"pending", "claimed"} and room.get("desktop_available"):
            return "applying command"
        return "ready" if room.get("desktop_available") else "waiting for Desktop"
    room_id = str(room["room_id"])
    status = service.status(room_id)
    if status.get("blocked"):
        return "needs attention"
    if status.get("counts", {}).get("stopping"):
        return "stopping"
    if status.get("working"):
        return "work queued or running"
    state = hosted_rooms.room_state(service.db_path, room_id=room_id)
    since = max(0, int(state.get("latest_seq") or 0) - 80)
    recent = hosted_rooms.read_events(
        service.db_path,
        room_id=room_id,
        since_seq=since,
        limit=80,
    ).get("events", [])
    latest_user = max(
        (int(event["seq"]) for event in recent if event.get("kind") == "message.user"),
        default=0,
    )
    latest_boundary = max(
        (
            int(event["seq"])
            for event in recent
            if event.get("kind") in {"room.activity", "room.stop_requested"}
        ),
        default=0,
    )
    if latest_user > latest_boundary:
        return "waiting for the room host"
    return "idle"


def format_room_list(service: Any, *, rooms_command: str = "/group") -> str:
    """Render a bounded, scan-friendly Group Chat list."""

    rooms = list_messaging_rooms(service)
    if not rooms:
        return (
            "No Group Chats yet. Create one in Hermes Desktop first.\n\n"
            f"Send: `{rooms_command} <number> send <message>`"
        )
    lines = ["Group Chats"]
    for room in rooms[:MAX_ROOM_CHOICES]:
        name = _clean_line(room.get("name") or room.get("room_id"), limit=72)
        raw_members = room.get("members")
        members: list[Any] = raw_members if isinstance(raw_members, list) else []
        lines.append(
            f"{room_reference(room)}. {name} — {_room_status(service, room)}, "
            f"{len(members)} bot{'s' if len(members) != 1 else ''}"
        )
    if len(rooms) > MAX_ROOM_CHOICES:
        lines.append(f"…and {len(rooms) - MAX_ROOM_CHOICES} more")
    lines.extend(
        [
            "",
            "Commands",
            f"Check: `{rooms_command} <number>`",
            f"Send: `{rooms_command} <number> send <message>`",
            f"Stop: `{rooms_command} <number> stop`",
        ]
    )
    return "\n".join(lines)


def _event_label(event: Mapping[str, Any], member_names: Mapping[str, str]) -> str:
    raw_actor = event.get("actor")
    actor: Mapping[str, Any] = raw_actor if isinstance(raw_actor, Mapping) else {}
    actor_id = str(actor.get("id") or "")
    display_name = _clean_line(actor.get("display_name"), limit=48)
    if display_name:
        return display_name
    if event.get("kind") == "message.member":
        raw_payload = event.get("payload")
        payload: Mapping[str, Any] = (
            raw_payload if isinstance(raw_payload, Mapping) else {}
        )
        member_id = str(payload.get("member_id") or actor_id)
        return member_names.get(member_id, "Bot")
    return "You"


def format_room_detail(
    service: Any,
    room: Mapping[str, Any],
    *,
    room_command: str = "/group",
) -> str:
    """Render status plus the latest visible room messages."""

    room_id = str(room["room_id"])
    if not isinstance(room.get("messaging_ref"), int):
        room = next(
            (
                candidate
                for candidate in list_messaging_rooms(service)
                if str(candidate["room_id"]) == room_id
            ),
            dict(room),
        )
    raw_members = room.get("members")
    members: list[Any] = raw_members if isinstance(raw_members, list) else []
    desktop_mode = room.get("_room_mode") == "desktop"
    if desktop_mode:
        visible = [
            event for event in room.get("log", []) if isinstance(event, Mapping)
        ][-MAX_RECENT_MESSAGES:]
        member_names: dict[str, str] = {}
    else:
        state = hosted_rooms.room_state(service.db_path, room_id=room_id)
        since = max(0, int(state.get("latest_seq") or 0) - 80)
        delta = hosted_rooms.read_events(
            service.db_path,
            room_id=room_id,
            since_seq=since,
            limit=80,
        )
        member_names = {
            str(member.get("member_id") or ""): _clean_line(
                member.get("display_name") or member.get("handle") or "Bot",
                limit=48,
            )
            for member in members
            if isinstance(member, Mapping)
        }
        visible = [
            event
            for event in delta.get("events", [])
            if isinstance(event, Mapping)
            and event.get("kind") in {"message.user", "message.member"}
        ][-MAX_RECENT_MESSAGES:]
    name = _clean_line(room.get("name") or room_id, limit=72)
    lines = [
        f"{name} — {_room_status(service, room)}",
        f"{len(members)} bot{'s' if len(members) != 1 else ''}",
    ]
    if visible:
        lines.append("Recent activity")
        for event in visible:
            if desktop_mode:
                source = event.get("from") if isinstance(event.get("from"), Mapping) else {}
                label = _clean_line(source.get("name") or "You", limit=48)
                text = event.get("text")
            else:
                payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
                label = _event_label(event, member_names)
                text = payload.get("text")
            lines.append(
                f"• {label}: {_clean_line(text)}"
            )
    else:
        lines.append("No messages yet.")
    command = room.get("desktop_command")
    if (
        desktop_mode
        and isinstance(command, Mapping)
        and command.get("state") == "failed"
    ):
        lines.append(
            "The latest command could not be applied. Open this room in "
            "Hermes Desktop, then try again."
        )
    lines.append(
        f"Send: `{room_command} {room_reference(room)} send <message>`"
    )
    lines.append(f"Stop: `{room_command} {room_reference(room)} stop`")
    return "\n".join(lines)


def messaging_actor(event: Any, *, gateway_id: str) -> dict[str, str]:
    """Build a stable actor without persisting raw platform user IDs."""

    source = getattr(event, "source", None)
    platform_value = getattr(getattr(source, "platform", None), "value", None)
    platform = _clean_line(platform_value or "messaging", limit=32).casefold()
    raw_user_id = (
        getattr(source, "user_id_alt", None)
        or getattr(event, "user_id", None)
        or getattr(source, "user_id", None)
        or "unknown"
    )
    scope = getattr(source, "scope_id", None) or getattr(source, "guild_id", None)
    chat_id = getattr(source, "chat_id", None)
    digest = hashlib.sha256(
        f"{gateway_id}:{platform}:{scope or ''}:{chat_id or ''}:{raw_user_id}".encode()
    ).hexdigest()[:20]
    raw_name = _clean_line(
        getattr(event, "user_name", None) or getattr(source, "user_name", None),
        limit=48,
    )
    platform_label = platform.replace("_", " ").title()
    display_name = (
        f"{raw_name} via {platform_label}"
        if raw_name and raw_name != str(raw_user_id)
        else platform_label
    )
    return {
        "kind": "user",
        "id": f"messaging:{platform}:{digest}",
        "display_name": display_name,
    }


def _raw_transport_id(event: Any) -> Any:
    """Extract one adapter-owned redelivery key without guessing from text."""

    metadata = getattr(event, "metadata", None)
    candidates: list[Any] = []
    if isinstance(metadata, Mapping):
        candidates.extend(
            metadata.get(key)
            for key in (
                "delivery_id",
                "event_id",
                "message_id",
                "request_id",
                "update_id",
            )
        )
    raw = getattr(event, "raw_message", None)
    payloads = [raw]
    if isinstance(raw, Mapping):
        payloads.extend(
            raw.get(key) for key in ("event", "message", "data", "container")
        )
    for payload in payloads:
        if isinstance(payload, Mapping):
            candidates.extend(
                payload.get(key)
                for key in (
                    "client_msg_id",
                    "trigger_id",
                    "event_id",
                    "message_id",
                    "id",
                    "ts",
                    "event_ts",
                    "timestamp_ms",
                    "timestamp",
                )
            )
    if raw is not None and not isinstance(raw, Mapping):
        candidates.extend(
            getattr(raw, key, None) for key in ("id", "interaction_id")
        )
    return next(
        (
            value
            for value in candidates
            if value is not None and str(value).strip()
        ),
        None,
    )


def messaging_event_id(event: Any) -> str:
    """Return a deterministic idempotency key when the transport provides one."""

    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", "messaging")
    stable_message_id = (
        getattr(event, "platform_update_id", None)
        or getattr(event, "message_id", None)
        or getattr(source, "message_id", None)
        or _raw_transport_id(event)
    )
    if stable_message_id is None:
        raise RoomControlError(
            "This channel didn’t provide a stable message ID, so Hermes can’t "
            "safely repeat this room command. Try another connected channel."
        )
    material = "|".join(
        str(value or "")
        for value in (
            platform,
            getattr(source, "chat_id", None),
            getattr(source, "thread_id", None),
            getattr(source, "user_id_alt", None)
            or getattr(event, "user_id", None)
            or getattr(source, "user_id", None),
            stable_message_id,
        )
    )
    return f"messaging:{hashlib.sha256(material.encode()).hexdigest()}"


def ensure_text_only(event: Any) -> None:
    """Reject media explicitly until hosted-room attachment transport exists."""

    if getattr(event, "media_urls", None) or getattr(event, "media_types", None):
        raise RoomControlError(
            "Attachments from messaging chats aren’t supported yet. Send text only."
        )


def is_machine_authored(event: Any) -> bool:
    """Recognize native and relayed bot/webhook provenance defensively."""

    source = getattr(event, "source", None)
    if getattr(source, "is_bot", False):
        return True
    metadata = getattr(event, "metadata", None)
    if isinstance(metadata, Mapping) and any(
        metadata.get(key) is True
        for key in ("is_bot", "sender_is_bot", "webhook_sender")
    ):
        return True
    raw = getattr(event, "raw_message", None)
    if isinstance(raw, Mapping):
        if raw.get("bot_id") or raw.get("bot_profile"):
            return True
        if raw.get("subtype") in {"bot_message", "webhook_message"}:
            return True
    for owner_field in ("author", "user"):
        owner = getattr(raw, owner_field, None)
        if getattr(owner, "bot", False) or getattr(owner, "is_bot", False):
            return True
    return False


def relay_provenance_is_unknown(event: Any) -> bool:
    """Fail closed until a relay producer classifies the inbound author."""

    source = getattr(event, "source", None)
    if not getattr(source, "delivered_via_upstream_relay", False):
        return False
    metadata = getattr(event, "metadata", None)
    return not (
        isinstance(metadata, Mapping)
        and metadata.get("relay_author_classified") is True
    )


def _desktop_authority_hash(room: Mapping[str, Any]) -> str:
    authority_hash = str(room.get("desktop_authority_hash") or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", authority_hash):
        raise RoomControlError(
            "Open this Group Chat once in the latest Hermes Desktop, then try again."
        )
    return authority_hash


def send_to_room(service: Any, room: Mapping[str, Any], event: Any, text: str) -> str:
    """Append or hand off one idempotent room turn."""

    ensure_text_only(event)
    event_id = messaging_event_id(event)
    name = _clean_line(room.get("name") or room.get("room_id"), limit=72)
    if room.get("_room_mode") == "desktop":
        from gateway.desktop_room_mailbox import enqueue_command, default_db_path

        actor = messaging_actor(
            event,
            gateway_id=hosted_rooms.local_authority_gateway_id(),
        )
        enqueue_command(
            default_db_path(),
            command_id=event_id,
            room_id=str(room["room_id"]),
            authority_hash=_desktop_authority_hash(room),
            action="send",
            payload={
                "message": text,
                "actor_display_name": actor.get("display_name") or "Messaging",
            },
        )
        if room.get("desktop_available"):
            return f"Queued in {name}."
        return f"Saved for {name}. Open or update Hermes Desktop to continue."
    service.send(
        room_id=str(room["room_id"]),
        event_id=event_id,
        payload={"text": text, "thread_id": event_id},
        actor=messaging_actor(
            event,
            gateway_id=str(room["authority_gateway_id"]),
        ),
    )
    return f"Queued in {name}."


def stop_room(service: Any, room: Mapping[str, Any], event: Any) -> str:
    """Cancel active room tasks using a transport-derived idempotency key."""

    cancel_id = f"stop:{messaging_event_id(event)}"
    name = _clean_line(room.get("name") or room.get("room_id"), limit=72)
    if room.get("_room_mode") == "desktop":
        from gateway.desktop_room_mailbox import enqueue_command, default_db_path

        enqueue_command(
            default_db_path(),
            command_id=cancel_id,
            room_id=str(room["room_id"]),
            authority_hash=_desktop_authority_hash(room),
            action="stop",
            payload={},
        )
        if room.get("desktop_available"):
            return f"Stop requested for {name}."
        return f"Stop saved for {name}. Open or update Hermes Desktop to apply it."
    service.stop_room(str(room["room_id"]), cancel_id=cancel_id)
    return f"Stop requested for {name}. Active work will stop safely."
