"""Tests for gateway/profile_routing.py — profile-based routing."""

import pytest
from gateway.profile_routing import (
    ProfileRoute,
    ProfileRouteAuthorization,
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


class TestParseProfileRoutes:
    def test_empty(self):
        assert parse_profile_routes(None) == []
        assert parse_profile_routes([]) == []

    def test_discord_route_authorization_is_normalized(self):
        routes = parse_profile_routes([{
            "name": "forum",
            "platform": "discord",
            "profile": "limited",
            "guild_id": "111",
            "chat_id": "222",
            "authorization": {
                "allowed_users": ["42", 42],
                "allowed_roles": ["555"],
            },
        }])

        assert routes[0].authorization == ProfileRouteAuthorization(
            allowed_users=("42",),
            allowed_roles=(555,),
        )

    @pytest.mark.parametrize(
        "authorization",
        [
            "open",
            {"allowed_users": "42"},
            {"allowed_users": None},
            {"allowed_roles": None},
            {"allowed_roles": ["not-a-snowflake"]},
            {"unknown": []},
        ],
    )
    def test_malformed_authorization_fails_closed(self, authorization):
        with pytest.raises(ValueError):
            parse_profile_routes([{
                "name": "forum",
                "platform": "discord",
                "profile": "limited",
                "guild_id": "111",
                "authorization": authorization,
            }])

    def test_role_authorization_requires_guild_scope(self):
        with pytest.raises(ValueError, match="requires guild_id"):
            parse_profile_routes([{
                "name": "forum",
                "platform": "discord",
                "profile": "limited",
                "chat_id": "222",
                "authorization": {"allowed_roles": ["555"]},
            }])

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("enabled", "false"),
            ("guild_id", "not-an-id"),
            ("chat_id", 0),
            ("thread_id", -1),
        ],
    )
    def test_authorized_route_discriminators_are_strict(self, field, value):
        route = {
            "name": "forum",
            "platform": "discord",
            "profile": "limited",
            "guild_id": "111",
            "chat_id": "222",
            "authorization": {"allowed_users": ["42"]},
        }
        route[field] = value

        with pytest.raises(ValueError):
            parse_profile_routes([route])

    def test_authorization_on_unsupported_platform_fails_closed(self):
        with pytest.raises(ValueError, match="only for discord"):
            parse_profile_routes([{
                "name": "chat",
                "platform": "telegram",
                "profile": "limited",
                "chat_id": "222",
                "authorization": {"allowed_users": ["42"]},
            }])

    def test_authorization_cannot_target_default_profile(self):
        with pytest.raises(ValueError, match="privileged default profile"):
            parse_profile_routes([{
                "name": "unsafe",
                "platform": "discord",
                "profile": "default",
                "guild_id": "111",
                "authorization": {"allowed_users": ["42"]},
            }])

    @pytest.mark.parametrize(
        "route",
        [
            {
                "name": "missing-profile",
                "platform": "discord",
                "authorization": {},
            },
            {
                "name": "invalid-profile",
                "platform": "discord",
                "profile": "../default",
                "authorization": {},
            },
        ],
    )
    def test_authorized_route_never_skips_invalid_target(self, route):
        with pytest.raises(ValueError):
            parse_profile_routes([route])

    def test_gateway_config_round_trip_preserves_authorization(self):
        from gateway.config import GatewayConfig

        config = GatewayConfig.from_dict({
            "multiplex_profiles": True,
            "profile_routes": [{
                "name": "forum",
                "platform": "discord",
                "profile": "limited",
                "guild_id": "111",
                "authorization": {
                    "allowed_users": ["42"],
                    "allowed_roles": ["555"],
                },
            }],
        })

        restored = GatewayConfig.from_dict(config.to_dict())
        assert restored.profile_routes == config.profile_routes


class TestMatchProfileRoute:


    def test_no_match_returns_none(self):
        routes = [
            ProfileRoute(name="r", platform="telegram", profile="p"),
        ]
        assert match_profile_route(routes, "discord") is None


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
