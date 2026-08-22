"""Isolated Phase 2 outage/recovery canary with no live-profile mutation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

from gateway.codex_bridge import (
    BridgeExecutionResult,
    BridgeOrigin,
    BridgeRequest,
    BridgeStore,
    CodexBridgeService,
    CodexBridgeSettings,
)
from gateway.codex_kanban_projection import (
    CodexKanbanProjector,
    KanbanProjectionSettings,
)
from hermes_cli import kanban_db


class ArtifactExecutor:
    def execute(self, request, *, codex_thread_id, on_thread, on_progress):
        on_thread(codex_thread_id or "phase2-canary-thread")
        on_progress("verification", "Running isolated Phase 2 canary")
        artifact = Path(request.workspace) / "phase2-canary.txt"
        artifact.write_bytes(b"CODEX_KANBAN_PHASE2_CANARY_OK\n")
        return BridgeExecutionResult("Phase 2 canary complete", (str(artifact),))


async def run_canary() -> dict:
    with tempfile.TemporaryDirectory(
        prefix="hermes-phase2-canary-", ignore_cleanup_errors=True
    ) as raw_temp:
        root = Path(raw_temp)
        os.environ["HERMES_HOME"] = str(root / "hermes-home")
        os.environ["HERMES_KANBAN_HOME"] = str(root / "kanban-home")
        os.environ["HERMES_KANBAN_ATTACHMENTS_ROOT"] = str(root / "attachments")
        workspace = root / "workspace"
        workspace.mkdir()
        bridge_store = BridgeStore(root / "bridge.db")
        projection_settings = KanbanProjectionSettings(
            enabled=True,
            board="default",
            retry_initial_seconds=0.1,
            retry_max_seconds=0.25,
            shutdown_timeout_seconds=1.0,
        )
        unavailable = root / "kanban-unavailable"
        unavailable.mkdir()
        outage_projector = CodexKanbanProjector(
            bridge_store.path,
            projection_settings,
            kanban_db_path=unavailable,
            owner_id="phase2-canary-outage",
        )
        bridge_settings = CodexBridgeSettings(
            enabled=True,
            allowed_origins=("api_server",),
            workspace_allowlist=(str(workspace),),
            default_workspace=str(workspace),
        )
        service = CodexBridgeService(
            bridge_settings,
            store=bridge_store,
            executor=ArtifactExecutor(),
            instance_id="phase2-canary-gateway",
            projector=outage_projector,
        )
        request = BridgeRequest(
            hermes_job_id="job-phase2-isolated-canary",
            idempotency_key="phase2-isolated-canary",
            origin=BridgeOrigin(
                type="api_server",
                conversation_id="phase2-canary-conversation",
                message_id="phase2-canary-message",
            ),
            workspace=str(workspace),
            prompt="Run the isolated Phase 2 canary",
        )
        service.start_projection()
        result = await service.execute(request, lambda _event: None)
        await service.wait_for_projection()
        mapping = bridge_store.get_by_job_id(request.hermes_job_id)
        outage_state = outage_projector.get_job_state(request.hermes_job_id)
        if mapping is None or mapping.phase != "done" or result != "Phase 2 canary complete":
            raise RuntimeError("Codex did not complete during the simulated Kanban outage")
        if not outage_state or not outage_state["last_error"]:
            raise RuntimeError("Projection outage was not recorded for retry")

        # Recover the same target. No new Codex event and no manual projector
        # call occurs: the service's bounded retry loop must catch up itself.
        unavailable.rmdir()
        deadline = asyncio.get_running_loop().time() + 10
        while outage_projector.status()["pending_count"]:
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("Autonomous projection retry did not drain backlog")
            await asyncio.sleep(0.05)
        state = outage_projector.get_job_state(request.hermes_job_id)
        await service.stop_projection()
        with kanban_db.connect_closing(db_path=unavailable) as connection:
            task = kanban_db.get_task(connection, state["kanban_task_id"])
            attachments = kanban_db.list_attachments(connection, task.id)
        if task is None or task.status != "done":
            raise RuntimeError("Recovered Kanban card is not terminal")
        if task.result != "Phase 2 canary complete":
            raise RuntimeError("Recovered Kanban card lost the result")
        if len(attachments) != 1:
            raise RuntimeError("Recovered Kanban card lost or duplicated its artifact")
        attachment_bytes = Path(attachments[0].stored_path).read_bytes()
        if attachment_bytes != b"CODEX_KANBAN_PHASE2_CANARY_OK\n":
            raise RuntimeError("Recovered attachment bytes do not match the source")
        receipts = outage_projector.list_receipts(request.hermes_job_id)
        return {
            "status": "pass",
            "codex_phase_during_outage": mapping.phase,
            "projection_error_recorded": bool(outage_state["last_error"]),
            "events_projected_after_recovery": len(receipts),
            "recovery_without_new_bridge_event": True,
            "manual_project_pending_calls_after_recovery": 0,
            "receipt_count": len(receipts),
            "distinct_receipt_count": len({item["event_id"] for item in receipts}),
            "projection_cursor": state["projection_cursor"],
            "notification_cursor": state["notification_cursor"],
            "kanban_status": task.status,
            "kanban_result": task.result,
            "artifact_filename": attachments[0].filename,
            "artifact_bytes_verified": True,
        }


def main() -> int:
    # The first half deliberately makes every asynchronous projection wake
    # fail. Keep the acceptance output focused on the final machine-readable
    # evidence instead of repeating expected traceback logs.
    logging.getLogger("gateway.codex_bridge").setLevel(logging.CRITICAL)
    print(json.dumps(asyncio.run(run_canary()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
