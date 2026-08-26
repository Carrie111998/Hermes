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


def _profile_name() -> str:
    return (os.getenv("HERMES_PROFILE") or "default").strip() or "default"


def _api_server_key() -> str:
    try:
        from agent.secret_scope import get_secret

        scoped = (get_secret("API_SERVER_KEY", "") or "").strip()
        if scoped:
            return scoped
    except Exception:
        pass
    return (os.getenv("API_SERVER_KEY") or "").strip()


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
    try:
        from gateway.hosted_room_peer import (
            derive_room_grant_secret,
            local_catalog_mapping,
        )

        derive_room_grant_secret(_api_server_key())
        catalog = local_catalog_mapping(
            installation_id=local_authority_gateway_id(),
            protocol_versions=(1,),
            link_modes=("direct", "pull"),
            text=True,
            attachments=False,
        )
        room_link = {
            "enabled": True,
            "profile": _profile_name(),
            "catalog": catalog,
            "endpoint": catalog["endpoint"],
        }
    except Exception:
        room_link = {
            "enabled": False,
            "reason": "gateway_api_key_required",
        }
    return _ok(
        rid,
        {
            "protocol_version": PROTOCOL_VERSION,
            "driver": driver_ready,
            "persistent_process": bool(
                room_link.get("catalog", {}).get("persistent_process", False)
            ),
            "authority_gateway_id": local_authority_gateway_id(),
            "room_link": room_link,
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
                "groups.peer.invite",
                "groups.peer.register",
            ],
            "max_log_limit": MAX_LOG_LIMIT,
        },
    )


@method("groups.peer.invite")
def _(rid, params: dict) -> dict:
    """Mint one target-issued room/profile grant for a prospective home."""
    try:
        from gateway.hosted_room_peer import (
            derive_room_grant_secret,
            issue_room_grant,
            local_catalog_mapping,
        )
        from gateway.hosted_rooms import local_authority_gateway_id

        installation_id = local_authority_gateway_id()
        profile = _profile_name()
        ttl = float(params.get("ttl_seconds", 3600))
        if not 60 <= ttl <= 24 * 60 * 60:
            raise ValueError("ttl_seconds must be between 60 and 86400")
        token = issue_room_grant(
            derive_room_grant_secret(_api_server_key()),
            grant_id=str(params.get("grant_id") or f"grant-{os.urandom(16).hex()}"),
            room_id=str(params.get("room_id") or ""),
            home_install_id=str(params.get("home_install_id") or ""),
            target_install_id=installation_id,
            target_profile=profile,
            ttl_seconds=ttl,
        )
        catalog = local_catalog_mapping(
            installation_id=installation_id,
            protocol_versions=(1,),
            link_modes=("direct", "pull"),
            text=True,
            attachments=False,
        )
        return _ok(
            rid,
            {
                "grant": token,
                "target_profile": profile,
                "catalog": catalog,
                "endpoint": catalog["endpoint"],
            },
        )
    except Exception as exc:
        return _err(rid, 4120, str(exc))


@method("groups.peer.register")
def _(rid, params: dict) -> dict:
    """Register and probe one scoped target route on the room home."""
    service = get_hosted_room_service()
    if service is None:
        return _err(rid, 4121, "hosted room driver is unavailable")
    try:
        from gateway.hosted_room_peer import (
            GatewayRoomCatalog,
            validate_room_link_url,
        )
        from gateway.hosted_rooms import local_authority_gateway_id
        from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient
        from tui_gateway.hosted_room_peer_transport import PeerMemberRoute

        target_url, transport_security = validate_room_link_url(
            params.get("target_url")
        )
        catalog = GatewayRoomCatalog.from_mapping(params.get("catalog"))
        if 1 not in catalog.protocol_versions:
            raise ValueError("target does not support RoomLink protocol v1")
        if "direct" not in catalog.link_modes:
            raise ValueError("target does not support a direct RoomLink")
        target_profile = str(params.get("target_profile") or "")
        grant = str(params.get("grant") or "")
        client = PeerRunsHTTPClient(
            base_url=target_url,
            api_key="",
            receipt_db_path=service.db_path,
        )
        probe = client.probe(grant=grant)
        live_catalog = GatewayRoomCatalog.from_mapping(probe.get("catalog"))
        if live_catalog != catalog:
            raise ValueError("target capability catalog changed during setup")
        if (
            1 not in live_catalog.protocol_versions
            or "direct" not in live_catalog.link_modes
        ):
            raise ValueError("target RoomLink capability is incompatible")
        room_id = str(params.get("room_id") or "")
        home_install_id = local_authority_gateway_id()
        if (
            probe.get("room_id") != room_id
            or probe.get("home_install_id") != home_install_id
            or probe.get("target_profile") != target_profile
        ):
            raise ValueError("room grant scope does not match this route")
        member_id = str(params.get("member_id") or "")
        route = PeerMemberRoute(
            home_install_id=home_install_id,
            member_id=member_id,
            target_install_id=catalog.installation_id,
            target_profile=target_profile,
            capability_digest=catalog.catalog_digest,
            cancellation_scope_id=str(
                params.get("cancellation_scope_id")
                or f"cancel-{params.get('room_id') or ''}"
            ),
            trace_id=str(params.get("trace_id") or f"trace-{os.urandom(16).hex()}"),
            grant=grant,
        )
        service.register_peer_route(
            room_id=room_id,
            member_id=member_id,
            route=route,
            client=client,
            target_url=target_url,
            catalog=catalog,
        )
        return _ok(
            rid,
            {
                "registered": True,
                "mode": "direct",
                "transport_security": transport_security,
                "target_install_id": catalog.installation_id,
                "target_profile": target_profile,
            },
        )
    except Exception as exc:
        return _err(rid, 5120, str(exc))


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
            service.revoke_room_routes(str(params.get("room_id") or ""))
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
