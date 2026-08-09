from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

_DROP_KEYS = {"observed_at", "timestamp", "source_id", "path", "inode", "offset", "pid"}

def _stable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _stable(v) for k, v in sorted(value.items()) if str(k) not in _DROP_KEYS}
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    return value

def incident_fingerprint(signal_type: str, payload: Mapping[str, Any], *, collector: str = "") -> str:
    data = {"collector": collector, "signal_type": signal_type, "payload": _stable(payload)}
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
