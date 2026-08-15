"""Observation persistence without converting samples into worker motion."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from typing import Mapping

from hermes_cli.kanban_store.canonical import canonical_json_bytes
from hermes_cli.kanban_store.types import RunFence

from .scopes import CgroupV2Sample


def record_observation(
    conn,
    *,
    fence: RunFence,
    source: str,
    detail: Mapping[str, object],
    ttl_seconds: int,
    observed_at: int | None = None,
) -> str:
    now = int(time.time()) if observed_at is None else int(observed_at)
    observation_id = str(uuid.uuid4())
    payload = {
        **dict(detail),
        "observation_id": observation_id,
        "source": source,
        "fresh_until": now + int(ttl_seconds),
        "generation_match": True,
    }
    conn.execute(
        """
        INSERT INTO run_observations(
            observation_id, task_id, run_id, claim_generation, source,
            observed_at, fresh_until, detail_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id,
            fence.task_id,
            fence.run_id,
            fence.claim_generation,
            source,
            now,
            now + int(ttl_seconds),
            canonical_json_bytes(payload).decode("utf-8"),
        ),
    )
    return observation_id


def cgroup_detail(current: CgroupV2Sample, previous: CgroupV2Sample | None) -> dict[str, object]:
    motion = False
    process = "alive" if current.pids else "dead"
    if previous is not None:
        motion = (
            current.cpu_usec > previous.cpu_usec
            or current.io_bytes > previous.io_bytes
            or current.pids != previous.pids
        )
    return {
        "complete": True,
        "coverage": "strong",
        "process": process,
        "worker_motion": motion,
        "heartbeat_only": False,
        "artifacts": "unknown",
        "publication": "unknown",
        "freeze_supported": current.freeze_supported,
        "scope": asdict(current),
    }
