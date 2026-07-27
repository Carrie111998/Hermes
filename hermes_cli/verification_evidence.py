"""Canonical independent-evidence envelopes for governed verification."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Mapping, Optional


class EvidenceContractError(ValueError):
    pass


def build(
    *,
    observer: str,
    source_kind: str,
    source_reference: str,
    facts: Mapping[str, Any],
    observed_at: Optional[int] = None,
    observation_id: Optional[str] = None,
) -> dict[str, Any]:
    facts_json = json.dumps(dict(facts), separators=(",", ":"), sort_keys=True)
    return {
        "observer": observer,
        "observation_id": observation_id or f"observation_{uuid.uuid4().hex}",
        "observed_at": int(time.time()) if observed_at is None else int(observed_at),
        "source_kind": source_kind,
        "source_reference": source_reference,
        "facts": dict(facts),
        "facts_sha256": hashlib.sha256(facts_json.encode()).hexdigest(),
    }


def validate(
    evidence: Any,
    *,
    expected_observer: str,
    max_age_seconds: int = 300,
    now: Optional[int] = None,
) -> Mapping[str, Any]:
    if not isinstance(evidence, Mapping):
        raise EvidenceContractError("verification evidence must be an object")
    required = {
        "observer",
        "observation_id",
        "observed_at",
        "source_kind",
        "source_reference",
        "facts",
        "facts_sha256",
    }
    missing = sorted(required - set(evidence))
    if missing:
        raise EvidenceContractError(
            f"verification evidence missing: {', '.join(missing)}"
        )
    if evidence["observer"] != expected_observer:
        raise EvidenceContractError("verification observer identity mismatch")
    if evidence["source_kind"] not in {
        "provider_readback",
        "authoritative_database_readback",
        "deterministic_check",
    }:
        raise EvidenceContractError("verification source kind is not independent")
    if not str(evidence["source_reference"]).strip() or not str(
        evidence["observation_id"]
    ).strip():
        raise EvidenceContractError("verification source and observation IDs are required")
    observed_at = int(evidence["observed_at"])
    ts = int(time.time()) if now is None else int(now)
    age = ts - observed_at
    if age < 0 or age > max_age_seconds:
        raise EvidenceContractError("verification evidence is stale or future-dated")
    if not isinstance(evidence["facts"], Mapping):
        raise EvidenceContractError("verification facts must be an object")
    facts_json = json.dumps(
        dict(evidence["facts"]), separators=(",", ":"), sort_keys=True
    )
    expected_hash = hashlib.sha256(facts_json.encode()).hexdigest()
    if evidence["facts_sha256"] != expected_hash:
        raise EvidenceContractError("verification facts hash mismatch")
    return evidence
