"""AuditLogger subscriber — append-only JSONL event trail.

Records every event for debugging and replay.  Rotated weekly by
an external cleanup job, retained for 90 days.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from events.bus import EventBus
from events.schema import Event
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)


class AuditLogger(BaseSubscriber):
    subscriber_id = "audit-logger"
    poll_interval_seconds = 5

    def __init__(self, bus: EventBus, audit_path: Optional[Path] = None):
        super().__init__(bus)
        if audit_path is None:
            from hermes_constants import get_hermes_home
            audit_path = get_hermes_home() / "events" / "audit.jsonl"
        self.audit_path = Path(audit_path)

    def handle(self, event: Event) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
