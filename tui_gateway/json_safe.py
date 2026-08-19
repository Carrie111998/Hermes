"""JSON-safe serialization helpers for TUI gateway.

PyYAML's safe_load can return Python datetime objects when config.yaml
contains unquoted timestamps (e.g., `last_check: 2024-01-15 10:30:00`).
These cannot be JSON-serialized directly and would crash the gateway when
returned via the config.get RPC.

This module provides helpers to recursively sanitize config values before
they reach json.dumps().
"""

from __future__ import annotations

import datetime
from typing import Any


def make_json_safe(obj: Any) -> Any:
    """Recursively convert non-JSON-serializable types to safe equivalents.
    
    - datetime.datetime → ISO 8601 string
    - datetime.date → ISO 8601 date string
    - datetime.time → ISO 8601 time string
    - dict → recursively sanitized dict
    - list/tuple → recursively sanitized list
    - Everything else → unchanged
    
    Returns a new structure; does not mutate the input.
    """
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    if isinstance(obj, datetime.time):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [make_json_safe(item) for item in obj]
    # str, int, float, bool, None are already JSON-safe
    return obj
