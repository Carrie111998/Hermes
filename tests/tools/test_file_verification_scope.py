"""Verification staleness must follow edited code roots, not task cwd."""

from pathlib import Path

import agent.coding_context as coding_context
import agent.verification_evidence as verification_evidence
from tools.file_tools import _mark_verification_stale


def test_runtime_state_outside_project_does_not_stale_task_workspace(monkeypatch, tmp_path):
    calls = []
    hermes_home = tmp_path / ".hermes"
    runtime = hermes_home / "cron" / "state" / "daily-radar" / "outbox" / "report.md"

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(coding_context, "project_facts_for", lambda _cwd: None)
    monkeypatch.setattr(
        verification_evidence,
        "mark_workspace_edited",
        lambda **kwargs: calls.append(kwargs),
    )

    _mark_verification_stale("cron-session", [str(runtime)], session_id="cron-session")

    assert calls == []


def test_runtime_state_is_ignored_even_when_hermes_home_has_workspace_marker(
    monkeypatch, tmp_path
):
    """A ~/.hermes marker must not turn cron receipts into edited code."""
    calls = []
    hermes_home = tmp_path / ".hermes"
    runtime = hermes_home / "cron" / "state" / "daily-radar" / "outbox" / "report.md"

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(
        coding_context,
        "project_facts_for",
        lambda _cwd: {"root": str(hermes_home)},
    )
    monkeypatch.setattr(
        verification_evidence,
        "mark_workspace_edited",
        lambda **kwargs: calls.append(kwargs),
    )

    _mark_verification_stale("cron-session", [str(runtime)], session_id="cron-session")

    assert calls == []


def test_mixed_paths_mark_only_detected_code_workspace(monkeypatch, tmp_path):
    calls = []
    code_root = tmp_path / "repo"
    code_path = code_root / "src" / "app.py"
    hermes_home = tmp_path / ".hermes"
    runtime_path = hermes_home / "cron" / "state" / "facts-sync-v2" / "outbox" / "report.md"

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    def fake_project_facts(cwd):
        candidate = Path(cwd).resolve()
        root = code_root.resolve()
        if candidate == root or root in candidate.parents:
            return {"root": str(root)}
        return None

    monkeypatch.setattr(coding_context, "project_facts_for", fake_project_facts)
    monkeypatch.setattr(
        verification_evidence,
        "mark_workspace_edited",
        lambda **kwargs: calls.append(kwargs),
    )

    _mark_verification_stale(
        "cron-session",
        [str(runtime_path), str(code_path)],
        session_id="cron-session",
    )

    assert calls == [
        {
            "session_id": "cron-session",
            "cwd": str(code_path.parent.resolve()),
            "paths": [str(code_path.resolve())],
        }
    ]


def test_nonproject_edit_preserves_authoritative_workspace_fallback(monkeypatch, tmp_path):
    calls = []
    workspace = tmp_path / "remote-workspace"
    changed = tmp_path / "container-mirror" / "src" / "app.py"

    monkeypatch.setattr(coding_context, "project_facts_for", lambda _cwd: None)
    monkeypatch.setattr(
        "tools.file_tools._authoritative_workspace_root",
        lambda _task_id: str(workspace),
    )
    monkeypatch.setattr(
        verification_evidence,
        "mark_workspace_edited",
        lambda **kwargs: calls.append(kwargs),
    )

    _mark_verification_stale("remote-task", [str(changed)], session_id="session")

    assert calls == [
        {
            "session_id": "session",
            "cwd": str(workspace),
            "paths": [str(changed.resolve())],
        }
    ]
