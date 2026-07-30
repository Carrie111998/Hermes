"""Storage facade factory — single entry point for all Hermes stores."""

from __future__ import annotations

import logging
import socket
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from hermes_storage.bootstrap import BootstrapConfig, get_bootstrap, is_mongo_mode
from hermes_storage.errors import MongoStorageError, raise_mongo_unavailable
from hermes_storage.machine_id import compute_machine_id, detect_capabilities
from hermes_storage.overlay import deep_merge

logger = logging.getLogger(__name__)

_STORAGE: Optional["HermesStorage"] = None


@dataclass
class HermesStorage:
    bootstrap: BootstrapConfig
    client: Any
    shared_db: Any
    profile_db: Any
    settings: Any
    knowledge: Any
    config: Any
    secrets: Any
    soul: Any
    memories: Any
    skills: Any
    machines: Any
    sessions: Any
    ledgers: Any
    cluster: Any
    machine_id: str
    node_id: str

    def load_effective_config(self, base: Optional[dict] = None) -> dict:
        """shared settings ⊕ profile config ⊕ machine overlay."""
        shared = self.settings.get("default") or {}
        profile = self.config.get("default") or {}
        merged = deep_merge(base or {}, shared)
        merged = deep_merge(merged, profile)
        overlay = self.machines.get_overlay(self.machine_id)
        return deep_merge(merged, overlay)

    def save_profile_config(self, config: dict) -> None:
        from hermes_storage.overlay import strip_machine_local

        self.config.put(strip_machine_local(config))

    def save_machine_overlay_from_config(self, config: dict) -> None:
        from hermes_storage.overlay import extract_machine_overlay

        overlay = extract_machine_overlay(config)
        self.machines.set_overlay(self.machine_id, overlay)

    def load_soul(self) -> str:
        doc = self.soul.get("default") or {}
        return str(doc.get("content") or "")

    def save_soul(self, content: str) -> None:
        self.soul.put({"content": content})

    def register_presence(
        self,
        *,
        api_base: Optional[str] = None,
        capabilities: Optional[list[str]] = None,
        hostname: Optional[str] = None,
    ) -> None:
        self.cluster.heartbeat({
            "node_id": self.node_id,
            "machine_id": self.machine_id,
            "profile": self.bootstrap.profile,
            "hostname": hostname or socket.gethostname(),
            "api_base": api_base or "",
            "capabilities": capabilities or detect_capabilities(),
            "status": "online",
        })
        self.machines.upsert_machine(self.machine_id, {
            "hostname": hostname or socket.gethostname(),
            "capabilities": capabilities or detect_capabilities(),
            "node_id": self.node_id,
            "last_seen": True,
        })

    def cluster_status(self) -> dict[str, Any]:
        state = self.cluster.get_state()
        nodes = self.cluster.list_nodes()
        return {
            "profile": self.bootstrap.profile,
            "this_node_id": self.node_id,
            "this_machine_id": self.machine_id,
            "state": state,
            "nodes": nodes,
        }

    def activate(self, target: str, *, reason: str = "manual") -> dict[str, Any]:
        """Activate a node by node_id, machine_id, or hostname."""
        nodes = self.cluster.list_nodes(online_within_s=120.0)
        match = None
        needle = target.strip().lower()
        for node in nodes:
            for field in ("node_id", "machine_id", "hostname"):
                val = str(node.get(field) or "").lower()
                if val == needle or val.startswith(needle):
                    match = node
                    break
            if match:
                break
        if not match:
            raise ValueError(f"No cluster node matched {target!r}")
        return self.cluster.set_active(match["node_id"], reason=reason)


def _build_storage(boot: BootstrapConfig) -> HermesStorage:
    from hermes_storage.mongo import ensure_indexes, get_client
    from hermes_storage.mongo.stores import (
        MongoClusterStore,
        MongoDocumentStore,
        MongoLedgerStore,
        MongoMachineStore,
        MongoMemoryEntriesStore,
        MongoSecretsStore,
        MongoSessionStore,
        MongoSkillsStore,
    )

    client = get_client(boot.mongo_uri)
    shared = client[boot.shared_db]
    profile = client[boot.profile_db]
    ensure_indexes(shared, profile)

    machine_id = compute_machine_id(override=boot.machine_id)
    node_id = f"{machine_id}-{uuid.uuid4().hex[:8]}"

    return HermesStorage(
        bootstrap=boot,
        client=client,
        shared_db=shared,
        profile_db=profile,
        settings=MongoDocumentStore(shared["settings"]),
        knowledge=MongoDocumentStore(shared["knowledge"]),
        config=MongoDocumentStore(profile["config"]),
        secrets=MongoSecretsStore(profile["secrets"]),
        soul=MongoDocumentStore(profile["soul"]),
        memories=MongoMemoryEntriesStore(profile["memories"]),
        skills=MongoSkillsStore(shared),
        machines=MongoMachineStore(profile),
        sessions=MongoSessionStore(profile),
        ledgers=MongoLedgerStore(profile),
        cluster=MongoClusterStore(shared),
        machine_id=machine_id,
        node_id=node_id,
    )


def get_storage(*, force: bool = False) -> Optional[HermesStorage]:
    """Return the process-wide storage facade, or None if not in Mongo mode.

    When Mongo mode is on, connection/build failures propagate (no silent
    ``None``). Callers that need a guarantee should use :func:`require_storage`.
    """
    global _STORAGE
    if not is_mongo_mode():
        return None
    if _STORAGE is not None and not force:
        return _STORAGE
    boot = get_bootstrap()
    if boot is None:
        raise_mongo_unavailable("bootstrap missing while is_mongo_mode() is true")
    try:
        _STORAGE = _build_storage(boot)
    except MongoStorageError:
        raise
    except Exception as exc:
        raise_mongo_unavailable(str(exc), cause=exc)
    return _STORAGE


def require_storage(*, force: bool = False) -> HermesStorage:
    """Return storage or raise :class:`MongoStorageError` (never local fallback)."""
    if not is_mongo_mode():
        raise MongoStorageError(
            "Mongo mode is not enabled (no bootstrap.yaml / HERMES_MONGO_URI)"
        )
    storage = get_storage(force=force)
    if storage is None:
        raise_mongo_unavailable("get_storage() returned None")
    return storage


def reset_storage() -> None:
    global _STORAGE
    from hermes_storage.mongo import close_client

    _STORAGE = None
    close_client()
