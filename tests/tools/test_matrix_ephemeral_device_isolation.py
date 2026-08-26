"""Regression tests for the ephemeral Matrix sender's device isolation (#95253).

The standalone fallback in ``_send_matrix_via_adapter`` constructs a
throwaway ``MatrixAdapter`` and logs in with password auth. When it
resolved the SAME device id as the live gateway adapter
(``MATRIX_DEVICE_ID`` / ``extra.device_id``), matrix.org's
one-token-per-device policy revoked the live session on the ephemeral
login — the live adapter then failed every sync with ``M_UNKNOWN_TOKEN``
until a gateway restart. The fix drops the configured device id on the
ephemeral instance (only when it would have to password-login), so the
homeserver issues a fresh device for the throwaway connection and the
persistent adapter's stable E2EE identity is untouched.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


class _StubEphemeralAdapter:
    """Captures the device-id state the fix must adjust."""

    instances = []

    def __init__(self, pconfig):
        self.pconfig = pconfig
        self._access_token = getattr(pconfig, "token", "") or ""
        # Mirror the real constructor: fixed device id from config/env.
        self._device_id = (
            (pconfig.extra or {}).get("device_id", "") or "FIXEDDEVICE"
        )
        self.connected = False
        self.disconnected = False
        _StubEphemeralAdapter.instances.append(self)

    async def connect(self):
        self.connected = True
        return True

    async def disconnect(self):
        self.disconnected = True

    async def send(self, chat_id, message, metadata=None):
        return SimpleNamespace(
            success=True, error=None, message_id="$stub-event"
        )


def _pconfig(token=""):
    return SimpleNamespace(
        enabled=True,
        token=token,
        extra={"homeserver": "https://matrix.example.com", "device_id": "LIVEDEVICE"},
    )


async def _run(pconfig):
    from tools.send_message_tool import _send_matrix_via_adapter

    _StubEphemeralAdapter.instances = []
    with patch(
        "gateway.run._gateway_runner_ref",
        side_effect=ImportError("no gateway in standalone context"),
    ), patch(
        "plugins.platforms.matrix.adapter.MatrixAdapter", _StubEphemeralAdapter
    ):
        return await _send_matrix_via_adapter(
            pconfig, "!room:example.com", "hello"
        )


class TestEphemeralDeviceIsolation:
    @pytest.mark.asyncio
    async def test_password_login_ephemeral_drops_configured_device(self):
        """The throwaway adapter must not login on the live gateway's
        device — a shared device id revokes the live session on
        one-token-per-device homeservers (#95253)."""
        result = await _run(_pconfig(token=""))
        assert result.get("success") is True
        (instance,) = _StubEphemeralAdapter.instances
        assert instance._device_id == "", (
            "the ephemeral password-login path must let the homeserver issue "
            "a fresh device instead of reusing the live adapter's"
        )
        assert instance.disconnected is True

    @pytest.mark.asyncio
    async def test_access_token_ephemeral_keeps_device_resolution(self):
        """Token-authenticated sends never login, so the device id is
        resolved from the token itself (whoami) and must not be blanked."""
        result = await _run(_pconfig(token="syt_tok"))
        assert result.get("success") is True
        (instance,) = _StubEphemeralAdapter.instances
        assert instance._device_id == "LIVEDEVICE", (
            "access-token ephemeral sends keep the configured device id — "
            "no login happens, so no revocation is possible"
        )
