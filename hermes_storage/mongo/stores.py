"""Mongo implementations of Hermes storage backends."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from hermes_storage.backend import (
    ClusterStore,
    DocumentStore,
    LedgerStore,
    MachineStore,
    MemoryEntriesStore,
    SecretsStore,
    SessionStore,
    SkillsStore,
    utcnow,
)
from hermes_storage.machine_id import machine_collection_name

logger = logging.getLogger(__name__)


def _strip_id(doc: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not doc:
        return None
    out = dict(doc)
    out.pop("_id", None)
    return out


class MongoDocumentStore(DocumentStore):
    def __init__(self, collection):
        self._col = collection

    def get(self, key: str = "default") -> Optional[dict[str, Any]]:
        doc = self._col.find_one({"key": key})
        if not doc:
            return None
        data = doc.get("data")
        return data if isinstance(data, dict) else _strip_id(doc)

    def put(self, data: dict[str, Any], key: str = "default") -> None:
        self._col.update_one(
            {"key": key},
            {"$set": {"key": key, "data": data, "updated_at": utcnow()}},
            upsert=True,
        )

    def delete(self, key: str = "default") -> bool:
        result = self._col.delete_one({"key": key})
        return result.deleted_count > 0


class MongoSecretsStore(SecretsStore):
    def __init__(self, collection):
        self._col = collection

    def get_all(self) -> dict[str, str]:
        doc = self._col.find_one({"key": "env"})
        if not doc:
            return {}
        values = doc.get("values") or {}
        return {str(k): str(v) for k, v in values.items() if v is not None}

    def set_many(self, values: dict[str, str]) -> None:
        self._col.update_one(
            {"key": "env"},
            {"$set": {"key": "env", "values": dict(values), "updated_at": utcnow()}},
            upsert=True,
        )

    def get(self, name: str) -> Optional[str]:
        return self.get_all().get(name)

    def set(self, name: str, value: str) -> None:
        values = self.get_all()
        values[name] = value
        self.set_many(values)


class MongoMemoryEntriesStore(MemoryEntriesStore):
    def __init__(self, collection):
        self._col = collection

    def load(self, target: str) -> str:
        key = target.lower().replace(".md", "")
        if key in ("memory", "MEMORY"):
            key = "memory"
        elif key in ("user", "USER"):
            key = "user"
        doc = self._col.find_one({"key": key})
        if not doc:
            return ""
        return str(doc.get("content") or "")

    def save(self, target: str, content: str) -> None:
        key = target.lower().replace(".md", "")
        if "user" in key:
            key = "user"
        else:
            key = "memory"
        self._col.update_one(
            {"key": key},
            {"$set": {"key": key, "content": content, "updated_at": utcnow()}},
            upsert=True,
        )


class MongoSkillsStore(SkillsStore):
    def __init__(self, db, fs_bucket=None):
        self._db = db
        self._col = db["skills"]
        self._fs = fs_bucket
        if self._fs is None:
            from gridfs import GridFS

            self._fs = GridFS(db, collection="skills_fs")

    def list_skills(self) -> list[dict[str, Any]]:
        return [_strip_id(d) or {} for d in self._col.find().sort("name", 1)]

    def get_skill(self, name: str) -> Optional[dict[str, Any]]:
        return _strip_id(self._col.find_one({"name": name}))

    def put_skill(self, skill: dict[str, Any], files: Optional[dict[str, bytes]] = None) -> None:
        name = skill["name"]
        file_ids: dict[str, Any] = {}
        if files:
            # Remove old files for this skill
            for old in self._fs.find({"filename": {"$regex": f"^{name}/"}}):
                self._fs.delete(old._id)
            for rel, data in files.items():
                fid = self._fs.put(data, filename=f"{name}/{rel}", skill=name)
                file_ids[rel] = fid
        doc = dict(skill)
        doc["updated_at"] = utcnow()
        if file_ids:
            doc["file_ids"] = {k: str(v) for k, v in file_ids.items()}
        self._col.update_one({"name": name}, {"$set": doc}, upsert=True)

    def delete_skill(self, name: str) -> bool:
        for old in self._fs.find({"filename": {"$regex": f"^{name}/"}}):
            self._fs.delete(old._id)
        result = self._col.delete_one({"name": name})
        return result.deleted_count > 0

    def materialize(self, name: str, dest_dir: Path) -> Path:
        dest = Path(dest_dir) / name
        dest.mkdir(parents=True, exist_ok=True)
        skill = self.get_skill(name)
        if not skill:
            raise FileNotFoundError(f"Skill not found: {name}")
        body = skill.get("skill_md") or skill.get("body") or ""
        (dest / "SKILL.md").write_text(body, encoding="utf-8")
        for grid_file in self._fs.find({"filename": {"$regex": f"^{name}/"}}):
            rel = grid_file.filename.split("/", 1)[-1]
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "wb") as fh:
                fh.write(grid_file.read())
        return dest


class MongoMachineStore(MachineStore):
    """Per-PC overlays: one document in ``machines`` plus dedicated collection."""

    def __init__(self, profile_db):
        self._db = profile_db
        self._index = profile_db["machines"]

    def upsert_machine(self, machine_id: str, doc: dict[str, Any]) -> None:
        payload = dict(doc)
        payload["machine_id"] = machine_id
        payload["updated_at"] = utcnow()
        self._index.update_one(
            {"machine_id": machine_id},
            {"$set": payload},
            upsert=True,
        )
        col_name = machine_collection_name(machine_id)
        self._db[col_name].update_one(
            {"_kind": "meta"},
            {"$set": {**payload, "_kind": "meta"}},
            upsert=True,
        )

    def get_machine(self, machine_id: str) -> Optional[dict[str, Any]]:
        return _strip_id(self._index.find_one({"machine_id": machine_id}))

    def list_machines(self) -> list[dict[str, Any]]:
        return [_strip_id(d) or {} for d in self._index.find().sort("updated_at", -1)]

    def get_overlay(self, machine_id: str) -> dict[str, Any]:
        col = self._db[machine_collection_name(machine_id)]
        doc = col.find_one({"_kind": "overlay"})
        if not doc:
            # Fall back to index doc overlay field
            machine = self.get_machine(machine_id) or {}
            overlay = machine.get("overlay") or {}
            return dict(overlay) if isinstance(overlay, dict) else {}
        data = doc.get("data") or {}
        return dict(data) if isinstance(data, dict) else {}

    def set_overlay(self, machine_id: str, overlay: dict[str, Any]) -> None:
        col = self._db[machine_collection_name(machine_id)]
        col.update_one(
            {"_kind": "overlay"},
            {"$set": {"_kind": "overlay", "data": overlay, "updated_at": utcnow()}},
            upsert=True,
        )
        self._index.update_one(
            {"machine_id": machine_id},
            {"$set": {"machine_id": machine_id, "overlay": overlay, "updated_at": utcnow()}},
            upsert=True,
        )


class MongoSessionStore(SessionStore):
    def __init__(self, profile_db):
        self._db = profile_db
        self._sessions = profile_db["sessions"]
        self._messages = profile_db["messages"]
        self._routing = profile_db["gateway_routing"]

    def create_session(self, session_id: str, source: str, **kwargs: Any) -> str:
        now = utcnow()
        doc = {
            "session_id": session_id,
            "source": source,
            "started_at": now,
            "updated_at": now,
            "ended_at": None,
            "end_reason": None,
            **kwargs,
        }
        self._sessions.update_one(
            {"session_id": session_id},
            {"$setOnInsert": doc},
            upsert=True,
        )
        return session_id

    def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        return _strip_id(self._sessions.find_one({"session_id": session_id}))

    def update_session(self, session_id: str, **fields: Any) -> None:
        fields = dict(fields)
        fields["updated_at"] = utcnow()
        self._sessions.update_one({"session_id": session_id}, {"$set": fields})

    def end_session(self, session_id: str, end_reason: str) -> None:
        self.update_session(session_id, ended_at=utcnow(), end_reason=end_reason)

    def append_message(self, session_id: str, role: str, content: Any = None, **kwargs: Any) -> int:
        last = self._messages.find_one(
            {"session_id": session_id},
            sort=[("message_index", -1)],
        )
        idx = int(last["message_index"]) + 1 if last else 0
        doc = {
            "session_id": session_id,
            "message_index": idx,
            "role": role,
            "content": content,
            "created_at": utcnow(),
            "active": True,
            **kwargs,
        }
        self._messages.insert_one(doc)
        self._sessions.update_one(
            {"session_id": session_id},
            {"$set": {"updated_at": utcnow()}},
        )
        return idx

    def get_messages(self, session_id: str, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"session_id": session_id}
        if not include_inactive:
            query["active"] = {"$ne": False}
        cursor = self._messages.find(query).sort("message_index", 1)
        return [_strip_id(d) or {} for d in cursor]

    def search_messages(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        if not query or not query.strip():
            return []
        try:
            cursor = self._messages.find(
                {"$text": {"$search": query}},
                {"score": {"$meta": "textScore"}},
            ).sort([("score", {"$meta": "textScore"})]).limit(limit)
            return [_strip_id(d) or {} for d in cursor]
        except Exception:
            # Fallback: regex substring search
            import re

            pattern = re.escape(query.strip())
            cursor = self._messages.find(
                {"content": {"$regex": pattern, "$options": "i"}}
            ).limit(limit)
            return [_strip_id(d) or {} for d in cursor]

    def list_sessions(self, *, limit: int = 50, source: Optional[str] = None) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if source:
            query["source"] = source
        cursor = self._sessions.find(query).sort("updated_at", -1).limit(limit)
        return [_strip_id(d) or {} for d in cursor]

    def delete_session(self, session_id: str) -> bool:
        self._messages.delete_many({"session_id": session_id})
        result = self._sessions.delete_one({"session_id": session_id})
        return result.deleted_count > 0

    def save_gateway_routing_entry(self, key: str, value: str, *, scope: str = "") -> None:
        self._routing.update_one(
            {"scope": scope, "key": key},
            {"$set": {"scope": scope, "key": key, "value": value, "updated_at": utcnow()}},
            upsert=True,
        )

    def load_gateway_routing_entries(self, *, scope: str = "") -> dict[str, str]:
        cursor = self._routing.find({"scope": scope})
        return {str(d["key"]): str(d["value"]) for d in cursor}


class MongoLedgerStore(LedgerStore):
    def __init__(self, profile_db):
        self._db = profile_db

    def insert(self, collection: str, doc: dict[str, Any]) -> str:
        payload = dict(doc)
        if "id" not in payload:
            payload["id"] = str(uuid.uuid4())
        payload.setdefault("created_at", utcnow())
        self._db[collection].insert_one(payload)
        return str(payload["id"])

    def find(
        self,
        collection: str,
        query: Optional[dict[str, Any]] = None,
        *,
        limit: int = 100,
        sort: Optional[list[tuple[str, int]]] = None,
    ) -> list[dict[str, Any]]:
        cursor = self._db[collection].find(query or {})
        if sort:
            cursor = cursor.sort(sort)
        cursor = cursor.limit(limit)
        return [_strip_id(d) or {} for d in cursor]

    def update(self, collection: str, query: dict[str, Any], patch: dict[str, Any]) -> int:
        result = self._db[collection].update_many(query, {"$set": {**patch, "updated_at": utcnow()}})
        return int(result.modified_count)

    def delete(self, collection: str, query: dict[str, Any]) -> int:
        result = self._db[collection].delete_many(query)
        return int(result.deleted_count)

    def replace_one(self, collection: str, query: dict[str, Any], doc: dict[str, Any], *, upsert: bool = True) -> None:
        payload = dict(doc)
        payload["updated_at"] = utcnow()
        self._db[collection].replace_one(query, payload, upsert=upsert)


class MongoClusterStore(ClusterStore):
    STATE_ID = "default"

    def __init__(self, shared_db):
        self._nodes = shared_db["cluster_nodes"]
        self._state = shared_db["cluster_state"]

    def heartbeat(self, node: dict[str, Any]) -> None:
        node_id = node["node_id"]
        payload = dict(node)
        payload["heartbeat_at"] = utcnow()
        payload["status"] = payload.get("status") or "online"
        self._nodes.update_one({"node_id": node_id}, {"$set": payload}, upsert=True)
        # Ensure state doc exists
        self._state.update_one(
            {"_id": self.STATE_ID},
            {"$setOnInsert": {
                "_id": self.STATE_ID,
                "active_node_id": node_id,
                "messaging_owner": node_id,
                "handoff_state": "idle",
                "failover": "auto",
                "history": [],
            }},
            upsert=True,
        )

    def list_nodes(self, *, online_within_s: float = 60.0) -> list[dict[str, Any]]:
        cutoff = utcnow() - timedelta(seconds=online_within_s)
        nodes = []
        for doc in self._nodes.find().sort("hostname", 1):
            item = _strip_id(doc) or {}
            hb = item.get("heartbeat_at")
            if isinstance(hb, datetime):
                if hb.tzinfo is None:
                    hb = hb.replace(tzinfo=timezone.utc)
                item["online"] = hb >= cutoff
            else:
                item["online"] = False
            nodes.append(item)
        return nodes

    def get_state(self) -> dict[str, Any]:
        doc = self._state.find_one({"_id": self.STATE_ID}) or {
            "_id": self.STATE_ID,
            "active_node_id": None,
            "messaging_owner": None,
            "handoff_state": "idle",
            "failover": "auto",
            "history": [],
        }
        return _strip_id(doc) or doc

    def _append_history(self, event: dict[str, Any]) -> None:
        event = dict(event)
        event["at"] = utcnow()
        self._state.update_one(
            {"_id": self.STATE_ID},
            {
                "$push": {"history": {"$each": [event], "$slice": -100}},
                "$set": {"updated_at": utcnow()},
            },
            upsert=True,
        )

    def set_active(self, node_id: str, *, reason: str = "manual") -> dict[str, Any]:
        self._state.update_one(
            {"_id": self.STATE_ID},
            {"$set": {
                "active_node_id": node_id,
                "pending_active_node_id": node_id,
                "updated_at": utcnow(),
            }},
            upsert=True,
        )
        self._append_history({"type": "activate", "node_id": node_id, "reason": reason})
        return self.begin_messaging_handoff(node_id)

    def begin_messaging_handoff(self, target_node_id: str, *, from_node_id: Optional[str] = None) -> dict[str, Any]:
        state = self.get_state()
        current = from_node_id or state.get("messaging_owner")
        self._state.update_one(
            {"_id": self.STATE_ID},
            {"$set": {
                "handoff_state": "releasing",
                "handoff_from": current,
                "handoff_to": target_node_id,
                "handoff_error": None,
                "updated_at": utcnow(),
            }},
            upsert=True,
        )
        self._append_history({
            "type": "handoff_begin",
            "from": current,
            "to": target_node_id,
        })
        return self.get_state()

    def mark_messaging_released(self, node_id: str) -> dict[str, Any]:
        state = self.get_state()
        if state.get("handoff_from") and state.get("handoff_from") != node_id:
            # Still allow force release
            pass
        target = state.get("handoff_to")
        self._state.update_one(
            {"_id": self.STATE_ID},
            {"$set": {
                "handoff_state": "acquiring",
                "messaging_owner": None,
                "updated_at": utcnow(),
            }},
        )
        self._append_history({"type": "messaging_released", "node_id": node_id, "next": target})
        return self.get_state()

    def complete_messaging_handoff(self, node_id: str) -> dict[str, Any]:
        self._state.update_one(
            {"_id": self.STATE_ID},
            {"$set": {
                "handoff_state": "done",
                "messaging_owner": node_id,
                "active_node_id": node_id,
                "pending_active_node_id": None,
                "handoff_from": None,
                "handoff_to": None,
                "handoff_error": None,
                "updated_at": utcnow(),
            }},
        )
        self._append_history({"type": "handoff_done", "node_id": node_id})
        # Reset to idle after done
        self._state.update_one(
            {"_id": self.STATE_ID},
            {"$set": {"handoff_state": "idle"}},
        )
        return self.get_state()

    def rollback_messaging_handoff(self, *, reason: str) -> dict[str, Any]:
        state = self.get_state()
        previous = state.get("handoff_from") or state.get("messaging_owner")
        self._state.update_one(
            {"_id": self.STATE_ID},
            {"$set": {
                "handoff_state": "failed",
                "handoff_error": reason,
                "messaging_owner": previous,
                "active_node_id": previous,
                "pending_active_node_id": None,
                "updated_at": utcnow(),
            }},
        )
        self._append_history({
            "type": "handoff_rollback",
            "to": previous,
            "reason": reason,
        })
        self._state.update_one(
            {"_id": self.STATE_ID},
            {"$set": {
                "handoff_state": "idle",
                "handoff_from": None,
                "handoff_to": None,
            }},
        )
        return self.get_state()

    def mark_node_offline(self, node_id: str) -> None:
        self._nodes.update_one(
            {"node_id": node_id},
            {"$set": {"status": "offline", "updated_at": utcnow()}},
        )
