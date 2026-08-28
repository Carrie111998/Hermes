"""Focused tests for scoped RoomLink attachment staging."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.hosted_room_peer import (
    HostedMemberDispatch,
    attachment_manifest_digest,
    issue_room_grant,
)
from gateway.platforms import api_server_room_attachments as room_attachments
from gateway.platforms.api_server import APIServerAdapter


TARGET_INSTALL = "install:target"


def test_capability_requires_the_pdf_renderer(monkeypatch):
    monkeypatch.setattr(room_attachments, "web", object())
    monkeypatch.setattr(room_attachments.shutil, "which", lambda _name: None)
    assert room_attachments.roomlink_attachments_available() is False

    monkeypatch.setattr(room_attachments.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert room_attachments.roomlink_attachments_available() is True


def _manifest(data: bytes = b"hello") -> list[dict[str, object]]:
    return [
        {
            "attachment_id": "att_0123456789abcdef0123456789abcdef",
            "kind": "file",
            "name": "brief.txt",
            "size": len(data),
            "mime": "text/plain",
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    ]


def _dispatch(manifest=None, **overrides) -> HostedMemberDispatch:
    prompt = "Review the attached brief."
    value = {
        "protocol_version": 2,
        "room_id": "room-1",
        "home_install_id": "install-home",
        "authority_gateway_id": "gateway-home",
        "authority_epoch": 1,
        "member_id": "member-reviewer",
        "target_install_id": TARGET_INSTALL,
        "target_profile": "default",
        "task_id": "task-1",
        "execution_generation": 1,
        "source_event_seq": 1,
        "cancellation_scope_id": "cancel-1",
        "prompt": prompt,
        "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
        "capability_digest": "a" * 64,
        "trace_id": "trace-1",
        **overrides,
    }
    if manifest is not None:
        value["attachment_manifest_digest"] = attachment_manifest_digest(manifest)
    return HostedMemberDispatch.from_mapping(value)


def test_spool_is_idempotent_and_survives_restart(tmp_path: Path):
    db = tmp_path / "state.db"
    root = tmp_path / "spool"
    manifest = _manifest()
    dispatch = _dispatch(manifest)
    claims = {
        "room_id": dispatch.room_id,
        "home_install_id": dispatch.home_install_id,
        "member_id": dispatch.member_id,
        "target_install_id": dispatch.target_install_id,
        "target_profile": dispatch.target_profile,
    }
    first = room_attachments.RoomAttachmentSpool(db, root=root)
    assert first.prepare(dispatch, manifest)["idempotent"] is False
    uploaded = first.put(
        claims=claims,
        task_id=dispatch.task_id,
        execution_generation=dispatch.execution_generation,
        attachment_id=str(manifest[0]["attachment_id"]),
        data=b"hello",
    )
    assert uploaded == {"complete": True, "idempotent": False}
    assert first.put(
        claims=claims,
        task_id=dispatch.task_id,
        execution_generation=dispatch.execution_generation,
        attachment_id=str(manifest[0]["attachment_id"]),
        data=b"hello",
    ) == {"complete": True, "idempotent": True}

    restarted = room_attachments.RoomAttachmentSpool(db, root=root)
    assert restarted.prepare(dispatch, manifest)["complete"] is True
    assert restarted.require_complete(dispatch) == manifest
    assert stat_mode(root) == 0o700
    assert all(stat_mode(path) == 0o600 for path in root.iterdir())

    materialized = restarted.materialize(dispatch)
    assert [item["name"] for item in materialized] == ["brief.txt"]
    assert Path(materialized[0]["path"]).read_bytes() == b"hello"


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_spool_rejects_conflict_incomplete_and_corrupt_batches(tmp_path: Path):
    spool = room_attachments.RoomAttachmentSpool(
        tmp_path / "state.db",
        root=tmp_path / "spool",
    )
    manifest = _manifest()
    dispatch = _dispatch(manifest)
    spool.prepare(dispatch, manifest)
    with pytest.raises(room_attachments.RoomAttachmentSpoolIncomplete):
        spool.require_complete(dispatch)
    claims = {
        "room_id": dispatch.room_id,
        "home_install_id": dispatch.home_install_id,
        "member_id": dispatch.member_id,
        "target_install_id": dispatch.target_install_id,
        "target_profile": dispatch.target_profile,
    }
    with pytest.raises(room_attachments.RoomAttachmentSpoolConflict):
        spool.put(
            claims=claims,
            task_id=dispatch.task_id,
            execution_generation=dispatch.execution_generation,
            attachment_id=str(manifest[0]["attachment_id"]),
            data=b"wrong",
        )
    spool.put(
        claims=claims,
        task_id=dispatch.task_id,
        execution_generation=dispatch.execution_generation,
        attachment_id=str(manifest[0]["attachment_id"]),
        data=b"hello",
    )
    next(path for path in spool.root.iterdir() if path.is_file()).write_bytes(b"bad")
    with pytest.raises(room_attachments.RoomAttachmentSpoolIncomplete):
        spool.require_complete(dispatch)
    repaired = spool.put(
        claims=claims,
        task_id=dispatch.task_id,
        execution_generation=dispatch.execution_generation,
        attachment_id=str(manifest[0]["attachment_id"]),
        data=b"hello",
    )
    assert repaired == {
        "complete": True,
        "idempotent": True,
        "repaired": True,
    }
    assert spool.require_complete(dispatch) == manifest


def test_spool_expires_partial_or_stopped_attempt_data_after_restart(tmp_path: Path):
    now = [100.0]
    db = tmp_path / "state.db"
    root = tmp_path / "spool"
    manifest = _manifest()
    dispatch = _dispatch(manifest)
    claims = {
        "room_id": dispatch.room_id,
        "home_install_id": dispatch.home_install_id,
        "member_id": dispatch.member_id,
        "target_install_id": dispatch.target_install_id,
        "target_profile": dispatch.target_profile,
    }
    spool = room_attachments.RoomAttachmentSpool(
        db,
        root=root,
        clock=lambda: now[0],
    )
    spool.prepare(dispatch, manifest)
    spool.put(
        claims=claims,
        task_id=dispatch.task_id,
        execution_generation=dispatch.execution_generation,
        attachment_id=str(manifest[0]["attachment_id"]),
        data=b"hello",
    )
    now[0] += room_attachments.SPOOL_TTL_SECONDS + 1

    restarted = room_attachments.RoomAttachmentSpool(
        db,
        root=root,
        clock=lambda: now[0],
    )
    with pytest.raises(room_attachments.RoomAttachmentSpoolIncomplete):
        restarted.require_complete(dispatch)
    assert list(root.iterdir()) == []


def test_revoked_member_scope_removes_only_its_staged_batches(tmp_path: Path):
    spool = room_attachments.RoomAttachmentSpool(
        tmp_path / "state.db",
        root=tmp_path / "spool",
    )
    manifest = _manifest()
    dispatch = _dispatch(manifest)
    claims = {
        "room_id": dispatch.room_id,
        "home_install_id": dispatch.home_install_id,
        "member_id": dispatch.member_id,
        "target_install_id": dispatch.target_install_id,
        "target_profile": dispatch.target_profile,
    }
    spool.prepare(dispatch, manifest)
    spool.put(
        claims=claims,
        task_id=dispatch.task_id,
        execution_generation=dispatch.execution_generation,
        attachment_id=str(manifest[0]["attachment_id"]),
        data=b"hello",
    )

    assert spool.discard_scope(claims) == 1
    with pytest.raises(room_attachments.RoomAttachmentSpoolIncomplete):
        spool.require_complete(dispatch)
    assert list(spool.root.iterdir()) == []


@pytest.fixture
def attachment_api(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "gateway.hosted_rooms.local_authority_gateway_id",
        lambda: TARGET_INSTALL,
    )
    room_attachments._spool.cache_clear()
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-secret"})
    )
    app = web.Application()
    app.router.add_post(
        "/v1/room-members/attachments",
        adapter._handle_room_attachment_manifest,
    )
    app.router.add_put(
        "/v1/room-members/attachments/{task_id}/{execution_generation}/{attachment_id}",
        adapter._handle_room_attachment_upload,
    )
    yield adapter, app
    room_attachments._spool.cache_clear()


def _grant(adapter: APIServerAdapter, *, permissions=("attachment.stage",)) -> str:
    return issue_room_grant(
        adapter._room_grant_secret(),
        grant_id="grant-attachment",
        room_id="room-1",
        home_install_id="install-home",
        authority_gateway_id="gateway-home",
        authority_epoch=1,
        member_id="member-reviewer",
        target_install_id=TARGET_INSTALL,
        target_profile="default",
        permissions=permissions,
    )


def test_api_server_registers_scoped_attachment_routes(attachment_api):
    adapter, _app = attachment_api
    routes = {(method, path) for method, path, _handler in adapter._http_route_table()}
    assert ("POST", "/v1/room-members/attachments") in routes
    assert (
        "PUT",
        "/v1/room-members/attachments/{task_id}/{execution_generation}/{attachment_id}",
    ) in routes


@pytest.mark.asyncio
async def test_api_requires_scoped_permission_and_completes_batch(attachment_api):
    adapter, app = attachment_api
    manifest = _manifest()
    dispatch = _dispatch(manifest)
    denied_grant = _grant(adapter, permissions=("dispatch",))
    grant = _grant(adapter)
    async with TestClient(TestServer(app)) as client:
        denied = await client.post(
            "/v1/room-members/attachments",
            json={
                "hosted_room_dispatch": dispatch.as_mapping(),
                "attachments": manifest,
            },
            headers={"Authorization": f"HermesRoom {denied_grant}"},
        )
        assert denied.status == 401

        prepared = await client.post(
            "/v1/room-members/attachments",
            json={
                "hosted_room_dispatch": dispatch.as_mapping(),
                "attachments": manifest,
            },
            headers={"Authorization": f"HermesRoom {grant}"},
        )
        assert prepared.status == 201
        uploaded = await client.put(
            "/v1/room-members/attachments/task-1/1/"
            "att_0123456789abcdef0123456789abcdef",
            data=b"hello",
            headers={"Authorization": f"HermesRoom {grant}"},
        )
        assert uploaded.status == 201
        assert (await uploaded.json())["complete"] is True
        repeated = await client.put(
            "/v1/room-members/attachments/task-1/1/"
            "att_0123456789abcdef0123456789abcdef",
            data=b"hello",
            headers={"Authorization": f"HermesRoom {grant}"},
        )
        assert repeated.status == 200
        assert (await repeated.json())["idempotent"] is True


@pytest.mark.asyncio
async def test_api_rejects_a_revoked_attachment_grant(attachment_api, monkeypatch):
    adapter, app = attachment_api
    manifest = _manifest()
    dispatch = _dispatch(manifest)
    grant = _grant(adapter)
    monkeypatch.setattr(
        "gateway.hosted_rooms.room_grant_is_revoked",
        lambda *args, **kwargs: True,
    )
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/room-members/attachments",
            json={
                "hosted_room_dispatch": dispatch.as_mapping(),
                "attachments": manifest,
            },
            headers={"Authorization": f"HermesRoom {grant}"},
        )
        body = await response.json()
    assert response.status == 401
    assert body["error"]["code"] == "invalid_room_grant"


@pytest.mark.asyncio
async def test_api_rejects_oversized_upload_before_reading_body(attachment_api):
    adapter, app = attachment_api
    grant = _grant(adapter)
    async with TestClient(TestServer(app)) as client:
        response = await client.put(
            "/v1/room-members/attachments/task-1/1/"
            "att_0123456789abcdef0123456789abcdef",
            data=b"x",
            headers={
                "Authorization": f"HermesRoom {grant}",
                "Content-Length": str(15_000_001),
            },
        )
        assert response.status == 413
        assert (await response.json())["error"]["code"] == "room_attachment_too_large"


@pytest.mark.asyncio
async def test_dispatch_admission_fails_closed_until_batch_is_complete(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    room_attachments._spool.cache_clear()
    manifest = _manifest()
    dispatch = _dispatch(manifest)
    normalized = {
        "input": dispatch.prompt,
        "hosted_room_dispatch": dispatch.as_mapping(),
    }
    unchanged, error = await room_attachments._validate_dispatch_attachments(
        normalized,
        _openai_error=lambda message, **kwargs: {
            "error": {"message": message, **kwargs}
        },
    )
    assert unchanged is normalized
    assert error.status == 409
    assert json.loads(error.text)["error"]["code"] == "room_attachments_incomplete"

    text_dispatch = _dispatch()
    text_only = {
        "input": text_dispatch.prompt,
        "hosted_room_dispatch": text_dispatch.as_mapping(),
    }
    assert await room_attachments._validate_dispatch_attachments(
        text_only,
        _openai_error=lambda message, **kwargs: {
            "error": {"message": message, **kwargs}
        },
    ) == (text_only, None)
    room_attachments._spool.cache_clear()


@pytest.mark.asyncio
async def test_dispatch_materializes_files_into_the_target_run_prompt(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    room_attachments._spool.cache_clear()
    manifest = _manifest()
    dispatch = _dispatch(manifest)
    claims = {
        "room_id": dispatch.room_id,
        "home_install_id": dispatch.home_install_id,
        "member_id": dispatch.member_id,
        "target_install_id": dispatch.target_install_id,
        "target_profile": dispatch.target_profile,
    }
    spool = room_attachments._default_spool()
    spool.prepare(dispatch, manifest)
    spool.put(
        claims=claims,
        task_id=dispatch.task_id,
        execution_generation=dispatch.execution_generation,
        attachment_id=str(manifest[0]["attachment_id"]),
        data=b"hello",
    )

    normalized, error = await room_attachments._validate_dispatch_attachments(
        {
            "input": dispatch.prompt,
            "hosted_room_dispatch": dispatch.as_mapping(),
        },
        _openai_error=lambda message, **kwargs: {
            "error": {"message": message, **kwargs}
        },
    )

    assert error is None
    assert "Attached to this Group Chat message: brief.txt." in normalized["input"]
    assert "Use the file tools" in normalized["input"]
    assert str(spool.root) in normalized["input"]
    assert "aGVsbG8=" not in normalized["input"]
    assert normalized["_room_persist_user_message"] == (
        "Review the attached brief.\n\n[Group Chat files: brief.txt]"
    )
    assert str(spool.root) not in normalized["_room_persist_user_message"]
    room_attachments._spool.cache_clear()


@pytest.mark.asyncio
async def test_dispatch_materializes_images_as_native_multimodal_input(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    room_attachments._spool.cache_clear()
    data = b"\x89PNG\r\n\x1a\nimage"
    manifest = [{
        "attachment_id": "att_0123456789abcdef0123456789abcdef",
        "kind": "image",
        "name": "diagram.png",
        "size": len(data),
        "mime": "image/png",
        "sha256": hashlib.sha256(data).hexdigest(),
    }]
    dispatch = _dispatch(manifest)
    claims = {
        "room_id": dispatch.room_id,
        "home_install_id": dispatch.home_install_id,
        "member_id": dispatch.member_id,
        "target_install_id": dispatch.target_install_id,
        "target_profile": dispatch.target_profile,
    }
    spool = room_attachments._default_spool()
    spool.prepare(dispatch, manifest)
    spool.put(
        claims=claims,
        task_id=dispatch.task_id,
        execution_generation=dispatch.execution_generation,
        attachment_id=str(manifest[0]["attachment_id"]),
        data=data,
    )

    normalized, error = await room_attachments._validate_dispatch_attachments(
        {
            "input": dispatch.prompt,
            "hosted_room_dispatch": dispatch.as_mapping(),
        },
        _openai_error=lambda message, **kwargs: {
            "error": {"message": message, **kwargs}
        },
    )

    assert error is None
    content = normalized["input"][-1]["content"]
    assert any(item.get("type") == "text" for item in content)
    image = next(item for item in content if item.get("type") == "image_url")
    assert image["image_url"]["url"].startswith("data:image/png;base64,")
    assert normalized["_room_persist_user_message"] == (
        "Review the attached brief.\n\n[Group Chat files: diagram.png]"
    )
    assert "base64" not in normalized["_room_persist_user_message"]
    room_attachments._spool.cache_clear()
