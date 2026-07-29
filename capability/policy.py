from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import yaml


POLICY_DIR = (
    Path(__file__).parent / "policies"
)


@lru_cache(maxsize=32)
def load_profile_policy(
    profile: str,
) -> dict:

    policy_dir = POLICY_DIR.resolve()
    path = (policy_dir / f"{profile}.yaml").resolve()

    if path.parent != policy_dir or not path.is_file():
        return {}

    with open(path, "r", encoding="utf-8") as f:
        value = yaml.safe_load(f) or {}
    return value if isinstance(value, dict) else {}


def get_policy_decision(
    profile: str,
    capability: str,
):

    policy = load_profile_policy(profile)

    entry = policy.get(capability)

    if not isinstance(entry, dict):
        return None

    decision = entry.get("decision")
    return decision if isinstance(decision, str) else None
