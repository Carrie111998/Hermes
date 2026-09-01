"""Tests for the SimpleX Chat platform-plugin adapter.

Loaded via the ``_plugin_adapter_loader`` helper so this lives under
``plugin_adapter_simplex`` in ``sys.modules`` and cannot collide with
sibling platform-plugin tests on the same xdist worker.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_simplex = load_plugin_adapter("simplex")

SimplexAdapter = _simplex.SimplexAdapter
check_requirements = _simplex.check_requirements
validate_config = _simplex.validate_config
is_connected = _simplex.is_connected
register = _simplex.register
_env_enablement = _simplex._env_enablement
_standalone_send = _simplex._standalone_send
_guess_extension = _simplex._guess_extension
_is_image_ext = _simplex._is_image_ext
_is_audio_ext = _simplex._is_audio_ext
_CORR_PREFIX = _simplex._CORR_PREFIX


# ---------------------------------------------------------------------------
# 1. Platform enum (plugin-discovered, not bundled)
# ---------------------------------------------------------------------------

def test_platform_enum_resolves_via_plugin_scan():
    """The plugin filesystem scan should expose Platform("simplex")."""
    from gateway.config import Platform
    p = Platform("simplex")
    assert p.value == "simplex"
    # Identity stability — repeated lookups return the same pseudo-member
    assert Platform("simplex") is p


# ---------------------------------------------------------------------------
# 2. check_requirements / validate_config / is_connected
# ---------------------------------------------------------------------------


def test_check_requirements_true_when_configured(monkeypatch):
    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://127.0.0.1:5225")
    # websockets is a dev dep in this repo via the test plugins; the
    # check_requirements() gate also asserts the package imports.
    websockets_present = True
    try:
        import websockets  # noqa: F401
    except ImportError:
        websockets_present = False
    assert check_requirements() is websockets_present


def test_validate_config_uses_env_or_extra():
    from gateway.config import PlatformConfig
    # Empty extra + no env → invalid
    cfg = PlatformConfig(enabled=True)
    assert validate_config(cfg) is False
    # extra-only path → valid
    cfg2 = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    assert validate_config(cfg2) is True


def test_is_connected_mirrors_validate(monkeypatch):
    from gateway.config import PlatformConfig
    monkeypatch.delenv("SIMPLEX_WS_URL", raising=False)
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://x"})
    assert is_connected(cfg) is True
    assert is_connected(PlatformConfig(enabled=True)) is False


# ---------------------------------------------------------------------------
# 3. _env_enablement seeds PlatformConfig.extra
# ---------------------------------------------------------------------------


def test_env_enablement_seeds_home_channel(monkeypatch):
    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://127.0.0.1:5225")
    monkeypatch.setenv("SIMPLEX_HOME_CHANNEL", "42")
    monkeypatch.setenv("SIMPLEX_HOME_CHANNEL_NAME", "Personal")
    seed = _env_enablement()
    assert seed["home_channel"] == {"chat_id": "42", "name": "Personal"}


# ---------------------------------------------------------------------------
# 4. Adapter init
# ---------------------------------------------------------------------------

def test_adapter_init_custom_url():
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)
    assert adapter.ws_url == "ws://localhost:5225"
    assert adapter._running is False
    assert adapter._ws is None


# ---------------------------------------------------------------------------
# 5. Helper functions (magic-byte detection)
# ---------------------------------------------------------------------------

def test_guess_extension_png():
    assert _guess_extension(b"\x89PNG\r\n\x1a\n") == ".png"


# ---------------------------------------------------------------------------
# 6. Correlation IDs
# ---------------------------------------------------------------------------


def test_corr_id_pending_set_self_trims():
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)
    adapter._max_pending_corr = 4
    for _ in range(10):
        adapter._make_corr_id()
    # After many additions, the pending set should be bounded by the trim
    # logic — at most one trim window above the cap.
    assert len(adapter._pending_corr_ids) <= adapter._max_pending_corr + 1


# ---------------------------------------------------------------------------
# 7. Outbound send (mocked WS)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_dm():
    """DMs use the structured ``/_send @<id> json [...]`` form.

    The bare ``@<id> text`` chat-command form is unreliable — the
    daemon silently drops messages when it cannot resolve the display
    name.  The structured ``/_send`` form addresses by ID and
    survives newlines/quoting through ``json.dumps``, matching what
    ``send_image`` and ``send_document`` already do.
    """
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)

    mock_ws = AsyncMock()
    adapter._ws = mock_ws

    result = await adapter.send("contact-42", "Hello, SimpleX!")
    mock_ws.send.assert_called_once()
    payload = json.loads(mock_ws.send.call_args[0][0])
    assert payload["cmd"].startswith("/_send @contact-42 json ")
    msg_content = json.loads(payload["cmd"].split(" json ", 1)[1])[0][
        "msgContent"
    ]
    assert msg_content == {"type": "text", "text": "Hello, SimpleX!"}
    assert payload["corrId"].startswith(_CORR_PREFIX)
    assert result.success is True



@pytest.mark.asyncio
async def test_send_group():
    """Groups use the structured ``/_send #<id> json [...]`` form.

    The bracket chat-command form ``#[<id>] text`` *looks* like an exact
    ID match in the daemon docs but is parsed as a display-name lookup
    — so messages to groups whose display name isn't literally the ID
    silently drop. The structured ``/_send`` form addresses by numeric
    ID and survives newlines/quoting through ``json.dumps``.
    """
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)

    mock_ws = AsyncMock()
    adapter._ws = mock_ws

    result = await adapter.send("group:grp-99", "Hello, group!")
    payload = json.loads(mock_ws.send.call_args[0][0])
    assert payload["cmd"].startswith("/_send #grp-99 json ")
    msg_content = json.loads(payload["cmd"].split(" json ", 1)[1])[0][
        "msgContent"
    ]
    assert msg_content == {"type": "text", "text": "Hello, group!"}
    assert result.success is True


# ---------------------------------------------------------------------------
# 7b. Channel directory enumeration (list_channels)
# ---------------------------------------------------------------------------


def _adapter_with_ws():
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)
    adapter._ws = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_list_channels_contacts_and_groups():
    adapter = _adapter_with_ws()

    async def fake_send_command(command, timeout=30.0):
        if command == "/contacts":
            return {
                "contacts": [
                    {"contactId": 1, "localDisplayName": "alice"},
                    {"contactId": 2, "profile": {"displayName": "bob"}},
                    "garbage",
                ]
            }
        if command == "/groups":
            return {
                "groups": [
                    {"groupId": 7, "localDisplayName": "friends"},
                    # [groupInfo, groupSummary] pair form
                    [{"groupId": 9, "groupProfile": {"displayName": "work"}}, {}],
                ]
            }
        return None

    adapter._send_command = fake_send_command
    channels = await adapter.list_channels()

    assert {"id": "alice", "name": "alice", "type": "dm"} in channels
    assert {"id": "bob", "name": "bob", "type": "dm"} in channels
    assert {"id": "group:7", "name": "friends", "type": "group"} in channels
    assert {"id": "group:9", "name": "work", "type": "group"} in channels


@pytest.mark.asyncio
async def test_list_channels_returns_none_when_disconnected():
    """None (not []) so the directory falls back to session discovery."""
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)
    assert adapter._ws is None
    assert await adapter.list_channels() is None


@pytest.mark.asyncio
async def test_list_channels_returns_none_on_contacts_timeout():
    adapter = _adapter_with_ws()

    async def fake_send_command(command, timeout=30.0):
        return None  # daemon unresponsive

    adapter._send_command = fake_send_command
    assert await adapter.list_channels() is None


# ---------------------------------------------------------------------------
# 8. Inbound: filter own-echo by corrId prefix
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 9. Standalone (out-of-process) send for cron
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_standalone_send_missing_websockets(monkeypatch):
    """When websockets is unimportable, return a clean error dict.

    Implementation detail: the standalone path does ``import websockets``
    inside the function body. We simulate the package being absent by
    pulling it out of ``sys.modules`` and pointing the finder at None.
    """
    import sys
    saved_websockets = sys.modules.pop("websockets", None)
    saved_meta = list(sys.meta_path)

    class _Blocker:
        @staticmethod
        def find_spec(name, path=None, target=None):
            if name == "websockets" or name.startswith("websockets."):
                raise ImportError("websockets blocked for test")
            return None

    sys.meta_path.insert(0, _Blocker())
    try:
        pconfig = MagicMock()
        pconfig.extra = {"ws_url": "ws://localhost:5225"}
        result = await _standalone_send(pconfig, "contact-42", "hi")
        assert isinstance(result, dict)
        assert "error" in result
        assert "websockets" in result["error"]
    finally:
        sys.meta_path[:] = saved_meta
        if saved_websockets is not None:
            sys.modules["websockets"] = saved_websockets


@pytest.mark.asyncio
async def test_standalone_send_defaults_to_local_daemon(monkeypatch):
    monkeypatch.delenv("SIMPLEX_WS_URL", raising=False)
    pconfig = MagicMock()
    pconfig.extra = {}

    sent_payloads = []

    class DummyWs:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def send(self, payload):
            sent_payloads.append(json.loads(payload))

    def fake_connect(url, **kwargs):
        assert url == "ws://127.0.0.1:5225"
        assert kwargs["open_timeout"] == 10
        assert kwargs["close_timeout"] == 5
        return DummyWs()

    import websockets
    monkeypatch.setattr(websockets, "connect", fake_connect)

    result = await _standalone_send(pconfig, "contact-42", "hi")
    assert result == {"success": True, "platform": "simplex", "chat_id": "contact-42"}
    assert sent_payloads[0]["cmd"].startswith("/_send @contact-42 json ")
    msg_content = json.loads(
        sent_payloads[0]["cmd"].split(" json ", 1)[1]
    )[0]["msgContent"]
    assert msg_content == {"type": "text", "text": "hi"}


@pytest.mark.asyncio
async def test_health_monitor_does_not_reconnect_quiet_healthy_ws(monkeypatch):
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://localhost:5225"})
    adapter = SimplexAdapter(cfg)
    adapter._running = True
    adapter._last_ws_activity = 0
    adapter._ws = AsyncMock()

    monkeypatch.setattr(_simplex, "HEALTH_CHECK_INTERVAL", 0.01)
    monkeypatch.setattr(_simplex, "HEALTH_CHECK_STALE_THRESHOLD", 0.01)

    task = asyncio.create_task(adapter._health_monitor())
    await asyncio.sleep(0.03)
    adapter._running = False
    await asyncio.wait_for(task, timeout=1)

    adapter._ws.close.assert_not_called()




# ---------------------------------------------------------------------------
# 10. register() — plugin-side metadata
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Inbound attachment message type classification
# ---------------------------------------------------------------------------

def _make_file_chat_item(file_path: str, file_name: str) -> dict:
    """Minimal direct-chat rcvMsgContent item carrying a completed file."""
    return {
        "chatInfo": {
            "type": "direct",
            "contact": {"contactId": 42, "localDisplayName": "tester"},
        },
        "chatItem": {
            "chatDir": {"type": "directRcv"},
            "meta": {"itemTs": "2026-01-01T00:00:00Z"},
            "content": {
                "type": "rcvMsgContent",
                "msgContent": {"type": "file", "text": "here you go"},
            },
            "file": {
                "fileId": 7,
                "fileName": file_name,
                "fileSource": {"filePath": file_path},
            },
        },
    }


# ---------------------------------------------------------------------------
# 11. Multiplex secondary-profile scope
# ---------------------------------------------------------------------------
#
# ``__init__``'s auto-accept/group-allowlist reads, the registry gates
# (``check_requirements``/``validate_config``/``is_connected``),
# ``_env_enablement``, and ``_standalone_send`` all previously read raw
# ``SIMPLEX_*`` env vars unconditionally. Under a multiplexed secondary
# profile, ``os.environ`` holds the DEFAULT profile's YAML-to-env bridge
# output — a secondary profile with its own (different, or absent) SimpleX
# config would silently borrow the default profile's daemon URL, group
# allowlist, or auto-accept setting. Mirrors the Buzz fix for #98738.

_SIMPLEX_ENV_VARS = (
    "SIMPLEX_WS_URL",
    "SIMPLEX_AUTO_ACCEPT",
    "SIMPLEX_GROUP_ALLOWED",
    "SIMPLEX_HOME_CHANNEL",
    "SIMPLEX_HOME_CHANNEL_NAME",
    "SIMPLEX_ALLOWED_USERS",
    "SIMPLEX_ALLOW_ALL_USERS",
)


@pytest.fixture(autouse=True)
def _clean_simplex_env(monkeypatch):
    """Keep the new multiplex tests hermetic regardless of ambient env."""
    for var in _SIMPLEX_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def multiplex_scope():
    """Install multiplex + a secondary-profile secret scope; restore after."""
    tokens = []

    def install(scope=None):
        from agent.secret_scope import set_multiplex_active, set_secret_scope

        set_multiplex_active(True)
        tokens.append(set_secret_scope(scope or {}))
        return tokens[-1]

    yield install

    from agent.secret_scope import reset_secret_scope, set_multiplex_active

    for token in reversed(tokens):
        reset_secret_scope(token)
    set_multiplex_active(False)


@pytest.fixture
def default_profile_env(monkeypatch):
    """The default profile's YAML-to-env bridge output in os.environ."""
    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://default-daemon:5225")
    monkeypatch.setenv("SIMPLEX_AUTO_ACCEPT", "false")
    monkeypatch.setenv("SIMPLEX_GROUP_ALLOWED", "*")
    monkeypatch.setenv("SIMPLEX_HOME_CHANNEL", "default-contact")


class TestMultiplexProfileScope:

    def test_secondary_extra_wins_over_default_profile_env(
        self, multiplex_scope, default_profile_env
    ):
        """The secondary profile's own config is authoritative, not the
        default profile's wildcard group-allow / disabled auto-accept."""
        from gateway.config import PlatformConfig

        multiplex_scope()
        cfg = PlatformConfig(
            enabled=True,
            extra={
                "ws_url": "ws://profile-daemon:5225",
                "auto_accept": True,
                "group_allowed": "grp-secondary-only",
            },
        )
        adapter = SimplexAdapter(cfg)
        assert adapter.auto_accept is True
        assert adapter.group_allow_from == {"grp-secondary-only"}

    def test_secondary_missing_keys_fail_closed(
        self, multiplex_scope, default_profile_env
    ):
        """Keys absent from the profile's config must NOT borrow the default
        profile's bridged env values — that would silently disable
        auto-accept or widen the group allowlist to the default's wildcard."""
        from gateway.config import PlatformConfig

        multiplex_scope()
        adapter = SimplexAdapter(PlatformConfig(enabled=True, extra={}))
        # Default profile's env has AUTO_ACCEPT=false and GROUP_ALLOWED=*;
        # an unconfigured secondary profile must fail closed to the
        # documented safe defaults instead, not inherit either value.
        assert adapter.auto_accept is True
        assert adapter.group_allow_from == set()

    def test_default_profile_unscoped_keeps_env_precedence(
        self, monkeypatch, default_profile_env
    ):
        """Multiplex ON but no scope (the DEFAULT profile constructs
        unscoped): env is its own bridge output and still wins."""
        from agent.secret_scope import set_multiplex_active
        from gateway.config import PlatformConfig

        set_multiplex_active(True)
        try:
            adapter = SimplexAdapter(
                PlatformConfig(enabled=True, extra={"auto_accept": True})
            )
        finally:
            set_multiplex_active(False)
        assert adapter.auto_accept is False
        assert adapter.group_allow_from == {"*"}

    def test_check_requirements_scoped_reads_profile_config(
        self, multiplex_scope, default_profile_env, tmp_path
    ):
        """The gate must consult the profile's own config.yaml, not the
        default profile's bridged SIMPLEX_WS_URL."""
        import yaml
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "gateway": {
                        "platforms": {
                            "simplex": {
                                "enabled": True,
                                "extra": {"ws_url": "ws://profile-daemon:5225"},
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        multiplex_scope()
        token = set_hermes_home_override(str(tmp_path))
        try:
            # The default profile's env WS_URL must NOT pass the gate on
            # its own for a profile whose config.yaml has its own entry...
            assert check_requirements() is True
        finally:
            reset_hermes_home_override(token)

        # ...and a profile whose config.yaml has no simplex entry fails
        # closed even though the default profile's env value is present.
        empty_home = tmp_path / "empty-profile"
        empty_home.mkdir()
        multiplex_scope()
        token = set_hermes_home_override(str(empty_home))
        try:
            assert check_requirements() is False
        finally:
            reset_hermes_home_override(token)

    def test_validate_config_and_is_connected_scoped(
        self, multiplex_scope, default_profile_env
    ):
        from gateway.config import PlatformConfig

        multiplex_scope()
        # Secondary profile's own extra is authoritative...
        cfg = PlatformConfig(enabled=True, extra={"ws_url": "ws://profile-daemon:5225"})
        assert validate_config(cfg) is True
        assert is_connected(cfg) is True
        # ...and an unconfigured secondary profile fails closed even though
        # the default profile's env WS_URL is present.
        empty_cfg = PlatformConfig(enabled=True, extra={})
        assert validate_config(empty_cfg) is False
        assert is_connected(empty_cfg) is False

    def test_env_enablement_returns_none_when_profile_scoped(
        self, multiplex_scope, default_profile_env
    ):
        """Env enablement must not fabricate a SimpleX platform for a
        secondary profile from the default profile's bridged env."""
        multiplex_scope()
        assert _env_enablement() is None

    @pytest.mark.asyncio
    async def test_standalone_send_uses_scoped_ws_url(
        self, default_profile_env, monkeypatch
    ):
        """Cron/out-of-process delivery must connect to the secondary
        profile's own daemon, not the default profile's bridged env URL.

        Scope is installed/reset inline (not via the ``multiplex_scope``
        fixture) so the ``ContextVar`` set/reset pair stays inside the same
        asyncio task context as this coroutine — resetting a token from the
        surrounding sync fixture-teardown context raises ``ValueError:
        token was created in a different Context``.
        """
        from agent.secret_scope import (
            reset_secret_scope,
            set_multiplex_active,
            set_secret_scope,
        )

        set_multiplex_active(True)
        token = set_secret_scope({})
        try:
            pconfig = MagicMock()
            pconfig.extra = {"ws_url": "ws://profile-daemon:5225"}

            class DummyWs:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return None

                async def send(self, payload):
                    pass

            connected_urls = []

            def fake_connect(url, **kwargs):
                connected_urls.append(url)
                return DummyWs()

            import websockets

            monkeypatch.setattr(websockets, "connect", fake_connect)
            result = await _standalone_send(pconfig, "contact-42", "hi")
        finally:
            reset_secret_scope(token)
            set_multiplex_active(False)

        assert result["success"] is True
        assert connected_urls == ["ws://profile-daemon:5225"]


