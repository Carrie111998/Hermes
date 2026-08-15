"""Strict worker supervisor orchestration."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from hermes_cli.kanban_store.baselines import create_baseline
from hermes_cli.kanban_store.canonical import canonical_json_bytes
from hermes_cli.kanban_store.claims import issue_claim
from hermes_cli.kanban_store.database import write_txn
from hermes_cli.kanban_store.types import RuntimeIdentity, RunBinding, WorkerContext

from .capabilities import CapabilityManifest
from .context import context_digest, write_context_file
from .oci import StrictOciConfig, create_and_verify, sanitized_runtime_env


class StrictWorkerSupervisor:
    def __init__(self, *, conn, runtime_root: str | Path, artifact_blob_root: str | Path) -> None:
        self.conn = conn
        self.runtime_root = Path(runtime_root).resolve()
        self.artifact_blob_root = Path(artifact_blob_root).resolve()
        self.runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifact_blob_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def prepare(
        self,
        *,
        task_id: str,
        worker_id: str,
        profile: str,
        workspace: str | Path,
        manifest: CapabilityManifest,
        runtime_identity: RuntimeIdentity,
        oci_image: str,
        command: tuple[str, ...],
        claim_ttl_seconds: int = 900,
    ) -> tuple[StrictOciConfig, WorkerContext, str]:
        workspace_path = Path(workspace).resolve(strict=True)
        run_dir = self.runtime_root / f"{task_id}-{uuid.uuid4().hex}"
        run_dir.mkdir(mode=0o700)
        broker_dir = run_dir / "broker"
        broker_dir.mkdir(mode=0o700)
        context_file = run_dir / "context.json"
        claim = issue_claim(
            self.conn,
            task_id=task_id,
            profile=profile,
            ttl_seconds=claim_ttl_seconds,
        )
        fence = claim.fence
        meta = {
            row[0]: row[1]
            for row in self.conn.execute(
                "SELECT key, value FROM kanban_security_meta WHERE key IN ('board_id','database_id')"
            )
        }
        context = WorkerContext(
            fence=fence,
            binding=RunBinding(
                board_id=meta["board_id"],
                database_id=meta["database_id"],
                worker_id=worker_id,
                profile=profile,
                strict=True,
            ),
            capability_manifest_sha256=manifest.sha256,
            runtime_identity=runtime_identity,
            workspace="/workspace",
            artifact_root="/workspace",
            broker_socket="/run/hermes-broker/broker.sock",
        )
        digest = write_context_file(context_file, context)
        with write_txn(self.conn):
            self.conn.execute(
                "UPDATE task_runs SET worker_context_digest=? WHERE id=? AND claim_generation=?",
                (digest, fence.run_id, fence.claim_generation),
            )
            create_baseline(
                self.conn,
                fence=fence,
                workspace=workspace_path,
                exclusions=(".hermes-observer", ".hermes-events", ".git/index.lock"),
            )
        config = StrictOciConfig(
            runtime="docker",
            image=oci_image,
            container_name=f"hermes-kanban-{fence.run_id}-{fence.claim_generation}",
            workspace_host=str(workspace_path),
            context_host=str(context_file),
            broker_dir_host=str(broker_dir),
            command=command,
        )
        return config, context, str(run_dir)

    def start(self, config: StrictOciConfig) -> str:
        container_id = create_and_verify(config)
        subprocess.run(
            [config.runtime, "start", container_id],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=sanitized_runtime_env(),
            timeout=30,
        )
        return container_id

    def cleanup(self, config: StrictOciConfig, run_dir: str | Path) -> None:
        subprocess.run(
            [config.runtime, "rm", "-f", config.container_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=sanitized_runtime_env(),
            timeout=30,
        )
        shutil.rmtree(run_dir, ignore_errors=True)
