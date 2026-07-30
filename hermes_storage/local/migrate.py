"""Read local $HERMES_HOME state for one-shot migration into Mongo."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to parse %s: %s", path, exc)
        return {}


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key:
            values[key] = val
    return values


def export_local_home(home: Path) -> dict[str, Any]:
    """Export a Hermes home directory into a portable dict for Mongo import."""
    home = Path(home)
    payload: dict[str, Any] = {
        "config": _read_yaml(home / "config.yaml"),
        "secrets": _read_env_file(home / ".env"),
        "soul": _read_text(home / "SOUL.md"),
        "memories": {
            "memory": _read_text(home / "memories" / "MEMORY.md"),
            "user": _read_text(home / "memories" / "USER.md"),
        },
        "skills": [],
        "sessions": [],
        "messages": [],
        "cron_jobs": None,
        "auth": None,
    }

    auth_path = home / "auth.json"
    if auth_path.is_file():
        try:
            payload["auth"] = json.loads(auth_path.read_text(encoding="utf-8"))
        except Exception:
            payload["auth"] = None

    skills_root = home / "skills"
    if skills_root.is_dir():
        for skill_md in skills_root.rglob("SKILL.md"):
            rel_dir = skill_md.parent.relative_to(skills_root)
            name = skill_md.parent.name
            files: dict[str, bytes] = {}
            for f in skill_md.parent.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(skill_md.parent)).replace("\\", "/")
                    try:
                        files[rel] = f.read_bytes()
                    except OSError:
                        continue
            payload["skills"].append({
                "name": name,
                "path": str(rel_dir).replace("\\", "/"),
                "skill_md": _read_text(skill_md),
                "files": files,
            })

    jobs_path = home / "cron" / "jobs.json"
    if jobs_path.is_file():
        try:
            payload["cron_jobs"] = json.loads(jobs_path.read_text(encoding="utf-8"))
        except Exception:
            payload["cron_jobs"] = None

    state_db = home / "state.db"
    if state_db.is_file():
        try:
            conn = sqlite3.connect(str(state_db))
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            try:
                sessions = cur.execute("SELECT * FROM sessions").fetchall()
                payload["sessions"] = [dict(r) for r in sessions]
            except sqlite3.Error as exc:
                logger.warning("Could not export sessions: %s", exc)
            try:
                messages = cur.execute(
                    "SELECT * FROM messages ORDER BY session_id, id"
                ).fetchall()
                payload["messages"] = [dict(r) for r in messages]
            except sqlite3.Error as exc:
                logger.warning("Could not export messages: %s", exc)
            conn.close()
        except sqlite3.Error as exc:
            logger.warning("Could not open state.db: %s", exc)

    return payload


def import_payload_to_storage(storage: Any, payload: dict[str, Any]) -> dict[str, int]:
    """Write an export payload into Mongo via HermesStorage. Returns counts."""
    counts = {
        "config": 0,
        "secrets": 0,
        "soul": 0,
        "memories": 0,
        "skills": 0,
        "sessions": 0,
        "messages": 0,
        "cron": 0,
    }

    if payload.get("config"):
        storage.save_profile_config(payload["config"])
        storage.save_machine_overlay_from_config(payload["config"])
        counts["config"] = 1

    secrets = dict(payload.get("secrets") or {})
    if payload.get("auth") is not None:
        secrets["__auth_json__"] = json.dumps(payload["auth"])
    if secrets:
        storage.secrets.set_many(secrets)
        counts["secrets"] = len(secrets)

    if payload.get("soul"):
        storage.save_soul(payload["soul"])
        counts["soul"] = 1

    memories = payload.get("memories") or {}
    for key in ("memory", "user"):
        if memories.get(key):
            storage.memories.save(key, memories[key])
            counts["memories"] += 1

    for skill in payload.get("skills") or []:
        meta = {
            "name": skill["name"],
            "path": skill.get("path"),
            "skill_md": skill.get("skill_md") or "",
        }
        storage.skills.put_skill(meta, files=skill.get("files") or {})
        counts["skills"] += 1

    # Sessions / messages
    for sess in payload.get("sessions") or []:
        sid = sess.get("id") or sess.get("session_id")
        if not sid:
            continue
        fields = {k: v for k, v in sess.items() if k not in ("id", "session_id", "_id")}
        storage.sessions.create_session(str(sid), str(sess.get("source") or "unknown"), **fields)
        counts["sessions"] += 1

    # Group messages by session and re-append in order
    by_session: dict[str, list] = {}
    for msg in payload.get("messages") or []:
        sid = str(msg.get("session_id") or "")
        if not sid:
            continue
        by_session.setdefault(sid, []).append(msg)
    for sid, msgs in by_session.items():
        msgs.sort(key=lambda m: m.get("id") or m.get("message_index") or 0)
        for msg in msgs:
            storage.sessions.append_message(
                sid,
                role=str(msg.get("role") or "user"),
                content=msg.get("content"),
                tool_calls=msg.get("tool_calls"),
                tool_call_id=msg.get("tool_call_id"),
                reasoning=msg.get("reasoning"),
                active=bool(msg.get("active", True)),
            )
            counts["messages"] += 1

    cron = payload.get("cron_jobs")
    if cron is not None:
        storage.ledgers.replace_one("cron_jobs", {"key": "default"}, {"key": "default", "data": cron})
        counts["cron"] = 1

    return counts
