"""Regression coverage for fail-closed Matrix E2EE required mode.

TODO(verification-before-merge): run this module through
``scripts/run_tests.sh`` with pytest available.  The isolated draft runner
used for this candidate has no pytest, ruff, ty, or gitleaks; those controls
remain UNKNOWN rather than passing by implication.
"""

import asyncio
import os
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


class _FakeClient:
    def __init__(self, encryption_state=None):
        self.crypto = object()
        self.encryption_state = encryption_state
        self.get_state_event = AsyncMock(return_value=encryption_state)
        self.send_message_event = AsyncMock(return_value="$event:example.org")
        self.upload_media = AsyncMock(return_value="mxc://example.org/media")
        self.state_store: object = None


def _adapter(monkeypatch, *, mode="required", allowed_rooms="!allowed:example.org"):
    monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@alice:example.org")
    monkeypatch.setenv("MATRIX_ALLOWED_ROOMS", allowed_rooms)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    from plugins.platforms.matrix.adapter import MatrixAdapter

    return MatrixAdapter(
        PlatformConfig(
            enabled=True,
            token="synthetic-test-token",
            extra={
                "homeserver": "https://matrix.example.org",
                "user_id": "@bot:example.org",
                "e2ee_mode": mode,
            },
        )
    )


@pytest.mark.asyncio
async def test_required_mode_blocks_all_outbound_paths_when_encryption_state_is_missing(monkeypatch, tmp_path):
    """Required mode must not emit plaintext, edits, media, or reactions."""
    adapter = _adapter(monkeypatch)
    client = _FakeClient(encryption_state=None)
    adapter._client = client

    text = await adapter.send("!allowed:example.org", "synthetic content")
    edit = await adapter.edit_message("!allowed:example.org", "$old", "synthetic edit")
    media = await adapter._upload_and_send(
        "!allowed:example.org", b"synthetic media", "probe.txt", "text/plain", "m.file"
    )
    reaction = await adapter._send_reaction("!allowed:example.org", "$old", "✅")

    assert text.success is False
    assert edit.success is False
    assert media.success is False
    assert reaction is None
    client.send_message_event.assert_not_awaited()
    client.upload_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_required_media_denies_when_remote_and_local_encryption_state_disagree(monkeypatch):
    """A stale local room store must never turn a required-mode upload plaintext."""
    adapter = _adapter(monkeypatch)
    client = _FakeClient(encryption_state={"algorithm": "m.megolm.v1.aes-sha2"})
    client.state_store = SimpleNamespace(is_encrypted=AsyncMock(return_value=False))
    adapter._client = client

    result = await adapter._upload_and_send(
        "!allowed:example.org", b"synthetic plaintext", "probe.txt", "text/plain", "m.file"
    )

    assert result.success is False
    client.upload_media.assert_not_awaited()
    client.send_message_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_required_mode_allows_only_megolm_room_and_optional_mode_is_unchanged(monkeypatch):
    """The strict algorithm applies only to required mode."""
    required = _adapter(monkeypatch, mode="required")
    required_client = _FakeClient(encryption_state={"algorithm": "m.megolm.v1.aes-sha2"})
    required._client = required_client

    required_result = await required.send("!allowed:example.org", "encrypted path")

    optional = _adapter(monkeypatch, mode="optional")
    optional_client = _FakeClient(encryption_state=None)
    optional._client = optional_client
    optional_result = await optional.send("!allowed:example.org", "legacy optional path")

    assert required_result.success is True
    assert optional_result.success is True
    required_client.send_message_event.assert_awaited_once()
    optional_client.send_message_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_required_mode_has_no_dm_bypass_for_locked_down_room_allowlist(monkeypatch):
    """A DM outside MATRIX_ALLOWED_ROOMS remains denied in required mode."""
    adapter = _adapter(monkeypatch)
    adapter._is_dm_room = AsyncMock(return_value=True)

    assert await adapter._is_allowed_matrix_room_event("!unlisted-dm:example.org") is False


@pytest.mark.asyncio
async def test_required_mode_drops_unencrypted_inbound_before_agent_turn(monkeypatch):
    """Missing encryption state prevents inbound content from reaching the agent."""
    adapter = _adapter(monkeypatch)
    adapter._client = _FakeClient(encryption_state=None)
    adapter.handle_message = AsyncMock()
    event = SimpleNamespace(
        room_id="!allowed:example.org",
        sender="@alice:example.org",
        event_id="$inbound",
        timestamp=10**15,
        content={"msgtype": "m.text", "body": "synthetic inbound"},
    )

    await adapter._on_room_message(event)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_required_mode_rejects_plain_inbound_event_without_decrypt_provenance(monkeypatch):
    """Encrypted-room state alone is not proof that this concrete event decrypted."""
    adapter = _adapter(monkeypatch)
    adapter._client = _FakeClient(encryption_state={"algorithm": "m.megolm.v1.aes-sha2"})
    event = SimpleNamespace(room_id="!allowed:example.org", event_id="$plaintext")

    assert await adapter._required_e2ee_event_allowed("!allowed:example.org", event) is False


@pytest.mark.asyncio
async def test_required_mode_records_only_successfully_decrypted_event_provenance(monkeypatch):
    """A raw encrypted event becomes eligible only after exact decrypt success."""
    adapter = _adapter(monkeypatch)
    decrypted = SimpleNamespace(
        room_id="!allowed:example.org",
        event_id="$encrypted",
        type="m.room.message",
    )
    client = _FakeClient(encryption_state={"algorithm": "m.megolm.v1.aes-sha2"})
    client.crypto = SimpleNamespace(decrypt_megolm_event=AsyncMock(return_value=decrypted))
    adapter._client = client
    adapter._on_room_message = AsyncMock()
    encrypted = SimpleNamespace(room_id="!allowed:example.org", event_id="$encrypted")

    await adapter._on_encrypted_event(encrypted)

    adapter._on_room_message.assert_awaited_once_with(decrypted)
    assert await adapter._required_e2ee_event_allowed("!allowed:example.org", decrypted) is True


def test_pending_invites_require_exact_inviter_before_scheduling_join(monkeypatch):
    """Initial-sync invite reconciliation must not bypass the live inviter gate."""
    adapter = _adapter(monkeypatch)
    scheduled = []
    adapter._schedule_invite_join = lambda room_id, **kwargs: scheduled.append((room_id, kwargs))
    sync_data = {
        "rooms": {
            "invite": {
                "!missing:example.org": {"invite_state": {"events": []}},
                "!mallory:example.org": {
                    "invite_state": {
                        "events": [
                            {
                                "type": "m.room.member",
                                "sender": "@mallory:example.org",
                                "content": {"membership": "invite", "is_direct": True},
                            }
                        ]
                    }
                },
                "!alice:example.org": {
                    "invite_state": {
                        "events": [
                            {
                                "type": "m.room.member",
                                "sender": "@alice:example.org",
                                "content": {"membership": "invite", "is_direct": True},
                            }
                        ]
                    }
                },
            }
        }
    }

    adapter._schedule_pending_invite_joins(sync_data)

    assert scheduled == [
        ("!alice:example.org", {"is_direct": True, "inviter": "@alice:example.org"})
    ]


def test_decrypted_media_cache_is_private_despite_process_umask(monkeypatch, tmp_path):
    """Cache parents and files use explicit private modes, not inherited umask."""
    from gateway.platforms import base

    cache_dir = tmp_path / "images"
    monkeypatch.setattr(base, "IMAGE_CACHE_DIR", cache_dir)
    old_umask = os.umask(0o022)
    try:
        path = base.cache_image_from_bytes(b"\x89PNG\r\n\x1a\nsynthetic", ext=".png")
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_decrypted_document_and_video_cache_are_private_despite_process_umask(monkeypatch, tmp_path):
    """Matrix document/video cache paths must use the owner-only shared helpers."""
    from gateway.platforms import base

    document_dir = tmp_path / "documents"
    video_dir = tmp_path / "videos"
    monkeypatch.setattr(base, "DOCUMENT_CACHE_DIR", document_dir)
    monkeypatch.setattr(base, "VIDEO_CACHE_DIR", video_dir)
    old_umask = os.umask(0o022)
    try:
        document = base.cache_document_from_bytes(b"synthetic document", "probe.txt")
        video = base.cache_video_from_bytes(b"synthetic video", ext=".mp4")
    finally:
        os.umask(old_umask)

    for cache_dir, cached_file in ((document_dir, document), (video_dir, video)):
        assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(os.stat(cached_file).st_mode) == 0o600


@pytest.mark.asyncio
async def test_required_live_invite_does_not_join_before_encryption_state_is_available(monkeypatch):
    """Live invites share the required-room gate used by pending reconciliation."""
    adapter = _adapter(monkeypatch)
    adapter._client = _FakeClient(encryption_state=None)
    adapter._schedule_invite_join = MagicMock()

    await adapter._on_invite(
        SimpleNamespace(
            room_id="!allowed:example.org",
            sender="@alice:example.org",
            content=SimpleNamespace(is_direct=True),
        )
    )

    adapter._schedule_invite_join.assert_not_called()


def test_required_storage_preflight_rejects_existing_weak_crypto_db_without_repair(monkeypatch, tmp_path):
    """An operator must repair pre-existing ACLs; startup never chmods them."""
    from plugins.platforms.matrix import adapter as matrix_mod

    store = tmp_path / "platforms" / "matrix" / "store"
    store.mkdir(parents=True, mode=0o700)
    for parent in (store.parent.parent, store.parent, store):
        os.chmod(parent, 0o700)
    crypto_db = store / "crypto.db"
    crypto_db.touch(mode=0o644)
    os.chmod(crypto_db, 0o644)
    monkeypatch.setattr(matrix_mod, "_STORE_DIR", store)
    monkeypatch.setattr(matrix_mod, "_CRYPTO_DB_PATH", crypto_db)

    assert matrix_mod._matrix_private_storage_preflight() is False
    assert stat.S_IMODE(crypto_db.stat().st_mode) == 0o644


def test_gateway_systemd_units_set_private_umask():
    """The generated user systemd service sets the deployment umask."""
    from hermes_cli.gateway import generate_systemd_unit

    assert "UMask=0077" in generate_systemd_unit(system=False)
