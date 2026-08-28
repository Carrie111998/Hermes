"""Security and durability tests for Bot-published Group Chat files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.hosted_room_artifacts import (
    RoomArtifactError,
    RoomArtifactOutbox,
    RoomArtifactScope,
    bind_room_artifact_scope,
    reset_room_artifact_scope,
    terminal_artifact_manifest,
    validate_terminal_artifact_manifest,
)
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.hosted_room_artifact import share_group_file


def _scope(**overrides) -> RoomArtifactScope:
    value = {
        "room_id": "room-1",
        "task_id": "dtask:abc",
        "execution_generation": 1,
        "member_id": "member-build",
        "target_profile": "build",
        "home_install_id": "install-home",
        "target_install_id": "install-target",
        "authority_gateway_id": "gateway-home",
        "authority_epoch": 1,
    }
    value.update(overrides)
    return RoomArtifactScope.from_mapping(value)


def test_outbox_is_idempotent_scoped_and_acknowledged(tmp_path: Path):
    db = tmp_path / "state.db"
    path = tmp_path / "handoff.md"
    path.write_text("# Handoff\n", encoding="utf-8")
    outbox = RoomArtifactOutbox(db)
    scope = _scope()

    first = outbox.put_path(scope=scope, path=path)
    replay = outbox.put_path(scope=scope, path=path)
    assert replay == first
    metadata, data = outbox.read(scope, first["artifact_id"])
    assert metadata == first
    assert data == b"# Handoff\n"
    with pytest.raises(RoomArtifactError, match="not found"):
        outbox.read(_scope(task_id="dtask:other"), first["artifact_id"])

    manifest = terminal_artifact_manifest(db, scope)
    assert validate_terminal_artifact_manifest(manifest) == [first]
    assert outbox.acknowledge(scope, [first["artifact_id"]]) == 1
    assert outbox.acknowledge(scope, [first["artifact_id"]]) == 0
    with pytest.raises(RoomArtifactError, match="not found"):
        outbox.read(scope, first["artifact_id"])


def test_share_group_file_is_visible_only_inside_bound_room_turn(tmp_path: Path):
    path = tmp_path / "review.md"
    path.write_text("Review this.\n", encoding="utf-8")
    assert json.loads(share_group_file(str(path)))["ok"] is False

    home_token = set_hermes_home_override(tmp_path)
    scope_token = bind_room_artifact_scope(_scope())
    try:
        result = json.loads(share_group_file(str(path), name="handoff.md"))
    finally:
        reset_room_artifact_scope(scope_token)
        reset_hermes_home_override(home_token)

    assert result["ok"] is True
    assert result["name"] == "handoff.md"
    assert str(path) not in json.dumps(result)
    assert RoomArtifactOutbox(tmp_path / "state.db").list(_scope())[0][
        "artifact_id"
    ] == result["artifact_id"]


def test_share_group_file_rejects_symlink(tmp_path: Path):
    target = tmp_path / "target.md"
    target.write_text("secret-ish\n", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(target)
    home_token = set_hermes_home_override(tmp_path)
    scope_token = bind_room_artifact_scope(_scope())
    try:
        result = json.loads(share_group_file(str(link)))
    finally:
        reset_room_artifact_scope(scope_token)
        reset_hermes_home_override(home_token)
    assert result == {"ok": False, "error": "Symbolic links cannot be shared."}


def test_terminal_manifest_rejects_tampered_digest(tmp_path: Path):
    db = tmp_path / "state.db"
    path = tmp_path / "handoff.md"
    path.write_text("# Handoff\n", encoding="utf-8")
    scope = _scope()
    RoomArtifactOutbox(db).put_path(scope=scope, path=path)
    manifest = terminal_artifact_manifest(db, scope)
    manifest["items"][0]["name"] = "changed.md"

    with pytest.raises(RoomArtifactError, match="digest changed"):
        validate_terminal_artifact_manifest(manifest)


def test_cancel_and_disband_purge_only_the_matching_scope(tmp_path: Path):
    db = tmp_path / "state.db"
    path = tmp_path / "handoff.md"
    path.write_text("handoff\n", encoding="utf-8")
    outbox = RoomArtifactOutbox(db)
    first = _scope()
    second = _scope(task_id="dtask:second")
    other_room = _scope(room_id="room-2", task_id="dtask:third")
    outbox.put_path(scope=first, path=path)
    outbox.put_path(scope=second, path=path)
    outbox.put_path(scope=other_room, path=path)

    assert outbox.discard(first) == 1
    assert outbox.list(first) == []
    assert len(outbox.list(second)) == 1
    assert outbox.discard_room("room-1") == 1
    assert outbox.list(second) == []
    assert len(outbox.list(other_room)) == 1
