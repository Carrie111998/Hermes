from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.config import Platform
from gateway.discord_scoped_auth import (
    channel_user_allowlists,
    is_channel_scoped_user_allowed,
)
from gateway.session import SessionSource
from plugins.platforms.discord.adapter import DiscordAdapter


POLICY_JSON = '{"888000000000000001":["scoped-a","scoped-b"]}'
POLICY_DICT = {"888000000000000001": ["scoped-a", "scoped-b"]}
SNOW = "888000000000000001"
REAL_USER_ID = "123456789012345678"
DISPLAY_NAME = "Alice#1234"


class _Runner(GatewayAuthorizationMixin):
    adapters = {}
    pairing_store = None
    pairing_stores = {}


def _adapter(global_users=(), channel_allowed_users=None):
    adapter = object.__new__(DiscordAdapter)
    adapter._allowed_user_ids = set(global_users)
    adapter._allowed_role_ids = set()
    adapter._client = MagicMock(guilds=[])
    extra = {}
    if channel_allowed_users is not None:
        extra["channel_allowed_users"] = channel_allowed_users
    adapter.config = SimpleNamespace(extra=extra)
    return adapter


def _source(user_id, chat_id, chat_type="group", parent_chat_id=None):
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        parent_chat_id=parent_chat_id,
    )


def _guild():
    return SimpleNamespace(id=1, get_member=lambda _uid: None)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parser_accepts_json_mapping_and_rejects_malformed_values():
    assert channel_user_allowlists(POLICY_JSON) == {
        SNOW: frozenset({"scoped-a", "scoped-b"})
    }
    assert channel_user_allowlists("not-json") == {}
    assert channel_user_allowlists('["not", "a", "mapping"]') == {}
    assert channel_user_allowlists('{"target": 42}') == {}
    assert channel_user_allowlists('{"target": []}') == {}


def test_parser_accepts_native_yaml_mapping():
    assert channel_user_allowlists(POLICY_DICT) == {
        SNOW: frozenset({"scoped-a", "scoped-b"})
    }


def test_parser_cleans_whitespace_and_coerces_scalars():
    assert channel_user_allowlists({"  target  ": ["  scoped-a  ", "", 42]}) == {
        "target": frozenset({"scoped-a", "42"})
    }


# ---------------------------------------------------------------------------
# Predicate matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user_id", "channels", "expected"),
    [
        ("scoped-a", {SNOW}, True),
        ("scoped-a", {"thread", SNOW}, True),
        ("scoped-a", {"elsewhere"}, False),
        ("scoped-a", None, False),
        ("unknown", {SNOW}, False),
        ("", {SNOW}, False),
        (None, {SNOW}, False),
    ],
)
def test_scoped_predicate_matrix(user_id, channels, expected):
    assert is_channel_scoped_user_allowed(user_id, channels, POLICY_JSON) is expected


def test_scoped_predicate_empty_policy():
    assert is_channel_scoped_user_allowed("scoped-a", {"target"}, {}) is False


def test_display_names_never_authorize_numeric_user_id():
    display_only = {"target": [DISPLAY_NAME, "Bob"]}
    assert (
        is_channel_scoped_user_allowed(REAL_USER_ID, {"target"}, display_only)
        is False
    )
    id_policy = {"target": [REAL_USER_ID]}
    assert (
        is_channel_scoped_user_allowed(REAL_USER_ID, {"target"}, id_policy)
        is True
    )


# ---------------------------------------------------------------------------
# Adapter _is_allowed_user
# ---------------------------------------------------------------------------


def test_adapter_enforces_global_owner_plus_scoped_users():
    adapter = _adapter(global_users={"owner"}, channel_allowed_users=POLICY_DICT)
    guild = _guild()

    assert adapter._is_allowed_user(
        "owner", guild=guild, channel_ids={"elsewhere"}
    )
    assert adapter._is_allowed_user("owner", is_dm=True)
    assert adapter._is_allowed_user(
        "scoped-a", guild=guild, channel_ids={"888000000000000001"}
    )
    assert adapter._is_allowed_user(
        "scoped-a", guild=guild, channel_ids={"thread", "888000000000000001"}
    )
    assert not adapter._is_allowed_user(
        "scoped-a", guild=guild, channel_ids={"elsewhere"}
    )
    assert not adapter._is_allowed_user("scoped-a", is_dm=True)
    assert not adapter._is_allowed_user(
        "unknown", guild=guild, channel_ids={"target"}
    )


def test_adapter_scoped_mapping_absent_preserves_global_allowlist():
    adapter = _adapter(global_users={"owner"})
    guild = _guild()
    assert adapter._is_allowed_user(
        "owner", guild=guild, channel_ids={"elsewhere"}
    )


def test_adapter_scoped_mapping_absent_preserves_allowed_channels_bypass(
    monkeypatch,
):
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "999")
    adapter = _adapter()
    guild = _guild()
    assert adapter._is_allowed_user(
        "42",
        guild=guild,
        is_dm=False,
        channel_ids={"999"},
    )


def test_adapter_malformed_scoped_config_denies_scoped_user():
    adapter = _adapter(channel_allowed_users='{"888000000000000001": 42}')
    guild = _guild()
    assert not adapter._is_allowed_user(
        "scoped-a", guild=guild, channel_ids={"888000000000000001"}
    )


def test_adapter_display_name_in_config_does_not_authorize_user_id():
    adapter = _adapter(
        channel_allowed_users={"888000000000000001": [DISPLAY_NAME]},
    )
    guild = _guild()
    assert not adapter._is_allowed_user(
        REAL_USER_ID, guild=guild, channel_ids={"888000000000000001"}
    )


def test_per_profile_scoped_mapping_isolation():
    adapter_a = _adapter(channel_allowed_users={"777000000000000001": ["user-a"]})
    adapter_b = _adapter(channel_allowed_users={"777000000000000002": ["user-b"]})
    guild = _guild()

    assert adapter_a._is_allowed_user(
        "user-a", guild=guild, channel_ids={"777000000000000001"}
    )
    assert not adapter_a._is_allowed_user(
        "user-a", guild=guild, channel_ids={"777000000000000002"}
    )
    assert adapter_b._is_allowed_user(
        "user-b", guild=guild, channel_ids={"777000000000000002"}
    )
    assert not adapter_b._is_allowed_user(
        "user-b", guild=guild, channel_ids={"777000000000000001"}
    )


VICTIM_CHANNEL_SNOWFLAKE = "111111111111111111"
CURRENT_CHANNEL_SNOWFLAKE = "999999999999999999"


def test_scoped_grant_rejects_slash_name_collision_with_foreign_snowflake():
    """A channel renamed to another channel's snowflake must not inherit its grant."""
    adapter = _adapter(
        channel_allowed_users={VICTIM_CHANNEL_SNOWFLAKE: ["scoped-a"]},
    )
    guild = _guild()
    slash_style_keys = {
        CURRENT_CHANNEL_SNOWFLAKE,
        VICTIM_CHANNEL_SNOWFLAKE,
        f"#{VICTIM_CHANNEL_SNOWFLAKE}",
    }
    assert not adapter._is_allowed_user(
        "scoped-a",
        guild=guild,
        channel_ids={CURRENT_CHANNEL_SNOWFLAKE},
        channel_keys=slash_style_keys,
    )


def test_scoped_grant_allows_when_real_snowflake_in_mapping():
    adapter = _adapter(
        channel_allowed_users={VICTIM_CHANNEL_SNOWFLAKE: ["scoped-a"]},
    )
    guild = _guild()
    slash_style_keys = {
        VICTIM_CHANNEL_SNOWFLAKE,
        "general",
        "#general",
    }
    assert adapter._is_allowed_user(
        "scoped-a",
        guild=guild,
        channel_ids={VICTIM_CHANNEL_SNOWFLAKE},
        channel_keys=slash_style_keys,
    )


def test_allowed_channels_bypass_still_honors_name_forms(monkeypatch):
    """DISCORD_ALLOWED_CHANNELS name matching is unchanged by scoped filtering."""
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "general")
    adapter = _adapter(
        channel_allowed_users={VICTIM_CHANNEL_SNOWFLAKE: ["scoped-a"]},
    )
    guild = _guild()
    slash_style_keys = {
        CURRENT_CHANNEL_SNOWFLAKE,
        "general",
        "#general",
    }
    assert adapter._is_allowed_user(
        "42",
        guild=guild,
        is_dm=False,
        channel_ids={CURRENT_CHANNEL_SNOWFLAKE},
        channel_keys=slash_style_keys,
    )


# ---------------------------------------------------------------------------
# Gateway second layer
# ---------------------------------------------------------------------------


def _runner_with_adapter(adapter):
    runner = _Runner()
    runner.adapters = {Platform.DISCORD: adapter}
    return runner


def test_gateway_second_layer_enforces_same_matrix(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "owner")
    monkeypatch.delenv("DISCORD_ALLOWED_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_ALLOWED_ROLES", raising=False)
    adapter = _adapter(channel_allowed_users=POLICY_DICT)
    runner = _runner_with_adapter(adapter)

    assert runner._is_user_authorized(_source("owner", "elsewhere"))
    assert runner._is_user_authorized(_source("owner", "dm", "dm"))
    assert runner._is_user_authorized(_source("scoped-a", SNOW))
    assert runner._is_user_authorized(
        _source("scoped-a", "thread", "thread", parent_chat_id=SNOW)
    )
    assert not runner._is_user_authorized(_source("scoped-a", "elsewhere"))
    assert not runner._is_user_authorized(_source("scoped-a", "dm", "dm"))
    assert not runner._is_user_authorized(_source("unknown", SNOW))
    assert not runner._is_user_authorized(
        _source("scoped-a", "thread", "thread", parent_chat_id=None)
    )


def test_gateway_no_scoped_mapping_preserves_env_allowlist(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "owner")
    monkeypatch.delenv("DISCORD_ALLOWED_CHANNELS", raising=False)
    adapter = _adapter()
    runner = _runner_with_adapter(adapter)

    assert runner._is_user_authorized(_source("owner", "elsewhere"))
    assert not runner._is_user_authorized(_source("stranger", "target"))


def test_gateway_malformed_scoped_config_denies_scoped_user(monkeypatch):
    monkeypatch.delenv("DISCORD_ALLOWED_USERS", raising=False)
    adapter = _adapter(channel_allowed_users='{"888000000000000001": 42}')
    runner = _runner_with_adapter(adapter)

    assert not runner._is_user_authorized(_source("scoped-a", "target"))


def test_gateway_per_profile_adapter_isolation(monkeypatch):
    monkeypatch.delenv("DISCORD_ALLOWED_USERS", raising=False)
    adapter_default = _adapter(channel_allowed_users={"chan-a": ["user-a"]})
    adapter_secondary = _adapter(channel_allowed_users={"chan-b": ["user-b"]})
    runner = _Runner()
    runner.adapters = {Platform.DISCORD: adapter_default}
    runner._profile_adapters = {"coder": {Platform.DISCORD: adapter_secondary}}

    source_default = _source("user-a", "chan-a")
    source_secondary = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chan-b",
        chat_type="group",
        user_id="user-b",
        profile="coder",
    )

    assert runner._is_user_authorized(source_default)
    assert not runner._is_user_authorized(
        SessionSource(
            platform=Platform.DISCORD,
            chat_id="chan-b",
            chat_type="group",
            user_id="user-b",
        )
    )
    assert runner._is_user_authorized(source_secondary)
    assert not runner._is_user_authorized(
        SessionSource(
            platform=Platform.DISCORD,
            chat_id="chan-a",
            chat_type="group",
            user_id="user-a",
            profile="coder",
        )
    )
