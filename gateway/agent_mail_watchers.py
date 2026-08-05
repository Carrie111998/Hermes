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

logger = logging.getLogger("gateway.run")

_MAIL_DB = Path.home() / ".cc-workspace" / "Resources" / "external" / "mcp_agent_mail" / "storage.sqlite3"
_DEFAULT_PROJECT = "/Users/zt_mini/.cc-workspace"


def _state_path(profile: str) -> Path:
    return Path.home() / ".hermes" / "state" / "agent-mail-watchers" / f"{profile}.json"


def _load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"last_seen_message_id": 0}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n")


def _fetch_unread(identity: str, project_key: str, after_id: int, limit: int) -> list[dict[str, Any]]:
    con = sqlite3.connect(f"file:{_MAIL_DB}?mode=ro", uri=True, timeout=5)
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


def _mark_read(identity: str, project_key: str, message_id: int) -> None:
    con = sqlite3.connect(_MAIL_DB, timeout=5)
    try:
        con.execute(
            """
            update message_recipients set read_ts = coalesce(read_ts, ?)
             where message_id = ? and agent_id = (
                select a.id from agents a join projects p on p.id = a.project_id
                 where p.human_key = ? and a.name = ? and a.retired_at is null
             )
            """,
            (time.strftime("%Y-%m-%dT%H:%M:%S%z"), message_id, project_key, identity),
        )
        con.commit()
    finally:
        con.close()


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
        if not identity or not channel_id or not user_id:
            logger.warning("agent-mail watcher disabled: identity, channel_id, or user_id missing")
            return
        try:
            interval = max(2.0, float(cfg.get("poll_seconds", 10)))
            batch_size = max(1, min(10, int(cfg.get("batch_size", 3))))
        except (TypeError, ValueError):
            interval, batch_size = 10.0, 3
        project_key = str(cfg.get("project_key") or _DEFAULT_PROJECT)
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
                    # `handle_message` queues a normal agent turn and returns before
                    # its response. Leave read-state ownership to the identity-bound
                    # Agent Mail tool after that turn has classified the message; the
                    # durable cursor prevents duplicate wakes meanwhile.
                    state.update({"last_seen_message_id": message_id, "last_delivered_at": int(time.time()), "identity": identity})
                    await asyncio.to_thread(_save_state, state_file, state)
                    logger.info("agent-mail watcher delivered profile=%s identity=%s message_id=%s", profile, identity, message_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("agent-mail watcher profile=%s identity=%s failed: %s", profile, identity, exc, exc_info=True)
            await asyncio.sleep(interval)
