"""A header-less HTTP 429 must not freeze a credential for a flat hour.

A 429 that carries no ``Retry-After`` / reset hint is almost always a short
transient throttle, not a spent quota window.  The old code gave it the flat
``EXHAUSTED_TTL_429_SECONDS`` hour anyway, which on a single-credential pool
guarantees total agent failure: there is nothing to rotate to, so refusing the
only key locally just takes the agent down for 57 more minutes while the
provider is already serving that same key again (observed: a direct call on
the same key returned HTTP 200 while Hermes was still self-refusing).

The fix rides an escalating ladder instead of a flat hour, on a much shorter
rung when the pool has nowhere to rotate to.  These tests lock the *shape* of
that ladder rather than its current seconds, so tuning the constants doesn't
break them:

* a sole credential recovers in a tiny fraction of the hour ceiling;
* a multi-credential pool escalates monotonically, reaches the hour ceiling,
  and never passes it;
* a provider-supplied ``reset_at`` is authoritative and overrides the ladder;
* 401 and 402 cooldowns are untouched by any of this;
* the strike counter survives the auth.json round-trip, so the ladder keeps
  climbing across processes instead of restarting at the base;
* an exhausted key in a multi-credential pool is still rotated away from.
"""

from __future__ import annotations

import json
import re
import time

import pytest


PROVIDER = "openrouter"


def _row(idx: int, key: str, **overrides) -> dict:
    row = {
        "id": f"cred-{idx}",
        "label": f"key-{idx}",
        "auth_type": "api_key",
        "priority": idx,
        "source": "manual",
        "access_token": key,
    }
    row.update(overrides)
    return row


def _seed_pool(tmp_path, monkeypatch, rows, provider: str = PROVIDER):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(
        json.dumps({"version": 1, "credential_pool": {provider: rows}})
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    from agent.credential_pool import load_pool

    return load_pool(provider)


def _credential(**overrides):
    from agent.credential_pool import PooledCredential

    payload = dict(
        provider=PROVIDER,
        id="cred-0",
        label="key-0",
        auth_type="api_key",
        priority=0,
        source="manual",
        access_token="key-a",
    )
    payload.update(overrides)
    return PooledCredential(**payload)


def _entry_by_id(pool, entry_id: str):
    return next(entry for entry in pool.entries() if entry.id == entry_id)


def _cooldown_seconds(entry, *, sole_credential: bool) -> float:
    """Cooldown the pool will actually serve for *entry*, in seconds.

    Measured against the strike's own timestamp rather than wall-clock ``now``
    so the assertion is about the policy, not about how long the test took.
    """
    from agent.credential_pool import _exhausted_until

    until = _exhausted_until(entry, sole_credential=sole_credential)
    assert until is not None
    return until - entry.last_status_at


def _mark_headerless_429(pool, credential_id: str):
    """One 429 strike with no reset hint of any kind in the error context."""
    return pool.mark_exhausted_and_rotate(
        status_code=429,
        credential_id=credential_id,
        error_context={"reason": "rate_limit_exceeded"},
    )


class TestHeaderlessLadderShape:
    """Unit contract for the fallback ladder itself."""

    def test_sole_credential_ladder_stays_a_small_fraction_of_the_hour(self):
        from agent.credential_pool import (
            EXHAUSTED_TTL_429_BACKOFF_FACTOR,
            EXHAUSTED_TTL_429_SECONDS,
            SOLE_CREDENTIAL_TTL_429_BASE_SECONDS,
            SOLE_CREDENTIAL_TTL_429_MAX_SECONDS,
            _headerless_429_ttl,
        )

        ladder = [_headerless_429_ttl(s, sole_credential=True) for s in range(1, 9)]

        # This is the bug in one assertion: the only key in the pool comes back
        # in a tiny fraction of the old flat hour, not after it.
        assert max(ladder) < EXHAUSTED_TTL_429_SECONDS / 30, ladder
        assert max(ladder) < 2 * 60, ladder

        assert ladder[0] == SOLE_CREDENTIAL_TTL_429_BASE_SECONDS
        assert ladder[0] > 0, "some floor must remain, or a hammer loop is possible"
        assert all(b >= a for a, b in zip(ladder, ladder[1:])), ladder
        assert max(ladder) == SOLE_CREDENTIAL_TTL_429_MAX_SECONDS
        for previous, current in zip(ladder, ladder[1:]):
            if current < SOLE_CREDENTIAL_TTL_429_MAX_SECONDS:
                assert current == previous * EXHAUSTED_TTL_429_BACKOFF_FACTOR

    def test_multi_credential_ladder_climbs_to_the_hour_ceiling_and_stops(self):
        from agent.credential_pool import (
            EXHAUSTED_TTL_429_BACKOFF_FACTOR,
            EXHAUSTED_TTL_429_BASE_SECONDS,
            EXHAUSTED_TTL_429_SECONDS,
            _headerless_429_ttl,
        )

        ladder = [_headerless_429_ttl(s, sole_credential=False) for s in range(1, 11)]

        assert ladder[0] == EXHAUSTED_TTL_429_BASE_SECONDS
        # A first throttle is still cheap even with somewhere to rotate to.
        assert ladder[0] < EXHAUSTED_TTL_429_SECONDS / 10, ladder
        assert all(b >= a for a, b in zip(ladder, ladder[1:])), ladder
        assert all(value <= EXHAUSTED_TTL_429_SECONDS for value in ladder), ladder
        # A genuinely spent quota still parks at the old flat hour.
        assert EXHAUSTED_TTL_429_SECONDS in ladder, ladder
        for previous, current in zip(ladder, ladder[1:]):
            if current < EXHAUSTED_TTL_429_SECONDS:
                assert current == previous * EXHAUSTED_TTL_429_BACKOFF_FACTOR
            else:
                assert current == EXHAUSTED_TTL_429_SECONDS

    @pytest.mark.parametrize("streak", [1, 2, 3, 4, 5, 6])
    def test_a_pool_with_nowhere_to_rotate_always_waits_less(self, streak):
        from agent.credential_pool import _headerless_429_ttl

        assert _headerless_429_ttl(streak, sole_credential=True) < _headerless_429_ttl(
            streak, sole_credential=False
        )

    @pytest.mark.parametrize("sole", [True, False])
    def test_runaway_and_degenerate_streaks_stay_inside_the_ceiling(self, sole):
        from agent.credential_pool import (
            EXHAUSTED_TTL_429_SECONDS,
            SOLE_CREDENTIAL_TTL_429_MAX_SECONDS,
            _headerless_429_ttl,
        )

        ceiling = (
            SOLE_CREDENTIAL_TTL_429_MAX_SECONDS if sole else EXHAUSTED_TTL_429_SECONDS
        )
        # A streak counter that ran away (or came back nonsense from disk) must
        # not build a giant int or escape the ceiling.
        assert _headerless_429_ttl(10_000, sole_credential=sole) == ceiling
        first = _headerless_429_ttl(1, sole_credential=sole)
        assert _headerless_429_ttl(0, sole_credential=sole) == first
        assert _headerless_429_ttl(-5, sole_credential=sole) == first


class TestNon429CooldownsAreUnchanged:
    """Regression guard: only the header-less 429 path moved."""

    @pytest.mark.parametrize("sole", [True, False])
    @pytest.mark.parametrize("streak", [1, 3, 9])
    def test_401_402_and_default_ignore_streak_and_pool_size(self, sole, streak):
        from agent.credential_pool import (
            EXHAUSTED_TTL_401_SECONDS,
            EXHAUSTED_TTL_DEFAULT_SECONDS,
            _exhausted_ttl,
        )

        assert (
            _exhausted_ttl(401, streak=streak, sole_credential=sole)
            == EXHAUSTED_TTL_401_SECONDS
        )
        assert (
            _exhausted_ttl(402, streak=streak, sole_credential=sole)
            == EXHAUSTED_TTL_DEFAULT_SECONDS
        )
        assert (
            _exhausted_ttl(500, streak=streak, sole_credential=sole)
            == EXHAUSTED_TTL_DEFAULT_SECONDS
        )
        assert (
            _exhausted_ttl(None, streak=streak, sole_credential=sole)
            == EXHAUSTED_TTL_DEFAULT_SECONDS
        )
        # The transient-auth cooldown stays well short of the billing one.
        assert EXHAUSTED_TTL_401_SECONDS < EXHAUSTED_TTL_DEFAULT_SECONDS

    def test_billing_402_still_parks_a_sole_credential_for_the_hour(
        self, tmp_path, monkeypatch
    ):
        """A 402 is a spent balance, not a throttle — no fast re-probe."""
        from agent.credential_pool import EXHAUSTED_TTL_DEFAULT_SECONDS

        struck_at = time.time() - EXHAUSTED_TTL_DEFAULT_SECONDS / 2
        pool = _seed_pool(
            tmp_path,
            monkeypatch,
            [
                _row(
                    0,
                    "key-a",
                    last_status="exhausted",
                    last_status_at=struck_at,
                    last_error_code=402,
                    last_error_reason="insufficient_quota",
                )
            ],
        )

        assert pool.has_available() is False
        assert pool.select() is None

    def test_transient_401_still_clears_on_its_own_short_window(
        self, tmp_path, monkeypatch
    ):
        from agent.credential_pool import EXHAUSTED_TTL_401_SECONDS

        rows = [
            _row(
                0,
                "key-a",
                last_status="exhausted",
                last_status_at=time.time() - EXHAUSTED_TTL_401_SECONDS / 5,
                last_error_code=401,
            )
        ]
        assert _seed_pool(tmp_path, monkeypatch, rows).has_available() is False

        rows[0]["last_status_at"] = time.time() - EXHAUSTED_TTL_401_SECONDS * 2
        assert _seed_pool(tmp_path, monkeypatch, rows).has_available() is True


class TestProviderResetWins:
    """A reset timestamp from the provider outranks every local heuristic."""

    @pytest.mark.parametrize("sole", [True, False])
    def test_reset_at_is_returned_verbatim(self, sole):
        from agent.credential_pool import STATUS_EXHAUSTED, _exhausted_until

        reset_at = 12345.0
        entry = _credential(
            last_status=STATUS_EXHAUSTED,
            last_status_at=time.time(),
            last_error_code=429,
            last_error_reset_at=reset_at,
            last_rate_limit_streak=3,
        )

        assert _exhausted_until(entry, sole_credential=sole) == reset_at

    @pytest.mark.parametrize("rows_count", [1, 2])
    def test_marking_with_a_reset_hint_skips_the_ladder(
        self, tmp_path, monkeypatch, rows_count
    ):
        """Sole and multi alike: the hint is stored and honoured as-is, and the
        header-less strike counter is left alone."""
        from agent.credential_pool import _exhausted_until

        rows = [_row(i, f"key-{i}") for i in range(rows_count)]
        pool = _seed_pool(tmp_path, monkeypatch, rows)
        reset_at = time.time() + 3 * 24 * 60 * 60

        pool.mark_exhausted_and_rotate(
            status_code=429,
            credential_id="cred-0",
            error_context={"reason": "rate_limit_exceeded", "reset_at": reset_at},
        )
        entry = _entry_by_id(pool, "cred-0")

        assert entry.last_error_reset_at == reset_at
        for sole in (True, False):
            assert _exhausted_until(entry, sole_credential=sole) == reset_at
        # Nothing header-less happened, so the escalating ladder never engaged.
        assert entry.last_rate_limit_streak == 0


class TestPoolCooldownBehaviour:

    def test_sole_credential_recovers_long_before_the_hour(
        self, tmp_path, monkeypatch
    ):
        """The reported bug: one key, one header-less 429, a lost day.

        Two minutes after the strike the pool must serve that key again — at
        every rung of the sole-credential ladder.
        """
        from agent.credential_pool import EXHAUSTED_TTL_429_SECONDS

        two_minutes = 2 * 60
        assert two_minutes < EXHAUSTED_TTL_429_SECONDS / 10, (
            "the old flat hour would still be freezing this credential"
        )

        for streak in (1, 2, 3, 7):
            pool = _seed_pool(
                tmp_path,
                monkeypatch,
                [
                    _row(
                        0,
                        "key-a",
                        last_status="exhausted",
                        last_status_at=time.time() - two_minutes,
                        last_error_code=429,
                        last_error_reason="rate_limit_exceeded",
                        last_rate_limit_streak=streak,
                    )
                ],
            )

            assert pool.has_available() is True, f"streak={streak}"
            selected = pool.select()
            assert selected is not None and selected.id == "cred-0"

    def test_sole_credential_strikes_escalate_but_never_reach_the_hour(
        self, tmp_path, monkeypatch
    ):
        from agent.credential_pool import (
            EXHAUSTED_TTL_429_SECONDS,
            SOLE_CREDENTIAL_TTL_429_MAX_SECONDS,
        )

        pool = _seed_pool(tmp_path, monkeypatch, [_row(0, "key-a")])

        cooldowns = []
        for strike in range(1, 5):
            _mark_headerless_429(pool, "cred-0")
            entry = _entry_by_id(pool, "cred-0")
            assert entry.last_rate_limit_streak == strike
            cooldowns.append(_cooldown_seconds(entry, sole_credential=True))

        assert all(b >= a for a, b in zip(cooldowns, cooldowns[1:])), cooldowns
        assert cooldowns[-1] > cooldowns[0]
        assert max(cooldowns) == SOLE_CREDENTIAL_TTL_429_MAX_SECONDS
        assert max(cooldowns) < EXHAUSTED_TTL_429_SECONDS / 30

    def test_multi_credential_strikes_escalate_to_the_ceiling_and_stop(
        self, tmp_path, monkeypatch
    ):
        """With somewhere to rotate to, a repeatedly-throttled key is allowed
        to climb all the way to the hour — and no further."""
        from agent.credential_pool import EXHAUSTED_TTL_429_SECONDS

        pool = _seed_pool(
            tmp_path, monkeypatch, [_row(0, "key-a"), _row(1, "key-b")]
        )

        cooldowns = []
        for strike in range(1, 7):
            _mark_headerless_429(pool, "cred-0")
            entry = _entry_by_id(pool, "cred-0")
            assert entry.last_rate_limit_streak == strike
            cooldowns.append(_cooldown_seconds(entry, sole_credential=False))

        assert all(b >= a for a, b in zip(cooldowns, cooldowns[1:])), cooldowns
        assert all(value <= EXHAUSTED_TTL_429_SECONDS for value in cooldowns), cooldowns
        assert cooldowns[0] < EXHAUSTED_TTL_429_SECONDS
        assert cooldowns[-1] == EXHAUSTED_TTL_429_SECONDS
        # And it is strictly an escalation, not a flat hour from strike one.
        assert cooldowns[1] > cooldowns[0]

    def test_multi_credential_pool_still_rotates_away_from_an_exhausted_key(
        self, tmp_path, monkeypatch
    ):
        """The fast sole-credential rung must not disable rotation."""
        pool = _seed_pool(
            tmp_path, monkeypatch, [_row(0, "key-a"), _row(1, "key-b")]
        )
        assert pool.select().id == "cred-0"

        rotated = _mark_headerless_429(pool, "cred-0")

        assert rotated is not None
        assert rotated.id == "cred-1"
        assert pool.select().id == "cred-1"
        assert {entry.id for entry in pool.entries() if entry.last_status != "exhausted"} == {
            "cred-1"
        }
        # The throttled key is on the multi-credential ladder, not the sole one.
        exhausted = _entry_by_id(pool, "cred-0")
        assert _cooldown_seconds(exhausted, sole_credential=False) > _cooldown_seconds(
            exhausted, sole_credential=True
        )


class TestStreakPersistence:

    def test_streak_survives_the_auth_store_round_trip(self, tmp_path, monkeypatch):
        """A reload must resume the ladder, not restart it at the base.

        Hermes runs several processes against one auth.json; if the strike
        count didn't persist, a genuinely spent key would re-probe on the base
        rung forever and never park at the ceiling.
        """
        pool = _seed_pool(tmp_path, monkeypatch, [_row(0, "key-a")])
        _mark_headerless_429(pool, "cred-0")
        assert _entry_by_id(pool, "cred-0").last_rate_limit_streak == 1

        stored = json.loads((tmp_path / "hermes" / "auth.json").read_text())
        row = next(
            r for r in stored["credential_pool"][PROVIDER] if r["id"] == "cred-0"
        )
        assert row["last_rate_limit_streak"] == 1
        assert isinstance(row["last_rate_limit_streak_at"], (int, float))

        from agent.credential_pool import load_pool

        reloaded = load_pool(PROVIDER)
        restored = _entry_by_id(reloaded, "cred-0")
        assert restored.last_rate_limit_streak == 1
        first = _cooldown_seconds(restored, sole_credential=True)

        _mark_headerless_429(reloaded, "cred-0")
        after_reload = _entry_by_id(reloaded, "cred-0")
        assert after_reload.last_rate_limit_streak == 2
        assert _cooldown_seconds(after_reload, sole_credential=True) > first

    def test_streak_fields_survive_borrowed_credential_sanitization(self):
        """The disk boundary strips secrets, not cooldown bookkeeping."""
        from agent.credential_persistence import (
            is_borrowed_credential_source,
            sanitize_borrowed_credential_payload,
        )

        payload = {
            "id": "cred-0",
            "label": "borrowed",
            "auth_type": "api_key",
            "priority": 0,
            "source": "env",
            "access_token": "sk-secret",
            "last_status": "exhausted",
            "last_status_at": 1000.0,
            "last_error_code": 429,
            "last_rate_limit_streak": 3,
            "last_rate_limit_streak_at": 1000.0,
        }
        assert is_borrowed_credential_source(payload["source"], PROVIDER) is True

        sanitized = sanitize_borrowed_credential_payload(payload, PROVIDER)

        assert "access_token" not in sanitized
        assert sanitized["last_rate_limit_streak"] == 3
        assert sanitized["last_rate_limit_streak_at"] == 1000.0

    def test_dataclass_round_trip_preserves_the_computed_cooldown(self):
        from agent.credential_pool import STATUS_EXHAUSTED, PooledCredential

        entry = _credential(
            last_status=STATUS_EXHAUSTED,
            last_status_at=time.time(),
            last_error_code=429,
            last_rate_limit_streak=3,
            last_rate_limit_streak_at=time.time(),
        )

        restored = PooledCredential.from_dict(PROVIDER, entry.to_dict())

        assert restored.last_rate_limit_streak == entry.last_rate_limit_streak
        for sole in (True, False):
            assert _cooldown_seconds(restored, sole_credential=sole) == _cooldown_seconds(
                entry, sole_credential=sole
            )


class TestAuthListDisplayMatchesEnforcement:
    """`hermes auth list` must quote the wait the pool will actually serve."""

    def test_single_credential_pool_shows_a_sub_minute_wait(self):
        from hermes_cli.auth_commands import _format_exhausted_status
        from agent.credential_pool import STATUS_EXHAUSTED

        entry = _credential(
            last_status=STATUS_EXHAUSTED,
            last_status_at=time.time(),
            last_error_code=429,
            last_error_reason="rate_limit_exceeded",
            last_rate_limit_streak=1,
        )

        sole = _format_exhausted_status(entry, pool_size=1)
        multi = _format_exhausted_status(entry, pool_size=2)

        assert "rate-limited" in sole
        assert re.search(r"\(\d+s left\)", sole), sole
        assert re.search(r"\(\d+[mh]", multi), multi
        assert sole != multi
