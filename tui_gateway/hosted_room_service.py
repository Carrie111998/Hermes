"""Production coordinator for same-gateway hosted Discussion rooms."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_controls
from gateway import hosted_room_driver as driver
from gateway import hosted_room_links
from gateway import hosted_rooms
from gateway.hosted_room_policy_checkpoint import (
    HostedRoomPolicyCheckpoint,
    PolicySnapshot,
)
from gateway.hosted_room_attachments import (
    AttachmentData,
    HostedRoomAttachmentStore,
    MAX_ATTACHMENTS_PER_MESSAGE,
)
from gateway.hosted_room_artifacts import (
    RoomArtifactOutbox,
    RoomArtifactScope,
    validate_terminal_artifact_manifest,
)
from gateway.hosted_room_peer import (
    GatewayRoomCatalog,
    HostedMemberDispatch,
    PROTOCOL_VERSION,
    attachment_manifest_digest,
)
from tui_gateway.hosted_room_driver import (
    ROOM_SESSION_SOURCE,
    HostedRoomBinding,
    HostedRoomRuntime,
    MemberTransportUnavailable,
)
from tui_gateway.hosted_room_server_rpc import HostedRoomServerRPC
from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient, PeerRunsHTTPError
from tui_gateway.hosted_room_peer_transport import (
    HostedRoomPeerClient,
    PeerHostedRoomTransport,
    PeerMemberRoute,
)


logger = logging.getLogger(__name__)


_HOSTED_ROOM_IDLE_FALLBACK_SECONDS = 5.0
_HOSTED_ROOM_ACTIVE_POLL_SECONDS = 0.25
_HOSTED_ROOM_TERMINAL_GRACE_SECONDS = 30.0


def _hosted_room_turn_timeout_seconds() -> float:
    try:
        agent_timeout = float(os.getenv("HERMES_AGENT_TIMEOUT", "1800"))
    except (TypeError, ValueError):
        agent_timeout = 1800.0
    if agent_timeout <= 0:
        agent_timeout = 1800.0
    return agent_timeout + _HOSTED_ROOM_TERMINAL_GRACE_SECONDS


def _grant_revoke_is_terminal(exc: PeerRunsHTTPError) -> bool:
    """Return whether the peer proves the scoped grant is already unusable."""

    return exc.status_code in {401, 403} and exc.error_code in {
        "invalid_room_grant",
        "room_reauthorization_required",
    }


class _ArtifactRetirementBlocked(RuntimeError):
    """Private peer bytes need a repaired scoped route before retirement."""


class HostedRoomService:
    """Own the hosted Discussion policy and its transport-free worker."""

    def __init__(
        self,
        server: ModuleType,
        *,
        db_path: Path | str | None = None,
        peer_routes: Mapping[tuple[str, str], PeerMemberRoute] | None = None,
        peer_clients: Mapping[Any, HostedRoomPeerClient] | None = None,
        artifact_clock: Callable[[], float] = time.time,
        artifact_retry_min_seconds: float = 1.0,
        artifact_retry_max_seconds: float = 60.0,
    ) -> None:
        self.server = server
        self.db_path = Path(db_path or hosted_rooms.default_db_path())
        hosted_rooms.prune_disbanded_rooms(self.db_path)
        self._policy_lock = threading.RLock()
        self.attachments = HostedRoomAttachmentStore(self.db_path)
        self._pending_actions: dict[tuple[str, str], dict[str, Any]] = {}
        self.policy_checkpoint = HostedRoomPolicyCheckpoint(self.db_path)
        self.rpc = HostedRoomServerRPC(server)
        self._link_load_error = None
        self._peer_route_status: dict[tuple[str, str], str] = {}
        self._persisted_peer_route_keys: set[tuple[str, str]] = set()
        self._artifact_clock = artifact_clock
        self._artifact_retry_min_seconds = max(0.01, float(artifact_retry_min_seconds))
        self._artifact_retry_max_seconds = max(
            self._artifact_retry_min_seconds,
            float(artifact_retry_max_seconds),
        )
        self._prepare_artifact_retry_store()
        self._prepare_disband_fence_store()
        self.peer_routes = {}
        self.peer_clients = {}
        try:
            stored_links, load_errors = hosted_room_links.load_room_links_tolerant(
                self.db_path
            )
            errors = list(load_errors)
            for stored in stored_links:
                if PROTOCOL_VERSION not in stored.catalog.protocol_versions:
                    errors.append(
                        f"{stored.room_id}:{stored.member_id}:protocol-upgrade-required"
                    )
                    continue
                client = PeerRunsHTTPClient(
                    base_url=stored.target_url,
                    api_key="",
                    target_profile=stored.target_profile,
                    receipt_db_path=self.db_path,
                )
                route = PeerMemberRoute(
                    home_install_id=hosted_rooms.local_authority_gateway_id(),
                    member_id=stored.member_id,
                    target_install_id=stored.catalog.installation_id,
                    target_profile=stored.target_profile,
                    capability_digest=stored.catalog.catalog_digest,
                    execution_policy_digest=(
                        stored.catalog.execution_policy.policy_digest
                    ),
                    cancellation_scope_id=stored.cancellation_scope_id,
                    trace_id=stored.trace_id,
                    grant=stored.grant,
                    attachments=stored.catalog.attachments,
                )
                self.peer_routes[(stored.room_id, stored.member_id)] = route
                self.peer_clients[(stored.room_id, stored.member_id)] = client
                self._persisted_peer_route_keys.add(
                    (stored.room_id, stored.member_id)
                )
                self._peer_route_status[(stored.room_id, stored.member_id)] = (
                    stored.status
                )
            if errors:
                self._link_load_error = ",".join(errors)
        except Exception as exc:
            self._link_load_error = str(exc)
        try:
            from gateway import hosted_room_messaging_approvals as approvals

            for action in approvals.list_all_pending_approvals(self.db_path):
                self._pending_actions[
                    (str(action["room_id"]), str(action["member_id"]))
                ] = action
        except Exception as exc:
            self._link_load_error = ",".join(
                value
                for value in (self._link_load_error, f"approval-recovery:{exc}")
                if value
            )
        supplied_routes = dict(peer_routes or {})
        supplied_clients = dict(peer_clients or {})
        self.peer_routes.update(supplied_routes)
        for key, route in supplied_routes.items():
            client = supplied_clients.get(key)
            if client is None:
                client = supplied_clients.get(route.target_install_id)
            if client is not None:
                self.peer_clients[key] = client
        self.runtime = HostedRoomRuntime(
            db_path=self.db_path,
            rooms=self.bindings,
            rpc=self.rpc,
            transport_resolver=self._resolve_member_transport,
            turn_lock=self._turn_lock,
            prepare_room=self.prepare_room,
            prepare_leased_room=self._apply_pending_controls,
            publish_terminal=self.publish_terminal,
            pending_action=self._set_pending_action,
            attachment_loader=self._load_task_attachments,
            poll_interval_seconds=_HOSTED_ROOM_IDLE_FALLBACK_SECONDS,
            active_poll_interval_seconds=_HOSTED_ROOM_ACTIVE_POLL_SECONDS,
            turn_timeout_seconds=_hosted_room_turn_timeout_seconds(),
        )

    @property
    def root(self) -> Path:
        return self.db_path.parent

    def local_profiles(self) -> tuple[str, ...]:
        profiles = {"default"}
        profiles_dir = self.root / "profiles"
        if profiles_dir.is_dir():
            profiles.update(
                path.name for path in profiles_dir.iterdir() if path.is_dir()
            )
        return tuple(sorted(profiles))

    def bindings(self) -> tuple[HostedRoomBinding, ...]:
        local_gateway_id = hosted_rooms.local_authority_gateway_id()
        try:
            from gateway import hosted_room_messaging_approvals as approvals

            approvals.terminalize_unowned_approval_commands(
                self.db_path,
                local_gateway_id=local_gateway_id,
            )
        except Exception as exc:
            logger.warning("Group Chat approval cleanup will retry: %s", exc)
        return tuple(
            HostedRoomBinding(
                room_id=str(room["room_id"]),
                gateway_id=str(room["authority_gateway_id"]),
                authority_epoch=int(room["authority_epoch"]),
            )
            for room in hosted_rooms.list_rooms(self.db_path)
            if str(room["authority_gateway_id"]) == local_gateway_id
        )

    def _owned_room(
        self,
        room_id: str,
        *,
        allow_disbanding: bool = False,
    ) -> dict[str, Any]:
        room = hosted_rooms.room_state(self.db_path, room_id=room_id)
        if not allow_disbanding and self._room_is_disbanding(room_id):
            raise driver.RoomUnavailableError("hosted room is being disbanded")
        if str(room["authority_gateway_id"]) != (
            hosted_rooms.local_authority_gateway_id()
        ):
            raise hosted_rooms.AuthorityConflictError(
                "This Group Chat is managed by another gateway."
            )
        return room

    def _prepare_disband_fence_store(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS hosted_room_disband_fences (
                    room_id TEXT PRIMARY KEY,
                    authority_gateway_id TEXT NOT NULL,
                    authority_epoch INTEGER NOT NULL CHECK (authority_epoch >= 1),
                    started_at REAL NOT NULL
                )"""
            )
            conn.execute(
                """DELETE FROM hosted_room_disband_fences
                    WHERE room_id NOT IN (SELECT room_id FROM hosted_rooms)"""
            )

    def _room_is_disbanding(self, room_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM hosted_room_disband_fences WHERE room_id=?",
                (room_id,),
            ).fetchone()
        return row is not None

    def begin_room_disband(self, room_id: str) -> dict[str, Any]:
        """Persist the no-new-work fence before Stop and grant revocation."""

        with self._policy_lock:
            room = hosted_rooms.room_state(self.db_path, room_id=room_id)
            if str(room["authority_gateway_id"]) != (
                hosted_rooms.local_authority_gateway_id()
            ):
                raise hosted_rooms.AuthorityConflictError(
                    "This Group Chat is managed by another gateway."
                )
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    """SELECT authority_gateway_id, authority_epoch
                         FROM hosted_room_disband_fences WHERE room_id=?""",
                    (room_id,),
                ).fetchone()
                if existing is not None and (
                    str(existing[0]), int(existing[1])
                ) != (
                    str(room["authority_gateway_id"]),
                    int(room["authority_epoch"]),
                ):
                    raise hosted_rooms.AuthorityConflictError(
                        "Group Chat disband fence has stale authority lineage."
                    )
                conn.execute(
                    """INSERT OR IGNORE INTO hosted_room_disband_fences(
                           room_id, authority_gateway_id, authority_epoch, started_at
                       ) VALUES (?, ?, ?, ?)""",
                    (
                        room_id,
                        str(room["authority_gateway_id"]),
                        int(room["authority_epoch"]),
                        time.time(),
                    ),
                )
                conn.commit()
            return room

    @contextlib.contextmanager
    def _turn_lock(self, profile: str) -> Iterator[None]:
        from tools.bot_relay import acquire_turn_lock

        with acquire_turn_lock(self.root, profile):
            yield

    def start(self) -> None:
        self.attachments.reconcile_room_events()
        self.attachments.prune()
        self.runtime.start()

    def stop(self, *, timeout: float = 5.0) -> bool:
        return self.runtime.stop(timeout=timeout)

    def wakeup(self) -> None:
        self.runtime.wakeup()

    def register_peer_route(
        self,
        *,
        room_id: str,
        member_id: str,
        route: PeerMemberRoute,
        client: HostedRoomPeerClient,
        target_url: str | None = None,
        catalog: GatewayRoomCatalog | None = None,
    ) -> None:
        """Persist and publish one verified route with its scoped grant."""
        if target_url is None or catalog is None:
            raise ValueError("peer route persistence identity is required")
        bind_store = getattr(client, "bind_receipt_store", None)
        if callable(bind_store):
            bind_store(self.db_path)
        if catalog is not None:
            if not route.execution_policy_digest:
                route = replace(
                    route,
                    execution_policy_digest=(
                        catalog.execution_policy.policy_digest
                    ),
                )
            if (
                route.capability_digest != catalog.catalog_digest
                or route.execution_policy_digest
                != catalog.execution_policy.policy_digest
            ):
                raise ValueError("peer route does not match its target catalog")
        hosted_room_links.save_room_link(
            self.db_path,
            hosted_room_links.make_stored_link(
                room_id=room_id,
                member_id=member_id,
                target_url=target_url,
                target_profile=route.target_profile,
                grant=route.grant,
                catalog=catalog,
                cancellation_scope_id=route.cancellation_scope_id,
                trace_id=route.trace_id,
            ),
        )
        # Persistence is the publication boundary. A failed disk write must
        # never leave a process-local route that disappears after restart.
        with self._policy_lock:
            key = (room_id, member_id)
            if hosted_rooms.room_link_retirement_started(
                self.db_path,
                room_id=room_id,
            ):
                raise hosted_rooms.HostedRoomError(
                    "Group Chat route registration is fenced"
                )
            self.peer_routes[key] = route
            self.peer_clients[key] = client
            self._peer_route_status[key] = "ready"
            self._persisted_peer_route_keys.add(key)
        self._unblock_artifact_retries(room_id, member_id)
        self.runtime.wakeup()

    def revoke_room_routes(self, room_id: str) -> int:
        """Revoke and forget every scoped peer route for one room.

        The remote revocation is the boundary: if a target is unreachable the
        room remains intact and the user may retry rather than receiving a
        false successful disband while a grant is still live.
        """
        with self._policy_lock:
            try:
                room = hosted_rooms.room_state(
                    self.db_path,
                    room_id=room_id,
                    include_disbanded=True,
                )
            except hosted_rooms.RoomNotFoundError:
                authority_gateway_id = hosted_rooms.local_authority_gateway_id()
                authority_epoch = 1
            else:
                authority_gateway_id = str(room["authority_gateway_id"])
                authority_epoch = int(room["authority_epoch"])
            hosted_rooms.begin_room_link_retirement(
                self.db_path,
                room_id=room_id,
                authority_gateway_id=authority_gateway_id,
                authority_epoch=authority_epoch,
            )
            links, errors = hosted_room_links.load_room_links_tolerant(self.db_path)
            if any(error.startswith(f"{room_id}:") for error in errors):
                raise RuntimeError("persisted peer room routes need repair")
            for stored in links:
                if stored.room_id == room_id:
                    self._hydrate_persisted_peer_route(room_id, stored.member_id)
            routes = [
                (key, route)
                for key, route in self.peer_routes.items()
                if key[0] == room_id
            ]
        with sqlite3.connect(self.db_path) as conn:
            table = conn.execute(
                """SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='hosted_room_links'"""
            ).fetchone()
            persisted_keys = (
                {
                    (room_id, str(row[0]))
                    for row in conn.execute(
                        "SELECT member_id FROM hosted_room_links WHERE room_id=?",
                        (room_id,),
                    ).fetchall()
                }
                if table is not None
                else set()
            )
        loaded_keys = {key for key, _route in routes}
        if not persisted_keys.issubset(loaded_keys):
            raise RuntimeError(
                "peer room links need repair before their grants can be revoked"
            )
        for key, route in routes:
            client = self.peer_clients.get(key)
            revoke = getattr(client, "revoke_grant", None)
            if not callable(revoke):
                raise RuntimeError("peer room grant cannot be revoked safely")
            try:
                revoke(grant=route.grant)
            except PeerRunsHTTPError as exc:
                if not _grant_revoke_is_terminal(exc):
                    raise

        hosted_rooms.complete_room_link_retirement(
            self.db_path,
            room_id=room_id,
            authority_gateway_id=authority_gateway_id,
            authority_epoch=authority_epoch,
        )
        hosted_rooms.delete_room_link_records(self.db_path, room_id=room_id)
        with self._policy_lock:
            for key, route in routes:
                self.peer_routes.pop(key, None)
                self._peer_route_status.pop(key, None)
                self.peer_clients.pop(key, None)
                self._persisted_peer_route_keys.discard(key)
        return len(routes)

    def _hydrate_persisted_peer_route(
        self,
        room_id: str,
        member_id: str,
    ) -> tuple[PeerMemberRoute, HostedRoomPeerClient] | None:
        """Hydrate or refresh one exact route persisted by another process."""

        key = (room_id, member_id)
        with self._policy_lock:
            route = self.peer_routes.get(key)
            client = self.peer_clients.get(key)
            if route is not None and client is not None and not isinstance(
                client, PeerRunsHTTPClient
            ):
                return route, client
            try:
                stored = hosted_room_links.load_room_link(
                    self.db_path,
                    room_id=room_id,
                    member_id=member_id,
                )
            except Exception as exc:
                self.peer_routes.pop(key, None)
                self.peer_clients.pop(key, None)
                self._peer_route_status[key] = "needs_reauthorization"
                raise RuntimeError("persisted peer room routes need repair") from exc
            if stored is None:
                if key in self._persisted_peer_route_keys:
                    self.peer_routes.pop(key, None)
                    self.peer_clients.pop(key, None)
                    self._peer_route_status.pop(key, None)
                    self._persisted_peer_route_keys.discard(key)
                    return None
                return (
                    (route, client)
                    if route is not None and client is not None
                    else None
                )
            if PROTOCOL_VERSION not in stored.catalog.protocol_versions:
                raise RuntimeError("persisted peer room route needs a protocol update")
            if (
                route is not None
                and isinstance(client, PeerRunsHTTPClient)
                and route.grant == stored.grant
                and route.target_install_id == stored.catalog.installation_id
                and route.target_profile == stored.target_profile
                and route.capability_digest == stored.catalog.catalog_digest
                and route.execution_policy_digest
                == stored.catalog.execution_policy.policy_digest
                and route.cancellation_scope_id == stored.cancellation_scope_id
                and route.trace_id == stored.trace_id
                and client.base_url == stored.target_url
            ):
                self._peer_route_status[key] = stored.status
                return route, client
            client = PeerRunsHTTPClient(
                base_url=stored.target_url,
                api_key="",
                target_profile=stored.target_profile,
                receipt_db_path=self.db_path,
            )
            route = PeerMemberRoute(
                home_install_id=hosted_rooms.local_authority_gateway_id(),
                member_id=stored.member_id,
                target_install_id=stored.catalog.installation_id,
                target_profile=stored.target_profile,
                capability_digest=stored.catalog.catalog_digest,
                execution_policy_digest=stored.catalog.execution_policy.policy_digest,
                cancellation_scope_id=stored.cancellation_scope_id,
                trace_id=stored.trace_id,
                grant=stored.grant,
            )
            self.peer_routes[key] = route
            self.peer_clients[key] = client
            self._peer_route_status[key] = stored.status
            self._persisted_peer_route_keys.add(key)
            return route, client

    def retire_room_artifacts(self, room_id: str) -> None:
        """Delete every local private output and retry before room tombstoning."""

        with self._policy_lock:
            for task in driver.list_tasks(self.db_path, room_id=room_id):
                self._discard_cancelled_task_artifacts(room_id, task)
            with self._artifact_retry_connection() as conn:
                conn.execute(
                    "DELETE FROM hosted_room_artifact_retries WHERE room_id=?",
                    (room_id,),
                )

    def retire_and_disband_room(
        self,
        room_id: str,
        *,
        expected_gateway_id: str,
        expected_epoch: int,
    ) -> dict[str, Any]:
        """Serialize private-byte retirement with the durable tombstone."""

        with self._policy_lock:
            self.retire_room_artifacts(room_id)
            tombstone = hosted_rooms.disband_room(
                self.db_path,
                room_id=room_id,
                expected_gateway_id=expected_gateway_id,
                expected_epoch=expected_epoch,
            )
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "DELETE FROM hosted_room_disband_fences WHERE room_id=?",
                    (room_id,),
                )
            return tombstone

    def _resolve_member_transport(
        self,
        binding: HostedRoomBinding,
        task: Mapping[str, Any],
    ):
        payload = task.get("payload", {})
        member_id = str(
            payload.get("target_member_id") or payload.get("target_profile") or ""
        )
        key = (binding.room_id, member_id)
        route = self.peer_routes.get(key)
        if route is None and not self._member_is_peer(binding.room_id, member_id):
            return self.rpc
        hydrated = self._hydrate_persisted_peer_route(binding.room_id, member_id)
        route = hydrated[0] if hydrated is not None else self.peer_routes.get(key)
        if route is None:
            raise MemberTransportUnavailable("peer room route is unavailable")
        client = hydrated[1] if hydrated is not None else self.peer_clients.get(key)
        if client is None:
            raise MemberTransportUnavailable("peer room client is unavailable")
        identity = task.get("identity")
        execution_generation = int(task.get("execution_generation") or 0)
        bind_observation = getattr(client, "bind_observation", None)
        if (
            callable(bind_observation)
            and isinstance(identity, driver.TaskIdentity)
            and execution_generation > 0
        ):
            bind_observation(
                task_id=identity.task_id,
                execution_generation=execution_generation,
            )
        tracked_client = self._tracked_peer_client(
            binding.room_id,
            member_id,
            client,
        )
        if task.get("payload", {}).get("attachments"):
            route = self._refresh_peer_attachment_catalog(
                binding.room_id,
                member_id,
                route,
                tracked_client,
            )
            if not route.attachments:
                raise MemberTransportUnavailable(
                    "The target gateway needs an update before it can receive files in this Group Chat."
                )
        self._recover_peer_admission(binding, task, route, tracked_client)
        return PeerHostedRoomTransport(
            binding=binding,
            route=route,
            client=tracked_client,
            source_event_seq=int(payload.get("source_event_seq") or 0),
            task_id=getattr(task.get("identity"), "task_id", None),
            execution_generation=int(task.get("execution_generation") or 0),
        )

    def _tracked_peer_client(
        self,
        room_id: str,
        member_id: str,
        client: HostedRoomPeerClient,
    ) -> "_RouteStatusPeerClient":
        route = self.peer_routes.get((room_id, member_id))
        if route is None:
            raise MemberTransportUnavailable("peer room route is unavailable")
        return _RouteStatusPeerClient(
            client,
            capability_digest=route.capability_digest,
            execution_policy_digest=route.execution_policy_digest,
            on_ready=lambda: self._set_route_status(
                room_id, member_id, "ready"
            ),
            on_reauthorization=lambda: self._set_route_status(
                room_id, member_id, "needs_reauthorization"
            ),
            on_unavailable=lambda: self._set_route_status(
                room_id, member_id, "unavailable"
            ),
            on_refreshed=lambda grant, catalog=None: self._rotate_route_grant(
                room_id, member_id, grant, catalog
            ),
        )

    def _recover_peer_admission(
        self,
        binding: HostedRoomBinding,
        task: Mapping[str, Any],
        route: PeerMemberRoute,
        client: Any,
    ) -> None:
        """Rediscover an admitted peer run without advancing its generation."""
        recover = getattr(client, "recover_dispatch", None)
        identity = task.get("identity")
        payload = task.get("payload")
        execution_generation = int(task.get("execution_generation") or 0)
        if (
            not callable(recover)
            or not isinstance(identity, driver.TaskIdentity)
            or not isinstance(payload, Mapping)
            or execution_generation < 1
            or task.get("status") not in {"indeterminate", "stopping"}
        ):
            return
        prompt = payload.get("prompt")
        source_event_seq = int(payload.get("source_event_seq") or 0)
        if not isinstance(prompt, str) or source_event_seq < 1 or not route.trace_id:
            raise RuntimeError("peer room admission identity is unavailable for recovery")
        attachment_manifest = []
        attachment_payloads = []
        for attachment, data in self._load_task_attachments(binding, task):
            manifest_item = {
                **dict(attachment),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            attachment_manifest.append(manifest_item)
            attachment_payloads.append({**manifest_item, "data": data})
        dispatch = HostedMemberDispatch.from_mapping({
            "protocol_version": PROTOCOL_VERSION,
            "room_id": identity.room_id,
            "home_install_id": route.home_install_id,
            "authority_gateway_id": binding.gateway_id,
            "authority_epoch": binding.authority_epoch,
            "member_id": route.member_id,
            "target_install_id": route.target_install_id,
            "target_profile": route.target_profile,
            "task_id": identity.task_id,
            "execution_generation": execution_generation,
            "source_event_seq": source_event_seq,
            "cancellation_scope_id": route.cancellation_scope_id,
            "prompt": prompt,
            "prompt_digest": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "capability_digest": route.capability_digest,
            "execution_policy_digest": route.execution_policy_digest,
            "trace_id": route.trace_id,
            **(
                {
                    "attachment_manifest_digest": attachment_manifest_digest(
                        attachment_manifest
                    )
                }
                if attachment_manifest
                else {}
            ),
        })
        if attachment_payloads:
            stage = getattr(client, "stage_attachments", None)
            if not callable(stage):
                raise RuntimeError(
                    "The target gateway needs an update before it can receive files in this Group Chat."
                )
            stage(
                dispatch=dispatch.as_mapping(),
                attachments=attachment_payloads,
                grant=route.grant,
            )
        recover(dispatch=dispatch.as_mapping(), grant=route.grant)

    def _member_is_peer(self, room_id: str, member_id: str) -> bool:
        room = hosted_rooms.room_state(
            self.db_path,
            room_id=room_id,
            include_disbanded=True,
        )
        for member in room.get("members") or []:
            if not isinstance(member, Mapping):
                continue
            if str(member.get("member_id") or member.get("profile") or "") != member_id:
                continue
            target = member.get("target")
            return isinstance(target, Mapping) and target.get("kind") == "peer"
        return False

    def _set_route_status(self, room_id: str, member_id: str, status: str) -> None:
        key = (room_id, member_id)
        with self._policy_lock:
            if self._peer_route_status.get(key) == status:
                return
            self._peer_route_status[key] = status
        hosted_room_links.mark_room_link_status(
            self.db_path,
            room_id=room_id,
            member_id=member_id,
            status=status,
        )

    def _set_pending_action(
        self,
        room_id: str,
        member_id: str,
        action: Mapping[str, Any] | None,
    ) -> None:
        from gateway import hosted_room_messaging_approvals as approvals

        key = (room_id, member_id)
        stored_action = (
            {**action, "member_id": member_id} if action is not None else None
        )
        if stored_action is not None and stored_action.get("kind") in {
            "approval",
            "approval_clear",
        }:
            profile = ""
            try:
                room = hosted_rooms.room_state(self.db_path, room_id=room_id)
            except hosted_rooms.RoomNotFoundError:
                approvals.clear_pending_approval(
                    self.db_path,
                    room_id=room_id,
                    member_id=member_id,
                )
                with self._policy_lock:
                    self._pending_actions.pop(key, None)
                return
            reported_gateway_id = str(
                stored_action.get("authority_gateway_id")
                or room.get("authority_gateway_id")
                or ""
            )
            reported_epoch = int(
                stored_action.get("authority_epoch")
                or room.get("authority_epoch")
                or 0
            )
            reported_observer = str(
                stored_action.get("observer_generation") or "legacy"
            )
            reported_lease_generation = int(
                stored_action.get("observer_lease_generation") or 0
            )
            if reported_observer != "legacy":
                lease = self.runtime._leases.get(room_id)
                if (
                    lease is None
                    or lease.gateway_id != reported_gateway_id
                    or lease.authority_epoch != reported_epoch
                    or lease.process_generation != reported_observer
                    or lease.lease_generation != reported_lease_generation
                ):
                    return
                try:
                    driver.require_active_lease(
                        self.db_path,
                        lease,
                        clock=self.runtime.clock,
                    )
                except driver.StaleLeaseError:
                    return
            if (
                reported_gateway_id != str(room["authority_gateway_id"])
                or reported_epoch != int(room["authority_epoch"])
            ):
                is_clear = stored_action.get("kind") == "approval_clear"
                approvals.clear_pending_approval(
                    self.db_path,
                    room_id=room_id,
                    member_id=member_id,
                    request_id=(
                        None if is_clear else stored_action.get("request_id")
                    ),
                    authority_gateway_id=reported_gateway_id,
                    authority_epoch=reported_epoch,
                )
                with self._policy_lock:
                    current = self._pending_actions.get(key)
                    same_authority = str(
                        (current or {}).get("authority_gateway_id") or ""
                    ) == reported_gateway_id and int(
                        (current or {}).get("authority_epoch") or 0
                    ) == reported_epoch
                    same_request = str((current or {}).get("request_id") or "") == str(
                        stored_action.get("request_id") or ""
                    )
                    if same_authority and (is_clear or same_request):
                        self._pending_actions.pop(key, None)
                return
            stored_action["authority_gateway_id"] = reported_gateway_id
            stored_action["authority_epoch"] = reported_epoch
            stored_action["observer_generation"] = reported_observer
            stored_action["observer_lease_generation"] = reported_lease_generation
            if stored_action.get("kind") == "approval_clear":
                with self._policy_lock:
                    current = self._pending_actions.get(key)
                if (
                    not isinstance(current, Mapping)
                    or str(current.get("authority_gateway_id") or "")
                    != reported_gateway_id
                    or int(current.get("authority_epoch") or 0) != reported_epoch
                    or str(current.get("task_id") or "")
                    != str(stored_action.get("task_id") or "")
                    or int(current.get("execution_generation") or 0)
                    != int(stored_action.get("execution_generation") or 0)
                    or str(current.get("session_id") or "")
                    != str(stored_action.get("session_id") or "")
                ):
                    return
                try:
                    approvals.clear_pending_approval(
                        self.db_path,
                        room_id=room_id,
                        member_id=member_id,
                        request_id=current.get("request_id"),
                        authority_gateway_id=reported_gateway_id,
                        authority_epoch=reported_epoch,
                        observer_generation=reported_observer,
                        observer_lease_generation=reported_lease_generation,
                    )
                except approvals.MessagingApprovalObservationStale:
                    return
                with self._policy_lock:
                    if self._pending_actions.get(key) == current:
                        self._pending_actions.pop(key, None)
                return
            for member in room.get("members") or []:
                if not isinstance(member, Mapping):
                    continue
                if str(member.get("member_id") or member.get("profile") or "") != member_id:
                    continue
                target = member.get("target")
                profile = str(
                    (target.get("profile") if isinstance(target, Mapping) else "")
                    or member.get("profile")
                    or ""
                )
                break
            stored_action["profile"] = profile
        changed = False
        previous_action: Mapping[str, Any] | None = None
        with self._policy_lock:
            current_action = self._pending_actions.get(key)
            if stored_action is None:
                if current_action is not None:
                    changed = True
                    previous_action = dict(current_action)
            elif current_action != stored_action:
                previous_action = (
                    dict(current_action) if current_action is not None else None
                )
                self._pending_actions[key] = stored_action
                changed = True
        if stored_action is None and changed:
            approvals.clear_pending_approval(
                self.db_path,
                room_id=room_id,
                member_id=member_id,
                request_id=(previous_action or {}).get("request_id"),
                authority_gateway_id=(previous_action or {}).get(
                    "authority_gateway_id"
                ),
                authority_epoch=(previous_action or {}).get("authority_epoch"),
            )
            with self._policy_lock:
                if self._pending_actions.get(key) == previous_action:
                    self._pending_actions.pop(key, None)
        elif changed and stored_action.get("kind") == "approval":
            try:
                approvals.persist_pending_approval(
                    self.db_path,
                    room_id=room_id,
                    member_id=member_id,
                    action=stored_action,
                )
            except Exception as exc:
                with self._policy_lock:
                    if self._pending_actions.get(key) == stored_action:
                        if previous_action is None:
                            self._pending_actions.pop(key, None)
                        else:
                            self._pending_actions[key] = dict(previous_action)
                if isinstance(exc, approvals.MessagingApprovalObservationStale):
                    return
                raise
        if stored_action is not None and stored_action.get("kind") == "approval":
            binding = next(
                (
                    candidate
                    for candidate in self.bindings()
                    if candidate.room_id == room_id
                ),
                None,
            )
            if binding is not None:
                self._apply_pending_control_approvals(binding)

    def _rotate_route_grant(
        self,
        room_id: str,
        member_id: str,
        grant: str,
        catalog: GatewayRoomCatalog | None = None,
    ) -> None:
        """Persist a target-refreshed scoped grant before publishing it live."""
        key = (room_id, member_id)
        route = self.peer_routes.get(key)
        if route is None:
            raise RuntimeError("peer room route is unavailable")
        stored = hosted_room_links.load_room_link(
            self.db_path,
            room_id=room_id,
            member_id=member_id,
        )
        if stored is None:
            raise RuntimeError("peer room route cannot be renewed before persistence")
        effective_catalog = catalog or stored.catalog
        if catalog is not None and (
            catalog.installation_id != route.target_install_id
            or catalog.execution_policy.target_profile != route.target_profile
            or PROTOCOL_VERSION not in catalog.protocol_versions
            or "direct" not in catalog.link_modes
            or not catalog.text
            or catalog.execution_policy.policy_digest
            != route.execution_policy_digest
        ):
            self._set_route_status(room_id, member_id, "needs_reauthorization")
            raise RuntimeError(
                "peer room execution policy changed; reauthorization is required"
            )
        rotated_route = replace(
            route,
            grant=grant,
            capability_digest=(
                catalog.catalog_digest
                if catalog is not None
                else route.capability_digest
            ),
            execution_policy_digest=(
                catalog.execution_policy.policy_digest
                if catalog is not None
                else route.execution_policy_digest
            ),
        )
        hosted_room_links.save_room_link(
            self.db_path,
            hosted_room_links.make_stored_link(
                room_id=room_id,
                member_id=member_id,
                target_url=stored.target_url,
                target_profile=stored.target_profile,
                grant=grant,
                catalog=effective_catalog,
                cancellation_scope_id=stored.cancellation_scope_id,
                trace_id=stored.trace_id,
            ),
        )
        with self._policy_lock:
            self.peer_routes[key] = rotated_route
            self._peer_route_status[key] = "ready"
        self._unblock_artifact_retries(room_id, member_id)

    def _refresh_peer_attachment_catalog(
        self,
        room_id: str,
        member_id: str,
        route: PeerMemberRoute,
        client: Any,
    ) -> PeerMemberRoute:
        """Re-probe binary support so upgrades and downgrades fail truthfully."""

        probe = getattr(client, "probe", None)
        if not callable(probe):
            return route
        result = probe(grant=route.grant)
        catalog = GatewayRoomCatalog.from_mapping(result.get("catalog"))
        if (
            catalog.installation_id != route.target_install_id
            or not catalog.persistent_process
            or not catalog.text
        ):
            raise RuntimeError("peer room capability identity changed")
        key = (room_id, member_id)
        stored = next(
            (
                link
                for link in hosted_room_links.load_room_links(self.db_path)
                if (link.room_id, link.member_id) == key
            ),
            None,
        )
        if stored is None:
            # Explicitly supplied in-process routes are valid for tests and
            # ephemeral callers, but cannot publish a refreshed catalog.
            return replace(
                route,
                capability_digest=catalog.catalog_digest,
                attachments=catalog.attachments,
            )
        refreshed = replace(
            route,
            capability_digest=catalog.catalog_digest,
            attachments=catalog.attachments,
        )
        if (
            stored.catalog.catalog_digest != catalog.catalog_digest
            or stored.catalog.attachments != catalog.attachments
        ):
            hosted_room_links.save_room_link(
                self.db_path,
                hosted_room_links.make_stored_link(
                    room_id=room_id,
                    member_id=member_id,
                    target_url=stored.target_url,
                    target_profile=stored.target_profile,
                    grant=route.grant,
                    catalog=catalog,
                    cancellation_scope_id=stored.cancellation_scope_id,
                    trace_id=stored.trace_id,
                ),
            )
            with self._policy_lock:
                self.peer_routes[key] = refreshed
                self._peer_route_status[key] = "ready"
        return refreshed

    def _route_statuses(self, room_id: str | None = None) -> list[dict[str, str]]:
        with self._policy_lock:
            rows = [
                {
                    "room_id": key[0],
                    "member_id": key[1],
                    "status": status,
                }
                for key, status in self._peer_route_status.items()
                if room_id is None or key[0] == room_id
            ]
        return sorted(rows, key=lambda row: (row["room_id"], row["member_id"]))

    def _events(self, room_id: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        cursor = 0
        while True:
            page = hosted_rooms.read_events(
                self.db_path,
                room_id=room_id,
                since_seq=cursor,
                limit=hosted_rooms.MAX_LOG_LIMIT,
            )
            rows = page.get("events")
            if isinstance(rows, list):
                events.extend(row for row in rows if isinstance(row, dict))
            next_cursor = int(page.get("cursor") or cursor)
            if not page.get("has_more"):
                return events
            if next_cursor <= cursor:
                raise RuntimeError("hosted room replay cursor did not advance")
            cursor = next_cursor

    def _append_plan(self, room_id: str, plan: discussion.PublicationPlan) -> None:
        for event in plan.events:
            hosted_rooms.append_event(
                self.db_path,
                **event.append_kwargs(room_id),
            )

    def _policy_snapshot(self, room: Mapping[str, Any]) -> PolicySnapshot:
        return self.policy_checkpoint.snapshot(
            room_id=str(room["room_id"]),
            latest_seq=int(room["latest_seq"]),
        )

    def _load_task_attachments(
        self,
        binding: HostedRoomBinding,
        task: Mapping[str, Any],
    ) -> Iterator[tuple[Mapping[str, Any], bytes]]:
        """Resolve task manifests to verified bytes for one frozen room member."""

        manifests = task.get("payload", {}).get("attachments") or []
        if not manifests:
            return
        room = hosted_rooms.room_state(self.db_path, room_id=binding.room_id)
        target_member_id = str(
            task.get("payload", {}).get("target_member_id") or ""
        )
        profile = str(task.get("payload", {}).get("target_profile") or "")
        member = next(
            (
                candidate
                for candidate in room["members"]
                if (
                    str(candidate.get("member_id") or "") == target_member_id
                    if target_member_id
                    else str(candidate.get("profile") or "") == profile
                )
            ),
            None,
        )
        if member is None:
            raise RuntimeError("hosted attachment target is not in the frozen roster")
        member_id = str(member.get("member_id") or "")
        for manifest in manifests:
            stored = self.attachments.read(
                room_id=binding.room_id,
                attachment_id=manifest.get("attachment_id"),
                recipient_member_id=member_id,
            )
            safe = {
                "attachment_id": stored.attachment["attachment_id"],
                "kind": stored.attachment["kind"],
                "name": stored.attachment["name"],
                "size": stored.attachment["size"],
                "mime": stored.attachment["mime"],
            }
            if safe != dict(manifest):
                raise RuntimeError(
                    "hosted attachment metadata changed after task admission"
                )
            yield safe, stored.data

    def _publish_terminal_tasks(
        self,
        room: Mapping[str, Any],
    ) -> bool:
        changed = False
        local_profiles = self.local_profiles()
        retry_keys = self._artifact_retry_keys(str(room["room_id"]))
        for status in ("deferred", "settled", "failed", "cancelled"):
            for task in driver.list_tasks(
                self.db_path,
                room_id=str(room["room_id"]),
                status=status,
            ):
                identity = task["identity"]
                publication_exists = self.policy_checkpoint.publication_exists(
                    room_id=str(room["room_id"]),
                    task_id=identity.task_id,
                    status=status,
                    execution_generation=int(task["execution_generation"]),
                )
                retry_key = self._artifact_retry_key(task)
                result = task.get("result")
                has_artifact_ack = (
                    isinstance(result, Mapping) and bool(result.get("artifacts"))
                )
                if (
                    publication_exists
                    and retry_key not in retry_keys
                    and not has_artifact_ack
                ):
                    continue
                task_events = self.policy_checkpoint.events_for_task(
                    room_id=str(room["room_id"]),
                    source_event_seq=int(task["payload"]["source_event_seq"]),
                )
                if status == "cancelled":
                    self._discard_cancelled_task_artifacts(
                        str(room["room_id"]),
                        task,
                    )
                if not self._artifact_retry_due(task):
                    continue
                publication_status = status
                if publication_exists and has_artifact_ack:
                    plan = self._published_artifact_plan(
                        room,
                        task,
                        local_profiles=local_profiles,
                    )
                else:
                    plan = discussion.reconstruct_task_plan(
                        room,
                        task_events,
                        task,
                        local_profiles=local_profiles,
                    )
                try:
                    result, acknowledge_artifacts = self._import_terminal_artifacts(
                        room=room,
                        task=task,
                        plan=plan,
                        events=task_events,
                    )
                except Exception as exc:
                    if bool(getattr(exc, "retryable", False)) or isinstance(
                        exc, (ConnectionError, OSError, TimeoutError)
                    ):
                        self._defer_artifact_retry(task, exc)
                        continue
                    if isinstance(exc, _ArtifactRetirementBlocked):
                        self._defer_artifact_retry(task, exc, permanent=True)
                        continue
                    try:
                        self._retire_failed_terminal_artifacts(
                            room=room,
                            task=task,
                            plan=plan,
                        )
                    except Exception as retirement_exc:
                        self._defer_artifact_retry(
                            task,
                            retirement_exc,
                            permanent=not (
                                bool(getattr(retirement_exc, "retryable", False))
                                or isinstance(
                                    retirement_exc,
                                    (ConnectionError, OSError, TimeoutError),
                                )
                            ),
                        )
                        continue
                    result = {
                        "error": "A Group Chat file could not be verified.",
                        "reason_code": "artifact_verification_failed",
                    }
                    acknowledge_artifacts = lambda: None
                    publication_status = "failed"
                if publication_exists:
                    if isinstance(result, Mapping) and result.get("attachments"):
                        digest = plan.identity.task_id.removeprefix("dtask:")
                        self.attachments.retain_event(
                            room_id=str(room["room_id"]),
                            event_id=f"dmessage:{digest}",
                        )
                        try:
                            acknowledge_artifacts()
                        except Exception as exc:
                            self._defer_artifact_retry(
                                task,
                                exc,
                                permanent=not (
                                    bool(getattr(exc, "retryable", False))
                                    or isinstance(exc, (ConnectionError, OSError, TimeoutError))
                                ),
                            )
                            continue
                    self._clear_artifact_retry(task)
                    continue
                publication = discussion.plan_publication(
                    room,
                    task_events,
                    plan,
                    status=publication_status,
                    result=result,
                    execution_generation=(
                        int(task["execution_generation"])
                        if status == "deferred"
                        else None
                    ),
                    local_profiles=local_profiles,
                )
                self._append_plan(str(room["room_id"]), publication)
                if isinstance(result, Mapping) and result.get("attachments"):
                    digest = plan.identity.task_id.removeprefix("dtask:")
                    self.attachments.retain_event(
                        room_id=str(room["room_id"]),
                        event_id=f"dmessage:{digest}",
                    )
                    try:
                        acknowledge_artifacts()
                    except Exception as exc:
                        if bool(getattr(exc, "retryable", False)) or isinstance(
                            exc, (ConnectionError, OSError, TimeoutError)
                        ):
                            self._defer_artifact_retry(task, exc)
                            continue
                        self._defer_artifact_retry(task, exc, permanent=True)
                        continue
                self._clear_artifact_retry(task)
                changed = True
        return changed

    @staticmethod
    def _published_artifact_plan(
        room: Mapping[str, Any],
        task: Mapping[str, Any],
        *,
        local_profiles: Iterable[str],
    ) -> discussion.DiscussionTaskPlan:
        """Recover the immutable artifact scope after policy compaction.

        A published member message no longer needs its source prompt to be
        reconstructed. The private artifact acknowledgement can outlive that
        bounded policy projection, so derive only its frozen target from the
        admitted task and room roster.
        """

        validated_room = discussion.validate_room(
            room,
            local_profiles=local_profiles,
        )
        identity = task.get("identity")
        payload = task.get("payload")
        if not isinstance(identity, driver.TaskIdentity) or not isinstance(
            payload, Mapping
        ):
            raise discussion.DiscussionReconstructionError(
                "published artifact task shape changed"
            )
        target_member_id = str(payload.get("target_member_id") or "")
        target_profile = str(payload.get("target_profile") or "")
        member_index = next(
            (
                index
                for index, member in enumerate(validated_room.members)
                if (
                    member.member_id == target_member_id
                    if target_member_id
                    else member.profile == target_profile
                )
            ),
            -1,
        )
        if member_index < 0:
            raise discussion.DiscussionReconstructionError(
                "published artifact target is no longer in the frozen roster"
            )
        return discussion.DiscussionTaskPlan(
            identity=identity,
            payload=payload,
            discussion_event_id="",
            member=validated_room.members[member_index],
            member_index=member_index,
            round_index=0,
            seen_through_seq=0,
        )

    def _artifact_retry_connection(self) -> sqlite3.Connection:
        from hermes_state import apply_wal_with_fallback

        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        apply_wal_with_fallback(conn, db_label="state.db (room artifact retries)")
        return conn

    def _prepare_artifact_retry_store(self) -> None:
        with self._artifact_retry_connection() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS hosted_room_artifact_retries (
                    room_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    execution_generation INTEGER NOT NULL,
                    member_id TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    next_attempt_at REAL NOT NULL,
                    blocked INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(room_id, task_id, execution_generation)
                )"""
            )
            columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(hosted_room_artifact_retries)"
                ).fetchall()
            }
            if "member_id" not in columns:
                conn.execute(
                    """ALTER TABLE hosted_room_artifact_retries
                       ADD COLUMN member_id TEXT NOT NULL DEFAULT ''"""
                )
            empty = conn.execute(
                """SELECT room_id, task_id, execution_generation
                   FROM hosted_room_artifact_retries WHERE member_id=''"""
            ).fetchall()
            has_driver_tasks = conn.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='hosted_room_driver_tasks'"""
            ).fetchone()
            if has_driver_tasks is None:
                return
            for row in empty:
                task = conn.execute(
                    """SELECT payload_json FROM hosted_room_driver_tasks
                       WHERE room_id=? AND task_id=?""",
                    (row["room_id"], row["task_id"]),
                ).fetchone()
                if task is None:
                    continue
                try:
                    payload = json.loads(task["payload_json"])
                    member_id = str(
                        payload.get("target_member_id")
                        or payload.get("target_profile")
                        or ""
                    )
                except Exception:
                    continue
                if member_id:
                    conn.execute(
                        """UPDATE hosted_room_artifact_retries SET member_id=?
                           WHERE room_id=? AND task_id=? AND execution_generation=?""",
                        (
                            member_id,
                            row["room_id"],
                            row["task_id"],
                            row["execution_generation"],
                        ),
                    )

    @staticmethod
    def _artifact_retry_key(task: Mapping[str, Any]) -> tuple[str, str, int]:
        identity = task["identity"]
        return (
            identity.room_id,
            identity.task_id,
            int(task["execution_generation"]),
        )

    def _artifact_retry_keys(self, room_id: str) -> set[tuple[str, str, int]]:
        with self._artifact_retry_connection() as conn:
            rows = conn.execute(
                """SELECT room_id, task_id, execution_generation
                   FROM hosted_room_artifact_retries WHERE room_id=?""",
                (room_id,),
            ).fetchall()
        return {
            (str(row["room_id"]), str(row["task_id"]), int(row["execution_generation"]))
            for row in rows
        }

    def _artifact_retry_due(self, task: Mapping[str, Any]) -> bool:
        key = self._artifact_retry_key(task)
        with self._artifact_retry_connection() as conn:
            row = conn.execute(
                """SELECT next_attempt_at, blocked FROM hosted_room_artifact_retries
                   WHERE room_id=? AND task_id=? AND execution_generation=?""",
                key,
            ).fetchone()
        return row is None or (
            not bool(row["blocked"])
            and self._artifact_clock() >= float(row["next_attempt_at"])
        )

    def _defer_artifact_retry(
        self,
        task: Mapping[str, Any],
        _exc: Exception,
        *,
        permanent: bool = False,
    ) -> None:
        key = self._artifact_retry_key(task)
        now = float(self._artifact_clock())
        with self._artifact_retry_connection() as conn:
            row = conn.execute(
                """SELECT attempts FROM hosted_room_artifact_retries
                   WHERE room_id=? AND task_id=? AND execution_generation=?""",
                key,
            ).fetchone()
            attempts = 1 if row is None else int(row["attempts"]) + 1
            delay = min(
                self._artifact_retry_max_seconds,
                self._artifact_retry_min_seconds * (2 ** min(attempts - 1, 16)),
            )
            conn.execute(
                """INSERT INTO hosted_room_artifact_retries
                   (room_id, task_id, execution_generation, member_id, attempts,
                    next_attempt_at, blocked, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(room_id, task_id, execution_generation) DO UPDATE SET
                    attempts=excluded.attempts,
                    next_attempt_at=excluded.next_attempt_at,
                    blocked=excluded.blocked,
                    updated_at=excluded.updated_at""",
                (
                    *key,
                    str(
                        task.get("payload", {}).get("target_member_id")
                        or task.get("payload", {}).get("target_profile")
                        or "unknown"
                    ),
                    attempts,
                    now + delay,
                    int(permanent),
                    now,
                ),
            )

    def _clear_artifact_retry(self, task: Mapping[str, Any]) -> None:
        with self._artifact_retry_connection() as conn:
            conn.execute(
                """DELETE FROM hosted_room_artifact_retries
                   WHERE room_id=? AND task_id=? AND execution_generation=?""",
                self._artifact_retry_key(task),
            )

    def _unblock_artifact_retries(self, room_id: str, member_id: str) -> None:
        with self._artifact_retry_connection() as conn:
            conn.execute(
                """UPDATE hosted_room_artifact_retries
                      SET blocked=0, next_attempt_at=0, updated_at=?
                    WHERE room_id=? AND member_id=? AND blocked=1""",
                (float(self._artifact_clock()), room_id, member_id),
            )

    def _import_terminal_artifacts(
        self,
        *,
        room: Mapping[str, Any],
        task: Mapping[str, Any],
        plan: discussion.DiscussionTaskPlan,
        events: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | Any, Any]:
        result = task.get("result")
        if not isinstance(result, Mapping) or not result.get("artifacts"):
            return result, lambda: None
        items = validate_terminal_artifact_manifest(result["artifacts"])
        if not items:
            return result, lambda: None
        target = plan.member.target or {"kind": "local", "profile": plan.member.profile}
        local_install = hosted_rooms.local_authority_gateway_id()
        peer_route = None
        peer_client = None
        if target.get("kind") == "peer":
            key = (str(room["room_id"]), plan.member.member_id)
            peer_route = self.peer_routes.get(key)
            peer_client = self.peer_clients.get(key)
            if peer_route is None or peer_client is None:
                raise RuntimeError("peer room output artifact route is unavailable")
            target_install = peer_route.target_install_id
            home_install = peer_route.home_install_id
        elif target.get("kind") == "local":
            target_install = local_install
            home_install = local_install
        else:
            raise RuntimeError("room output artifact target is invalid")
        if peer_client is not None:
            peer_client = self._tracked_peer_client(
                str(room["room_id"]),
                plan.member.member_id,
                peer_client,
            )
        scope = RoomArtifactScope.from_mapping({
            "room_id": room["room_id"],
            "task_id": plan.identity.task_id,
            "execution_generation": int(task["execution_generation"]),
            "member_id": plan.member.member_id,
            "target_profile": plan.member.profile,
            "home_install_id": home_install,
            "target_install_id": target_install,
            "authority_gateway_id": room["authority_gateway_id"],
            "authority_epoch": int(room["authority_epoch"]),
        })
        outbox = None
        if target.get("kind") == "local":
            profile_root = (
                self.root
                if plan.member.profile == "default"
                else self.root / "profiles" / plan.member.profile
            )
            outbox = RoomArtifactOutbox(profile_root / "state.db")
        artifact_ids = tuple(item["artifact_id"] for item in items)
        message_event_id = f"dmessage:{plan.identity.task_id.removeprefix('dtask:')}"
        recipient_ids = tuple(plan.payload.get("recipient_member_ids") or ())
        if not recipient_ids:
            # Pre-file-handoff tasks have no admission-time recipient snapshot.
            # Their text replies remain replayable after upgrade, but publishing
            # a file would otherwise grant bytes using today's room membership.
            # Retire the exact private outbox first, then fail the visible turn.
            if outbox is not None:
                outbox.discard_durably(scope)
            else:
                run_id = str(result.get("run_id") or "")
                if not run_id:
                    raise RuntimeError("peer room output artifact run is missing")
                try:
                    peer_client.acknowledge_artifacts(
                        run_id=run_id,
                        artifact_ids=artifact_ids,
                        manifest_digest=result["artifacts"]["manifest_digest"],
                        message_event_id=message_event_id,
                        grant=peer_route.grant,
                    )
                except Exception as exc:
                    # Do not publish a terminal failure while private bytes are
                    # still live on the peer. Keep the bounded retry active so
                    # route refresh or reconnection can finish retirement.
                    if bool(getattr(exc, "retryable", False)) or isinstance(
                        exc, (ConnectionError, OSError, TimeoutError)
                    ):
                        raise
                    self._set_route_status(
                        str(room["room_id"]),
                        plan.member.member_id,
                        "needs_reauthorization",
                    )
                    raise _ArtifactRetirementBlocked(
                        "peer room output artifact retirement needs a repaired route"
                    ) from exc
            raise RuntimeError("room output artifact recipient roster is missing")
        existing_message = next(
            (
                event
                for event in events
                if event.get("event_id") == message_event_id
                and event.get("kind") == "message.member"
            ),
            None,
        )
        if existing_message is not None:
            existing_attachments = existing_message.get("payload", {}).get("attachments")
            if not isinstance(existing_attachments, list) or not existing_attachments:
                raise RuntimeError("room output artifact publication changed after commit")
            normalized = dict(result)
            normalized["attachments"] = existing_attachments
            if outbox is not None:
                return normalized, lambda: outbox.acknowledge(
                    scope,
                    artifact_ids,
                    message_event_id=message_event_id,
                )
            run_id = str(result.get("run_id") or "")
            return normalized, lambda: peer_client.acknowledge_artifacts(
                run_id=run_id,
                artifact_ids=artifact_ids,
                manifest_digest=result["artifacts"]["manifest_digest"],
                message_event_id=message_event_id,
                grant=peer_route.grant,
            )

        canonical: list[dict[str, Any]] = []
        for item in items:
            if outbox is not None:
                metadata, data = outbox.read(scope, item["artifact_id"])
                if metadata != item:
                    raise RuntimeError(
                        "room output artifact metadata changed before import"
                    )
            else:
                run_id = str(result.get("run_id") or "")
                if not run_id:
                    raise RuntimeError("peer room output artifact run is missing")
                data = peer_client.read_artifact(
                    run_id=run_id,
                    artifact_id=item["artifact_id"],
                    grant=peer_route.grant,
                )
                if (
                    len(data) != item["size"]
                    or hashlib.sha256(data).hexdigest() != item["sha256"]
                ):
                    raise RuntimeError("peer room output artifact failed verification")
            upload_digest = hashlib.sha256(
                (
                    f"{plan.identity.task_id}\0{task['execution_generation']}\0"
                    f"{item['artifact_id']}"
                ).encode()
            ).hexdigest()[:32]
            stored = self.attachments.put(
                room_id=str(room["room_id"]),
                upload_id=f"bot-output:{upload_digest}",
                kind=item["kind"],
                name=item["name"],
                mime=item["mime"],
                data=data,
            )
            if stored["sha256"] != item["sha256"]:
                raise RuntimeError("room output artifact digest changed during import")
            canonical.append({
                key: stored[key]
                for key in ("attachment_id", "kind", "name", "size", "mime")
            })

        digest = plan.identity.task_id.removeprefix("dtask:")
        event_id = f"dmessage:{digest}"
        recipient_ids = tuple(plan.payload.get("recipient_member_ids") or ())
        if not recipient_ids:
            raise RuntimeError("room output artifact recipient roster is missing")
        normalized = dict(result)
        normalized["attachments"] = self.attachments.commit_message(
            room_id=str(room["room_id"]),
            event_id=event_id,
            manifest=canonical,
            recipient_member_ids=recipient_ids,
            viewer_access=True,
            hold_until_event=True,
        )
        if outbox is not None:
            return normalized, lambda: outbox.acknowledge(
                scope,
                artifact_ids,
                message_event_id=message_event_id,
            )
        run_id = str(result.get("run_id") or "")
        return normalized, lambda: peer_client.acknowledge_artifacts(
            run_id=run_id,
            artifact_ids=artifact_ids,
            manifest_digest=result["artifacts"]["manifest_digest"],
            message_event_id=message_event_id,
            grant=peer_route.grant,
        )

    def _retire_failed_terminal_artifacts(
        self,
        *,
        room: Mapping[str, Any],
        task: Mapping[str, Any],
        plan: discussion.DiscussionTaskPlan,
    ) -> None:
        """Confirm exact source-byte retirement before publishing failure."""

        result = task.get("result")
        if not isinstance(result, Mapping) or not result.get("artifacts"):
            return
        target = plan.member.target or {
            "kind": "local",
            "profile": plan.member.profile,
        }
        if target.get("kind") == "local":
            installation = hosted_rooms.local_authority_gateway_id()
            scope = RoomArtifactScope.from_mapping({
                "room_id": room["room_id"],
                "task_id": plan.identity.task_id,
                "execution_generation": int(task["execution_generation"]),
                "member_id": plan.member.member_id,
                "target_profile": plan.member.profile,
                "home_install_id": installation,
                "target_install_id": installation,
                "authority_gateway_id": room["authority_gateway_id"],
                "authority_epoch": int(room["authority_epoch"]),
            })
            profile_root = (
                self.root
                if plan.member.profile == "default"
                else self.root / "profiles" / plan.member.profile
            )
            RoomArtifactOutbox(profile_root / "state.db").discard_durably(scope)
            return
        if target.get("kind") != "peer":
            raise _ArtifactRetirementBlocked(
                "room output artifact target cannot be retired"
            )
        key = (str(room["room_id"]), plan.member.member_id)
        route = self.peer_routes.get(key)
        client = self.peer_clients.get(key)
        run_id = str(result.get("run_id") or "")
        if route is None or client is None or not run_id:
            raise _ArtifactRetirementBlocked(
                "peer room output artifact route needs repair"
            )
        client = self._tracked_peer_client(
            str(room["room_id"]),
            plan.member.member_id,
            client,
        )
        try:
            retirement = client.discard_artifacts(run_id=run_id, grant=route.grant)
            removed = retirement.get("removed") if isinstance(retirement, Mapping) else None
            if (
                not isinstance(retirement, Mapping)
                or retirement.get("discarded") is not True
                or isinstance(removed, bool)
                or not isinstance(removed, int)
                or not 0 <= removed <= MAX_ATTACHMENTS_PER_MESSAGE
            ):
                raise PeerRunsHTTPError(
                    "peer returned an invalid artifact retirement receipt"
                )
        except Exception as exc:
            if bool(getattr(exc, "retryable", False)) or isinstance(
                exc, (ConnectionError, OSError, TimeoutError)
            ):
                raise
            self._set_route_status(
                str(room["room_id"]),
                plan.member.member_id,
                "needs_reauthorization",
            )
            raise _ArtifactRetirementBlocked(
                "peer room output artifact retirement needs a repaired route"
            ) from exc

    def _append_room_status(
        self,
        room: Mapping[str, Any],
        decision: discussion.DiscussionDecision,
    ) -> None:
        if decision.discussion_event_id is None:
            return
        hosted_rooms.append_event(
            self.db_path,
            room_id=str(room["room_id"]),
            event_id=f"dactivity:{decision.discussion_event_id}:{decision.reason}",
            kind="room.activity",
            actor={"kind": "gateway", "id": str(room["authority_gateway_id"])},
            payload={
                "status": decision.status,
                "reason_code": decision.reason,
                "thread_id": decision.thread_id,
                "discussion_event_id": decision.discussion_event_id,
            },
            authority_gateway_id=str(room["authority_gateway_id"]),
            authority_epoch=int(room["authority_epoch"]),
        )

    def prepare_room(self, binding: HostedRoomBinding) -> None:
        with self._policy_lock:
            if self._room_is_disbanding(binding.room_id):
                raise driver.RoomUnavailableError(
                    "hosted room is being disbanded"
                )
            room = hosted_rooms.room_state(self.db_path, room_id=binding.room_id)
            snapshot = self._policy_snapshot(room)
            events = list(snapshot.events)
            if self._publish_terminal_tasks(room):
                room = hosted_rooms.room_state(
                    self.db_path,
                    room_id=binding.room_id,
                )
                snapshot = self._policy_snapshot(room)
                events = list(snapshot.events)
            self.policy_checkpoint.compact_completed(room_id=binding.room_id)
            driver.prune_published_terminal_tasks(
                self.db_path,
                room_id=binding.room_id,
                clock=self.runtime.clock,
            )
            if any(
                driver.list_tasks(
                    self.db_path,
                    room_id=binding.room_id,
                    status=status,
                )
                for status in ("queued", "running", "stopping")
            ):
                return
            decision = discussion.plan_next_task(
                room,
                events,
                local_profiles=self.local_profiles(),
                initial_watermarks=snapshot.watermarks,
            )
            if decision.status == "task" and decision.task is not None:
                driver.admit_task(
                    self.db_path,
                    decision.task.identity,
                    payload=decision.task.payload,
                    clock=time.time,
                )
                # A stop can race the policy read from another process. Re-read
                # after admission and cancel before the runtime can execute a
                # task whose source event is now behind the room stop fence.
                fresh_room = hosted_rooms.room_state(
                    self.db_path,
                    room_id=binding.room_id,
                )
                stopped_through_seq = self._policy_snapshot(
                    fresh_room
                ).stopped_through_seq
                if (
                    decision.source_event_seq is not None
                    and decision.source_event_seq < stopped_through_seq
                ):
                    self.runtime.cancel(
                        decision.task.identity,
                        cancel_id=f"stop-fence:{stopped_through_seq}",
                    )
            elif decision.status in {"settled", "bounded"}:
                self._append_room_status(room, decision)

    def _apply_pending_control_retries(
        self,
        binding: HostedRoomBinding,
        lease: driver.DriverLease,
    ) -> None:
        """Apply cross-process Retry commands under this worker's active lease."""

        pending = hosted_room_controls.load_pending_control_retries(
            self.db_path,
            room_id=binding.room_id,
        )
        if not pending:
            return
        room = hosted_rooms.room_state(self.db_path, room_id=binding.room_id)
        stopped_through_seq = self._policy_snapshot(room).stopped_through_seq
        tasks = {
            task["identity"].task_id: task
            for task in driver.list_tasks(self.db_path, room_id=binding.room_id)
        }
        for command in pending:
            try:
                for task_id in command.task_ids:
                    task = tasks.get(task_id)
                    if task is None:
                        continue
                    status = str(task.get("status") or "")
                    if status in driver.TERMINAL_STATUSES or status == "stopping":
                        continue
                    source_event_seq = int(
                        (task.get("payload") or {}).get("source_event_seq") or 0
                    )
                    if source_event_seq < stopped_through_seq:
                        self.runtime.cancel(
                            task["identity"],
                            cancel_id=f"stop-fence:{stopped_through_seq}",
                        )
                        continue
                    if status in {"deferred", "indeterminate"}:
                        self.retry_room_task(
                            binding.room_id,
                            task_id=task_id,
                            retry_id=hosted_room_controls.control_retry_attempt_id(
                                command.command_id, task_id
                            ),
                        )
                hosted_room_controls.complete_control_retry(
                    self.db_path,
                    command_id=command.command_id,
                    result={
                        "action": "retry",
                        "processed": len(command.task_ids),
                    },
                    lease=lease,
                    now=self.runtime.clock(),
                )
            except Exception as exc:
                hosted_room_controls.defer_control_retry(
                    self.db_path,
                    command_id=command.command_id,
                    now=self.runtime.clock(),
                )
                logger.warning(
                    "Hosted room retry command %s remains pending: %s",
                    command.command_id,
                    exc,
                )

    def _apply_pending_controls(
        self,
        binding: HostedRoomBinding,
        lease: driver.DriverLease,
    ) -> None:
        self._apply_pending_control_retries(binding, lease)
        self._apply_pending_control_approvals(binding)

    def _apply_pending_control_approvals(
        self,
        binding: HostedRoomBinding,
    ) -> None:
        from gateway import hosted_room_messaging_approvals as approvals

        pending = approvals.list_pending_approval_commands(
            self.db_path,
            room_id=binding.room_id,
        )
        if not pending:
            return
        durable = {
            (
                item["authority_gateway_id"],
                item["authority_epoch"],
                item["member_id"],
                item["task_id"],
                item["execution_generation"],
                item["request_id"],
            )
            for item in approvals.list_pending_approvals(
                self.db_path,
                room_id=binding.room_id,
            )
        }
        for command in pending:
            coordinates = (
                str(command["authority_gateway_id"]),
                int(command["authority_epoch"]),
                str(command["member_id"]),
                str(command["task_id"]),
                int(command["execution_generation"]),
                str(command["request_id"]),
            )
            if coordinates not in durable:
                approvals.complete_approval_command(
                    self.db_path,
                    command_id=command["command_id"],
                    result="Approval was already resolved.",
                )
                continue
            with self._policy_lock:
                action = self._pending_actions.get(
                    (binding.room_id, coordinates[2])
                )
            if (
                not isinstance(action, Mapping)
                or coordinates[0] != binding.gateway_id
                or coordinates[1] != binding.authority_epoch
                or str(action.get("authority_gateway_id") or "") != coordinates[0]
                or int(action.get("authority_epoch") or 0) != coordinates[1]
                or str(action.get("task_id") or "") != coordinates[3]
                or int(action.get("execution_generation") or 0) != coordinates[4]
                or str(action.get("request_id") or "") != coordinates[5]
            ):
                continue
            try:
                self.approve_room_task(
                    binding.room_id,
                    member_id=coordinates[2],
                    task_id=coordinates[3],
                    execution_generation=coordinates[4],
                    choice=str(command["choice"]),
                    request_id=coordinates[5],
                )
                result = (
                    "Approved once."
                    if command["choice"] == "once"
                    else "Denied."
                )
                approvals.complete_approval_command(
                    self.db_path,
                    command_id=command["command_id"],
                    result=result,
                )
            except Exception as exc:
                logger.warning(
                    "Hosted room approval command %s remains pending: %s",
                    command["command_id"],
                    exc,
                )

    def publish_terminal(
        self,
        binding: HostedRoomBinding,
        _task: Mapping[str, Any],
    ) -> None:
        self.prepare_room(binding)
        self.runtime.wakeup()

    def create_room(self, *, room_id: str, name: str, members: Any) -> dict[str, Any]:
        normalized = discussion.validate_roster(
            members,
            local_profiles=self.local_profiles(),
        )
        room = hosted_rooms.create_room(
            self.db_path,
            room_id=room_id,
            name=name,
            members=[
                {
                    "member_id": member.member_id,
                    "profile": member.profile,
                    "handle": member.handle,
                    "target": dict(member.target or {}),
                    **(
                        {"display_name": member.display_name}
                        if member.display_name
                        else {}
                    ),
                }
                for member in normalized
            ],
            authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
        )
        self.runtime.wakeup()
        return room

    def put_attachment(
        self,
        *,
        room_id: str,
        upload_id: str,
        kind: str,
        name: str,
        mime: str,
        data: bytes,
    ) -> dict[str, Any]:
        """Store one bounded upload for a live room without exposing its path."""

        self._owned_room(room_id)
        if kind == "pdf" and shutil.which("pdftoppm") is None:
            raise hosted_rooms.HostedRoomError(
                "This gateway cannot receive PDFs until Poppler is installed."
            )
        return self.attachments.put(
            room_id=room_id,
            upload_id=upload_id,
            kind=kind,
            name=name,
            mime=mime,
            data=data,
        )

    def read_attachment(
        self,
        *,
        room_id: str,
        attachment_id: str,
        recipient_member_id: str | None,
        event_id: str | None = None,
        viewer: bool = False,
    ) -> AttachmentData:
        """Return verified bytes only when send-time recipient ownership permits it."""

        return self.attachments.read(
            room_id=room_id,
            attachment_id=attachment_id,
            recipient_member_id=recipient_member_id,
            event_id=event_id,
            viewer=viewer,
        )

    def send(
        self,
        *,
        room_id: str,
        event_id: str,
        payload: Any,
    ) -> dict[str, Any]:
        return self.send_server_owned(
            room_id=room_id,
            event_id=event_id,
            payload=payload,
            actor={"kind": "user", "id": "desktop"},
        )

    def send_server_owned(
        self,
        *,
        room_id: str,
        event_id: str,
        payload: Any,
        actor: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Append a user event whose actor was derived by trusted gateway code."""

        with self._policy_lock:
            return self._send_locked(
                room_id=room_id,
                event_id=event_id,
                payload=payload,
                actor=actor,
            )

    def _send_locked(
        self,
        *,
        room_id: str,
        event_id: str,
        payload: Any,
        actor: Mapping[str, Any],
    ) -> dict[str, Any]:
        room = self._owned_room(room_id)
        member_ids = tuple(
            str(member.get("member_id") or member.get("profile") or "")
            for member in room["members"]
        )
        if isinstance(payload, Mapping) and "thread_id" not in payload:
            payload = {**payload, "thread_id": event_id}
        normalized = discussion.validate_user_payload(
            payload,
            member_ids=member_ids,
        )
        transitioned_attachment_ids: tuple[str, ...] = ()
        if normalized.get("attachments"):
            normalized["attachments"], transitioned_attachment_ids = (
                self.attachments.commit_message_with_receipt(
                    room_id=room_id,
                    event_id=event_id,
                    manifest=normalized["attachments"],
                    recipient_member_ids=member_ids,
                    viewer_access=True,
                    hold_until_event=True,
                )
            )
        try:
            event = hosted_rooms.append_event(
                self.db_path,
                room_id=room_id,
                event_id=event_id,
                kind="message.user",
                actor=dict(actor),
                payload=normalized,
                authority_gateway_id=str(room["authority_gateway_id"]),
                authority_epoch=int(room["authority_epoch"]),
            )
        except Exception:
            if transitioned_attachment_ids:
                self.attachments.abort_message_commit(
                    room_id=room_id,
                    event_id=event_id,
                    attachment_ids=transitioned_attachment_ids,
                )
            raise
        if normalized.get("attachments"):
            self.attachments.retain_event(room_id=room_id, event_id=event_id)
        binding = next(
            (
                candidate
                for candidate in self.bindings()
                if candidate.room_id == room_id
            ),
            None,
        )
        if binding is None:
            raise hosted_rooms.RoomNotFoundError("hosted room not found")
        self.prepare_room(binding)
        self.runtime.wakeup()
        return event

    def stop_room(
        self,
        room_id: str,
        *,
        cancel_id: str,
        require_acknowledged: bool = False,
    ) -> int:
        room = self._owned_room(room_id, allow_disbanding=True)
        stop_event = hosted_rooms.request_room_stop(
            self.db_path,
            room_id=room_id,
            cancel_id=cancel_id,
            expected_gateway_id=str(room["authority_gateway_id"]),
            expected_epoch=int(room["authority_epoch"]),
        )
        stop_seq = int(stop_event["seq"])
        cancelled = 0
        pending = 0
        with self._policy_lock:
            tasks = {}
            for status in (
                "queued",
                "running",
                "indeterminate",
                "deferred",
                "stopping",
            ):
                for task in driver.list_tasks(
                    self.db_path,
                    room_id=room_id,
                    status=status,
                ):
                    if int(task["payload"]["source_event_seq"]) >= stop_seq:
                        continue
                    identity = task["identity"]
                    tasks[(identity.room_id, identity.task_id)] = task
            for task in tasks.values():
                task_cancel_id = (
                    str(task.get("cancel_id") or "")
                    if task.get("status") == "stopping"
                    else ""
                )
                result = self.runtime.cancel(
                    task["identity"],
                    cancel_id=task_cancel_id or cancel_id,
                )
                cancelled += 1
                if result["status"] == "stopping":
                    pending += 1
                else:
                    self._discard_cancelled_task_artifacts(room_id, task)
        if require_acknowledged and pending:
            raise RuntimeError(
                "room work is still stopping; retry deletion after Stop completes"
            )
        self.runtime.wakeup()
        return cancelled

    def _discard_cancelled_task_artifacts(
        self,
        room_id: str,
        task: Mapping[str, Any],
    ) -> None:
        generation = int(task.get("execution_generation") or 0)
        if generation < 1:
            return
        payload = task.get("payload") or {}
        member_id = str(
            payload.get("target_member_id") or payload.get("target_profile") or ""
        )
        if not member_id or (room_id, member_id) in self.peer_routes:
            return
        room = hosted_rooms.room_state(
            self.db_path,
            room_id=room_id,
            include_disbanded=True,
        )
        profile = str(payload.get("target_profile") or "default")
        installation = hosted_rooms.local_authority_gateway_id()
        scope = RoomArtifactScope.from_mapping({
            "room_id": room_id,
            "task_id": task["identity"].task_id,
            "execution_generation": generation,
            "member_id": member_id,
            "target_profile": profile,
            "home_install_id": installation,
            "target_install_id": installation,
            "authority_gateway_id": room["authority_gateway_id"],
            "authority_epoch": int(room["authority_epoch"]),
        })
        profile_root = (
            self.root
            if profile == "default"
            else self.root / "profiles" / profile
        )
        RoomArtifactOutbox(profile_root / "state.db").discard_durably(scope)

    def retry_room_task(
        self,
        room_id: str,
        *,
        task_id: str,
        retry_id: str | None = None,
    ) -> dict[str, Any]:
        """Retry one uncertain or deferred task only after explicit user action."""

        task = next(
            (
                candidate
                for candidate in driver.list_tasks(self.db_path, room_id=room_id)
                if candidate["identity"].task_id == task_id
            ),
            None,
        )
        if task is None:
            raise driver.InvalidTaskTransitionError(
                "no retryable room task matches task_id"
            )
        if retry_id and driver.retry_receipt_exists(
            self.db_path,
            room_id=room_id,
            task_id=task_id,
            retry_id=retry_id,
        ):
            return {**task, "idempotent": True}
        if task["status"] not in {"indeterminate", "deferred"}:
            raise driver.InvalidTaskTransitionError(
                "no retryable room task matches task_id"
            )
        return self.runtime.retry_indeterminate(task["identity"], retry_id=retry_id)

    def approve_room_task(
        self,
        room_id: str,
        *,
        member_id: str,
        task_id: str,
        execution_generation: int,
        choice: str,
        request_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Resolve one exact local or peer approval and wake room observation."""
        key = (room_id, member_id)
        route = self.peer_routes.get(key)
        client = self.peer_clients.get(key)
        with self._policy_lock:
            action = self._pending_actions.get(key)
        from gateway import hosted_room_messaging_approvals as approvals

        try:
            room = self._owned_room(room_id)
        except hosted_rooms.RoomNotFoundError:
            approvals.clear_pending_approval(
                self.db_path,
                room_id=room_id,
                member_id=member_id,
                request_id=request_id,
                authority_gateway_id=(action or {}).get("authority_gateway_id"),
                authority_epoch=(action or {}).get("authority_epoch"),
            )
            with self._policy_lock:
                current = self._pending_actions.get(key)
                if current == action:
                    self._pending_actions.pop(key, None)
            raise approvals.MessagingApprovalTerminalError(
                "Approval expired because the Group Chat is no longer available."
            ) from None
        except hosted_rooms.AuthorityConflictError:
            approvals.clear_pending_approval(
                self.db_path,
                room_id=room_id,
                member_id=member_id,
                request_id=request_id,
                authority_gateway_id=(action or {}).get("authority_gateway_id"),
                authority_epoch=(action or {}).get("authority_epoch"),
            )
            with self._policy_lock:
                current = self._pending_actions.get(key)
                if current == action:
                    self._pending_actions.pop(key, None)
            raise approvals.MessagingApprovalTerminalError(
                "Approval expired because Group Chat authority changed."
            ) from None
        requested_approval_id = str(request_id or "")
        pending_approval_id = str((action or {}).get("request_id") or "")
        if (
            action is None
            or action.get("task_id") != task_id
            or int(action.get("execution_generation") or 0)
            != execution_generation
            or not requested_approval_id
            or not pending_approval_id
            or requested_approval_id != pending_approval_id
        ):
            raise RuntimeError("room approval is no longer pending")
        if (
            str((action or {}).get("authority_gateway_id") or "")
            != str(room["authority_gateway_id"])
            or int((action or {}).get("authority_epoch") or 0)
            != int(room["authority_epoch"])
        ):
            approvals.clear_pending_approval(
                self.db_path,
                room_id=room_id,
                member_id=member_id,
                request_id=request_id,
                authority_gateway_id=(action or {}).get("authority_gateway_id"),
                authority_epoch=(action or {}).get("authority_epoch"),
            )
            with self._policy_lock:
                current = self._pending_actions.get(key)
                if current == action:
                    self._pending_actions.pop(key, None)
            raise approvals.MessagingApprovalTerminalError(
                "Approval expired because Group Chat authority changed."
            )
        if choice not in {"once", "deny"}:
            raise RuntimeError("room approval choice must be once or deny")
        approve = getattr(client, "approve_receipt", None)
        if route is not None and callable(approve):
            result = approve(
                task_id=task_id,
                execution_generation=execution_generation,
                request_id=requested_approval_id,
                choice=choice,
                grant=route.grant,
            )
        else:
            session_id = str(action.get("session_id") or "")
            profile = str(action.get("profile") or "")
            if not session_id:
                raise RuntimeError("local room approval identity is unavailable")
            if profile:
                resumed = self.rpc.resume(
                    profile=profile,
                    session_id=session_id,
                    source=ROOM_SESSION_SOURCE,
                )
                session_id = str((resumed or {}).get("session_id") or session_id)
            result = self.rpc.approve(
                session_id=session_id,
                request_id=requested_approval_id,
                choice=choice,
            )
        if not isinstance(result, Mapping) or int(result.get("resolved") or 0) != 1:
            raise RuntimeError("room approval target did not resolve the exact request")
        for attempt in range(2):
            try:
                approvals.clear_pending_approval(
                    self.db_path,
                    room_id=room_id,
                    member_id=member_id,
                    request_id=requested_approval_id,
                    authority_gateway_id=(action or {}).get(
                        "authority_gateway_id"
                    ),
                    authority_epoch=(action or {}).get("authority_epoch"),
                )
                break
            except Exception:
                if attempt:
                    raise
                logger.warning("Retrying durable Group Chat approval cleanup")
        with self._policy_lock:
            current = self._pending_actions.get(key)
            if (
                current is not None
                and str(current.get("request_id") or "") == requested_approval_id
                and current.get("task_id") == task_id
                and int(current.get("execution_generation") or 0)
                == execution_generation
            ):
                self._pending_actions.pop(key, None)
        self.runtime.wakeup()
        return result

    def status(self, room_id: str | None = None) -> dict[str, Any]:
        runtime = self.runtime.status()
        runtime = {**runtime, "peer_routes": self._route_statuses(room_id)}
        if self._link_load_error:
            runtime = {**runtime, "link_load_error": self._link_load_error}
        if room_id is None:
            return runtime
        tasks = driver.list_tasks(self.db_path, room_id=room_id)
        counts = Counter(str(task["status"]) for task in tasks)
        pending_actions = [
            {
                "kind": "retry",
                "task_id": task["identity"].task_id,
            }
            for task in tasks
            if task["status"] in {"indeterminate", "deferred"}
        ]
        with self._policy_lock:
            pending_actions.extend(
                {
                    key: value
                    for key, value in action.items()
                    if key
                    not in {
                        "profile",
                        "authority_gateway_id",
                        "authority_epoch",
                        "observer_generation",
                        "observer_lease_generation",
                    }
                }
                for (
                    action_room_id,
                    _member_id,
                ), action in self._pending_actions.items()
                if action_room_id == room_id
            )
        return {
            "running": runtime["running"],
            "working": bool(
                counts.get("running") or counts.get("queued") or counts.get("stopping")
            ),
            "blocked": room_id in runtime["blocked_rooms"]
            or bool(counts.get("indeterminate") or counts.get("stopping")),
            "counts": dict(counts),
            "pending_actions": pending_actions,
            "peer_routes": self._route_statuses(room_id),
        }


class _RouteStatusPeerClient:
    """Classify scoped-auth failures without exposing route credentials."""

    def __init__(
        self,
        client,
        *,
        on_ready,
        on_reauthorization,
        on_unavailable,
        on_refreshed,
        capability_digest="",
        execution_policy_digest="",
    ) -> None:
        self._client = client
        self._on_ready = on_ready
        self._on_reauthorization = on_reauthorization
        self._on_unavailable = on_unavailable
        self._on_refreshed = on_refreshed
        self._capability_digest = str(capability_digest or "")
        self._execution_policy_digest = str(execution_policy_digest or "")

    def __getattr__(self, name):
        value = getattr(self._client, name)
        if not callable(value):
            return value

        def tracked(*args, **kwargs):
            if name in {
                "acknowledge_artifacts",
                "discard_artifacts",
                "dispatch",
                "read_artifact",
                "recover_dispatch",
                "stage_attachments",
            } and "grant" in kwargs:
                from gateway.hosted_room_peer import (
                    room_grant_needs_dispatch_refresh,
                )

                grant = kwargs["grant"]
                if room_grant_needs_dispatch_refresh(grant):
                    checked = (
                        HostedMemberDispatch.from_mapping(kwargs["dispatch"])
                        if "dispatch" in kwargs
                        else None
                    )
                    capability_digest = (
                        checked.capability_digest
                        if checked is not None
                        else self._capability_digest
                    )
                    execution_policy_digest = (
                        checked.execution_policy_digest
                        if checked is not None
                        else self._execution_policy_digest
                    )
                    refresh = getattr(self._client, "refresh_grant", None)
                    if callable(refresh):
                        try:
                            refreshed = refresh(
                                grant=grant,
                                capability_digest=capability_digest,
                                execution_policy_digest=execution_policy_digest,
                            )
                        except Exception as exc:
                            if bool(
                                getattr(exc, "needs_reauthorization", False)
                            ):
                                self._on_reauthorization()
                                raise
                            if room_grant_needs_dispatch_refresh(
                                grant, leeway_seconds=0
                            ):
                                self._on_reauthorization()
                                raise
                        else:
                            replacement = str(refreshed.get("grant") or "")
                            if not replacement:
                                raise RuntimeError(
                                    "peer returned no refreshed room grant"
                                )
                            try:
                                refreshed_catalog = None
                                if refreshed.get("catalog") is not None:
                                    refreshed_catalog = (
                                        GatewayRoomCatalog.from_mapping(
                                            refreshed.get("catalog")
                                        )
                                    )
                                    if (
                                        refreshed_catalog.execution_policy.policy_digest
                                        != execution_policy_digest
                                    ):
                                        raise PeerRunsHTTPError(
                                            "peer room execution policy needs reauthorization",
                                            status_code=403,
                                            error_code="room_execution_policy_changed",
                                            not_admitted=True,
                                        )
                                    if (
                                        refreshed_catalog.catalog_digest
                                        != capability_digest
                                    ):
                                        raise PeerRunsHTTPError(
                                            "peer room capabilities need reauthorization",
                                            status_code=403,
                                            error_code="room_capability_catalog_changed",
                                            not_admitted=True,
                                        )
                                self._on_refreshed(replacement, refreshed_catalog)
                            except Exception:
                                revoke = getattr(self._client, "revoke_grant", None)
                                if not callable(revoke):
                                    raise RuntimeError(
                                        "unpublished refreshed grant cannot be revoked"
                                    )
                                revoke(grant=replacement)
                                self._on_reauthorization()
                                raise
                            kwargs = {**kwargs, "grant": replacement}
            try:
                result = value(*args, **kwargs)
            except Exception as exc:
                if bool(getattr(exc, "needs_reauthorization", False)):
                    self._on_reauthorization()
                    raise
                elif bool(getattr(exc, "not_admitted", False)):
                    self._on_unavailable()
                    raise
                else:
                    raise
            if name != "prepare":
                self._on_ready()
            return result

        return tracked
