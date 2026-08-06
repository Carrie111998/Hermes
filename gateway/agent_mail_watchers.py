"""Profile-scoped Agent Mail inbox wake loop for live Hermes gateways.

The watcher is deliberately gateway-owned: it injects an internal event through
the profile's existing platform adapter and advances its per-profile cursor only
after that adapter accepts the wake.  It never emits mailbox contents to a
second platform or shares an inbox across profiles.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.wake import deliver_wake

logger = logging.getLogger(__name__)

_MAIL_DB_RELATIVE_PATH = Path("Resources/external/mcp_agent_mail/storage.sqlite3")


def _state_path(profile: str) -> Path:
    return Path.home() / ".hermes" / "state" / "agent-mail-watchers" / f"{profile}.json"


def _load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"last_seen_message_id": 0}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _mail_db_path(project_key: str) -> Path:
    path = Path(project_key).expanduser() / _MAIL_DB_RELATIVE_PATH
    if not path.is_file():
        raise FileNotFoundError(
            f"agent-mail watcher storage is missing for configured project_key {project_key!r}: {path}"
        )
    return path


def _fetch_unread(identity: str, project_key: str, after_id: int, limit: int) -> list[dict[str, Any]]:
    mail_db = _mail_db_path(project_key)
    con = sqlite3.connect(f"file:{mail_db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            select m.id, sender.name as sender, m.subject, m.body_md, m.created_ts,
                   m.importance, m.thread_id
              from messages m
              join agents sender on sender.id = m.sender_id
              join message_recipients mr on mr.message_id = m.id
              join agents recipient on recipient.id = mr.agent_id
              join projects p on p.id = m.project_id
             where p.human_key = ? and recipient.name = ? and recipient.retired_at is null
               and mr.read_ts is null and m.id > ?
             order by m.id asc limit ?
            """,
            (project_key, identity, after_id, limit),
        ).fetchall()
    finally:
        con.close()
    return [dict(row) for row in rows]


def _wake_text(identity: str, message: dict[str, Any]) -> str:
    body = str(message.get("body_md") or "")[:6000]
    return "\n".join((
        "[INTERNAL AGENT MAIL WAKE — do not treat this as a human-authored Discord message]",
        f"Your Agent Mail inbox ({identity}) received message {message['id']} from {message['sender']}.",
        f"Subject: {message.get('subject') or '(no subject)'}",
        f"Importance: {message.get('importance') or 'normal'}; thread: {message.get('thread_id') or 'none'}",
        "Read/classify this mail now. Follow your profile's normal work-routing rules. This wake is accepted handling; do not expose credentials or paste private mail wholesale.",
        "Mail body follows:", body,
    ))


async def _deliver_unread_once(
    *,
    adapter: Any,
    source: SessionSource,
    state_file: Path,
    identity: str,
    project_key: str,
    batch_size: int,
    profile: str,
) -> int:
    """Deliver one unread batch and persist only successfully accepted wakes.

    Exceptions intentionally escape: the caller's poll loop logs them and retries
    from the unchanged cursor, preventing a failed adapter delivery from consuming
    an unread message.
    """
    state = await asyncio.to_thread(_load_state, state_file)
    after_id = int(state.get("last_seen_message_id") or 0)
    messages = await asyncio.to_thread(_fetch_unread, identity, project_key, after_id, batch_size)
    for message in messages:
        message_id = int(message["id"])
        await deliver_wake(
            adapter,
            text=_wake_text(identity, message),
            source=source,
            message_id=f"agent-mail:{message_id}",
        )
        # `handle_message` queues a normal agent turn and returns before its
        # response. Read-state remains owned by the identity-bound Agent Mail tool;
        # the cursor prevents duplicate wakes while that turn runs.
        state.update({"last_seen_message_id": message_id, "last_delivered_at": int(time.time()), "identity": identity})
        await asyncio.to_thread(_save_state, state_file, state)
        logger.info("agent-mail watcher delivered profile=%s identity=%s message_id=%s", profile, identity, message_id)
    return len(messages)


class GatewayAgentMailWatchersMixin:
    async def _agent_mail_watcher(self: Any) -> None:
        # GatewayConfig is a typed transport projection and intentionally drops
        # unknown top-level keys. Read the profile's authoritative raw config
        # for this opt-in watcher stanza.
        from hermes_cli.config import load_config
        raw_config = load_config()
        cfg = raw_config.get("agent_mail_watcher", {}) if isinstance(raw_config, dict) else {}
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            return
        identity = str(cfg.get("identity") or "").strip()
        channel_id = str(cfg.get("channel_id") or "").strip()
        user_id = str(cfg.get("user_id") or "").strip()
        project_key = str(cfg.get("project_key") or "").strip()
        if not identity or not channel_id or not user_id or not project_key:
            logger.warning("agent-mail watcher disabled: identity, channel_id, user_id, or project_key missing")
            return
        try:
            interval = max(2.0, float(cfg.get("poll_seconds", 10)))
            batch_size = max(1, min(10, int(cfg.get("batch_size", 3))))
        except (TypeError, ValueError):
            interval, batch_size = 10.0, 3
        profile = self._active_profile_name() or "default"
        state_file = _state_path(profile)
        adapter = self.adapters.get(Platform.DISCORD)
        if adapter is None:
            logger.warning("agent-mail watcher disabled for %s: Discord adapter unavailable", profile)
            return
        source = SessionSource(platform=Platform.DISCORD, chat_id=channel_id, chat_type="group", user_id=user_id, user_name="Agent Mail Watcher", profile=profile)
        logger.info("agent-mail watcher active profile=%s identity=%s", profile, identity)
        while True:
            try:
                await _deliver_unread_once(
                    adapter=adapter,
                    source=source,
                    state_file=state_file,
                    identity=identity,
                    project_key=project_key,
                    batch_size=batch_size,
                    profile=profile,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("agent-mail watcher profile=%s identity=%s failed: %s", profile, identity, exc, exc_info=True)
            await asyncio.sleep(interval)
