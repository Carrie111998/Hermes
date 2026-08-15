"""Tests for the W3 Discord profile routing acceptance matrix."""

import pytest

from plugins.platforms.discord.profile_routing import (
    ProfileRoute,
    ProfileRouter,
    ProfileRoutingError,
)


def test_register_resolve_roundtrip():
    router = ProfileRouter()
    router.register("alice")
    route = router.resolve("alice")
    assert isinstance(route, ProfileRoute)
    assert route == ProfileRoute(profile_id="alice", adapter_ready=True)
    assert route.profile_id == "alice"
    assert route.adapter_ready is True


def test_unregistered_profile_raises():
    router = ProfileRouter()
    with pytest.raises(ProfileRoutingError):
        router.resolve("ghost")
    # ProfileRoutingError must remain a ValueError for fail-closed callers.
    with pytest.raises(ValueError):
        router.resolve("ghost")


def test_unready_adapter_raises():
    router = ProfileRouter()
    router.register("bob", adapter_ready=False)
    # Registered but unready -> still fail closed.
    assert router.has_profile("bob")
    with pytest.raises(ProfileRoutingError):
        router.resolve("bob")


def test_reregister_ready_to_unready_fails_closed():
    router = ProfileRouter()
    router.register("alice")
    assert router.resolve("alice").adapter_ready is True
    router.register("alice", adapter_ready=False)
    with pytest.raises(ProfileRoutingError):
        router.resolve("alice")


def test_has_profile():
    router = ProfileRouter()
    assert router.has_profile("nobody") is False
    router.register("carol")
    assert router.has_profile("carol") is True
    router.register("dave", adapter_ready=False)
    assert router.has_profile("dave") is True


def test_route_for_mapping():
    router = ProfileRouter()
    router.register("alice")
    router.register("bob")
    profile_map = {"channel-1": "alice", "channel-2": "bob"}
    assert router.route_for("channel-1", profile_map) == "alice"
    assert router.route_for("channel-2", profile_map) == "bob"


def test_route_for_missing_channel_raises():
    router = ProfileRouter()
    router.register("alice")
    with pytest.raises(ProfileRoutingError):
        router.route_for("unknown-channel", {"channel-1": "alice"})


def test_route_for_unregistered_profile_fail_closed():
    router = ProfileRouter()
    router.register("alice")
    # Mapped profile was never registered.
    with pytest.raises(ProfileRoutingError):
        router.route_for("channel-9", {"channel-9": "ghost"})
    # Mapped profile registered but adapter unready.
    router.register("bob", adapter_ready=False)
    with pytest.raises(ProfileRoutingError):
        router.route_for("channel-8", {"channel-8": "bob"})


def test_route_for_empty_map_fail_closed():
    router = ProfileRouter()
    router.register("alice")
    with pytest.raises(ProfileRoutingError):
        router.route_for("channel-1", {})
