from pathlib import Path

import pytest

from hermes_cli import projects_db
from hermes_cli.workspace_context_store import WorkspaceContextStore
from hermes_cli.web_routers import workspace as workspace_routes
from hermes_cli.web_routers.workspace import (
    merge_remote_runner_bindings,
    resolve_workspace_binding,
    workspace_project_payload,
)


def test_workspace_context_route_persists_only_valid_project_allowlists(monkeypatch, tmp_path):
    with projects_db.connect_closing(tmp_path / "projects.db") as connection:
        project_id = projects_db.create_project(
            connection,
            name="Context",
            primary_path=str(tmp_path),
        )
    monkeypatch.setattr(workspace_routes, "_profile_home", lambda _profile: tmp_path)
    result = workspace_routes.workspace_project_context(
        project_id,
        workspace_routes.WorkspaceContextBody(
            notion_page_ids=["1234567890abcdef1234567890abcdef"],
            slack_channel_ids=["C123ABC"],
        ),
    )
    assert result["context"]["slack_channel_ids"] == ["C123ABC"]
    assert WorkspaceContextStore(tmp_path).get(project_id) == result["context"]


def test_workspace_project_payload_never_exposes_local_paths(tmp_path):
    repository = tmp_path / "secret" / "repo"
    repository.mkdir(parents=True)
    database = tmp_path / "projects.db"
    with projects_db.connect_closing(database) as connection:
        project_id = projects_db.create_project(
            connection,
            name="Launch",
            primary_path=str(repository),
        )
        project = projects_db.get_project(connection, project_id)

    assert project is not None
    WorkspaceContextStore(tmp_path).set(
        project_id,
        notion_page_ids=["1234567890abcdef1234567890abcdef"],
        slack_channel_ids=["C123ABC"],
    )
    payload = workspace_project_payload(project, profile_home=tmp_path)
    serialized = repr(payload)

    assert str(repository) not in serialized
    assert payload["id"] == project_id
    assert payload["bindings"][0]["binding_id"].startswith("b_")
    assert "path" not in payload["bindings"][0]
    assert payload["context"] == {
        "notion_page_ids": ["1234567890abcdef1234567890abcdef"],
        "slack_channel_ids": ["C123ABC"],
    }


def test_workspace_binding_resolution_is_opaque_stable_and_fail_closed(tmp_path):
    first = tmp_path / "repo-one"
    second = tmp_path / "repo-two"
    first.mkdir()
    second.mkdir()
    database = tmp_path / "projects.db"
    with projects_db.connect_closing(database) as connection:
        project_id = projects_db.create_project(
            connection,
            folders=[str(first), str(second)],
            name="Launch",
            primary_path=str(first),
        )
        project = projects_db.get_project(connection, project_id)

    assert project is not None
    payload = workspace_project_payload(project, profile_home=tmp_path)
    binding_id = payload["bindings"][0]["binding_id"]

    assert resolve_workspace_binding(
        binding_id,
        db_path=database,
        profile_home=tmp_path,
    ) == first.resolve()
    assert resolve_workspace_binding(
        binding_id,
        db_path=database,
        profile_home=tmp_path,
    ) == first.resolve()
    with pytest.raises(ValueError, match="unknown"):
        resolve_workspace_binding(
            "b_unknown",
            db_path=database,
            profile_home=tmp_path,
        )


def test_remote_runner_bindings_merge_without_becoming_local_chat_targets():
    projects = [{"id": "project-1", "bindings": []}]
    runners = [
        {
            "runner_id": "runner-1",
            "label": "Studio Mac",
            "status": "online",
            "capabilities": ["worker.codex"],
            "bindings": [
                {
                    "binding_id": "binding-1",
                    "label": "Launch",
                    "project_id": "project-1",
                    "status": "online",
                }
            ],
        }
    ]

    merged = merge_remote_runner_bindings(projects, runners)

    assert merged[0]["bindings"] == [
        {
            "binding_id": "binding-1",
            "capabilities": ["worker.codex"],
            "chat_available": False,
            "is_primary": False,
            "label": "Launch",
            "runner_id": "runner-1",
            "status": "online",
        }
    ]
