"""Unified, consent-first learning inbox.

The inbox is an adapter over Hermes' existing approval stores. It deliberately
owns no second knowledge database:

* staged memory and skill writes come from ``tools.write_approval``;
* automation proposals come from ``cron.suggestions``.

The desktop can therefore present one review surface while approval still uses
the same replay and deduplication paths as the CLI and gateway commands.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from hermes_time import now as _hermes_now
from tools import write_approval as wa

MAX_PREVIEW_CHARS = 4_000
_VALID_WRITE_KINDS = frozenset({"memory", "skill"})


def _iso_from_timestamp(value: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _truncate(value: str, limit: int = MAX_PREVIEW_CHARS) -> str:
    value = str(value or "")
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n…"


def _write_title(kind: str, payload: Dict[str, Any]) -> str:
    action = str(payload.get("action") or "update").replace("_", " ")
    if kind == wa.MEMORY:
        target = str(payload.get("target") or "memory")
        return f"Memory {action} · {target}"
    name = str(payload.get("name") or "skill")
    return f"Skill {action} · {name}"


def _memory_preview(payload: Dict[str, Any]) -> str:
    if payload.get("action") == "batch":
        operations = payload.get("operations") or []
        return "\n".join(
            f"{op.get('action', 'update')}: {op.get('content') or op.get('old_text') or ''}"
            for op in operations
            if isinstance(op, dict)
        )
    return str(payload.get("content") or payload.get("old_text") or "")


def _write_item(subsystem: str, record: Dict[str, Any]) -> Dict[str, Any]:
    payload_value = record.get("payload")
    payload: Dict[str, Any] = payload_value if isinstance(payload_value, dict) else {}
    kind = "memory" if subsystem == wa.MEMORY else "skill"
    return {
        "id": f"{kind}:{record.get('id', '')}",
        "kind": kind,
        "title": _write_title(kind, payload),
        "summary": str(record.get("summary") or ""),
        "source": str(record.get("origin") or "foreground"),
        "created_at": _iso_from_timestamp(record.get("created_at")),
        "action": str(record.get("action") or payload.get("action") or "update"),
        "target": payload.get("target") or payload.get("name"),
        "preview": _truncate(_memory_preview(payload) if kind == "memory" else record.get("summary", "")),
        "actions": ["approve", "dismiss"],
    }


def _automation_item(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": f"automation:{record.get('id', '')}",
        "kind": "automation",
        "title": str(record.get("title") or "Suggested automation"),
        "summary": str(record.get("description") or ""),
        "source": str(record.get("source") or "usage"),
        "created_at": str(record.get("created_at") or "") or None,
        "action": "create_job",
        "target": record.get("dedup_key"),
        "preview": _truncate(str(record.get("description") or "")),
        "actions": ["approve", "dismiss"],
    }


def _split_ref(kind: str, item_id: str) -> Tuple[str, str]:
    if kind not in {"memory", "skill", "automation"}:
        raise ValueError(f"Unknown learning item kind: {kind}")
    if not item_id or "/" in item_id or "\\" in item_id or not all(c in "0123456789abcdef" for c in item_id):
        raise ValueError("Invalid learning item id")
    return kind, item_id


def _iter_items() -> Iterable[Dict[str, Any]]:
    for subsystem in (wa.MEMORY, wa.SKILLS):
        for record in wa.list_pending(subsystem):
            yield _write_item(subsystem, record)

    from cron import suggestions

    for record in suggestions.list_pending():
        yield _automation_item(record)


def list_items() -> List[Dict[str, Any]]:
    """Return pending learning candidates in creation order."""
    items = list(_iter_items())
    items.sort(key=lambda item: item.get("created_at") or "")
    return items


def inbox_payload() -> Dict[str, Any]:
    """Build the REST-safe inbox payload for the active profile."""
    items = list_items()
    counts = {"memory": 0, "skill": 0, "automation": 0}
    for item in items:
        counts[item["kind"]] += 1
    return {
        "items": items,
        "count": len(items),
        "counts": counts,
        "settings": {
            "memory_write_approval": wa.write_approval_enabled(wa.MEMORY),
            "skills_write_approval": wa.write_approval_enabled(wa.SKILLS),
        },
        "generated_at": _hermes_now().isoformat(),
    }


def get_item(kind: str, item_id: str) -> Optional[Dict[str, Any]]:
    """Return one candidate with its full review detail."""
    kind, item_id = _split_ref(kind, item_id)
    if kind in _VALID_WRITE_KINDS:
        subsystem = wa.MEMORY if kind == "memory" else wa.SKILLS
        record = wa.get_pending(subsystem, item_id)
        if not record:
            return None
        item = _write_item(subsystem, record)
        payload_value = record.get("payload")
        payload: Dict[str, Any] = payload_value if isinstance(payload_value, dict) else {}
        if kind == "skill":
            item["detail"] = _truncate(wa.skill_pending_diff(record), limit=40_000)
        else:
            item["detail"] = _truncate(
                json.dumps(payload, ensure_ascii=False, indent=2), limit=40_000
            )
        item["evidence"] = {
            "origin": record.get("origin") or "foreground",
            "session_id": record.get("session_id"),
            "note": "The current staged record does not include a session reference."
            if not record.get("session_id")
            else None,
        }
        return item

    from cron import suggestions

    record = suggestions.get_suggestion(item_id)
    if not record or record.get("status") != "pending":
        return None
    item = _automation_item(record)
    item["detail"] = _truncate(
        json.dumps(record.get("job_spec") or {}, ensure_ascii=False, indent=2), limit=40_000
    )
    item["evidence"] = {
        "source": record.get("source") or "usage",
        "dedup_key": record.get("dedup_key"),
    }
    return item


def _apply_write(kind: str, item_id: str) -> Dict[str, Any]:
    subsystem = wa.MEMORY if kind == "memory" else wa.SKILLS
    record = wa.get_pending(subsystem, item_id)
    if not record:
        return {"ok": False, "error": "Learning candidate not found or already resolved."}

    payload_value = record.get("payload")
    payload: Dict[str, Any] = payload_value if isinstance(payload_value, dict) else {}
    try:
        if kind == "memory":
            from tools.memory_tool import apply_memory_pending, load_on_disk_store

            result = apply_memory_pending(payload, load_on_disk_store())
        else:
            from tools.skill_manager_tool import apply_skill_pending

            result = json.loads(apply_skill_pending(payload))
    except Exception as exc:  # pragma: no cover - defensive boundary
        return {"ok": False, "error": str(exc)}

    if not result.get("success"):
        return {"ok": False, "error": result.get("error") or "The proposed write failed."}
    wa.discard_pending(subsystem, item_id)
    return {"ok": True, "kind": kind, "id": item_id}


def approve(kind: str, item_id: str) -> Dict[str, Any]:
    kind, item_id = _split_ref(kind, item_id)
    if kind in _VALID_WRITE_KINDS:
        return _apply_write(kind, item_id)

    from cron import suggestions

    try:
        job = suggestions.accept_suggestion(item_id)
    except Exception as exc:  # pragma: no cover - defensive boundary
        return {"ok": False, "error": str(exc)}
    if job is None:
        return {"ok": False, "error": "Learning candidate not found or already resolved."}
    return {"ok": True, "kind": kind, "id": item_id, "job": job}


def dismiss(kind: str, item_id: str) -> Dict[str, Any]:
    kind, item_id = _split_ref(kind, item_id)
    if kind in _VALID_WRITE_KINDS:
        subsystem = wa.MEMORY if kind == "memory" else wa.SKILLS
        if not wa.discard_pending(subsystem, item_id):
            return {"ok": False, "error": "Learning candidate not found or already resolved."}
        return {"ok": True, "kind": kind, "id": item_id}

    from cron import suggestions

    if not suggestions.dismiss_suggestion(item_id):
        return {"ok": False, "error": "Learning candidate not found or already resolved."}
    return {"ok": True, "kind": kind, "id": item_id}
