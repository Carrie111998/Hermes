"""Abstract storage interfaces for Hermes remote state."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DocumentStore(ABC):
    """Simple document store for single-doc collections (config, soul, …)."""

    @abstractmethod
    def get(self, key: str = "default") -> Optional[dict[str, Any]]:
        ...

    @abstractmethod
    def put(self, data: dict[str, Any], key: str = "default") -> None:
        ...

    @abstractmethod
    def delete(self, key: str = "default") -> bool:
        ...


class SecretsStore(ABC):
    @abstractmethod
    def get_all(self) -> dict[str, str]:
        ...

    @abstractmethod
    def set_many(self, values: dict[str, str]) -> None:
        ...

    @abstractmethod
    def get(self, name: str) -> Optional[str]:
        ...

    @abstractmethod
    def set(self, name: str, value: str) -> None:
        ...


class MemoryEntriesStore(ABC):
    @abstractmethod
    def load(self, target: str) -> str:
        """Return raw markdown body for MEMORY or USER."""

    @abstractmethod
    def save(self, target: str, content: str) -> None:
        ...


class SkillsStore(ABC):
    @abstractmethod
    def list_skills(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def get_skill(self, name: str) -> Optional[dict[str, Any]]:
        ...

    @abstractmethod
    def put_skill(self, skill: dict[str, Any], files: Optional[dict[str, bytes]] = None) -> None:
        ...

    @abstractmethod
    def delete_skill(self, name: str) -> bool:
        ...

    @abstractmethod
    def materialize(self, name: str, dest_dir: "Any") -> "Any":
        ...


class MachineStore(ABC):
    @abstractmethod
    def upsert_machine(self, machine_id: str, doc: dict[str, Any]) -> None:
        ...

    @abstractmethod
    def get_machine(self, machine_id: str) -> Optional[dict[str, Any]]:
        ...

    @abstractmethod
    def list_machines(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def get_overlay(self, machine_id: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def set_overlay(self, machine_id: str, overlay: dict[str, Any]) -> None:
        ...


class SessionStore(ABC):
    """Core session/message persistence (replaces state.db)."""

    @abstractmethod
    def create_session(self, session_id: str, source: str, **kwargs: Any) -> str:
        ...

    @abstractmethod
    def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        ...

    @abstractmethod
    def update_session(self, session_id: str, **fields: Any) -> None:
        ...

    @abstractmethod
    def end_session(self, session_id: str, end_reason: str) -> None:
        ...

    @abstractmethod
    def append_message(self, session_id: str, role: str, content: Any = None, **kwargs: Any) -> int:
        ...

    @abstractmethod
    def get_messages(self, session_id: str, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def search_messages(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def list_sessions(self, *, limit: int = 50, source: Optional[str] = None) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def delete_session(self, session_id: str) -> bool:
        ...

    @abstractmethod
    def save_gateway_routing_entry(self, key: str, value: str, *, scope: str = "") -> None:
        ...

    @abstractmethod
    def load_gateway_routing_entries(self, *, scope: str = "") -> dict[str, str]:
        ...


class LedgerStore(ABC):
    """Generic collection CRUD for cron/kanban/projects/etc."""

    @abstractmethod
    def insert(self, collection: str, doc: dict[str, Any]) -> str:
        ...

    @abstractmethod
    def find(
        self,
        collection: str,
        query: Optional[dict[str, Any]] = None,
        *,
        limit: int = 100,
        sort: Optional[list[tuple[str, int]]] = None,
    ) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def update(self, collection: str, query: dict[str, Any], patch: dict[str, Any]) -> int:
        ...

    @abstractmethod
    def delete(self, collection: str, query: dict[str, Any]) -> int:
        ...

    @abstractmethod
    def replace_one(self, collection: str, query: dict[str, Any], doc: dict[str, Any], *, upsert: bool = True) -> None:
        ...


class ClusterStore(ABC):
    @abstractmethod
    def heartbeat(self, node: dict[str, Any]) -> None:
        ...

    @abstractmethod
    def list_nodes(self, *, online_within_s: float = 60.0) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def get_state(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def set_active(self, node_id: str, *, reason: str = "manual") -> dict[str, Any]:
        ...

    @abstractmethod
    def begin_messaging_handoff(self, target_node_id: str, *, from_node_id: Optional[str] = None) -> dict[str, Any]:
        ...

    @abstractmethod
    def mark_messaging_released(self, node_id: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def complete_messaging_handoff(self, node_id: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def rollback_messaging_handoff(self, *, reason: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def mark_node_offline(self, node_id: str) -> None:
        ...
