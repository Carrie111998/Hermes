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
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }

    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]

    if hasattr(value, "__dataclass_fields__"):
        return _canonical(asdict(value))

    return value


def canonical_json(payload: object) -> str:
    return json.dumps(
        _canonical(payload),
        sort_keys=True,
        separators=(",", ":"),
    )


def identity_for(prefix: str, payload: object) -> str:
    clean_prefix = prefix.strip()
    if not clean_prefix:
        raise ValueError("identity prefix must not be empty")

    encoded = canonical_json(payload).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"{clean_prefix}-{digest}"


def source_proposal_identity(payload: object) -> str:
    return identity_for("opi-src", payload)


def order_intent_identity(payload: object) -> str:
    return identity_for("opi", payload)


def order_intent_package_identity(payload: object) -> str:
    return identity_for("oip", payload)


def approval_request_identity(payload: object) -> str:
    return identity_for("oar", payload)


def approval_record_identity(payload: object) -> str:
    return identity_for("oac", payload)


def verify_identity(
    prefix: str,
    payload: object,
    identity: str,
) -> bool:
    return identity_for(prefix, payload) == identity


def verify_source_proposal_identity(
    payload: object,
    identity: str,
) -> bool:
    return source_proposal_identity(payload) == identity


def verify_order_intent_identity(
    payload: object,
    identity: str,
) -> bool:
    from dataclasses import replace

    normalized = (
        replace(payload, intent_id="")
        if hasattr(payload, "intent_id")
        else payload
    )
    return order_intent_identity(normalized) == identity

def verify_order_intent_package_identity(
    payload: object,
    identity: str,
) -> bool:
    from dataclasses import replace

    normalized = (
        replace(payload, package_id="")
        if hasattr(payload, "package_id")
        else payload
    )
    return order_intent_package_identity(normalized) == identity

def verify_approval_request_identity(
    payload: object,
    identity: str,
) -> bool:
    if hasattr(payload, "request_id"):
        normalized: object = {
            "order_intent_package_id": payload.order_intent_package_id,
            "source_rebalance_package_id": (
                payload.source_rebalance_package_id
            ),
            "intent_ids": payload.intent_ids,
            "created_at": payload.created_at,
            "expires_at": payload.expires_at,
            "required_approver_role": payload.required_approver_role,
        }
    else:
        normalized = payload

    return approval_request_identity(normalized) == identity

def verify_approval_record_identity(
    payload: object,
    identity: str,
) -> bool:
    if hasattr(payload, "record_id"):
        normalized: object = {
            "request_id": payload.request_id,
            "order_intent_package_id": (
                payload.order_intent_package_id
            ),
            "decision": payload.decision.value,
            "status": payload.status.value,
            "approver_identity": payload.approver_identity,
            "decided_at": payload.decided_at,
            "reason": payload.reason,
        }
    else:
        normalized = payload

    return approval_record_identity(normalized) == identity
