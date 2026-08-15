"""Immutable worker-context serialization and verification."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict
from pathlib import Path

from hermes_cli.kanban_store.canonical import canonical_json_bytes
from hermes_cli.kanban_store.types import (
    ContractError,
    RunBinding,
    RunFence,
    RuntimeIdentity,
    WorkerContext,
)


def context_bytes(context: WorkerContext) -> bytes:
    value = {
        "schema_version": context.schema_version,
        "fence": {
            "task_id": context.fence.task_id,
            "run_id": context.fence.run_id,
            "claim_generation": context.fence.claim_generation,
            "claim_token": context.fence.claim_token,
        },
        "binding": asdict(context.binding),
        "capability_manifest_sha256": context.capability_manifest_sha256,
        "runtime_identity": context.runtime_identity.as_dict(),
        "workspace": context.workspace,
        "artifact_root": context.artifact_root,
        "broker_socket": context.broker_socket,
    }
    return canonical_json_bytes(value)


def context_digest(context: WorkerContext) -> str:
    return hashlib.sha256(context_bytes(context)).hexdigest()


def write_context_file(path: str | Path, context: WorkerContext) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = context_bytes(context)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o400)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    return hashlib.sha256(payload).hexdigest()


def read_context_file(path: str | Path, expected_digest: str) -> WorkerContext:
    target = Path(path)
    st = target.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ContractError("worker context must be a regular non-symlink file")
    if st.st_mode & 0o222:
        raise ContractError("worker context file is writable")
    payload = target.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_digest:
        raise ContractError("worker context digest mismatch")
    value = json.loads(payload)
    if set(value) != {
        "schema_version", "fence", "binding", "capability_manifest_sha256",
        "runtime_identity", "workspace", "artifact_root", "broker_socket",
    }:
        raise ContractError("worker context fields do not match schema")
    fence = value["fence"]
    binding = value["binding"]
    runtime_identity = value["runtime_identity"]
    return WorkerContext(
        fence=RunFence(
            task_id=fence["task_id"],
            run_id=int(fence["run_id"]),
            claim_generation=int(fence["claim_generation"]),
            claim_token=fence["claim_token"],
        ),
        binding=RunBinding(
            board_id=binding["board_id"],
            database_id=binding["database_id"],
            worker_id=binding["worker_id"],
            profile=binding["profile"],
            strict=bool(binding["strict"]),
        ),
        capability_manifest_sha256=value["capability_manifest_sha256"],
        runtime_identity=RuntimeIdentity(
            provider=runtime_identity["provider"],
            model=runtime_identity["model"],
            api_mode=runtime_identity["api_mode"],
            session_id=runtime_identity["session_id"],
            source=runtime_identity["source"],
        ),
        workspace=value["workspace"],
        artifact_root=value["artifact_root"],
        broker_socket=value["broker_socket"],
        schema_version=int(value["schema_version"]),
    )
