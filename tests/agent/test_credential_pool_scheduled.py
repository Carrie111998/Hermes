"""Tests for the scheduled_round_robin credential pool strategy."""

from __future__ import annotations

import json
import time

import pytest

from agent.credential_pool import (
    STRATEGY_FILL_FIRST,
    STRATEGY_SCHEDULED_ROUND_ROBIN,
    SUPPORTED_POOL_STRATEGIES,
    CredentialPool,
    PooledCredential,
    get_pool_scheduling,
    get_pool_strategy,
)

INTERVAL_MINUTES = 60
INTERVAL_SECONDS = INTERVAL_MINUTES * 60


def _entry(entry_id: str, priority: int, **overrides) -> PooledCredential:
    payload = {
        "provider": "test",
        "id": entry_id,
        "label": entry_id,
        "auth_type": "api_key",
        "priority": priority,
        "source": "manual",
        "access_token": f"tok-{entry_id}",
    }
    payload.update(overrides)
    return PooledCredential(**payload)


def _exhausted(entry_id: str, priority: int) -> PooledCredential:
    """An entry in a fresh 429 cooldown, i.e. unhealthy for the next hour."""
    return _entry(
        entry_id,
        priority,
        last_status="exhausted",
        last_status_at=time.time(),
        last_error_code=429,
    )


def _make_pool(
    monkeypatch,
    tmp_path,
    entries,
    *,
    strategy: str = STRATEGY_SCHEDULED_ROUND_ROBIN,
    **scheduling,
) -> CredentialPool:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(
        "agent.credential_pool.get_pool_strategy",
        lambda _provider: strategy,
    )
    config = {
        "enabled": True,
        "interval_minutes": INTERVAL_MINUTES,
        "skip_unhealthy": True,
        "rotate_after_request": True,
    }
    config.update(scheduling)
    monkeypatch.setattr(
        "agent.credential_pool.get_pool_scheduling",
        lambda _provider: config,
    )
    return CredentialPool("test", entries)


def _expire_interval(pool: CredentialPool) -> None:
    """Backdate the rotation stamp so the next selection is due."""
    assert pool._last_rotation_time is not None
    pool._last_rotation_time -= INTERVAL_SECONDS + 1


def _persisted_entries(tmp_path):
    auth_json = tmp_path / "hermes" / "auth.json"
    store = json.loads(auth_json.read_text())
    return store["credential_pool"]["test"]


def test_rotates_only_after_the_interval_elapses(tmp_path, monkeypatch):
    """Selection sticks to one credential per interval, then advances one step."""
    pool = _make_pool(
        monkeypatch, tmp_path, [_entry("a", 0), _entry("b", 1), _entry("c", 2)]
    )

    first = pool.select()
    assert first is not None and first.id == "a"
    # Repeated selections inside the window must not rotate (unlike round_robin).
    assert pool.select().id == "a"
    assert pool.select().id == "a"

    _expire_interval(pool)
    assert pool.select().id == "b"
    assert pool.select().id == "b"

    _expire_interval(pool)
    assert pool.select().id == "c"

    # Ring wraps back to the first credential.
    _expire_interval(pool)
    assert pool.select().id == "a"


def test_rotation_skips_unhealthy_credential(tmp_path, monkeypatch):
    """A credential in cooldown is stepped over, not handed out."""
    pool = _make_pool(
        monkeypatch, tmp_path, [_entry("a", 0), _exhausted("b", 1), _entry("c", 2)]
    )

    assert pool.select().id == "a"

    _expire_interval(pool)
    assert pool.select().id == "c"


def test_skip_unhealthy_disabled_holds_cursor_on_unhealthy_successor(
    tmp_path, monkeypatch
):
    """With skip_unhealthy off, rotation waits instead of reordering the ring."""
    pool = _make_pool(
        monkeypatch,
        tmp_path,
        [_entry("a", 0), _exhausted("b", 1), _entry("c", 2)],
        skip_unhealthy=False,
    )

    assert pool.select().id == "a"

    _expire_interval(pool)
    # 'b' is next in the ring but benched, so 'a' keeps serving — 'c' is not
    # pulled forward.
    assert pool.select().id == "a"


def test_active_lease_defers_rotation_until_request_finishes(tmp_path, monkeypatch):
    """rotate_after_request: an in-flight request blocks the timed swap."""
    pool = _make_pool(monkeypatch, tmp_path, [_entry("a", 0), _entry("b", 1)])

    assert pool.select().id == "a"
    assert pool.acquire_lease("a") == "a"

    _expire_interval(pool)
    assert pool.select().id == "a", "rotation must not interrupt an active lease"

    pool.release_lease("a")
    assert pool.select().id == "b", "rotation happens as soon as the lease drains"


def test_rotate_after_request_disabled_rotates_despite_active_lease(
    tmp_path, monkeypatch
):
    pool = _make_pool(
        monkeypatch,
        tmp_path,
        [_entry("a", 0), _entry("b", 1)],
        rotate_after_request=False,
    )

    assert pool.select().id == "a"
    pool.acquire_lease("a")

    _expire_interval(pool)
    assert pool.select().id == "b"


def test_exhaustion_rotates_immediately_without_waiting_for_the_timer(
    tmp_path, monkeypatch
):
    """Error-driven rotation keeps its existing, immediate behaviour."""
    pool = _make_pool(monkeypatch, tmp_path, [_entry("a", 0), _entry("b", 1)])

    assert pool.select().id == "a"
    rotated = pool.mark_exhausted_and_rotate(status_code=429, credential_id="a")
    assert rotated is not None and rotated.id == "b"
    assert pool.select().id == "b"


def test_rotation_state_survives_restart(tmp_path, monkeypatch):
    """A restart resumes on the same credential with the same deadline."""
    pool = _make_pool(
        monkeypatch, tmp_path, [_entry("a", 0), _entry("b", 1), _entry("c", 2)]
    )
    assert pool.select().id == "a"
    _expire_interval(pool)
    assert pool.select().id == "b"
    rotated_at = pool._last_rotation_time

    stored = _persisted_entries(tmp_path)
    stamped = [e for e in stored if "last_rotation_at" in e]
    assert [e["id"] for e in stamped] == ["b"], "only the current entry is stamped"
    assert stamped[0]["last_rotation_at"] == pytest.approx(rotated_at)

    restarted = _make_pool(
        monkeypatch,
        tmp_path,
        [PooledCredential.from_dict("test", payload) for payload in stored],
    )
    assert restarted._last_rotation_time == pytest.approx(rotated_at)
    # Resumes on 'b' rather than falling back to the first credential, and the
    # restored deadline still governs the next rotation.
    assert restarted.select().id == "b"
    _expire_interval(restarted)
    assert restarted.select().id == "c"


def test_scheduled_strategy_is_recognized_by_get_pool_strategy(monkeypatch):
    assert STRATEGY_SCHEDULED_ROUND_ROBIN in SUPPORTED_POOL_STRATEGIES
    monkeypatch.setattr(
        "agent.credential_pool._load_config_safe",
        lambda: {
            "credential_pool_strategies": {"anthropic": "scheduled_round_robin"},
            "credential_pool_scheduling": {
                "anthropic": {"interval_minutes": 30, "skip_unhealthy": False}
            },
        },
    )
    assert get_pool_strategy("anthropic") == STRATEGY_SCHEDULED_ROUND_ROBIN
    assert get_pool_scheduling("anthropic") == {
        "enabled": True,
        "interval_minutes": 30.0,
        "skip_unhealthy": False,
        "rotate_after_request": True,
    }
    # Unconfigured providers fall back to the pool's own defaults.
    assert get_pool_scheduling("openrouter") == {}


@pytest.mark.parametrize("bad_interval", [0, -5, "sixty", None])
def test_invalid_interval_falls_back_to_the_default(monkeypatch, bad_interval):
    """A bad interval must not degrade into per-request rotation."""
    monkeypatch.setattr(
        "agent.credential_pool._load_config_safe",
        lambda: {
            "credential_pool_scheduling": {
                "anthropic": {"interval_minutes": bad_interval}
            }
        },
    )
    assert get_pool_scheduling("anthropic")["interval_minutes"] == 60.0


def test_fill_first_still_pins_the_first_credential(tmp_path, monkeypatch):
    """Backwards compat: the default strategy is unaffected by the new branch."""
    pool = _make_pool(
        monkeypatch,
        tmp_path,
        [_entry("a", 0), _entry("b", 1), _entry("c", 2)],
        strategy=STRATEGY_FILL_FIRST,
    )

    assert [pool.select().id for _ in range(3)] == ["a", "a", "a"]
    assert pool._scheduled_interval is None
    assert pool._last_rotation_time is None

    # fill_first exhaustion still rotates to the next healthy credential.
    assert pool.mark_exhausted_and_rotate(status_code=429, credential_id="a").id == "b"


def test_round_robin_still_rotates_per_request(tmp_path, monkeypatch):
    """Backwards compat: per-request rotation keeps its old behaviour."""
    pool = _make_pool(
        monkeypatch,
        tmp_path,
        [_entry("a", 0), _entry("b", 1), _entry("c", 2)],
        strategy="round_robin",
    )

    assert [pool.select().id for _ in range(4)] == ["a", "b", "c", "a"]
    assert not any(
        "last_rotation_at" in entry for entry in _persisted_entries(tmp_path)
    ), "non-scheduled strategies must not write rotation state"
