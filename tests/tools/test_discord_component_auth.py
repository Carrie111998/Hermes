"""Tests for the Discord component authorization seam (feature I4)."""

import time

import pytest

from plugins.platforms.discord.component_auth import (
    ComponentAuthError,
    ComponentAuthPolicy,
    reused_custom_id,
)


class TestComponentAuthPolicyAuthorize:
    def test_allowlist_allows_known_actor(self):
        policy = ComponentAuthPolicy(allowlist={"111", "222"})
        assert policy.authorize("111") is True
        assert policy.authorize("222") is True

    def test_allowlist_denies_unknown_actor(self):
        policy = ComponentAuthPolicy(allowlist={"111"})
        assert policy.authorize("999") is False

    def test_allowlist_ignores_int_str_mismatch(self):
        policy = ComponentAuthPolicy(allowlist={111})
        assert policy.authorize("111") is True
        assert policy.authorize(222) is False

    def test_owner_allows(self):
        policy = ComponentAuthPolicy()
        assert policy.authorize("111", owner_id="111") is True

    def test_owner_denies_other_actor(self):
        policy = ComponentAuthPolicy()
        assert policy.authorize("111", owner_id="222") is False

    def test_role_allows(self):
        policy = ComponentAuthPolicy(admin_role_ids={"role-admin"})
        assert policy.authorize("111", allowed_role_ids={"role-admin"}) is True

    def test_role_denies_unprivileged_role(self):
        policy = ComponentAuthPolicy(admin_role_ids={"role-admin"})
        assert policy.authorize("111", allowed_role_ids={"role-user"}) is False

    def test_fail_closed_default(self):
        policy = ComponentAuthPolicy()
        assert policy.authorize("111") is False
        assert policy.authorize("111", owner_id="222") is False
        assert policy.authorize("111", allowed_role_ids={"role-admin"}) is False


class TestComponentAuthPolicyIsExpired:
    def test_fresh_view_not_expired(self):
        policy = ComponentAuthPolicy()
        assert policy.is_expired(time.time() - 60) is False

    def test_stale_view_expired(self):
        policy = ComponentAuthPolicy()
        assert policy.is_expired(time.time() - 1800) is True

    def test_custom_max_age(self):
        policy = ComponentAuthPolicy()
        assert policy.is_expired(time.time() - 120, max_age_seconds=60) is True
        assert policy.is_expired(time.time() - 30, max_age_seconds=60) is False

    def test_negative_timestamp_raises(self):
        policy = ComponentAuthPolicy()
        with pytest.raises(ComponentAuthError):
            policy.is_expired(-1.0)

    def test_non_numeric_timestamp_raises(self):
        policy = ComponentAuthPolicy()
        with pytest.raises(ComponentAuthError):
            policy.is_expired("now")

    def test_error_is_value_error(self):
        assert issubclass(ComponentAuthError, ValueError)


class TestReusedCustomId:
    def test_new_custom_id_recorded(self):
        seen = set()
        assert reused_custom_id("button:123", seen) is False
        assert "button:123" in seen

    def test_stale_reused_custom_id_detected(self):
        seen = {"button:123"}
        assert reused_custom_id("button:123", seen) is True

    def test_second_use_is_reuse(self):
        seen = set()
        reused_custom_id("button:123", seen)
        assert reused_custom_id("button:123", seen) is True

    def test_empty_custom_id_raises(self):
        with pytest.raises(ComponentAuthError):
            reused_custom_id("", set())

    def test_none_custom_id_raises(self):
        with pytest.raises(ComponentAuthError):
            reused_custom_id(None, set())
