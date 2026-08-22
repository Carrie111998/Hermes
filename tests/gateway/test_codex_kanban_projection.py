from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gateway.codex_bridge import (
    BridgeOrigin,
    BridgeExecutionResult,
    BridgeRequest,
    BridgeStore,
    CodexBridgeService,
    CodexBridgeSettings,
    ProgressEvent,
)
from gateway.codex_kanban_projection import (
    CodexKanbanReconciler,
    CodexKanbanProjector,
    KanbanProjectionSettings,
    load_kanban_projection_settings,
    probe_projection_dependency,
    read_projection_status,
    reconciliation_main,
)
from hermes_cli import kanban_db as kb


def _request(workspace: Path, job_id: str = "job-phase2") -> BridgeRequest:
    return BridgeRequest(
        hermes_job_id=job_id,
        idempotency_key=f"request-{job_id}",
        origin=BridgeOrigin(
            type="api_server",
            conversation_id="phase2-conversation",
            message_id="phase2-message",
            user_id="phase2-user",
        ),
        workspace=str(workspace),
        prompt="not persisted in the projection",
    )


def _event(
    request: BridgeRequest,
    phase: str,
    sequence: int,
    *,
    summary: str | None = None,
    progress: dict | None = None,
) -> ProgressEvent:
    return ProgressEvent(
        event_id=f"evt-phase2-{sequence}",
        task_id=request.hermes_job_id,
        executor="codex",
        phase=phase,
        summary=summary or f"Public {phase} summary",
        progress=progress or {"current_step": phase},
        origin=request.origin.as_dict(),
        created_at=f"2026-08-22T08:00:0{sequence}+00:00",
        idempotency_key=request.idempotency_key,
    )


def _captured_store(tmp_path: Path, job_id: str = "job-phase2") -> tuple[BridgeStore, BridgeRequest]:
    workspace = tmp_path / f"workspace-{job_id}"
    workspace.mkdir()
    store = BridgeStore(tmp_path / f"{job_id}.db")
    request = _request(workspace, job_id)
    store.capture(request, owner_instance_id="gateway-phase2", stale_recovery_seconds=60)
    return store, request


def _projector(store: BridgeStore, target: Path) -> CodexKanbanProjector:
    return CodexKanbanProjector(
        store.path,
        KanbanProjectionSettings(enabled=True, board="default"),
        kanban_db_path=target,
    )


class _LifecycleExecutor:
    def execute(self, request, *, codex_thread_id, on_thread, on_progress):
        on_thread(codex_thread_id or "phase2-lifecycle-thread")
        on_progress("verification", "Lifecycle verification")
        return BridgeExecutionResult("Lifecycle complete")


class _TerminalOutageApi:
    def __init__(self):
        self.fail_output = True

    def __getattr__(self, name):
        return getattr(kb, name)

    def publish_task_output(
        self,
        conn,
        task_id,
        *,
        summary=None,
        metadata=None,
        with_reason=False,
    ):
        if self.fail_output:
            raise OSError("simulated terminal-event outage")
        return kb.publish_task_output(
            conn,
            task_id,
            summary=summary,
            metadata=metadata,
            with_reason=with_reason,
        )


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        await asyncio.sleep(0.02)


def test_projection_config_is_explicit_and_fails_closed(tmp_path):
    missing = load_kanban_projection_settings(tmp_path / "missing.yaml")
    assert missing.enabled is False

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("kanban_projection: [not, a, mapping]\n", encoding="utf-8")
    assert load_kanban_projection_settings(invalid).enabled is False

    enabled = tmp_path / "enabled.yaml"
    enabled.write_text(
        "kanban_projection:\n  enabled: true\n  board: pilot-board\n",
        encoding="utf-8",
    )
    settings = load_kanban_projection_settings(enabled)
    assert settings.enabled is True
    assert settings.board == "pilot-board"

    unsafe = tmp_path / "unsafe.yaml"
    unsafe.write_text(
        "kanban_projection:\n  enabled: true\n  board: ../outside\n",
        encoding="utf-8",
    )
    assert load_kanban_projection_settings(unsafe).enabled is False


def test_dependency_contract_reports_missing_outcome_first_api():
    class BaselineOnlyApi:
        VALID_STATUSES = {"working"}

        def __getattr__(self, name):
            if name == "publish_task_output":
                raise AttributeError(name)
            return getattr(kb, name)

    report = probe_projection_dependency(BaselineOnlyApi())

    assert report["ready"] is False
    assert report["missing_api"] == ["publish_task_output"]
    assert report["missing_statuses"] == ["output_ready"]


def test_read_only_status_reports_pre_enable_backlog(tmp_path):
    store, request = _captured_store(tmp_path, "job-status-backlog")
    store.append_event(_event(request, "captured", 1))
    target = tmp_path / "kanban-status.db"
    with kb.connect_closing(db_path=target):
        pass

    report = read_projection_status(store.path, kanban_db_path=target)

    assert report["mode"] == "read-only"
    assert report["mutations"] == 0
    assert report["pending_count"] == 1
    assert report["projection_cursor"] == 0
    assert report["dependency"]["ready"] is True


@pytest.mark.asyncio
async def test_startup_backlog_drains_without_a_new_bridge_event(tmp_path):
    store, request = _captured_store(tmp_path, "job-startup-backlog")
    store.append_event(_event(request, "captured", 1))
    store.append_event(_event(request, "working", 2))
    target = tmp_path / "kanban-startup.db"
    projector = CodexKanbanProjector(
        store.path,
        KanbanProjectionSettings(
            enabled=True,
            retry_initial_seconds=0.05,
            retry_max_seconds=0.1,
            shutdown_timeout_seconds=0.5,
        ),
        kanban_db_path=target,
    )
    settings = CodexBridgeSettings(
        enabled=True,
        allowed_origins=("api_server",),
        workspace_allowlist=(request.workspace,),
        default_workspace=request.workspace,
    )
    service = CodexBridgeService(
        settings,
        store=store,
        executor=_LifecycleExecutor(),
        projector=projector,
    )

    service.start_projection()
    await _wait_until(lambda: projector.status()["pending_count"] == 0)
    await service.stop_projection()

    assert len(projector.list_receipts(request.hermes_job_id)) == 2
    assert service._projection_worker is None
    assert projector.get_job_state(request.hermes_job_id)["retry_state"] == "stopped"


@pytest.mark.asyncio
async def test_terminal_outage_recovers_autonomously_without_new_event(tmp_path):
    workspace = tmp_path / "workspace-terminal-outage"
    workspace.mkdir()
    store = BridgeStore(tmp_path / "job-terminal-outage.db")
    request = _request(workspace, "job-terminal-outage")
    target = tmp_path / "kanban-terminal-outage.db"
    outage_api = _TerminalOutageApi()
    projector = CodexKanbanProjector(
        store.path,
        KanbanProjectionSettings(
            enabled=True,
            retry_initial_seconds=0.05,
            retry_max_seconds=0.1,
            shutdown_timeout_seconds=0.5,
        ),
        kanban_db_path=target,
        kanban_api=outage_api,
    )
    settings = CodexBridgeSettings(
        enabled=True,
        allowed_origins=("api_server",),
        workspace_allowlist=(request.workspace,),
        default_workspace=request.workspace,
    )
    service = CodexBridgeService(
        settings,
        store=store,
        executor=_LifecycleExecutor(),
        projector=projector,
    )

    service.start_projection()
    result = await service.execute(request, lambda _event: None)
    await _wait_until(lambda: bool(projector.status()["last_error"]))
    terminal_event_count = len(store.list_events(request.hermes_job_id))
    outage_api.fail_output = False
    await _wait_until(lambda: projector.status()["pending_count"] == 0)
    await service.stop_projection()

    assert result == "Lifecycle complete"
    assert store.get_by_job_id(request.hermes_job_id).phase == "done"
    assert len(store.list_events(request.hermes_job_id)) == terminal_event_count
    receipts = projector.list_receipts(request.hermes_job_id)
    assert len(receipts) == len({item["event_id"] for item in receipts})
    state = projector.get_job_state(request.hermes_job_id)
    with kb.connect_closing(db_path=target) as conn:
        task = kb.get_task(conn, state["kanban_task_id"])
        assert task.status == "done"
        assert task.result == "Lifecycle complete"
        assert conn.execute(
            "SELECT count(1) FROM tasks WHERE idempotency_key = ?",
            (f"codex-bridge:{request.hermes_job_id}",),
        ).fetchone()[0] == 1


def test_projection_is_outcome_first_restart_safe_and_deduplicated(tmp_path):
    store, request = _captured_store(tmp_path)
    artifact = str(Path(request.workspace) / "report.html")
    Path(artifact).write_text("<h1>Phase 2 artifact</h1>\n", encoding="utf-8")
    store.append_event(_event(request, "captured", 1))
    store.append_event(
        _event(
            request,
            "working",
            2,
            summary="Running verification",
            progress={"current_step": "verification"},
        )
    )
    store.append_event(
        _event(
            request,
            "output_ready",
            3,
            summary="Outcome is ready",
            progress={"current_step": "delivery", "artifacts": [artifact]},
        ),
        final_result="Verified Phase 2 result",
        artifacts=(artifact,),
    )
    store.append_event(
        _event(request, "done", 4, summary="Delivered to authenticated origin"),
        final_result="Verified Phase 2 result",
        artifacts=(artifact,),
    )

    target = tmp_path / "kanban.db"
    projector = _projector(store, target)
    assert projector.project_pending() == 4
    assert projector.project_pending() == 0

    state = projector.get_job_state(request.hermes_job_id)
    assert state is not None
    assert state["projection_cursor"] == 4
    assert state["notification_cursor"] == "evt-phase2-3"
    assert state["last_error"] is None

    receipts = projector.list_receipts(request.hermes_job_id)
    assert [item["event_id"] for item in receipts] == [
        "evt-phase2-1",
        "evt-phase2-2",
        "evt-phase2-3",
        "evt-phase2-4",
    ]
    assert [item["notification_eligible"] for item in receipts] == [1, 0, 1, 0]

    with kb.connect_closing(db_path=target) as conn:
        task = kb.get_task(conn, state["kanban_task_id"])
        assert task is not None
        assert task.status == "done"
        assert task.result == "Verified Phase 2 result"
        assert task.files_changed == [artifact]
        assert task.progress_percent == 100
        assert "report.html" not in (task.body or "")
        assert "not persisted" not in (task.body or "")
        event_counts = dict(
            conn.execute(
                "SELECT kind, count(1) FROM task_events WHERE task_id = ? GROUP BY kind",
                (task.id,),
            ).fetchall()
        )
        assert event_counts["output_ready"] == 1
        assert event_counts["completed"] == 1
        attachments = kb.list_attachments(conn, task.id)
        assert len(attachments) == 1
        assert attachments[0].filename == "report.html"
        assert Path(attachments[0].stored_path).read_text(encoding="utf-8") == (
            "<h1>Phase 2 artifact</h1>\n"
        )

    restarted = _projector(store, target)
    assert restarted.project_pending() == 0
    assert restarted.get_job_state(request.hermes_job_id)["kanban_task_id"] == task.id


def test_needs_user_projection_contains_concrete_action_and_prompt(tmp_path):
    store, request = _captured_store(tmp_path, "job-needs-user")
    store.append_event(_event(request, "captured", 1))
    store.append_event(_event(request, "working", 2))
    store.append_event(
        _event(
            request,
            "needs_user",
            3,
            summary="Chọn môi trường triển khai: staging hoặc production.",
            progress={"current_step": "user_action", "prompt_id": "prompt-phase2"},
        )
    )

    target = tmp_path / "kanban-needs-user.db"
    projector = _projector(store, target)
    assert projector.project_pending() == 3
    state = projector.get_job_state(request.hermes_job_id)

    with kb.connect_closing(db_path=target) as conn:
        task = kb.get_task(conn, state["kanban_task_id"])
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert task.current_step.startswith("Needs You:")
        assert "staging" in task.current_step
        assert "prompt-phase2" in task.latest_log
        assert task.last_heartbeat_at is not None


def test_projection_outage_leaves_receipts_pending_then_recovers(tmp_path):
    store, request = _captured_store(tmp_path, "job-outage")
    store.append_event(_event(request, "captured", 1))
    store.append_event(_event(request, "working", 2))

    unavailable_target = tmp_path / "target-is-a-directory"
    unavailable_target.mkdir()
    projector = _projector(store, unavailable_target)
    with pytest.raises(Exception):
        projector.project_pending()

    state = projector.get_job_state(request.hermes_job_id)
    assert state is not None
    assert state["last_error"]
    assert projector.list_receipts(request.hermes_job_id) == []
    assert store.get_by_job_id(request.hermes_job_id).phase == "working"

    healthy_target = tmp_path / "kanban-recovered.db"
    recovered = _projector(store, healthy_target)
    assert recovered.project_pending() == 2
    assert len(recovered.list_receipts(request.hermes_job_id)) == 2
    assert recovered.get_job_state(request.hermes_job_id)["last_error"] is None

    with kb.connect_closing(db_path=healthy_target) as conn:
        task = kb.get_task(
            conn, recovered.get_job_state(request.hermes_job_id)["kanban_task_id"]
        )
        assert task is not None
        assert task.status == "working"
        assert task.current_step == "working"


def test_projected_artifact_uses_dashboard_download_contract(tmp_path, monkeypatch):
    store, request = _captured_store(tmp_path, "job-download")
    artifact = Path(request.workspace) / "result.txt"
    artifact.write_text("downloadable result\n", encoding="utf-8")
    source_bytes = artifact.read_bytes()
    store.append_event(_event(request, "captured", 1))
    store.append_event(
        _event(request, "output_ready", 2),
        final_result="Download the artifact",
        artifacts=(str(artifact),),
    )

    target = tmp_path / "kanban-download.db"
    projector = _projector(store, target)
    assert projector.project_pending() == 2
    state = projector.get_job_state(request.hermes_job_id)

    with kb.connect_closing(db_path=target) as conn:
        task = kb.get_task(conn, state["kanban_task_id"])
        attachment = kb.list_attachments(conn, task.id)[0]
        assert task.status == "output_ready"
        assert task.result == "Download the artifact"

    monkeypatch.setenv("HERMES_KANBAN_DB", str(target))
    from plugins.kanban.dashboard import plugin_api

    response = plugin_api.download_attachment(attachment.id, board="default")
    assert Path(response.path).read_bytes() == source_bytes
    assert response.filename == "result.txt"


def test_reconciliation_is_read_only_and_classifies_drift(tmp_path, capsys):
    exact_store, exact_request = _captured_store(tmp_path, "job-reconcile-exact")
    missing_request = _request(
        Path(exact_request.workspace), "job-reconcile-missing"
    )
    exact_store.capture(
        missing_request,
        owner_instance_id="gateway-phase2",
        stale_recovery_seconds=60,
    )
    probable_request = _request(
        Path(exact_request.workspace), "job-reconcile-probable"
    )
    exact_store.capture(
        probable_request,
        owner_instance_id="gateway-phase2",
        stale_recovery_seconds=60,
    )

    target = tmp_path / "kanban-reconcile.db"
    with kb.connect_closing(db_path=target) as conn:
        exact_id = kb.create_task(
            conn,
            title="Exact projected card",
            workspace_kind="dir",
            workspace_path=exact_request.workspace,
            idempotency_key="codex-bridge:job-reconcile-exact",
            initial_status="working",
        )
        probable_id = kb.create_task(
            conn,
            title="Legacy job-reconcile-probable card",
            workspace_kind="dir",
            workspace_path=str(tmp_path / "legacy-workspace"),
            initial_status="working",
        )
        orphan_id = kb.create_task(
            conn,
            title="Orphan projection",
            workspace_kind="dir",
            workspace_path=str(tmp_path / "orphan-workspace"),
            idempotency_key="codex-bridge:job-no-longer-exists",
            initial_status="working",
        )
        before = [tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")]

    report = CodexKanbanReconciler(
        exact_store.path, kanban_db_path=target
    ).inspect()
    assert report["mode"] == "dry-run"
    assert report["mutations"] == 0
    assert report["counts"] == {
        "exact_match": 1,
        "missing_card": 1,
        "orphan_card": 1,
        "probable_legacy_match": 1,
    }
    by_class = {item["classification"]: item for item in report["items"]}
    assert by_class["exact_match"]["task_ids"] == [exact_id]
    assert by_class["probable_legacy_match"]["task_ids"] == [probable_id]
    assert by_class["orphan_card"]["task_ids"] == [orphan_id]

    with kb.connect_closing(db_path=target) as conn:
        after = [tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")]
    assert after == before

    assert reconciliation_main(
        [
            "--bridge-db",
            str(exact_store.path),
            "--kanban-db",
            str(target),
            "--json",
        ]
    ) == 0
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["mutations"] == 0
    assert reconciliation_main(
        [
            "--bridge-db",
            str(exact_store.path),
            "--kanban-db",
            str(target),
            "--fail-on-findings",
        ]
    ) == 1
