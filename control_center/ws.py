"""WebSocket broadcaster for Phase D iter2 — live event stream to connected
Control Center pages.

Architecture:
  * One background task per uvicorn worker tails ~/.hermes/events/event_bus.db
    (rowid > last_seen) every 2 seconds and emits new rows to all subscribers.
  * Each WebSocket connection is a Subscriber instance with an asyncio.Queue.
  * Page-side JS listens for "event" frames and triggers HTMX-targeted refreshes
    of the affected panel(s).

Why poll the SQLite (not subscribe to a notification channel): SQLite has no
LISTEN/NOTIFY, and the bus is shared across multiple writers (gateway + crons).
Polling rowid is the established Hermes-internal pattern (see
events/subscribers/base.py BaseSubscriber.poll).

Frame shape (JSON):
  {"kind": "event", "event_type": "...", "source": "...", "priority": "...",
   "summary": "...", "ts": "..."}
  {"kind": "heartbeat", "ts": "..."}     # every 30s, no events
  {"kind": "hello", "subscriber_id": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

EVENT_BUS = Path.home() / ".hermes" / "events" / "event_bus.db"
POLL_INTERVAL_S = 2.0
HEARTBEAT_INTERVAL_S = 30.0

# Event types we forward to the Control Center (filter to the same set the
# activity panel surfaces; otherwise the firehose includes 600K+ historical
# rows and a lot of cron_started noise).
RELAYED_TYPES = (
    "job_discovered", "job_vip_discovered", "job_scored", "job_high_score",
    "tailor_completed", "application_ready", "application_submitted",
    "approval_request", "apply_packet", "stage_transition",
    "critic_proposal", "interview_signal", "offer_signal",
    "agent_error", "cron_failed", "cron_failed_consecutive",
    "gateway_health", "secret_detected",
)


class Subscriber:
    """A connected WebSocket client waiting for events."""

    def __init__(self, ws: WebSocket):
        self.id = uuid.uuid4().hex[:8]
        self.ws = ws
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=128)

    async def send(self, frame: dict) -> bool:
        try:
            await self.ws.send_text(json.dumps(frame, default=str))
            return True
        except Exception as exc:
            logger.debug("ws.send to %s failed: %s", self.id, exc)
            return False


class Broadcaster:
    """Singleton owning the bus-tail task + the subscriber set."""

    def __init__(self):
        self._subs: set[Subscriber] = set()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._last_rowid: int = 0
        self._heartbeat_due_at: float = 0.0

    async def start(self) -> None:
        if self._task is None or self._task.done():
            # Seed last_rowid to "now" so we don't replay history on first start.
            self._last_rowid = self._read_max_rowid()
            self._task = asyncio.create_task(self._tail_loop(), name="cc.ws.tail")
            logger.info("ws.broadcaster: started tail loop from rowid=%s", self._last_rowid)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def add(self, sub: Subscriber) -> None:
        async with self._lock:
            self._subs.add(sub)
        await sub.send({"kind": "hello", "subscriber_id": sub.id})
        logger.info("ws.broadcaster: +sub %s (total=%d)", sub.id, len(self._subs))

    async def remove(self, sub: Subscriber) -> None:
        async with self._lock:
            self._subs.discard(sub)
        logger.info("ws.broadcaster: -sub %s (total=%d)", sub.id, len(self._subs))

    def _read_max_rowid(self) -> int:
        if not EVENT_BUS.exists():
            return 0
        try:
            conn = sqlite3.connect(str(EVENT_BUS))
            row = conn.execute("SELECT MAX(rowid) FROM events").fetchone()
            conn.close()
            return int(row[0] or 0)
        except Exception:
            return 0

    def _read_new(self) -> list[dict]:
        if not EVENT_BUS.exists() or self._last_rowid <= 0:
            return []
        try:
            conn = sqlite3.connect(str(EVENT_BUS))
            types_csv = ",".join(f"'{t}'" for t in RELAYED_TYPES)
            rows = conn.execute(
                f"""
                SELECT rowid, event_id, event_type, source, priority, created_at, payload
                FROM events
                WHERE rowid > ? AND event_type IN ({types_csv})
                ORDER BY rowid ASC
                LIMIT 50
                """,
                (self._last_rowid,),
            ).fetchall()
            # Always advance _last_rowid even past unrelayed types so we don't
            # rescan them every tick.
            max_row = conn.execute("SELECT MAX(rowid) FROM events").fetchone()
            conn.close()
            self._last_rowid = int((max_row and max_row[0]) or self._last_rowid)
            out = []
            for r in rows:
                try:
                    payload = json.loads(r[6]) if r[6] else {}
                except Exception:
                    payload = {}
                out.append(
                    {
                        "rowid": r[0],
                        "event_id": r[1],
                        "event_type": r[2],
                        "source": r[3],
                        "priority": r[4],
                        "ts": r[5],
                        "payload": payload,
                    }
                )
            return out
        except Exception as exc:
            logger.debug("ws.broadcaster: bus read error: %s", exc)
            return []

    @staticmethod
    def _summary(event: dict) -> str:
        et = event.get("event_type", "?")
        p = event.get("payload") or {}
        if et in ("job_scored", "job_high_score"):
            return f"{p.get('title','?')} @ {p.get('company','?')} · score {p.get('score','?')}"
        if et == "approval_request":
            return f"⚠ {p.get('job_title','?')} @ {p.get('job_company','?')} · score {p.get('score','?')}"
        if et == "apply_packet":
            return f"📦 {p.get('title','?')} @ {p.get('company','?')}"
        if et == "stage_transition":
            return f"{p.get('job_title') or p.get('job_id','?')} → {p.get('new_stage','?')}"
        if et == "critic_proposal":
            return f"🧠 {p.get('proposal_id','?')} ({p.get('kind','?')})"
        if et == "agent_error":
            return f"✗ {p.get('subscriber_id') or p.get('error','?')}"
        if et == "gateway_health":
            return f"{p.get('platform','?')} {p.get('status','?')}"
        return ""

    @staticmethod
    def _affects_panel(event_type: str) -> str:
        """Which Control Center panel should refresh when this event fires."""
        if event_type in ("approval_request",):
            return "approvals"
        if event_type == "critic_proposal":
            return "proposals"
        if event_type in ("gateway_health", "agent_error", "cron_failed", "cron_failed_consecutive"):
            return "health"
        return "activity"

    async def _broadcast(self, frame: dict) -> None:
        async with self._lock:
            subs = list(self._subs)
        if not subs:
            return
        for sub in subs:
            ok = await sub.send(frame)
            if not ok:
                await self.remove(sub)

    async def _tail_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL_S)
                events = self._read_new()
                for e in events:
                    frame = {
                        "kind": "event",
                        "event_type": e["event_type"],
                        "source": e["source"],
                        "priority": e["priority"],
                        "ts": e["ts"],
                        "summary": self._summary(e),
                        "panel": self._affects_panel(e["event_type"]),
                    }
                    await self._broadcast(frame)
                # Heartbeat every HEARTBEAT_INTERVAL_S even when bus is quiet.
                now = time.monotonic()
                if now >= self._heartbeat_due_at:
                    self._heartbeat_due_at = now + HEARTBEAT_INTERVAL_S
                    await self._broadcast(
                        {"kind": "heartbeat", "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ws.broadcaster tail loop crashed; will not restart automatically")


broadcaster = Broadcaster()


async def ws_endpoint(websocket: WebSocket) -> None:
    """FastAPI route handler. Accepts the connection, registers a subscriber,
    keeps the socket open, and removes on disconnect.
    """
    await websocket.accept()
    await broadcaster.start()
    sub = Subscriber(websocket)
    await broadcaster.add(sub)
    try:
        while True:
            # We don't expect inbound messages; receive_text just blocks on
            # disconnect. Any inbound is treated as a ping-style nop.
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
            except asyncio.TimeoutError:
                # No client traffic for 60s — drop a heartbeat ourselves so
                # the connection stays warm through proxies.
                if not await sub.send({"kind": "heartbeat", "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}):
                    break
            except WebSocketDisconnect:
                break
    finally:
        await broadcaster.remove(sub)
