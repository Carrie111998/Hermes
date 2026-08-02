"""QQ group @-message end-to-end authorization tests.

Drives the full inbound chain for a raw ``GROUP_AT_MESSAGE_CREATE`` dispatch:

    QQAdapter._dispatch_payload → _on_message → _handle_group_message
    → MessageEvent → GatewayRunner._is_user_authorized (the gateway authz gate)

and asserts the combined adapter + authz outcome for each configuration.

Final design semantics being pinned:

* ``group_policy=allowlist`` + ``group_allow_from`` (from config.yaml /
  PlatformConfig.extra, or the ``QQ_GROUP_ALLOWED_USERS`` env var) is the ONLY
  configuration path that authorizes QQ group traffic.
* An allowed group with NO member allowlist admits every member who really
  @-ed the bot; a configured member allowlist (``group_member_allow_from``)
  gates members.
* ``group_policy=open`` is NOT an authorization path for QQ groups — with or
  without a configured group allowlist, open never authorizes group traffic at
  the authz layer (open is "forwarded, not authorized"; only the allowlist
  policy is trusted).  This guarantees open can never blanket-open every group.
* Group ``member_openid`` values are never compared against the C2C
  ``QQ_ALLOWED_USERS`` allowlist (different identity namespace).
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig

GROUP_ID = "47A1B2C3D4E5F60718293A4B5C6D7E8F"
OTHER_GROUP_ID = "58B2C3D4E5F60718293A4B5C6D7E8F0A"
MEMBER_ID = "6C93D4E5F60718293A4B5C6D7E8F0A1B"
OTHER_MEMBER_ID = "7DA4E5F60718293A4B5C6D7E8F0A1B2C"
C2C_OPENID = "C2C_OPENID_0000000000000000000000000000000C"
MSG_ID = "ROBOT1.0_MSG_0000000000000000000000000000000000000000"


def _make_adapter(**extra):
    from gateway.platforms.qqbot import QQAdapter
    return QQAdapter(PlatformConfig(enabled=True, extra=dict(extra)))


def _make_runner(adapter):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.QQBOT: adapter}
    runner.pairing_store = None
    runner.pairing_stores = {}
    runner.config = None
    return runner


def _group_payload(group_id=GROUP_ID, member_id=MEMBER_ID):
    return {
        "id": MSG_ID,
        "content": "@bot hello",
        "group_openid": group_id,
        "author": {"member_openid": member_id},
        "timestamp": "2026-08-01T10:00:00+08:00",
    }


async def _dispatch_group_at(adapter, payload, monkeypatch):
    """Dispatch a raw GROUP_AT_MESSAGE_CREATE payload and capture the event.

    Returns the MessageEvent created by the adapter, or ``None`` when the
    adapter's group policy denied the message before event construction.
    """
    captured = {}

    async def _fake_handle(event):
        captured["event"] = event

    adapter.handle_message = _fake_handle

    created = []
    orig_create_task = asyncio.create_task

    def _capture_task(coro):
        task = orig_create_task(coro)
        created.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", _capture_task)
    adapter._dispatch_payload({"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": payload})
    if created:
        await asyncio.gather(*created)
    return captured.get("event")


def _clean_qq_env(monkeypatch):
    for var in (
        "QQ_ALLOWED_USERS",
        "QQ_GROUP_ALLOWED_USERS",
        "QQ_ALLOW_ALL_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# 1. allowlist via config.yaml / PlatformConfig.extra only (no group env var)
# ---------------------------------------------------------------------------

class TestAllowlistFromConfigExtra:
    @pytest.mark.asyncio
    async def test_allowed_group_enters_agent(self, monkeypatch):
        _clean_qq_env(monkeypatch)
        # QQ_ALLOWED_USERS (C2C) is set in production; it must not interfere.
        monkeypatch.setenv("QQ_ALLOWED_USERS", C2C_OPENID)
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist",
            group_allow_from=GROUP_ID,
        )
        runner = _make_runner(adapter)

        event = await _dispatch_group_at(adapter, _group_payload(), monkeypatch)

        assert event is not None
        assert event.source.chat_type == "group"
        assert event.source.chat_id == GROUP_ID
        assert event.source.user_id == MEMBER_ID
        assert runner._is_user_authorized(event.source) is True

    @pytest.mark.asyncio
    async def test_list_via_config_extra_member_openid_never_needs_c2c_list(
        self, monkeypatch
    ):
        _clean_qq_env(monkeypatch)
        monkeypatch.setenv("QQ_ALLOWED_USERS", C2C_OPENID)
        # Member allowlist is independent of the C2C allowlist: the member is
        # NOT in QQ_ALLOWED_USERS yet must still be authorized.
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist",
            group_allow_from=GROUP_ID,
        )
        runner = _make_runner(adapter)

        event = await _dispatch_group_at(adapter, _group_payload(), monkeypatch)

        assert event is not None
        assert runner._is_user_authorized(event.source) is True


# ---------------------------------------------------------------------------
# 2. allowlist via QQ_GROUP_ALLOWED_USERS env var
# ---------------------------------------------------------------------------

def _make_adapter_from_env(monkeypatch, env):
    """Build the QQ adapter exactly as the production gateway does.

    Sets the given env vars, then runs the real env→config bridging
    (``gateway.config._apply_env_overrides``) so the adapter sees the same
    ``extra`` that production config loading produces (env vars are bridged
    into ``extra.group_allow_from`` etc.).

    ``group_policy`` has no env bridge for QQ — it comes from config.yaml
    (``platforms.qqbot.extra.group_policy``), so the helper applies
    ``allowlist`` on the bridged PlatformConfig, mirroring the documented
    production combination: config.yaml policy + env allowlist.
    """
    from gateway.config import GatewayConfig, _apply_env_overrides
    from gateway.platforms.qqbot import QQAdapter

    _clean_qq_env(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config = GatewayConfig()
    _apply_env_overrides(config)
    platform_cfg = config.platforms[Platform.QQBOT]
    platform_cfg.extra["group_policy"] = "allowlist"
    return QQAdapter(platform_cfg)


class TestAllowlistFromEnv:
    @pytest.mark.asyncio
    async def test_env_group_allowlist_authorizes_group(self, monkeypatch):
        adapter = _make_adapter_from_env(
            monkeypatch,
            {
                "QQ_APP_ID": "a",
                "QQ_CLIENT_SECRET": "b",
                "QQ_GROUP_ALLOWED_USERS": GROUP_ID,
            },
        )
        runner = _make_runner(adapter)

        event = await _dispatch_group_at(adapter, _group_payload(), monkeypatch)

        assert event is not None
        assert runner._is_user_authorized(event.source) is True


# ---------------------------------------------------------------------------
# 3. Group not in the allowlist → denied at the adapter with masked log
# ---------------------------------------------------------------------------

class TestNonAllowedGroupDenied:
    @pytest.mark.asyncio
    async def test_unknown_group_denied_before_authz(self, monkeypatch, caplog):
        _clean_qq_env(monkeypatch)
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist",
            group_allow_from=GROUP_ID,
        )
        runner = _make_runner(adapter)

        event = await _dispatch_group_at(
            adapter, _group_payload(group_id=OTHER_GROUP_ID), monkeypatch
        )

        assert event is None  # no MessageEvent → authz never consulted
        log_text = "\n".join(rec.message for rec in caplog.records)
        assert "QQ group message denied" in log_text
        assert "reason=group_not_allowed" in log_text
        assert OTHER_GROUP_ID not in log_text
        assert MEMBER_ID not in log_text


# ---------------------------------------------------------------------------
# 4. open + no group allowlist → MUST be denied (never blanket-open)
# ---------------------------------------------------------------------------

class TestOpenWithoutAllowlistDenied:
    @pytest.mark.asyncio
    async def test_open_without_any_allowlist_denied_at_authz(self, monkeypatch):
        _clean_qq_env(monkeypatch)
        adapter = _make_adapter(app_id="a", client_secret="b", group_policy="open")
        runner = _make_runner(adapter)

        event = await _dispatch_group_at(adapter, _group_payload(), monkeypatch)

        # The adapter forwards (open = no adapter-level restriction), but the
        # gateway authz layer must deny: open is not an authorization path.
        assert event is not None
        assert runner._is_user_authorized(event.source) is False

    @pytest.mark.asyncio
    async def test_open_denied_even_with_c2c_allowlist_set(self, monkeypatch):
        _clean_qq_env(monkeypatch)
        monkeypatch.setenv("QQ_ALLOWED_USERS", MEMBER_ID)
        adapter = _make_adapter(app_id="a", client_secret="b", group_policy="open")
        runner = _make_runner(adapter)

        event = await _dispatch_group_at(adapter, _group_payload(), monkeypatch)

        # Even if the member id sits in the C2C allowlist, group traffic under
        # open must not be authorized by it.
        assert event is not None
        assert runner._is_user_authorized(event.source) is False


# ---------------------------------------------------------------------------
# 5. open + explicit group allowlist → still denied (open is not an
#    authorization path for QQ groups; only the allowlist policy is trusted)
# ---------------------------------------------------------------------------

class TestOpenWithAllowlistDenied:
    @pytest.mark.asyncio
    async def test_open_with_group_allowlist_is_not_authorized(self, monkeypatch):
        _clean_qq_env(monkeypatch)
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="open",
            group_allow_from=GROUP_ID,
        )
        runner = _make_runner(adapter)

        event = await _dispatch_group_at(adapter, _group_payload(), monkeypatch)

        assert event is not None
        assert runner._is_user_authorized(event.source) is False


# ---------------------------------------------------------------------------
# 6. Allowed group, no member allowlist → any real-@ member enters
# ---------------------------------------------------------------------------

class TestAllowedGroupAnyMember:
    @pytest.mark.asyncio
    async def test_unlisted_member_still_authorized(self, monkeypatch):
        _clean_qq_env(monkeypatch)
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist",
            group_allow_from=GROUP_ID,
        )
        runner = _make_runner(adapter)

        event = await _dispatch_group_at(
            adapter, _group_payload(member_id=OTHER_MEMBER_ID), monkeypatch
        )

        assert event is not None
        assert event.source.user_id == OTHER_MEMBER_ID
        assert runner._is_user_authorized(event.source) is True


# ---------------------------------------------------------------------------
# 7. Allowed group + member allowlist → only matching member_openid
# ---------------------------------------------------------------------------

class TestMemberAllowlistGatesMembers:
    @pytest.mark.asyncio
    async def test_listed_member_authorized(self, monkeypatch):
        _clean_qq_env(monkeypatch)
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist",
            group_allow_from=GROUP_ID,
            group_member_allow_from=MEMBER_ID,
        )
        runner = _make_runner(adapter)

        event = await _dispatch_group_at(adapter, _group_payload(), monkeypatch)

        assert event is not None
        assert runner._is_user_authorized(event.source) is True

    @pytest.mark.asyncio
    async def test_unlisted_member_denied_at_adapter(self, monkeypatch, caplog):
        _clean_qq_env(monkeypatch)
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist",
            group_allow_from=GROUP_ID,
            group_member_allow_from=MEMBER_ID,
        )
        runner = _make_runner(adapter)

        event = await _dispatch_group_at(
            adapter, _group_payload(member_id=OTHER_MEMBER_ID), monkeypatch
        )

        assert event is None
        log_text = "\n".join(rec.message for rec in caplog.records)
        assert "reason=member_not_allowed" in log_text
        assert OTHER_MEMBER_ID not in log_text


# ---------------------------------------------------------------------------
# 8. member_openid is never compared against the C2C QQ_ALLOWED_USERS list
# ---------------------------------------------------------------------------

class TestMemberNeverComparedToC2CList:
    @pytest.mark.asyncio
    async def test_c2c_allowlist_does_not_authorize_group_members(self, monkeypatch):
        _clean_qq_env(monkeypatch)
        # The member's openid sits in the C2C allowlist — that must NOT
        # authorize the group message when no group policy is configured.
        monkeypatch.setenv("QQ_ALLOWED_USERS", MEMBER_ID)
        adapter = _make_adapter(app_id="a", client_secret="b")  # pairing default
        runner = _make_runner(adapter)

        event = await _dispatch_group_at(adapter, _group_payload(), monkeypatch)

        # Adapter gate (pairing) denies before the C2C list could ever apply.
        assert event is None

    @pytest.mark.asyncio
    async def test_member_allowlist_is_authoritative_over_c2c_list(self, monkeypatch):
        _clean_qq_env(monkeypatch)
        # Member IS in the C2C allowlist but NOT in the member allowlist —
        # the member allowlist must win and deny.
        monkeypatch.setenv("QQ_ALLOWED_USERS", OTHER_MEMBER_ID)
        adapter = _make_adapter(
            app_id="a", client_secret="b",
            group_policy="allowlist",
            group_allow_from=GROUP_ID,
            group_member_allow_from=MEMBER_ID,
        )
        runner = _make_runner(adapter)

        event = await _dispatch_group_at(
            adapter, _group_payload(member_id=OTHER_MEMBER_ID), monkeypatch
        )

        assert event is None
