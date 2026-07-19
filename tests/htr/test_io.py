import tempfile
from unittest import mock

from htr.ids import new_attempt_id, new_run_id, new_task_id
from htr import io, paths
from htr.schemas import validate


def test_atomic_write_json_round_trip(tmp_path):
    target = tmp_path / "nested" / "data.json"
    payload = {"hello": "world", "n": 1}
    io.atomic_write_json(target, payload)
    assert io.read_json(target) == payload


def test_atomic_write_json_repeated_write_produces_valid_final_json(tmp_path):
    target = tmp_path / "data.json"
    io.atomic_write_json(target, {"version": 1})
    io.atomic_write_json(target, {"version": 2, "ok": True})
    assert io.read_json(target) == {"version": 2, "ok": True}


def test_atomic_write_json_uses_named_tempfile_not_fixed_suffix(tmp_path):
    target = tmp_path / "manifest.json"
    captured: dict[str, object] = {}

    original_named_temp = tempfile.NamedTemporaryFile

    def _capture(**kwargs):
        captured.update(kwargs)
        return original_named_temp(**kwargs)

    with mock.patch("htr.io.tempfile.NamedTemporaryFile", side_effect=_capture):
        io.atomic_write_json(target, {"run_id": "run_test"})

    assert captured["dir"] == target.parent
    assert captured["prefix"] == ".manifest.json."
    assert captured["suffix"] == ".tmp"
    assert captured["delete"] is False
    assert not (target.parent / "manifest.json.tmp").exists()


def test_append_and_read_jsonl(tmp_path):
    target = tmp_path / "events.jsonl"
    io.append_jsonl(target, {"event_id": "evt_1"})
    io.append_jsonl(target, {"event_id": "evt_2"})
    rows = io.read_jsonl(target)
    assert len(rows) == 2
    assert rows[0]["event_id"] == "evt_1"


def test_sha256_file(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("abc", encoding="utf-8")
    digest = io.sha256_file(target)
    assert len(digest) == 64


def test_create_run_workspace_structure(tmp_path):
    run_id = new_run_id()
    root = io.create_run_workspace(run_id, base_dir=tmp_path)

    assert root.is_dir()
    assert paths.reports_dir(run_id, tmp_path).is_dir()
    assert paths.tasks_dir(run_id, tmp_path).is_dir()
    assert paths.task_events_path(run_id, tmp_path).exists()
    assert paths.approvals_path(run_id, tmp_path).exists()

    manifest = io.read_json(paths.run_manifest_path(run_id, tmp_path))
    validate(manifest, "run_manifest")
    assert manifest["run_id"] == run_id
    assert manifest["status"] == "created"


def test_create_task_workspace_structure(tmp_path):
    run_id = new_run_id()
    task_id = new_task_id()
    task_root = io.create_task_workspace(run_id, task_id, base_dir=tmp_path)

    assert task_root.is_dir()
    status = io.read_json(paths.task_status_path(run_id, task_id, tmp_path))
    validate(status, "task_status")
    assert status["task_id"] == task_id
    assert status["attempts"] == []
    assert not paths.task_card_path(run_id, task_id, tmp_path).exists()


def test_create_attempt_workspace_structure(tmp_path):
    run_id = new_run_id()
    task_id = new_task_id()
    attempt_id = new_attempt_id()
    attempt_root = io.create_attempt_workspace(
        run_id, task_id, attempt_id, base_dir=tmp_path
    )

    assert attempt_root.is_dir()
    for subdir in (
        "input",
        "working",
        "output",
        "artifacts",
        "logs",
        "verification",
        "heal",
    ):
        assert (attempt_root / subdir).is_dir()

    attempt_status = io.read_json(
        paths.attempt_status_path(run_id, task_id, attempt_id, tmp_path)
    )
    validate(attempt_status, "attempt_status")

    manifest = io.read_json(
        paths.artifact_manifest_path(run_id, task_id, attempt_id, tmp_path)
    )
    validate(manifest, "artifact_manifest")
    assert paths.tool_calls_path(run_id, task_id, attempt_id, tmp_path).exists()
    assert not paths.result_json_path(run_id, task_id, attempt_id, tmp_path).exists()


def test_repeated_create_run_workspace_preserves_created_at(tmp_path):
    run_id = new_run_id()
    manifest_path = paths.run_manifest_path(run_id, tmp_path)

    io.create_run_workspace(run_id, base_dir=tmp_path)
    first_created_at = io.read_json(manifest_path)["created_at"]

    io.create_run_workspace(run_id, base_dir=tmp_path)
    second_manifest = io.read_json(manifest_path)

    assert second_manifest["created_at"] == first_created_at


def test_repeated_create_task_workspace_does_not_overwrite_status(tmp_path):
    run_id = new_run_id()
    task_id = new_task_id()
    status_path = paths.task_status_path(run_id, task_id, tmp_path)

    io.create_task_workspace(run_id, task_id, base_dir=tmp_path)
    io.atomic_write_json(
        status_path,
        {
            "task_id": task_id,
            "run_id": run_id,
            "status": "running",
            "attempts": ["att_existing"],
        },
    )

    io.create_task_workspace(run_id, task_id, base_dir=tmp_path)
    status = io.read_json(status_path)

    assert status["status"] == "running"
    assert status["attempts"] == ["att_existing"]


def test_repeated_create_attempt_workspace_preserves_status_and_manifest(tmp_path):
    run_id = new_run_id()
    task_id = new_task_id()
    attempt_id = new_attempt_id()
    attempt_status_path = paths.attempt_status_path(
        run_id, task_id, attempt_id, tmp_path
    )
    artifact_manifest_path = paths.artifact_manifest_path(
        run_id, task_id, attempt_id, tmp_path
    )

    io.create_attempt_workspace(run_id, task_id, attempt_id, base_dir=tmp_path)
    io.atomic_write_json(
        attempt_status_path,
        {
            "attempt_id": attempt_id,
            "task_id": task_id,
            "run_id": run_id,
            "status": "running",
        },
    )
    io.atomic_write_json(
        artifact_manifest_path,
        {"attempt_id": attempt_id, "artifacts": [{"artifact_id": "art_1"}]},
    )

    io.create_attempt_workspace(run_id, task_id, attempt_id, base_dir=tmp_path)

    assert io.read_json(attempt_status_path)["status"] == "running"
    assert io.read_json(artifact_manifest_path)["artifacts"] == [
        {"artifact_id": "art_1"}
    ]


def test_repeated_create_workspace_does_not_truncate_jsonl(tmp_path):
    run_id = new_run_id()
    task_id = new_task_id()
    attempt_id = new_attempt_id()

    io.create_run_workspace(run_id, base_dir=tmp_path)
    io.append_jsonl(
        paths.task_events_path(run_id, tmp_path),
        {"event_id": "evt_keep"},
    )
    io.append_jsonl(
        paths.approvals_path(run_id, tmp_path),
        {"approval_id": "apr_keep"},
    )

    io.create_task_workspace(run_id, task_id, base_dir=tmp_path)
    io.create_attempt_workspace(run_id, task_id, attempt_id, base_dir=tmp_path)
    io.append_jsonl(
        paths.tool_calls_path(run_id, task_id, attempt_id, tmp_path),
        {"tool_call_id": "tc_keep"},
    )

    io.create_run_workspace(run_id, base_dir=tmp_path)
    io.create_task_workspace(run_id, task_id, base_dir=tmp_path)
    io.create_attempt_workspace(run_id, task_id, attempt_id, base_dir=tmp_path)

    assert io.read_jsonl(paths.task_events_path(run_id, tmp_path)) == [
        {"event_id": "evt_keep"}
    ]
    assert io.read_jsonl(paths.approvals_path(run_id, tmp_path)) == [
        {"approval_id": "apr_keep"}
    ]
    assert io.read_jsonl(
        paths.tool_calls_path(run_id, task_id, attempt_id, tmp_path)
    ) == [{"tool_call_id": "tc_keep"}]
