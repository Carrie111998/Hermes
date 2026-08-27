"""Hosted-room JSON-RPC contract.

These methods expose durable room identity, replay, and the process-owned
same-gateway Discussion driver. ``groups.capabilities`` keeps that boundary
machine-readable so older clients stay on the renderer-owned room path.
"""

from .method_ctx import HandlerRegistry

import os
import threading

_registry = HandlerRegistry()
method = _registry.method

_service_lock = threading.Lock()
_bound_server = None
_service = None


def bind_server(server) -> None:
    """Bind the fully initialized server module without starting a worker."""

    global _bound_server
    _bound_server = server


def start_hosted_room_service():
    """Start one process-owned hosted room service idempotently."""

    global _service
    if _bound_server is None:
        return None
    from gateway.hosted_rooms import default_db_path
    from tui_gateway.hosted_room_service import HostedRoomService

    db_path = default_db_path()
    with _service_lock:
        if _service is not None and _service.db_path != db_path:
            _service.stop(timeout=1.0)
            _service = None
        if _service is None:
            _service = HostedRoomService(_bound_server, db_path=db_path)
        _service.start()
        return _service


def stop_hosted_room_service(*, timeout: float = 5.0) -> bool:
    """Stop the process-owned worker without interrupting accepted turns."""

    global _service
    with _service_lock:
        service = _service
        _service = None
    return True if service is None else service.stop(timeout=timeout)


def get_hosted_room_service():
    """Return the active service, if its lifecycle owner started it."""

    return _service


@method("groups.capabilities")
def _(rid, params: dict) -> dict:
    """Describe the hosted-room protocol implemented by this gateway."""
    from gateway.hosted_rooms import (
        MAX_LOG_LIMIT,
        PROTOCOL_VERSION,
        local_authority_gateway_id,
    )

    service = get_hosted_room_service()
    driver_ready = bool(service and service.runtime.status()["running"])
    return _ok(
        rid,
        {
            "protocol_version": PROTOCOL_VERSION,
            "driver": driver_ready,
            "persistent_process": os.getenv("HERMES_DESKTOP") != "1",
            "authority_gateway_id": local_authority_gateway_id(),
            "features": [
                "authority_epoch",
                "coordinator_fencing",
                "room_identity",
                "monotonic_log",
                "idempotent_send",
                "replayable_disband",
                "typed_events",
                "actor_identity",
            ],
            "methods": [
                "groups.capabilities",
                "groups.list",
                "groups.create",
                "groups.state",
                "groups.send",
                "groups.log",
                "groups.disband",
                "groups.stop",
            ],
            "max_log_limit": MAX_LOG_LIMIT,
        },
    )


@method("groups.list")
def _(rid, params: dict) -> dict:
    """List rooms hosted by this gateway."""
    try:
        from gateway.hosted_rooms import default_db_path, list_rooms

        return _ok(
            rid,
            {
                "rooms": list_rooms(
                    default_db_path(),
                    include_disbanded=params.get("include_disbanded") is True,
                )
            },
        )
    except Exception as exc:
        return _err(rid, 5110, str(exc))


@method("groups.create")
def _(rid, params: dict) -> dict:
    """Create a hosted room idempotently.

    Required params: ``room_id``, ``name``, and ``members``. Authority is
    derived from this gateway's stable install identity, never from the client.
    """
    from gateway.hosted_rooms import (
        HostedRoomError,
        create_room,
        default_db_path,
        local_authority_gateway_id,
    )

    try:
        service = get_hosted_room_service()
        room = (
            service.create_room(
                room_id=params.get("room_id"),
                name=params.get("name"),
                members=params.get("members"),
            )
            if service is not None
            else create_room(
                default_db_path(),
                room_id=params.get("room_id"),
                name=params.get("name"),
                members=params.get("members"),
                authority_gateway_id=local_authority_gateway_id(),
            )
        )
        return _ok(rid, {"room": room})
    except HostedRoomError as exc:
        return _err(rid, 4110, str(exc))
    except Exception as exc:
        return _err(rid, 5111, str(exc))


@method("groups.state")
def _(rid, params: dict) -> dict:
    """Return one hosted room's replay cursor and fenced authority state."""
    from gateway.hosted_rooms import HostedRoomError, default_db_path, room_state

    try:
        room = room_state(
            default_db_path(),
            room_id=params.get("room_id"),
            include_disbanded=params.get("include_disbanded") is True,
        )
        service = get_hosted_room_service()
        return _ok(
            rid,
            {
                "room": room,
                **(
                    {"driver_status": service.status(str(room["room_id"]))}
                    if service is not None and room.get("disbanded_at") is None
                    else {}
                ),
            },
        )
    except HostedRoomError as exc:
        return _err(rid, 4114, str(exc))
    except Exception as exc:
        return _err(rid, 5115, str(exc))


@method("groups.send")
def _(rid, params: dict) -> dict:
    """Append one typed event to a hosted room idempotently.

    Required params: ``room_id``, ``event_id``, and object ``payload``. Only
    inert ``message.user`` events are accepted through this client-facing
    method. The actor is server-owned rather than trusted from params.
    Admission is durable; no Bot turn is started by this slice.
    """
    from gateway.hosted_rooms import HostedRoomError, append_event, default_db_path

    try:
        service = get_hosted_room_service()
        event = (
            service.send(
                room_id=params.get("room_id"),
                event_id=params.get("event_id"),
                payload=params.get("payload"),
            )
            if service is not None
            else append_event(
                default_db_path(),
                room_id=params.get("room_id"),
                event_id=params.get("event_id"),
                kind="message.user",
                actor={"kind": "user", "id": "desktop"},
                payload=params.get("payload"),
            )
        )
        return _ok(
            rid,
            {
                "event": event,
                "accepted": True,
                "driver_started": service is not None,
            },
        )
    except HostedRoomError as exc:
        return _err(rid, 4111, str(exc))
    except Exception as exc:
        return _err(rid, 5112, str(exc))


@method("groups.disband")
def _(rid, params: dict) -> dict:
    """Permanently tombstone a hosted room id."""
    from gateway.hosted_rooms import HostedRoomError, default_db_path, disband_room

    try:
        service = get_hosted_room_service()
        if service is not None:
            service.stop_room(
                str(params.get("room_id") or ""),
                cancel_id=str(params.get("cancel_id") or "room-disbanded"),
            )
        tombstone = disband_room(
            default_db_path(),
            room_id=params.get("room_id"),
        )
        return _ok(rid, {"tombstone": tombstone})
    except HostedRoomError as exc:
        return _err(rid, 4113, str(exc))
    except Exception as exc:
        return _err(rid, 5114, str(exc))


@method("groups.stop")
def _(rid, params: dict) -> dict:
    """Durably cancel queued or running work for one hosted room."""

    service = get_hosted_room_service()
    if service is None:
        return _err(rid, 4115, "hosted room driver is unavailable")
    try:
        count = service.stop_room(
            str(params.get("room_id") or ""),
            cancel_id=str(params.get("cancel_id") or "desktop-stop"),
        )
        return _ok(rid, {"cancelled": count})
    except Exception as exc:
        return _err(rid, 5116, str(exc))


@method("groups.log")
def _(rid, params: dict) -> dict:
    """Return a monotonic room-log delta after ``since_seq``."""
    from gateway.hosted_rooms import HostedRoomError, default_db_path, read_events

    try:
        delta = read_events(
            default_db_path(),
            room_id=params.get("room_id"),
            since_seq=params.get("since_seq", 0),
            limit=params.get("limit", 100),
            include_disbanded=params.get("include_disbanded") is True,
        )
        return _ok(rid, delta)
    except HostedRoomError as exc:
        return _err(rid, 4112, str(exc))
    except Exception as exc:
        return _err(rid, 5113, str(exc))


def register(server) -> None:
    _registry.install(server)
