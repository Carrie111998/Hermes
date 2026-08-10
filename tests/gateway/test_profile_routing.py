"""Tests for gateway/profile_routing.py — profile-based routing."""

import logging

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

    def test_specificity_sender(self):
        r = ProfileRoute(name="u", platform="discord", profile="p",
                         user_id="42")
        assert r.specificity == 16

    def test_specificity_sender_thread(self):
        r = ProfileRoute(name="ut", platform="discord", profile="p",
                         guild_id="g", chat_id="c", thread_id="t", user_id="42")
        assert r.specificity == 30  # 2 + 4 + 8 + 16


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

    def test_sender_match(self):
        r = ProfileRoute(name="u", platform="discord", profile="builder",
                         user_id="42")
        assert r.matches("discord", user_id="42")
        assert not r.matches("discord", user_id="43")
        assert not r.matches("discord")

    def test_sender_and_chat_are_conjunctive(self):
        r = ProfileRoute(name="uc", platform="discord", profile="builder",
                         user_id="42", chat_id="222")
        assert r.matches("discord", user_id="42", chat_id="222")
        assert not r.matches("discord", user_id="42", chat_id="333")
        assert not r.matches("discord", user_id="43", chat_id="222")

    def test_sender_guild_chat_thread_conjunctive(self):
        r = ProfileRoute(
            name="full",
            platform="discord",
            profile="scoped",
            user_id="42",
            guild_id="111",
            chat_id="222",
            thread_id="333",
        )
        base = dict(
            platform="discord",
            user_id="42",
            guild_id="111",
            chat_id="222",
            thread_id="333",
        )
        assert r.matches(**base)
        assert not r.matches(**{**base, "user_id": "99"})
        assert not r.matches(**{**base, "guild_id": "999"})
        assert not r.matches(**{**base, "chat_id": "888"})
        assert not r.matches(**{**base, "thread_id": "444"})


class TestParseProfileRoutes:
    def test_empty(self):
        assert parse_profile_routes(None) == []
        assert parse_profile_routes([]) == []

    def test_sender_route_parsed_and_coerced(self):
        raw = [
            {"name": "thread", "platform": "discord", "profile": "p",
             "guild_id": "1", "chat_id": "2", "thread_id": "3"},
            {"name": "sender", "platform": "discord", "profile": "p",
             "user_id": 42},
        ]
        routes = parse_profile_routes(raw)
        by_name = {r.name: r for r in routes}
        assert by_name["sender"].user_id == "42"

    @pytest.mark.parametrize("user_id", ["", "   "])
    def test_empty_user_id_route_skipped(self, user_id, caplog):
        raw = [
            {"name": "bad-sender", "platform": "discord", "profile": "p",
             "user_id": user_id},
        ]
        with caplog.at_level(logging.WARNING):
            routes = parse_profile_routes(raw)
        assert routes == []
        assert any("bad-sender" in r.message and "empty user_id" in r.message
                   for r in caplog.records)

    @pytest.mark.parametrize(
        "user_id",
        [True, 1.5, [123], {"id": 1}],
    )
    def test_malformed_user_id_route_skipped(self, user_id, caplog):
        raw = [
            {"name": "bad-sender", "platform": "discord", "profile": "p",
             "user_id": user_id},
        ]
        with caplog.at_level(logging.WARNING):
            routes = parse_profile_routes(raw)
        assert routes == []
        assert any("bad-sender" in r.message and "invalid user_id" in r.message
                   for r in caplog.records)

    @pytest.mark.parametrize("user_id", [0, -7])
    def test_nonpositive_int_user_id_route_skipped(self, user_id, caplog):
        raw = [
            {"name": "bad-sender", "platform": "discord", "profile": "p",
             "user_id": user_id},
        ]
        with caplog.at_level(logging.WARNING):
            routes = parse_profile_routes(raw)
        assert routes == []
        assert any(
            "bad-sender" in r.message and "positive integer" in r.message
            for r in caplog.records
        )

    def test_valid_user_id_normalization(self):
        snowflake = 392686399226380294
        raw = [
            {"name": "int-sender", "platform": "discord", "profile": "p",
             "user_id": 42},
            {"name": "snowflake-sender", "platform": "discord", "profile": "p",
             "user_id": snowflake},
            {"name": "str-sender", "platform": "discord", "profile": "p",
             "user_id": " 42 "},
        ]
        routes = parse_profile_routes(raw)
        by_name = {r.name: r for r in routes}
        assert by_name["int-sender"].user_id == "42"
        assert by_name["snowflake-sender"].user_id == str(snowflake)
        assert by_name["str-sender"].user_id == "42"

    def test_absent_user_id_stays_none(self):
        routes = parse_profile_routes([
            {"name": "loc", "platform": "discord", "profile": "p",
             "guild_id": "1", "chat_id": "2"},
        ])
        assert len(routes) == 1
        assert routes[0].user_id is None


class TestMatchProfileRoute:


    def test_no_match_returns_none(self):
        routes = [
            ProfileRoute(name="r", platform="telegram", profile="p"),
        ]
        assert match_profile_route(routes, "discord") is None

    def test_sender_route_outranks_location_only_route(self):
        routes = parse_profile_routes([
            {"name": "thread", "platform": "discord", "profile": "thread-profile",
             "guild_id": "1", "chat_id": "2", "thread_id": "3"},
            {"name": "sender", "platform": "discord", "profile": "builder",
             "user_id": "42"},
        ])
        matched = match_profile_route(
            routes,
            "discord",
            guild_id="1",
            chat_id="2",
            thread_id="3",
            user_id="42",
        )
        assert matched is not None
        assert matched.profile == "builder"


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
