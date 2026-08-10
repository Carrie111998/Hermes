"""Security contract for Gateway-minted DevFlow login grants."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from gateway.devflow_auth import (
    DevflowGrantError,
    DevflowLoginGrantStore,
    DevflowRateLimited,
)


def test_grant_is_digest_stored_audience_bound_one_time_and_opaque() -> None:
    now = [100.0]
    events: list[tuple[str, dict[str, str]]] = []
    store = DevflowLoginGrantStore(
        monotonic=lambda: now[0],
        secret=b"test-pepper",
        token_factory=iter(("raw-login-grant", "opaque-subject")).__next__,
        audit=lambda event, fields: events.append((event, fields)),
    )

    grant = store.mint(authenticated_actor="telegram:admin-42", audience="devflow-local")

    assert grant == "raw-login-grant"
    assert "raw-login-grant" not in repr(vars(store))
    assert all(isinstance(digest, bytes) for digest in store._grants)
    assert "telegram:admin-42" not in repr(tuple(store._grants))
    redeemed = store.redeem(grant=grant, audience="devflow-local")
    assert redeemed.subject == "opaque-subject"
    assert "admin-42" not in redeemed.subject
    assert store.actor_for_subject(redeemed.subject) == "telegram:admin-42"
    with pytest.raises(FrozenInstanceError):
        redeemed.subject = "changed"  # type: ignore[misc]
    with pytest.raises(DevflowGrantError, match="grant unavailable"):
        store.redeem(grant=grant, audience="devflow-local")

    assert events == [
        ("devflow_grant_minted", {"outcome": "success"}),
        ("devflow_grant_redeemed", {"outcome": "success"}),
        ("devflow_grant_redeemed", {"outcome": "failure"}),
    ]
    assert "raw-login-grant" not in repr(events)
    assert "admin-42" not in repr(events)


def test_grant_default_ttl_is_exactly_60_seconds_and_failure_shape_is_constant() -> None:
    now = [10.0]
    store = DevflowLoginGrantStore(monotonic=lambda: now[0], secret=b"pepper")
    grant = store.mint(authenticated_actor="telegram:7", audience="devflow-local")

    now[0] = 70.0
    assert store.redeem(grant=grant, audience="devflow-local").subject

    expired = store.mint(authenticated_actor="telegram:7", audience="devflow-local")
    now[0] = 130.000001
    failures = []
    for candidate, audience in ((expired, "devflow-local"), ("unknown", "devflow-local"), (expired, "wrong")):
        with pytest.raises(DevflowGrantError) as caught:
            store.redeem(grant=candidate, audience=audience)
        failures.append((type(caught.value), str(caught.value)))
    assert failures == [(DevflowGrantError, "grant unavailable")] * 3


def test_grants_are_restart_invalidated_and_mint_requires_pre_authenticated_actor() -> None:
    store = DevflowLoginGrantStore(secret=b"one")
    with pytest.raises(DevflowGrantError, match="grant unavailable"):
        store.mint(authenticated_actor="", audience="devflow-local")
    grant = store.mint(authenticated_actor="telegram:7", audience="devflow-local")

    restarted = DevflowLoginGrantStore(secret=b"two")
    with pytest.raises(DevflowGrantError, match="grant unavailable"):
        restarted.redeem(grant=grant, audience="devflow-local")


def test_failed_redemptions_are_rate_limited_with_bounded_state() -> None:
    now = [0.0]
    store = DevflowLoginGrantStore(
        monotonic=lambda: now[0],
        secret=b"pepper",
        max_failed_redemptions=3,
        rate_limit_window_seconds=60,
    )

    for _ in range(3):
        with pytest.raises(DevflowGrantError):
            store.redeem(grant="bad", audience="devflow-local")
    with pytest.raises(DevflowRateLimited, match="temporarily unavailable"):
        store.redeem(grant="bad", audience="devflow-local")
    assert store.failed_attempt_count <= 3

    now[0] = 61.0
    with pytest.raises(DevflowGrantError, match="grant unavailable"):
        store.redeem(grant="bad", audience="devflow-local")
