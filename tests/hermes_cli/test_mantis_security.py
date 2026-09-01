from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.mantis_security import (
    MANTIS_CAPABILITIES,
    MANTIS_FINDING_STATES,
    FindingTransitionError,
    MantisHealth,
    advance_finding,
    mantis_health,
    plane_security_projection,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_mantis_capability_and_finding_models_are_complete():
    assert MANTIS_CAPABILITIES == (
        "architecture",
        "threat_model",
        "research",
        "review",
        "reproduce",
        "patch",
    )
    assert MANTIS_FINDING_STATES == (
        "candidate",
        "reviewed",
        "reproduced",
        "validated",
        "accepted",
        "fixed",
        "verified",
        "dismissed",
    )


def test_reproducer_health_fails_closed_without_verified_isolation():
    result = mantis_health(
        {
            "enabled": True,
            "capabilities": ["review", "reproduce"],
            "isolated_runtime": False,
            "production_credentials": False,
            "internal_network": False,
        },
        "reproduce",
    )

    assert result.ready is False
    assert result.blockers == ("isolated_runtime must be verified",)


def test_reproducer_health_rejects_production_connections():
    result = mantis_health(
        {
            "enabled": True,
            "capabilities": ["reproduce"],
            "isolated_runtime": True,
            "production_credentials": True,
            "internal_network": True,
        },
        "reproduce",
    )

    assert result.ready is False
    assert result.blockers == (
        "production_credentials must be false",
        "internal_network must be false",
    )


def test_review_health_does_not_require_executable_runtime():
    result = mantis_health(
        {
            "enabled": True,
            "capabilities": ["review"],
            "isolated_runtime": False,
            "production_credentials": False,
            "internal_network": False,
        },
        "review",
    )

    assert result.ready is True
    assert result.blockers == ()


def test_finding_lifecycle_rejects_skipping_review_and_validation():
    with pytest.raises(FindingTransitionError):
        advance_finding("candidate", "validated")
    with pytest.raises(FindingTransitionError):
        advance_finding("reproduced", "accepted")

    assert advance_finding("candidate", "reviewed") == "reviewed"
    assert advance_finding("reviewed", "reproduced") == "reproduced"
    assert advance_finding("reproduced", "validated") == "validated"
    assert advance_finding("validated", "accepted") == "accepted"
    assert advance_finding("accepted", "fixed") == "fixed"
    assert advance_finding("fixed", "verified") == "verified"
    assert advance_finding("reviewed", "dismissed") == "dismissed"


def test_plane_projection_only_includes_reviewed_or_validated_findings():
    findings = [
        {"id": "F-1", "state": "candidate", "title": "unreviewed"},
        {"id": "F-2", "state": "reviewed", "title": "reviewed"},
        {"id": "F-3", "state": "reproduced", "title": "reproduced"},
        {"id": "F-4", "state": "validated", "title": "validated"},
        {"id": "F-5", "state": "accepted", "title": "accepted"},
    ]

    projection = plane_security_projection(findings)

    assert [item["id"] for item in projection["findings"]] == ["F-2", "F-4"]
    assert projection["source"] == "mantis"
    assert projection["projection_only"] is True


def test_production_connected_profile_cannot_claim_or_spawn_mantis_reproducer(
    kanban_home,
):
    profile = kanban_home / "profiles" / "prod-worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "mantis:\n"
        "  enabled: true\n"
        "  capabilities: [reproduce, patch]\n"
        "  isolated_runtime: true\n"
        "  production_credentials: true\n"
        "  internal_network: true\n",
        encoding="utf-8",
    )
    spawned = []

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="reproduce finding",
            assignee="prod-worker",
            mantis_capability="reproduce",
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *args, **kwargs: spawned.append(args) or 123,
            reconcile_orphans=False,
        )
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert spawned == []
    assert result.spawned == []
    assert task is not None and task.status == "blocked"
    assert any(event.kind == "mantis_isolation_rejected" for event in events)


@pytest.mark.parametrize("capability", ["reproduce", "patch"])
def test_production_connected_profile_cannot_claim_mantis_execution(
    kanban_home,
    capability,
):
    profile = kanban_home / "profiles" / "prod-worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "mantis:\n"
        "  enabled: true\n"
        f"  capabilities: [{capability}]\n"
        "  isolated_runtime: true\n"
        "  production_credentials: true\n"
        "  internal_network: false\n",
        encoding="utf-8",
    )

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title=f"{capability} finding",
            assignee="prod-worker",
            mantis_capability=capability,
        )
        claimed = kb.claim_task(conn, task_id)
        task = kb.get_task(conn, task_id)

    assert claimed is None
    assert task is not None and task.status == "blocked"


def test_dispatch_rechecks_mantis_health_before_execution(kanban_home, monkeypatch):
    profile = kanban_home / "profiles" / "security-worker"
    profile.mkdir(parents=True)
    calls = []

    def changing_health(capability, profile_name):
        calls.append((capability, profile_name))
        if len(calls) == 1:
            return MantisHealth(capability, True, ())
        return MantisHealth(
            capability,
            False,
            ("isolated_runtime must be verified",),
        )

    monkeypatch.setattr(
        "hermes_cli.mantis_security.task_mantis_health",
        changing_health,
    )
    spawned = []
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="reproduce finding",
            assignee="security-worker",
            mantis_capability="reproduce",
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *args, **kwargs: spawned.append(args) or 123,
            reconcile_orphans=False,
        )
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert len(calls) == 2
    assert spawned == []
    assert result.spawned == []
    assert task is not None and task.status == "blocked"
    rejected = [event for event in events if event.kind == "mantis_isolation_rejected"]
    assert rejected
    assert rejected[-1].payload is not None
    assert rejected[-1].payload["stage"] == "execute"


def test_verified_isolated_profile_can_spawn_mantis_patch(kanban_home):
    profile = kanban_home / "profiles" / "security-worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "mantis:\n"
        "  enabled: true\n"
        "  capabilities: [patch]\n"
        "  isolated_runtime: true\n"
        "  production_credentials: false\n"
        "  internal_network: false\n",
        encoding="utf-8",
    )

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="patch finding",
            assignee="security-worker",
            mantis_capability="patch",
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *args, **kwargs: 123,
            reconcile_orphans=False,
        )
        task = kb.get_task(conn, task_id)

    assert any(item[0] == task_id for item in result.spawned)
    assert task is not None and task.status == "running"
