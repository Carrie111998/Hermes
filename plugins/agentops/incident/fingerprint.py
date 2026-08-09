from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

_DROP_KEYS = {"observed_at", "timestamp", "source_id", "path", "inode", "offset", "pid", "request_id", "trace_id", "span_id", "event_id", "correlation_id"}

def _stable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _stable(v) for k, v in sorted(value.items()) if str(k) not in _DROP_KEYS}
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", value): return "<uuid>"
    return value

def incident_fingerprint(signal_type: str, payload: Mapping[str, Any], *, collector: str = "") -> str:
    data = {"collector": collector, "signal_type": signal_type, "payload": _stable(payload)}
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
