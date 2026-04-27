"""MailboxWatcher — polls inter-agent mailbox for new messages and emits events.

Scans ~/.hermes/mailbox/*/inbox/ for new JSON files matching the protocol
naming convention.  Substantive messages are emitted as mailbox_message events.
Tracks seen files via a watermark file to avoid re-processing.

**Recovery pass (2026-04-19):** also scans ~/.hermes/mailbox/*/processed/ for
files modified within PROCESSED_RECOVERY_WINDOW seconds.  The inbox-sweeper
cron (jaum-inbox-sweeper, every 10 min) moves files from inbox/ → processed/
based on classification rules; if the sweeper wins the race against the 60 s
inbox scan (which happens during gateway restarts or when the sweeper
classifies a file as non-actionable before the next scan), the file was
previously lost to the event bus forever.  The recovery pass picks them up
from processed/ within the window and emits events as if scanned from inbox.
Watermark dedup is by filename basename (not relative path), so the same
message is never emitted twice even when moved between dirs.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional, Set

from events.bus import EventBus
from events.schema import EventType

logger = logging.getLogger(__name__)

# How far back to recover files that landed in processed/ without being
# scanned from inbox/.  2 hours comfortably covers a gateway restart loop
# and the 10-min sweeper cadence while bounding the per-poll work.
PROCESSED_RECOVERY_WINDOW_SECONDS = 2 * 60 * 60

# Message types worth mirroring (from protocol.md)
MIRRORED_MESSAGE_TYPES = {
    "SCOUT_DISCOVERY", "SCORE_REQUEST", "SCORE_RESULT", "SCORE_BATCH_SUMMARY",
    "TAILOR_REQUEST", "TAILOR_COMPLETE", "TAILOR_REVISION",
    "TAILOR_MODULE_REQUEST", "TAILOR_MODULE_COMPLETE",
    "SUBMIT_REQUEST", "DRY_RUN_COMPLETE", "SUBMIT_CONFIRM", "BLOCKED_QUESTION",
    "PIPELINE_UPDATE", "STATUS_REQUEST", "STATUS_RESPONSE", "FOLLOWUP_ALERT",
    "NOTIFICATION", "HIGH_SCORE_ALERT",
    "VIP_DISCOVERY", "VIP_PROMOTE", "VIP_SCAN_REQUEST",
    "KB_QUERY", "KB_RESPONSE", "ERROR",
}


def _pipeline_update_aliases(payload: dict) -> tuple[str, str, str]:
    """Normalize PIPELINE_UPDATE field aliases from real mailbox payloads."""
    metadata = payload.get("metadata") or {}
    previous_stage = payload.get("previous_stage") or payload.get("from_stage") or "?"
    new_stage = payload.get("new_stage") or payload.get("to_stage") or "?"
    job_ref = (
        payload.get("company")
        or metadata.get("company")
        or payload.get("job_key")
        or payload.get("job_id")
        or payload.get("title")
        or metadata.get("title")
        or "?"
    )
    return str(previous_stage), str(new_stage), str(job_ref)


class MailboxWatcher:
    """Polls inter-agent mailbox directories for new protocol messages."""

    def __init__(
        self,
        bus: EventBus,
        mailbox_root: Optional[Path] = None,
    ):
        self.bus = bus
        if mailbox_root is None:
            from events.paths import mailbox_root as _default_mailbox_root
            mailbox_root = _default_mailbox_root()
        self.mailbox_root = Path(mailbox_root)
        self._watermark_path = self.mailbox_root / ".event_watermark.json"
        self._seen: Set[str] = self._load_watermark()

    def _load_watermark(self) -> Set[str]:
        """Load the set of already-seen file paths from disk."""
        if self._watermark_path.exists():
            try:
                data = json.loads(self._watermark_path.read_text(encoding="utf-8"))
                return set(data.get("seen", []))
            except (json.JSONDecodeError, KeyError):
                return set()
        return set()

    def _save_watermark(self) -> None:
        """Persist the seen set to disk."""
        self._watermark_path.parent.mkdir(parents=True, exist_ok=True)
        # Keep only the last 2000 entries to prevent unbounded growth
        trimmed = sorted(self._seen)[-2000:]
        self._seen = set(trimmed)
        self._watermark_path.write_text(
            json.dumps({"seen": trimmed}),
            encoding="utf-8",
        )

    def scan(self) -> int:
        """Scan all inboxes (and recent processed/) for new messages.

        Returns count of events emitted.  Dedup is by filename basename so a
        file moved inbox/ → processed/ between polls is never double-emitted.
        """
        if not self.mailbox_root.exists():
            return 0

        # Build a basename set once per scan — watermark stores relative paths
        # like ``main\inbox\foo.json`` and ``main\processed\foo.json`` which
        # must both match the single underlying file when we consult seen.
        seen_basenames: Set[str] = {Path(s).name for s in self._seen}

        count = 0
        recovery_cutoff = time.time() - PROCESSED_RECOVERY_WINDOW_SECONDS

        for profile_dir in self.mailbox_root.iterdir():
            if not profile_dir.is_dir():
                continue

            inbox = profile_dir / "inbox"
            if inbox.exists():
                count += self._scan_dir(
                    inbox,
                    profile_dir,
                    seen_basenames,
                    mtime_cutoff=None,
                    recovery=False,
                )

            # Recovery pass: catch files that jaum-inbox-sweeper (or any
            # other consumer) moved from inbox/ → processed/ before the
            # 60 s inbox scan caught them.  Bounded by recency window to
            # keep per-poll work O(recent-files), not O(archive).
            processed = profile_dir / "processed"
            if processed.exists():
                count += self._scan_dir(
                    processed,
                    profile_dir,
                    seen_basenames,
                    mtime_cutoff=recovery_cutoff,
                    recovery=True,
                )

        if count:
            self._save_watermark()

        return count

    def _scan_dir(
        self,
        directory: Path,
        profile_dir: Path,
        seen_basenames: Set[str],
        mtime_cutoff: Optional[float],
        recovery: bool,
    ) -> int:
        """Scan one directory (inbox or processed) for protocol messages.

        ``seen_basenames`` is mutated: every emitted file's basename is added
        so the next directory in the same scan cannot re-emit it.
        ``mtime_cutoff`` skips files older than the cutoff (for bounded
        recovery scans of processed/).
        """
        count = 0
        for msg_file in directory.iterdir():
            if not msg_file.is_file() or not msg_file.suffix == ".json":
                continue

            if mtime_cutoff is not None:
                try:
                    if msg_file.stat().st_mtime < mtime_cutoff:
                        continue
                except OSError:
                    continue

            file_key = str(msg_file.relative_to(self.mailbox_root))
            if file_key in self._seen or msg_file.name in seen_basenames:
                continue

            self._seen.add(file_key)
            seen_basenames.add(msg_file.name)

            if not self._is_protocol_message(msg_file.name):
                continue

            try:
                msg = json.loads(msg_file.read_text(encoding="utf-8"))
                msg_type = msg.get("type", "")
                if msg_type not in MIRRORED_MESSAGE_TYPES:
                    continue

                payload = {
                    "message_type": msg_type,
                    "from": msg.get("from", "unknown"),
                    "to": msg.get("to", profile_dir.name),
                    "file": file_key,
                    "summary": self._summarize(msg),
                    "inner_payload": msg.get("payload", {}),
                }
                if recovery:
                    payload["recovered_from"] = "processed"

                self.bus.emit(
                    event_type=EventType.MAILBOX_MESSAGE,
                    source=msg.get("from", "unknown"),
                    payload=payload,
                    correlation_id=msg.get("correlation_id"),
                    job_id=msg.get("job_id"),
                )
                count += 1
                if recovery:
                    logger.info(
                        "MailboxWatcher: recovered %s (%s) from processed/ after race",
                        msg_file.name, msg_type,
                    )
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to read mailbox message %s: %s", msg_file, e)

        return count

    def _is_protocol_message(self, filename: str) -> bool:
        """Check if filename matches the protocol naming convention:
        {timestamp}_{TYPE}_{from}.json
        """
        parts = filename.rsplit(".", 1)[0].split("_", 2)
        return len(parts) >= 2

    def _summarize(self, msg: dict) -> str:
        """Create a short human-readable summary of the message payload."""
        payload = msg.get("payload", {})
        msg_type = msg.get("type", "")

        if msg_type == "SCORE_BATCH_SUMMARY":
            jobs = payload.get("results") or payload.get("scored_jobs", [])
            return f"{len(jobs)} jobs scored"
        if msg_type == "SCORE_RESULT":
            score = payload.get("score", "?")
            company = payload.get("company", "?")
            title = payload.get("title", "")
            return f"score {score} for {company} ({title})"
        if msg_type == "SCOUT_DISCOVERY":
            jobs = payload.get("jobs", [])
            return f"{len(jobs)} jobs discovered"
        if msg_type in ("TAILOR_REQUEST", "TAILOR_COMPLETE"):
            return payload.get("job_title") or payload.get("title") or msg_type
        if msg_type == "SUBMIT_REQUEST" or msg_type == "SUBMIT_CONFIRM":
            company = payload.get("company", "?")
            title = payload.get("title", "?")
            return f"{msg_type.lower().replace('_', ' ')}: {company} / {title}"
        if msg_type == "BLOCKED_QUESTION":
            return (payload.get("question") or "Blocked question")[:200]
        if msg_type == "PIPELINE_UPDATE":
            previous_stage, new_stage, job_ref = _pipeline_update_aliases(payload)
            return f"{previous_stage} -> {new_stage} ({job_ref})"
        if msg_type == "FOLLOWUP_ALERT":
            days = payload.get("days_since_application", "?")
            company = payload.get("company", "?")
            return f"follow up with {company} ({days} days since application)"
        if msg_type == "NOTIFICATION":
            # Notifications often carry their own summary/body. Coerce to str
            # because some producers send dict/list payloads.
            return str(payload.get("summary") or payload.get("body") or "Notification")[:200]
        if msg_type == "ERROR":
            return str(payload.get("message") or "Error")[:200]

        return msg_type
