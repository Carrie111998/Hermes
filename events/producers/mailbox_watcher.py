"""MailboxWatcher — polls inter-agent mailbox for new messages and emits events.

Scans ~/.hermes/mailbox/*/inbox/ for new JSON files matching the protocol
naming convention.  Substantive messages are emitted as mailbox_message events.
Tracks seen files via a watermark file to avoid re-processing.
"""

import json
import logging
from pathlib import Path
from typing import Optional, Set

from events.bus import EventBus
from events.schema import EventType

logger = logging.getLogger(__name__)

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
        """Scan all inboxes for new messages.  Returns count of events emitted."""
        if not self.mailbox_root.exists():
            return 0

        count = 0
        for profile_dir in self.mailbox_root.iterdir():
            if not profile_dir.is_dir():
                continue
            inbox = profile_dir / "inbox"
            if not inbox.exists():
                continue

            for msg_file in inbox.iterdir():
                if not msg_file.is_file() or not msg_file.suffix == ".json":
                    continue

                file_key = str(msg_file.relative_to(self.mailbox_root))
                if file_key in self._seen:
                    continue

                self._seen.add(file_key)

                if not self._is_protocol_message(msg_file.name):
                    continue

                try:
                    msg = json.loads(msg_file.read_text(encoding="utf-8"))
                    msg_type = msg.get("type", "")
                    if msg_type not in MIRRORED_MESSAGE_TYPES:
                        continue

                    self.bus.emit(
                        event_type=EventType.MAILBOX_MESSAGE,
                        source=msg.get("from", "unknown"),
                        payload={
                            "message_type": msg_type,
                            "from": msg.get("from", "unknown"),
                            "to": msg.get("to", profile_dir.name),
                            "file": file_key,
                            "summary": self._summarize(msg),
                            "inner_payload": msg.get("payload", {}),
                        },
                        correlation_id=msg.get("correlation_id"),
                        job_id=msg.get("job_id"),
                    )
                    count += 1
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("Failed to read mailbox message %s: %s", msg_file, e)

        if count:
            self._save_watermark()

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
            # Notifications often carry their own summary/body
            return (payload.get("summary") or payload.get("body") or "Notification")[:200]
        if msg_type == "ERROR":
            return (payload.get("message") or "Error")[:200]

        return msg_type
