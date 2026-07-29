from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import yaml


SOURCE_FILE = (
    Path(__file__).parent /
    "sources.yaml"
)


@lru_cache(maxsize=1)
def load_sources() -> dict:

    with open(
        SOURCE_FILE,
        "r",
        encoding="utf-8",
    ) as f:
        value = yaml.safe_load(f) or {}
    return value if isinstance(value, dict) else {}


def resolve_source(
    runtime: str,
    tool: str,
):

    sources = load_sources()

    for capability, entry in sources.items():
        if not isinstance(capability, str) or not isinstance(entry, dict):
            continue

        for source in entry.get(
            "sources",
            [],
        ):
            if source == f"{runtime}:{tool}":
                return capability

    return None
