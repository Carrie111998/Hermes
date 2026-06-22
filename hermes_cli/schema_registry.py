"""Typed I/O contract registry for kanban card schemas.

Loads ``kanban_card_schemas.json`` from the same package directory and
provides a ``get_schema(assignee)`` lookup.  If no schema is registered
for an assignee, validation is a no-op (free-form pass-through).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

_SCHEMAS_PATH = Path(__file__).parent / "kanban_card_schemas.json"
_registry: Optional[dict[str, Any]] = None


def _load() -> dict[str, Any]:
    global _registry
    if _registry is None:
        if _SCHEMAS_PATH.exists():
            with open(_SCHEMAS_PATH) as f:
                _registry = json.load(f)
        else:
            _registry = {"schema_version": 1, "schemas": {}}
    return _registry


def get_schema(assignee: str) -> Optional[dict]:
    """Return the JSON-Schema dict for *assignee*, or ``None``."""
    reg = _load()
    return reg.get("schemas", {}).get(assignee)


def register_schema(assignee: str, schema: dict) -> None:
    """Register a schema at runtime (for tests)."""
    reg = _load()
    reg.setdefault("schemas", {})[assignee] = schema


def reset_registry() -> None:
    """Reset the in-memory cache (for tests)."""
    global _registry
    _registry = None
