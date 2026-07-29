"""Durable outbox for Telegram sends — survives SIGKILL / host reboot.

Problem this solves: an in-flight Telegram send that dies mid-flight (process
killed, host rebooted) before send_message_tool returns is silently lost —
there is no record it was ever attempted. A SIGTERM handler can flush
in-flight work on a *graceful* shutdown, but it can never catch SIGKILL or an
unclean host reboot, which never deliver any signal at all.

Design: append-before-send, delete-after-success. The record on disk is the
single source of truth for "did this get sent" — no reliance on any
in-memory state or signal handler surviving the death of the process.

    entry_id = outbox_append(chat_id, message, thread_id=thread_id)
    try:
        result = await _send_to_platform(...)
        if result.get("success"):
            outbox_mark_sent(entry_id)
    except Exception:
        ...  # entry stays pending — picked up by the next outbox_drain()

outbox_drain() is invoked by the gateway after the Telegram adapter reports
connected (cold start and outage reconnect; see
GatewayRunner._schedule_telegram_outbox_drain) with an item/deadline budget.
It stays independently callable for tests and manual recovery.

Every function here is best-effort: a failure in outbox bookkeeping must
NEVER prevent or delay the real send it wraps. Callers should treat all
outbox_* functions as advisory and continue past any exception.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_OUTBOX_FILENAME = "telegram-outbox.jsonl"


class _outbox_lock:
    """Advisory inter-process lock over the outbox file (sidecar .lock).

    Serializes hot-path appends/tombstones against the drain's compaction
    rewrite so an append landing mid-compaction can never be dropped by the
    os.replace. Held only around file I/O — never across network sends.
    Best-effort like everything here: if flock is unavailable (non-POSIX) or
    fails, callers proceed unlocked rather than blocking the real send.
    """

    def __init__(self, path: Path):
        self._lock_path = path.with_suffix(path.suffix + ".lock")
        self._fh = None

    def __enter__(self):
        try:
            import fcntl

            self._fh = open(self._lock_path, "a+")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._fh.close()
            except Exception:
                pass
        return False


def _outbox_path() -> Path:
    """Resolve the outbox file path under the Hermes state directory.

    Imports get_hermes_home() lazily so this module has no import-time
    dependency on the rest of the agent — keeps it independently testable.
    """
    from hermes_constants import get_hermes_home

    state_dir = get_hermes_home() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / _OUTBOX_FILENAME


def outbox_append(chat_id: str, message: str, thread_id: str | None = None) -> str | None:
    """Record a pending send before attempting it. Returns the entry_id, or
    None if the append itself failed (caller proceeds with the send regardless
    — durability is best-effort, never a gate on whether a message can send).
    """
    entry_id = uuid.uuid4().hex
    entry = {
        "id": entry_id,
        "chat_id": chat_id,
        "message": message,
        "thread_id": thread_id,
        "created_at": time.time(),
        "status": "pending",
    }
    try:
        path = _outbox_path()
        with _outbox_lock(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry_id
    except Exception as e:
        logger.warning("telegram_outbox: append failed (send proceeds anyway): %s", e)
        return None


def outbox_mark_sent(entry_id: str | None) -> None:
    """Mark an entry as sent by appending a tombstone record with the same id.

    Append-only by design (never rewrites/truncates the file in the hot
    send path — that would risk corrupting the file if interrupted mid-write).
    Compaction (dropping fully-resolved id pairs) happens in outbox_drain(),
    which runs far less often and can afford a rewrite-the-whole-file pass.
    """
    if not entry_id:
        return
    try:
        path = _outbox_path()
        with _outbox_lock(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"id": entry_id, "status": "sent", "sent_at": time.time()}, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("telegram_outbox: mark_sent failed for %s (message was sent; only the outbox record is stale): %s", entry_id, e)


def _load_resolved_ids(lines: list[str]) -> tuple[dict[str, dict], set[str]]:
    """Parse outbox lines into (id -> pending entry, set of resolved ids).

    Malformed lines are skipped (best-effort log parsing — a single corrupt
    line must never make the whole outbox unreadable).
    """
    pending: dict[str, dict] = {}
    resolved: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        rid = rec.get("id")
        if not rid:
            continue
        if rec.get("status") == "sent":
            resolved.add(rid)
        elif rec.get("status") == "pending":
            pending[rid] = rec
    return pending, resolved


def outbox_pending_entries() -> list[dict]:
    """Return pending (not-yet-marked-sent) entries, oldest first.

    Read-only — safe to call for inspection/monitoring without side effects.
    """
    path = _outbox_path()
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        logger.warning("telegram_outbox: failed to read outbox for inspection: %s", e)
        return []
    pending, resolved = _load_resolved_ids(lines)
    return [e for rid, e in sorted(pending.items(), key=lambda kv: kv[1].get("created_at", 0)) if rid not in resolved]


def outbox_drain(
    send_fn=None,
    max_age_seconds: float = 7 * 24 * 3600,
    max_items: int = 100,
    deadline_seconds: float | None = None,
    grace_seconds: float = 60.0,
) -> dict:
    """Re-attempt delivery for still-pending entries, then compact the file.

    Bounded: at most ``max_items`` resend attempts per call, and no new
    attempt starts after ``deadline_seconds`` (entries left over simply stay
    pending for the next drain). The deadline bounds *starting* attempts —
    a single already-running ``send_fn`` call (network stall, RetryAfter
    sleep) can overshoot it; strict wall-clock kill of a synchronous sender
    is deliberately out of scope here. Entries younger than ``grace_seconds`` are
    skipped — an entry that fresh is very likely an in-flight send by a live
    sender in this or another process, and resending it now would double-send;
    it either gets tombstoned by its own sender or picked up by a later drain.

    Args:
        send_fn: callable(chat_id, message, thread_id) -> bool (True = delivered).
            Defaults to a real Telegram send via send_message_tool when None
            (kept as a parameter so this function is unit-testable without
            hitting the network or requiring gateway config to be loaded).
        max_age_seconds: entries older than this are dropped without a resend
            attempt (default 7 days) — an alert this stale has lost its value
            and resending it would be confusing, not helpful.

    Returns a summary dict: {"attempted": N, "sent": N, "dropped_stale": N,
    "still_pending": N}. Never raises — any per-entry failure just leaves
    that entry pending for the next drain call.
    """
    if send_fn is None:
        def send_fn(chat_id, message, thread_id):
            from tools.send_message_tool import send_message_tool
            # Telegram topic target format is "chat_id:thread_id" (see
            # _TELEGRAM_TOPIC_TARGET_RE in send_message_tool.py).
            target = f"telegram:{chat_id}:{thread_id}" if thread_id else f"telegram:{chat_id}"
            # _skip_outbox=True: this call IS the drain's own resend attempt —
            # without this flag _handle_send would outbox_append() again on
            # every retry, growing the file with duplicate pending entries
            # for what is logically the same message.
            result_str = send_message_tool({
                "action": "send",
                "target": target,
                "message": message,
                "_skip_outbox": True,
            })
            try:
                result = json.loads(result_str)
            except Exception:
                return False
            return bool(result.get("success"))

    path = _outbox_path()
    if not path.exists():
        return {"attempted": 0, "sent": 0, "dropped_stale": 0, "still_pending": 0}

    try:
        with _outbox_lock(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
    except Exception as e:
        logger.warning("telegram_outbox: drain could not read outbox: %s", e)
        return {"attempted": 0, "sent": 0, "dropped_stale": 0, "still_pending": 0}

    pending, resolved = _load_resolved_ids(lines)
    now = time.time()
    started = time.monotonic()
    attempted = 0
    sent = 0
    dropped_stale = 0
    sent_ids: set[str] = set()
    stale_ids: set[str] = set()

    for rid, entry in pending.items():
        if rid in resolved:
            continue
        age = now - entry.get("created_at", now)
        if age > max_age_seconds:
            dropped_stale += 1
            stale_ids.add(rid)
            continue
        if age < grace_seconds:
            # Likely in-flight by a live sender — leave it alone this pass.
            continue
        if attempted >= max_items:
            break
        if deadline_seconds is not None and time.monotonic() - started > deadline_seconds:
            break
        attempted += 1
        try:
            ok = bool(send_fn(entry.get("chat_id"), entry.get("message"), entry.get("thread_id")))
        except Exception as e:
            logger.warning("telegram_outbox: drain resend attempt raised for %s: %s", rid, e)
            ok = False
        if ok:
            sent += 1
            sent_ids.add(rid)

    # Fallback summary count from the snapshot (used when compaction fails:
    # entries then remain on disk, so reporting 0 would be wrong).
    still_pending_count = len(
        [rid for rid in pending if rid not in resolved and rid not in sent_ids and rid not in stale_ids]
    )
    # Compact under the lock, against a FRESH read of the file — not the
    # pre-send snapshot. Appends/tombstones that landed while this drain was
    # sending (from live senders in this or another process) are therefore
    # preserved, closing the lost-append window a snapshot-based os.replace
    # would have. We drop only ids that are resolved in the fresh content,
    # were sent by this drain, or aged out. Atomic tmp+os.replace as before.
    try:
        with _outbox_lock(path):
            with open(path, "r", encoding="utf-8") as f:
                fresh_lines = f.readlines()
            fresh_pending, fresh_resolved = _load_resolved_ids(fresh_lines)
            drop = fresh_resolved | sent_ids | stale_ids
            keep = {rid: e for rid, e in fresh_pending.items() if rid not in drop}
            still_pending_count = len(keep)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in keep.values():
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.replace(tmp_path, path)
    except Exception as e:
        logger.warning("telegram_outbox: drain compaction failed (outbox left as-is, will re-attempt next drain): %s", e)

    return {
        "attempted": attempted,
        "sent": sent,
        "dropped_stale": dropped_stale,
        "still_pending": still_pending_count,
    }
