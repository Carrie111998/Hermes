from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import yaml


_MAPPING_FILE = (
    Path(__file__).parent / "tool_mapping.yaml"
)


@lru_cache(maxsize=1)
def load_tool_mapping() -> dict:
    if not _MAPPING_FILE.exists():
        return {}

    with open(_MAPPING_FILE, "r", encoding="utf-8") as f:
        value = yaml.safe_load(f) or {}
    return value if isinstance(value, dict) else {}


def resolve_capability(tool_name: str) -> str | None:
    mapping = load_tool_mapping()

    entry = mapping.get(tool_name)

    if not isinstance(entry, dict):
        return None

    capability = entry.get("capability")
    return capability if isinstance(capability, str) else None
