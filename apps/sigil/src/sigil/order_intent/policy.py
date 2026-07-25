from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

from .models import OrderIntentPolicy, TimeInForce


def policy_snapshot(policy: OrderIntentPolicy) -> dict[str, str]:
    snapshot: dict[str, str] = {}

    for key, value in asdict(policy).items():
        if isinstance(value, Decimal):
            snapshot[key] = str(value)
        elif isinstance(value, tuple):
            snapshot[key] = ",".join(
                item.value if isinstance(item, TimeInForce) else str(item)
                for item in value
            )
        else:
            snapshot[key] = repr(value)

    return dict(sorted(snapshot.items()))
