#!/usr/bin/env python3
"""Pure identity for the owner-approved 100-GB canary host successor.

The original host plan created a 40-GB disk.  On 2026-08-03 that exact disk
was expanded to 100 GB after a full filesystem made the canary unreachable.
This module does not authorize or perform a resize.  It only makes the
already-completed, owner-approved steady state part of every future host-plan
digest so a 40/80-GB host cannot be mistaken for the canonical target.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


SUCCESSOR_SCHEMA = "muncho-isolated-canary-host-storage-successor.v1"
PROJECT = "adventico-ai-platform"
ZONE = "europe-west3-a"
VM_NAME = "muncho-canary-v2-01"
DISK_NAME = "muncho-canary-v2-01"
TARGET_SIZE_GB = 100
AUDIT_METHOD = "v1.compute.disks.resize"
AUDIT_PRINCIPAL = "lomliev@adventico.com"
AUDIT_RESOURCE_NAME = (
    "projects/adventico-ai-platform/zones/europe-west3-a/disks/muncho-canary-v2-01"
)
AUDIT_REQUESTED_AT = "2026-08-03T22:38:45.472891Z"
AUDIT_COMPLETED_AT = "2026-08-03T22:38:50.119667Z"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")


def canonical_payload() -> Mapping[str, Any]:
    """Return the exact immutable adoption record for the 100-GB target."""

    return {
        "schema": SUCCESSOR_SCHEMA,
        "project": PROJECT,
        "zone": ZONE,
        "vm_name": VM_NAME,
        "disk_name": DISK_NAME,
        "target_size_gb": TARGET_SIZE_GB,
        "audit": {
            "method": AUDIT_METHOD,
            "principal": AUDIT_PRINCIPAL,
            "resource_name": AUDIT_RESOURCE_NAME,
            "requested_at": AUDIT_REQUESTED_AT,
            "completed_at": AUDIT_COMPLETED_AT,
            "requested_size_gb": TARGET_SIZE_GB,
        },
        "mutation_authority": False,
        "resize_authority": False,
        "delete_authority": False,
        "shrink_authority": False,
        "steady_state_only": True,
    }


def canonical_sha256() -> str:
    return hashlib.sha256(_canonical_bytes(canonical_payload())).hexdigest()


__all__ = [
    "SUCCESSOR_SCHEMA",
    "TARGET_SIZE_GB",
    "canonical_payload",
    "canonical_sha256",
]
