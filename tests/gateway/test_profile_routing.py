"""Tests for gateway/profile_routing.py — profile-based routing."""

import pytest
from gateway.profile_routing import (
    ProfileRoute,
    parse_profile_routes,
    match_profile_route,
)


class TestProfileRoute:
    def test_specificity_thread(self):
        r = ProfileRoute(name="t", platform="discord", profile="p",
                         guild_id="g", chat_id="c", thread_id="t")
        assert r.specificity == 14  # 2 + 4 + 8


    def test_frozen(self):
        r = ProfileRoute(name="x", platform="discord", profile="p")
        with pytest.raises(AttributeError):
            r.name = "y"

    def test_scope_route_is_more_specific_than_platform_only(self):
        r = ProfileRoute(
            name="line",
            platform="custom-phone",
            profile="sales",
            scope_id="sales-line",
        )
        assert r.specificity == 1

    def test_existing_positional_constructor_order_is_preserved(self):
        r = ProfileRoute("r", "discord", "p", "g", "c", "t", False)
        assert r.guild_id == "g"
        assert r.chat_id == "c"
        assert r.thread_id == "t"
        assert r.enabled is False
        assert r.scope_id is None


class TestProfileRouteMatching:
    def test_exact_thread_match(self):
        r = ProfileRoute(name="t", platform="discord", profile="trader",
                         guild_id="111", chat_id="222", thread_id="333")
        assert r.matches("discord", guild_id="111", chat_id="222", thread_id="333")
        assert not r.matches("discord", guild_id="111", chat_id="222", thread_id="444")


    def test_guild_and_chat_are_conjunctive(self):
        # A route declaring BOTH guild_id and chat_id requires both to match.
        # Regression guard: previously chat_id was checked first and returned
        # True before guild_id was ever consulted.
        r = ProfileRoute(name="gc", platform="discord", profile="scoped",
                         guild_id="111", chat_id="222")
        # Both match (direct channel) -> match
        assert r.matches("discord", guild_id="111", chat_id="222")
        # Both match via parent (thread inside the channel) -> match
        assert r.matches("discord", guild_id="111", chat_id="333", parent_chat_id="222")
        # chat matches but guild differs -> NO match (the bug this guards)
        assert not r.matches("discord", guild_id="999", chat_id="222")
        # guild matches but chat differs -> NO match
        assert not r.matches("discord", guild_id="111", chat_id="333")

    def test_scope_id_must_match(self):
        r = ProfileRoute(
            name="line",
            platform="custom-phone",
            profile="sales",
            scope_id="sales-line",
        )
        assert r.matches("custom-phone", scope_id="sales-line")
        assert not r.matches("custom-phone", scope_id="support-line")
        assert not r.matches("custom-phone")

    def test_scope_and_chat_are_conjunctive(self):
        r = ProfileRoute(
            name="line-channel",
            platform="custom-phone",
            profile="sales",
            scope_id="sales-line",
            chat_id="call-1",
        )
        assert r.specificity == 5
        assert r.matches("custom-phone", scope_id="sales-line", chat_id="call-1")
        assert not r.matches("custom-phone", scope_id="sales-line", chat_id="call-2")
        assert not r.matches("custom-phone", scope_id="support-line", chat_id="call-1")

    def test_existing_positional_match_order_is_preserved(self):
        r = ProfileRoute(
            name="thread",
            platform="discord",
            profile="p",
            guild_id="g",
            chat_id="c",
            thread_id="t",
        )
        assert r.matches("discord", "g", "c", "t", "parent")


class TestParseProfileRoutes:
    def test_empty(self):
        assert parse_profile_routes(None) == []
        assert parse_profile_routes([]) == []

    def test_scope_id_is_parsed(self):
        routes = parse_profile_routes([
            {
                "name": "sales-line",
                "platform": "custom-phone",
                "scope_id": "sales",
                "profile": "sales-agent",
            }
        ])
        assert routes[0].scope_id == "sales"


class TestMatchProfileRoute:


    def test_no_match_returns_none(self):
        routes = [
            ProfileRoute(name="r", platform="telegram", profile="p"),
        ]
        assert match_profile_route(routes, "discord") is None

    def test_scope_route_is_selected(self):
        routes = [
            ProfileRoute(
                name="sales",
                platform="custom-phone",
                profile="sales-agent",
                scope_id="sales-line",
            ),
        ]
        matched = match_profile_route(
            routes,
            "custom-phone",
            scope_id="sales-line",
        )
        assert matched is not None
        assert matched.profile == "sales-agent"


def test_gateway_runner_routes_scope_to_served_profile(monkeypatch, tmp_path):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        multiplex_profiles=True,
        profile_routes=[
            ProfileRoute(
                name="sales",
                platform="telegram",
                profile="sales-agent",
                scope_id="sales-line",
            )
        ],
    )
    monkeypatch.setattr(
        "gateway.run._multiplex_profile_homes",
        lambda _config: [("default", tmp_path), ("sales-agent", tmp_path / "sales-agent")],
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="call-1",
        scope_id="sales-line",
    )

    assert runner._profile_name_for_source(source) == "sales-agent"


class TestSessionKeyIntegration:
    def test_default_profile_key(self):
        from gateway.session import build_session_key, SessionSource, Platform
        src = SessionSource(platform=Platform.DISCORD, chat_id="123",
                            chat_type="channel", user_id="456")
        key = build_session_key(src)
        assert key.startswith("agent:main:")


class TestParentChatIdMatching:
    """Thread messages carry thread_id as chat_id; parent_chat_id is the channel."""

    def test_channel_route_matches_via_parent_chat_id(self):
        r = ProfileRoute(name="ch", platform="discord", profile="trader",
                         chat_id="222")
        assert r.matches("discord", chat_id="333", parent_chat_id="222")


    def test_match_profile_route_with_parent_chat_id(self):
        routes = [
            ProfileRoute(name="ch", platform="discord", profile="trader",
                         chat_id="222"),
        ]
        m = match_profile_route(routes, "discord", chat_id="333", parent_chat_id="222")
        assert m is not None
        assert m.profile == "trader"


class TestForumPostMatching:
    """Test that forum posts match via parent_chat_id (direct parent)."""


    def test_forum_post_comment_matches_channel_not_thread_id(self):
        """Verify that thread_id matching is distinct from parent_chat_id matching."""
        routes = [
            ProfileRoute(name="forum", platform="discord", profile="forum_profile",
                         chat_id="forum_channel_123"),
            ProfileRoute(name="post", platform="discord", profile="post_profile",
                         thread_id="post_thread_456"),
        ]
        # A comment on the forum post should match the forum channel route, not the thread route
        m = match_profile_route(routes, "discord", chat_id="post_thread_456", 
                                 parent_chat_id="forum_channel_123")
        assert m is not None
        assert m.profile == "forum_profile"
