from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from decimal import Decimal
from enum import Enum
from typing import Any


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _canonical(asdict(value))
    return value


def package_identity(payload: object) -> str:
    encoded = json.dumps(
        _canonical(payload),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"pcp-{hashlib.sha256(encoded).hexdigest()}"


def verify_package_identity(package: object, package_id: str) -> bool:
    return package_identity(package) == package_id
