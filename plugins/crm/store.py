"""Durable local state for the personal CRM plugin.

A JSON-backed, lock-guarded store with atomic writes — the same durability
pattern used by :class:`plugins.teams_pipeline.store.TeamsPipelineStore`. It
holds two collections: ``contacts`` (keyed by contact id) and ``interactions``
(keyed by interaction id, each referencing a contact id).
"""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home


DEFAULT_CRM_STORE_FILENAME = "crm_store.json"


class CrmStoreError(RuntimeError):
    """Raised when the store file exists but cannot be read safely.

    Deliberately loud: silently treating a corrupt store as empty would let
    the next write atomically replace it, destroying every contact. Callers
    surface this to the operator and never persist over the damaged file.
    """


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_crm_store_path(path: str | Path | None = None) -> Path:
    """Resolve the CRM store path.

    Precedence: explicit argument → ``HERMES_CRM_STORE_PATH`` env var →
    ``<hermes home>/crm_store.json``.
    """
    if path is not None:
        explicit = str(path).strip()
        if explicit:
            return Path(explicit)

    env_path = os.getenv("HERMES_CRM_STORE_PATH", "").strip()
    if env_path:
        return Path(env_path)

    return get_hermes_home() / DEFAULT_CRM_STORE_FILENAME


class CrmStore:
    """JSON-backed durable store for CRM contacts and interactions."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state: Dict[str, Dict[str, Any]] = {
            "contacts": {},
            "interactions": {},
        }
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            except json.JSONDecodeError as exc:
                raise CrmStoreError(
                    f"CRM store at {self.path} is not valid JSON ({exc}). "
                    "Repair or move the file aside; refusing to load (and "
                    "potentially overwrite) it."
                ) from exc
            if not isinstance(data, dict):
                raise CrmStoreError(
                    f"CRM store at {self.path} must be a JSON object, "
                    f"found {type(data).__name__}."
                )
            for key in ("contacts", "interactions"):
                collection = data.get(key) or {}
                if not isinstance(collection, dict) or any(
                    not isinstance(record, dict) for record in collection.values()
                ):
                    raise CrmStoreError(
                        f"CRM store at {self.path} has a malformed {key!r} "
                        "section (expected an object of record objects)."
                    )
                self._state[key] = dict(collection)

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            delete=False,
        ) as tmp:
            json.dump(self._state, tmp, indent=2, sort_keys=True)
            tmp.flush()
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.path)

    # -- contacts -----------------------------------------------------------

    def list_contacts(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return deepcopy(self._state["contacts"])

    def get_contact(self, contact_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._state["contacts"].get(contact_id)
            return deepcopy(record) if isinstance(record, dict) else None

    def contact_ids(self) -> set[str]:
        with self._lock:
            return set(self._state["contacts"].keys())

    def upsert_contact(self, contact_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Merge ``payload`` into the stored contact, stamping timestamps.

        A ``None`` value in ``payload`` explicitly clears that field; keys
        absent from ``payload`` are left untouched.
        """
        with self._lock:
            existing = self._state["contacts"].get(contact_id, {})
            merged = {**existing, **deepcopy(payload)}
            merged = {k: v for k, v in merged.items() if v is not None}
            merged["contact_id"] = contact_id
            merged.setdefault("created_at", existing.get("created_at") or _utc_now_iso())
            merged["updated_at"] = _utc_now_iso()
            self._state["contacts"][contact_id] = merged
            self._persist()
            return deepcopy(merged)

    def delete_contact(self, contact_id: str, *, cascade: bool = True) -> bool:
        """Remove a contact. When ``cascade`` is true, also drop its interactions."""
        with self._lock:
            removed = self._state["contacts"].pop(contact_id, None)
            if removed is None:
                return False
            if cascade:
                self._state["interactions"] = {
                    iid: rec
                    for iid, rec in self._state["interactions"].items()
                    if rec.get("contact_id") != contact_id
                }
            self._persist()
            return True

    # -- interactions -------------------------------------------------------

    def list_interactions(
        self, contact_id: Optional[str] = None
    ) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            records = self._state["interactions"]
            if contact_id is None:
                return deepcopy(records)
            return deepcopy(
                {
                    iid: rec
                    for iid, rec in records.items()
                    if rec.get("contact_id") == contact_id
                }
            )

    def get_interaction(self, interaction_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._state["interactions"].get(interaction_id)
            return deepcopy(record) if isinstance(record, dict) else None

    def upsert_interaction(
        self, interaction_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        with self._lock:
            existing = self._state["interactions"].get(interaction_id, {})
            merged = {**existing, **deepcopy(payload)}
            merged["interaction_id"] = interaction_id
            merged.setdefault("created_at", existing.get("created_at") or _utc_now_iso())
            self._state["interactions"][interaction_id] = merged
            self._persist()
            return deepcopy(merged)

    def delete_interaction(self, interaction_id: str) -> bool:
        with self._lock:
            removed = self._state["interactions"].pop(interaction_id, None)
            if removed is None:
                return False
            self._persist()
            return True

    # -- introspection ------------------------------------------------------

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "contacts": len(self._state["contacts"]),
                "interactions": len(self._state["interactions"]),
            }

    def all_tags(self) -> List[str]:
        with self._lock:
            tags: set[str] = set()
            for rec in self._state["contacts"].values():
                for tag in rec.get("tags") or []:
                    tags.add(str(tag))
            return sorted(tags)


__all__ = [
    "CrmStore",
    "CrmStoreError",
    "resolve_crm_store_path",
    "DEFAULT_CRM_STORE_FILENAME",
]
