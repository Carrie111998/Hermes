"""Tests for the durable profile Team registry."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from hermes_cli.profile_teams import (
    REGISTRY_VERSION,
    ProfileTeamError,
    ProfileTeamRegistry,
    ProfileTeamRegistryCorruptError,
)


KNOWN = {"default", "lead", "researcher", "coder"}


def _create_in_process(path: str, team_id: str) -> None:
    registry = ProfileTeamRegistry(path, known_profiles=KNOWN)
    registry.create(
        team_id=team_id,
        name=f"Team {team_id}",
        lead="lead",
        members=["lead", "researcher"],
    )


@pytest.fixture
def registry(tmp_path: Path) -> ProfileTeamRegistry:
    return ProfileTeamRegistry(tmp_path / "profile_teams.json", known_profiles=KNOWN)


def test_create_persists_versioned_canonical_registry(registry: ProfileTeamRegistry) -> None:
    created = registry.create(
        team_id="launch-team",
        name="  Launch Team  ",
        lead="LEAD",
        members=["Lead", "Researcher"],
    )

    assert created == {
        "id": "launch-team",
        "name": "Launch Team",
        "lead": "lead",
        "members": ["lead", "researcher"],
    }
    raw = json.loads(registry.path.read_text(encoding="utf-8"))
    assert raw == {"version": REGISTRY_VERSION, "teams": [created]}


def test_list_and_get_return_defensive_member_copies(registry: ProfileTeamRegistry) -> None:
    registry.create(team_id="one", name="One", lead="lead", members=["lead", "coder"])

    listed = registry.list()
    listed[0]["members"].append("researcher")
    fetched = registry.get("one")
    assert fetched is not None
    assert fetched["members"] == ["lead", "coder"]
    assert registry.get("missing") is None


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"team_id": "Bad ID", "name": "Team", "lead": "lead", "members": ["lead", "coder"]}, "team id"),
        ({"team_id": "team", "name": " ", "lead": "lead", "members": ["lead", "coder"]}, "name"),
        ({"team_id": "team", "name": "Team", "lead": "lead", "members": ["lead"]}, "at least two"),
        ({"team_id": "team", "name": "Team", "lead": "lead", "members": ["lead", "LEAD"]}, "unique"),
        ({"team_id": "team", "name": "Team", "lead": "coder", "members": ["lead", "researcher"]}, "lead must"),
        ({"team_id": "team", "name": "Team", "lead": "lead", "members": ["lead", "ghost"]}, "unknown member"),
    ],
)
def test_create_rejects_invalid_team(
    registry: ProfileTeamRegistry,
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ProfileTeamError, match=message):
        registry.create(**kwargs)  # type: ignore[arg-type]
    assert not registry.path.exists()


def test_duplicate_id_is_rejected_without_changing_file(registry: ProfileTeamRegistry) -> None:
    registry.create(team_id="same", name="First", lead="lead", members=["lead", "coder"])
    before = registry.path.read_bytes()

    with pytest.raises(ProfileTeamError, match="already exists"):
        registry.create(team_id="same", name="Second", lead="lead", members=["lead", "researcher"])

    assert registry.path.read_bytes() == before


def test_update_revalidates_live_known_profiles(tmp_path: Path) -> None:
    known = {"lead", "coder", "researcher"}
    registry = ProfileTeamRegistry(tmp_path / "teams.json", known_profiles=lambda: known)
    registry.create(team_id="crew", name="Crew", lead="lead", members=["lead", "coder"])
    known.remove("coder")

    with pytest.raises(ProfileTeamError, match="unknown member.*coder"):
        registry.update("crew", name="Crew", lead="lead", members=["lead", "coder"])

    updated = registry.update("crew", name="New Crew", lead="researcher", members=["lead", "researcher"])
    assert updated["name"] == "New Crew"
    assert updated["lead"] == "researcher"


def test_delete_is_idempotent(registry: ProfileTeamRegistry) -> None:
    registry.create(team_id="crew", name="Crew", lead="lead", members=["lead", "coder"])
    assert registry.delete("crew") is True
    assert registry.delete("crew") is False
    assert registry.list() == []


def test_unsupported_or_malformed_registry_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "teams.json"
    path.write_text('{"version":99,"teams":[]}', encoding="utf-8")
    registry = ProfileTeamRegistry(path, known_profiles=KNOWN)

    with pytest.raises(ProfileTeamRegistryCorruptError, match="unsupported"):
        registry.list()
    with pytest.raises(ProfileTeamRegistryCorruptError, match="unsupported"):
        registry.create(team_id="new", name="New", lead="lead", members=["lead", "coder"])
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 99

    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(ProfileTeamRegistryCorruptError, match="could not read"):
        registry.list()


def test_invalid_persisted_team_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "teams.json"
    path.write_text(
        json.dumps(
            {
                "version": REGISTRY_VERSION,
                "teams": [
                    {"id": "broken", "name": "Broken", "lead": "lead", "members": ["lead", "lead"]}
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProfileTeamRegistryCorruptError, match="unique"):
        ProfileTeamRegistry(path, known_profiles=KNOWN).list()


def test_writes_use_atomic_owner_only_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hermes_cli.profile_teams as module

    calls: list[tuple[Path, dict[str, object], int | None]] = []

    def fake_atomic(path: Path, data: dict[str, object], *, mode: int | None = None, **_: object) -> None:
        calls.append((path, data, mode))
        path.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(module, "atomic_json_write", fake_atomic)
    registry = ProfileTeamRegistry(tmp_path / "teams.json", known_profiles=KNOWN)
    registry.create(team_id="crew", name="Crew", lead="lead", members=["lead", "coder"])

    assert calls and calls[0][0] == registry.path
    assert calls[0][1]["version"] == REGISTRY_VERSION
    assert calls[0][2] == 0o600


def test_process_lock_prevents_lost_concurrent_creates(tmp_path: Path) -> None:
    path = tmp_path / "teams.json"
    ctx = multiprocessing.get_context("spawn")
    processes = [ctx.Process(target=_create_in_process, args=(str(path), f"team-{index}")) for index in range(8)]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    teams = ProfileTeamRegistry(path, known_profiles=KNOWN).list()
    assert {team["id"] for team in teams} == {f"team-{index}" for index in range(8)}
