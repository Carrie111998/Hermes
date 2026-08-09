from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

_DROP_KEYS = {
    "observed_at", "timestamp", "source_id", "path", "inode", "offset", "pid",
    "request_id", "trace_id", "span_id", "event_id", "correlation_id", "message_id",
    "session_id", "task_id", "run_id", "execution_id", "attempt_id",
}
_UUID_RE = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])")

def _stable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _stable(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
            if str(key).lower() not in _DROP_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    if isinstance(value, str):
        return _UUID_RE.sub("<uuid>", value)
    return value

def incident_fingerprint(signal_type: str, payload: Mapping[str, Any], *, collector: str = "") -> str:
    data = {"collector": collector, "signal_type": signal_type, "payload": _stable(payload)}
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
