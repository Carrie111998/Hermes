from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb

CANARY = "opaque-canary-secret-8f39c1"


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.kanban_sensitive.active_secret_values", lambda: (CANARY,)
    )


def _sensitive_task(conn, workspace: Path) -> str:
    return kb.create_task(
        conn,
        title="sensitive",
        workspace_kind="dir",
        workspace_path=str(workspace),
        sensitive_execution=True,
        sensitive_runner_id="fixed-v1",
        protected_resource_ids=["resource-a"],
    )


def test_sensitive_comment_review_and_completion_exact_redact(tmp_path, secrets):
    conn = kb.connect(tmp_path / "kanban.db")
    task_id = _sensitive_task(conn, tmp_path)
    kb.add_comment(conn, task_id, "worker", f"comment {CANARY}")
    assert CANARY not in kb.list_comments(conn, task_id)[0].body

    assert kb.request_review(
        conn,
        task_id,
        summary=f"review {CANARY}",
        metadata={"opaque": CANARY},
        force=True,
    )
    assert kb.complete_task(
        conn,
        task_id,
        summary=f"done {CANARY}",
        result=f"result {CANARY}",
        metadata={"opaque": CANARY, CANARY: "secret-as-key"},
    )
    task = kb.get_task(conn, task_id)
    run = kb.latest_run(conn, task_id)
    events = kb.list_events(conn, task_id)
    durable = json.dumps(
        {
            "result": task.result if task else None,
            "summary": run.summary if run else None,
            "metadata": run.metadata if run else None,
            "events": [event.payload for event in events],
        }
    )
    assert CANARY not in durable
    conn.close()


def test_sensitive_block_reason_exact_redacts(tmp_path, secrets):
    conn = kb.connect(tmp_path / "kanban.db")
    task_id = _sensitive_task(conn, tmp_path)
    assert kb.block_task(conn, task_id, reason=f"blocked {CANARY}")
    run = kb.latest_run(conn, task_id)
    events = kb.list_events(conn, task_id)
    durable = json.dumps({
        "summary": run.summary if run else None,
        "events": [event.payload for event in events],
    })
    assert CANARY not in durable
    conn.close()


def test_sensitive_changes_requested_and_generic_events_exact_redact(
    tmp_path, secrets
):
    conn = kb.connect(tmp_path / "kanban.db")
    task_id = kb.create_task(
        conn,
        title="sensitive review",
        assignee="builder",
        sensitive_execution=True,
        sensitive_runner_id="fixed-v1",
    )
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="ready",
        reviewer="reviewer",
        expected_run_id=claimed.current_run_id,
    )
    review = kb.claim_review_task(conn, task_id)
    assert review is not None
    assert kb.request_changes(
        conn,
        task_id,
        reason=f"change {CANARY}",
        expected_run_id=review.current_run_id,
    ) == (True, "builder")
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, "test_event", {"error": CANARY})
    durable = json.dumps(
        {
            "runs": [run.__dict__ for run in kb.list_runs(conn, task_id)],
            "events": [event.payload for event in kb.list_events(conn, task_id)],
        }
    )
    assert CANARY not in durable
    conn.close()


def test_sensitive_attachment_rejects_exact_secret_and_binary(tmp_path, secrets, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    conn = kb.connect(tmp_path / "kanban.db")
    task_id = _sensitive_task(conn, tmp_path)
    with pytest.raises(ValueError, match="credential material"):
        kb.store_attachment_bytes(conn, task_id, "secret.txt", CANARY.encode())
    with pytest.raises(ValueError, match="auditable UTF-8"):
        kb.store_attachment_bytes(conn, task_id, "binary.bin", b"\x00\xff\x00")
    with pytest.raises(ValueError, match="auditable UTF-8"):
        kb.store_attachment_bytes(conn, task_id, "nul.bin", b"valid-utf8\x00binary")
    assert kb.list_attachments(conn, task_id) == []
    conn.close()


def test_sensitive_completion_persists_the_exact_scanned_bytes(tmp_path, secrets, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "report.txt"
    artifact.write_text("safe-before-scan")
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    conn = kb.connect(tmp_path / "kanban.db")
    task_id = _sensitive_task(conn, workspace)

    from hermes_cli import kanban_sensitive

    original = kanban_sensitive.read_and_scan_sensitive_artifact

    def mutate_after_read(path):
        data = original(path)
        Path(path).write_text(CANARY)
        return data

    monkeypatch.setattr(
        kanban_sensitive, "read_and_scan_sensitive_artifact", mutate_after_read
    )
    assert kb.complete_task(
        conn,
        task_id,
        summary="done",
        metadata={"artifacts": [str(artifact)]},
    )
    attachments = kb.list_attachments(conn, task_id)
    assert len(attachments) == 1
    assert Path(attachments[0].stored_path).read_bytes() == b"safe-before-scan"
    assert CANARY.encode() not in Path(attachments[0].stored_path).read_bytes()
    conn.close()


def test_sensitive_completion_rejects_secret_artifact(tmp_path, secrets, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "report.txt"
    artifact.write_text(CANARY)
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    conn = kb.connect(tmp_path / "kanban.db")
    task_id = _sensitive_task(conn, workspace)
    prior_status = kb.get_task(conn, task_id).status
    with pytest.raises(kb.ArtifactPreservationError):
        kb.complete_task(
            conn,
            task_id,
            summary="done",
            metadata={"artifacts": [str(artifact)]},
        )
    assert kb.get_task(conn, task_id).status == prior_status
    conn.close()


def test_sensitive_worker_wrapper_redacts_stdout_and_stderr(monkeypatch, capsys):
    from hermes_cli import kanban_sensitive_worker

    monkeypatch.setattr(
        kanban_sensitive_worker.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=7,
            stdout=f"stdout {CANARY}\nstderr {CANARY}\n".encode(),
        ),
    )
    monkeypatch.setattr(
        kanban_sensitive_worker, "active_secret_values", lambda: (CANARY,)
    )
    assert kanban_sensitive_worker.main(["--", "/fixed/executable"]) == 7
    output = capsys.readouterr().out
    assert CANARY not in output
    assert "redacted" in output
