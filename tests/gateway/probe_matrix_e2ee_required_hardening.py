"""Dependency-light executable contract for Matrix required E2EE hardening.

TODO(verification-before-merge): this is a supplemental offline probe, not a
replacement for the pytest, ruff, ty, gitleaks, and live-Matrix checks listed
in ``MATRIX_E2EE_REQUIRED_HARDENING_DRAFT.md``.
"""

import asyncio
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gateway.config import PlatformConfig
from gateway.platforms import base
from hermes_cli.gateway import generate_systemd_unit
from plugins.platforms.matrix.adapter import MatrixAdapter


class FakeClient:
    def __init__(self, encryption_state, *, crypto=True):
        self.crypto = object() if crypto else None
        self.get_state_event = AsyncMock(return_value=encryption_state)
        self.send_message_event = AsyncMock(return_value="$event:example.org")
        self.upload_media = AsyncMock(return_value="mxc://example.org/media")
        self.state_store: object = None


def make_adapter(mode="required"):
    os.environ["MATRIX_ALLOWED_USERS"] = "@alice:example.org"
    os.environ["MATRIX_ALLOWED_ROOMS"] = "!allowed:example.org"
    os.environ.pop("GATEWAY_ALLOW_ALL_USERS", None)
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


async def check_required_deny_and_positive_paths():
    denied = make_adapter()
    denied_client = FakeClient(None)
    denied._client = denied_client
    assert not (await denied.send("!allowed:example.org", "synthetic text")).success
    assert not (await denied.edit_message("!allowed:example.org", "$old", "synthetic edit")).success
    assert not (
        await denied._upload_and_send(
            "!allowed:example.org", b"synthetic", "probe.txt", "text/plain", "m.file"
        )
    ).success
    assert await denied._send_reaction("!allowed:example.org", "$old", "✅") is None
    denied_client.send_message_event.assert_not_awaited()
    denied_client.upload_media.assert_not_awaited()

    divergent = make_adapter()
    divergent_client = FakeClient({"algorithm": "m.megolm.v1.aes-sha2"})
    divergent_client.state_store = SimpleNamespace(is_encrypted=AsyncMock(return_value=False))
    divergent._client = divergent_client
    assert not (
        await divergent._upload_and_send(
            "!allowed:example.org", b"synthetic", "probe.txt", "text/plain", "m.file"
        )
    ).success
    divergent_client.upload_media.assert_not_awaited()

    inbound = make_adapter()
    inbound._client = FakeClient({"algorithm": "m.megolm.v1.aes-sha2"}, crypto=False)
    inbound.handle_message = AsyncMock()
    await inbound._on_room_message(
        SimpleNamespace(
            room_id="!allowed:example.org",
            sender="@alice:example.org",
            event_id="$inbound",
            timestamp=10**15,
            content={"msgtype": "m.text", "body": "synthetic"},
        )
    )
    inbound.handle_message.assert_not_awaited()

    positive = make_adapter()
    positive_client = FakeClient({"algorithm": "m.megolm.v1.aes-sha2"})
    positive._client = positive_client
    assert (await positive.send("!allowed:example.org", "encrypted text")).success
    positive_client.send_message_event.assert_awaited_once()

    unsupported = make_adapter()
    unsupported._client = FakeClient({"algorithm": "m.unknown"})
    assert not (await unsupported.send("!allowed:example.org", "wrong algorithm")).success

    optional = make_adapter("optional")
    optional_client = FakeClient(None)
    optional._client = optional_client
    assert (await optional.send("!allowed:example.org", "optional legacy path")).success
    optional_client.send_message_event.assert_awaited_once()

    plain_event = SimpleNamespace(room_id="!allowed:example.org", event_id="$plaintext")
    assert not await positive._required_e2ee_event_allowed("!allowed:example.org", plain_event)
    decrypted = SimpleNamespace(
        room_id="!allowed:example.org",
        event_id="$encrypted",
        type="m.room.message",
    )
    provenance_client = FakeClient({"algorithm": "m.megolm.v1.aes-sha2"})
    provenance_client.crypto = SimpleNamespace(
        decrypt_megolm_event=AsyncMock(return_value=decrypted)
    )
    positive._client = provenance_client
    positive._on_room_message = AsyncMock()
    await positive._on_encrypted_event(
        SimpleNamespace(room_id="!allowed:example.org", event_id="$encrypted")
    )
    positive._on_room_message.assert_awaited_once_with(decrypted)
    assert await positive._required_e2ee_event_allowed("!allowed:example.org", decrypted)


async def check_acl_and_invites():
    adapter = make_adapter()
    adapter._is_dm_room = AsyncMock(return_value=True)
    assert not await adapter._is_allowed_matrix_room_event("!unlisted-dm:example.org")

    scheduled = []
    adapter._schedule_invite_join = lambda room_id, **kwargs: scheduled.append((room_id, kwargs))
    adapter._schedule_pending_invite_joins(
        {
            "rooms": {
                "invite": {
                    "!missing:example.org": {"invite_state": {"events": []}},
                    "!mallory:example.org": {
                        "invite_state": {"events": [{"type": "m.room.member", "sender": "@mallory:example.org", "content": {"membership": "invite", "is_direct": True}}]}
                    },
                    "!alice:example.org": {
                        "invite_state": {"events": [{"type": "m.room.member", "sender": "@alice:example.org", "content": {"membership": "invite", "is_direct": True}}]}
                    },
                }
            }
        }
    )
    assert scheduled == [("!alice:example.org", {"is_direct": True, "inviter": "@alice:example.org"})]


def check_private_cache_and_systemd_contract():
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        old_dir = base.IMAGE_CACHE_DIR
        old_document_dir = base.DOCUMENT_CACHE_DIR
        old_video_dir = base.VIDEO_CACHE_DIR
        old_umask = os.umask(0o022)
        try:
            base.IMAGE_CACHE_DIR = tmp / "images"
            base.DOCUMENT_CACHE_DIR = tmp / "documents"
            base.VIDEO_CACHE_DIR = tmp / "videos"
            output = Path(base.cache_image_from_bytes(b"\x89PNG\r\n\x1a\nsynthetic", ext=".png"))
            document = Path(base.cache_document_from_bytes(b"synthetic", "probe.txt"))
            video = Path(base.cache_video_from_bytes(b"synthetic", ext=".mp4"))
        finally:
            base.IMAGE_CACHE_DIR = old_dir
            base.DOCUMENT_CACHE_DIR = old_document_dir
            base.VIDEO_CACHE_DIR = old_video_dir
            os.umask(old_umask)
        for cache_dir, cached_file in (
            (tmp / "images", output),
            (tmp / "documents", document),
            (tmp / "videos", video),
        ):
            assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700
            assert stat.S_IMODE(cached_file.stat().st_mode) == 0o600
    assert "UMask=0077" in generate_systemd_unit(system=False)


async def main():
    await check_required_deny_and_positive_paths()
    await check_acl_and_invites()
    check_private_cache_and_systemd_contract()
    print("matrix_e2ee_required_hardening=PASS")


if __name__ == "__main__":
    asyncio.run(main())
