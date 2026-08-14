"""Tests for plugins.platforms.discord.command_scope (feature I2).

Covers: guild scope resolution (config wins, default fallback, None),
snowflake validation, integration-types default [0], dedupe, rejection
of non-0/1 values, and is_guild_scoped.
"""

import pytest

from plugins.platforms.discord.command_scope import (
    CommandScope,
    CommandScopeError,
    is_guild_scoped,
    normalize_integration_types,
    resolve_guild_scope,
)


class TestResolveGuildScope:
    def test_config_guild_wins_over_default(self):
        assert (
            resolve_guild_scope("123456789012345678", default_guild="999999999")
            == "123456789012345678"
        )

    def test_default_fallback_when_config_none(self):
        assert (
            resolve_guild_scope(None, default_guild="123456789012345678")
            == "123456789012345678"
        )

    def test_none_when_nothing_provided(self):
        assert resolve_guild_scope(None) is None
        assert resolve_guild_scope(None, default_guild=None) is None

    def test_invalid_config_guild_raises(self):
        for bad in ("abc", "", "-1", "0", "12.5", "123 456", "١٢٣", "²", "+5"):
            with pytest.raises(CommandScopeError):
                resolve_guild_scope(bad)

    def test_invalid_default_guild_raises(self):
        with pytest.raises(CommandScopeError):
            resolve_guild_scope(None, default_guild="not-a-snowflake")

    def test_error_is_value_error_subclass(self):
        with pytest.raises(ValueError):
            resolve_guild_scope("not-a-snowflake")

    def test_snowflake_boundaries(self):
        assert resolve_guild_scope("1") == "1"
        assert resolve_guild_scope("9223372036854775807") == "9223372036854775807"
        with pytest.raises(CommandScopeError):
            resolve_guild_scope("9223372036854775808")  # 2**63, out of range


class TestNormalizeIntegrationTypes:
    def test_defaults_to_guild_install(self):
        assert normalize_integration_types(None) == [0]
        assert normalize_integration_types([]) == [0]

    def test_keeps_valid_values(self):
        assert normalize_integration_types([0]) == [0]
        assert normalize_integration_types([1]) == [1]
        assert normalize_integration_types([0, 1]) == [0, 1]
        assert normalize_integration_types([1, 0]) == [1, 0]

    def test_dedupes_preserving_first_seen_order(self):
        assert normalize_integration_types([0, 0, 1, 0, 1]) == [0, 1]
        assert normalize_integration_types([1, 1, 0]) == [1, 0]

    def test_rejects_non_0_1_values(self):
        for bad in ([2], [-1], [0, 2], [1, 99], ["0"], [True], [False], [None], ["1"]):
            with pytest.raises(CommandScopeError):
                normalize_integration_types(bad)

    def test_error_is_value_error_subclass(self):
        with pytest.raises(ValueError):
            normalize_integration_types([7])


class TestIsGuildScoped:
    def test_guild_install_only_is_guild_scoped(self):
        assert is_guild_scoped(CommandScope(guild_id=None, integration_types=[0])) is True

    def test_guild_id_does_not_change_result(self):
        assert is_guild_scoped(CommandScope(guild_id="123456", integration_types=[0])) is True

    def test_user_install_included_is_not_guild_scoped(self):
        assert is_guild_scoped(CommandScope(guild_id=None, integration_types=[0, 1])) is False
        assert is_guild_scoped(CommandScope(guild_id=None, integration_types=[1])) is False

    def test_empty_integration_types_is_not_guild_scoped(self):
        assert is_guild_scoped(CommandScope(guild_id=None, integration_types=[])) is False
