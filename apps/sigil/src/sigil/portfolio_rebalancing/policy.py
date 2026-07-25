from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

from .models import RebalancingPolicy


def policy_snapshot(policy: RebalancingPolicy) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for key, value in asdict(policy).items():
        snapshot[key] = str(value) if isinstance(value, Decimal) else repr(value)
    return snapshot
