"""Multiplex cron delivery must use each profile's own live adapters.

Regression coverage for a multiplex-only delivery bug: when
``gateway.multiplex_profiles`` is on, a secondary profile's cron job was
delivered through the DEFAULT profile's adapter map instead of that
profile's own adapters.

Why that breaks in the real world: per-app messaging platforms scope their
user identifiers to the bot/app that received them (QQ and WeCom openids,
for example). Delivering a secondary profile's job through the default
profile's adapter sends the message with the wrong bot's token, so the
platform rejects the recipient id outright — the job runs, is recorded as
successful, and the user silently never receives the output.

``GatewayRunner._profile_adapters`` (``Dict[str, Dict[Platform, adapter]]``,
populated by ``_start_secondary_profile_adapters``) already holds the
per-profile adapter maps for the inbound path. These tests pin the contract
that the multiplex cron ticker routes delivery through the same map, that an
unregistered secondary profile fails closed instead of borrowing the default
profile's adapters, and that callers which pass no registry keep the previous
behaviour.
"""

import threading

import pytest


def _drive_one_multiplex_tick(monkeypatch, *, profile_homes, adapters, profile_adapters):
    """Run ``_start_multiplex`` for a single tick and return the captured
    ``adapters=`` kwarg that ``cron.scheduler.tick`` was called with, keyed by
    the HERMES_HOME override active at call time.

    The ticker loop is stopped after the first full pass so the assertions run
    against exactly one tick per profile.
    """
    from cron import scheduler_provider as sp
    from cron.scheduler_provider import InProcessCronScheduler

    seen = []
    stop = threading.Event()

    def fake_tick(*args, **kwargs):
        # Record which home is scoped so each call can be attributed to the
        # profile whose store is being ticked.
        from hermes_constants import get_hermes_home

        seen.append((str(get_hermes_home()), kwargs.get("adapters")))
        if len(seen) >= len(profile_homes):
            stop.set()
        return 0

    monkeypatch.setattr("cron.scheduler.tick", fake_tick)

    # Neutralise the per-profile bookkeeping so the test exercises delivery
    # routing only; these are covered by their own suites.
    monkeypatch.setattr(sp.InProcessCronScheduler, "recover_interrupted", lambda self: 0)
    monkeypatch.setattr("cron.jobs.record_ticker_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr("cron.jobs.clear_ticker_error", lambda *a, **k: None)
    monkeypatch.setattr("cron.jobs.record_ticker_error", lambda *a, **k: None)

    scheduler = InProcessCronScheduler()
    scheduler._start_multiplex(
        stop,
        profile_homes=profile_homes,
        adapters=adapters,
        loop=None,
        interval=0,
        profile_adapters=profile_adapters,
    )
    return seen


@pytest.fixture
def profile_homes(tmp_path):
    """Two served profiles, mirroring a default + secondary multiplex setup."""
    default_home = tmp_path / "default"
    secondary_home = tmp_path / "secondary"
    for home in (default_home, secondary_home):
        (home / "cron").mkdir(parents=True, exist_ok=True)
    return [("default", default_home), ("secondary", secondary_home)]


def test_secondary_profile_ticks_with_its_own_adapters(monkeypatch, profile_homes):
    """Each profile's tick receives that profile's own adapter map.

    This is the bug: the secondary profile used to be ticked with the shared
    (default) adapters, so its delivery went out over the wrong bot.
    """
    shared_adapters = {"qq": "default-bot-adapter"}
    profile_adapters = {
        "default": {"qq": "default-bot-adapter"},
        "secondary": {"qq": "secondary-bot-adapter"},
    }

    seen = _drive_one_multiplex_tick(
        monkeypatch,
        profile_homes=profile_homes,
        adapters=shared_adapters,
        profile_adapters=profile_adapters,
    )

    by_home = {home: used for home, used in seen}
    default_home = str(profile_homes[0][1])
    secondary_home = str(profile_homes[1][1])

    assert by_home[default_home] == profile_adapters["default"]
    assert by_home[secondary_home] == profile_adapters["secondary"], (
        "secondary profile was ticked with the wrong adapter map — its cron "
        "delivery would go out over the default profile's bot token"
    )


def test_unregistered_secondary_profile_does_not_fall_back_to_default(
    monkeypatch, profile_homes
):
    """A secondary profile missing from the registry must NOT use the shared map.

    Secondary adapters connect asynchronously and can fail to connect at all,
    so a tick can land while a profile has no registry entry. Falling back to
    the shared (default) adapters would re-introduce exactly the bug this fix
    closes: the message goes out over the default bot's token and a per-app
    platform rejects the recipient id.

    This mirrors the inbound path's fail-closed adapter resolution for
    unregistered secondary profiles.
    """
    shared_adapters = {"qq": "default-bot-adapter"}
    # Only the default profile is registered; 'secondary' is absent.
    profile_adapters = {"default": {"qq": "default-bot-adapter"}}

    seen = _drive_one_multiplex_tick(
        monkeypatch,
        profile_homes=profile_homes,
        adapters=shared_adapters,
        profile_adapters=profile_adapters,
    )

    by_home = {home: used for home, used in seen}
    default_home = str(profile_homes[0][1])
    secondary_home = str(profile_homes[1][1])

    # The default profile still uses its own (== shared) adapters.
    assert by_home[default_home] == profile_adapters["default"]
    # The unregistered secondary profile must not inherit them.
    assert by_home[secondary_home] != shared_adapters, (
        "unregistered secondary profile fell back to the default profile's "
        "adapters — delivery would go out over the wrong bot's token"
    )
    assert not by_home[secondary_home]


def test_no_registry_preserves_single_profile_behaviour(monkeypatch, profile_homes):
    """Without ``profile_adapters`` every profile ticks with the shared map.

    Back-compat contract: callers that never pass the registry (and the
    non-multiplex path) must behave exactly as before this fix.
    """
    shared_adapters = {"qq": "default-bot-adapter"}

    seen = _drive_one_multiplex_tick(
        monkeypatch,
        profile_homes=profile_homes,
        adapters=shared_adapters,
        profile_adapters=None,
    )

    assert seen, "ticker never called tick()"
    for home, used in seen:
        assert used == shared_adapters, f"profile at {home} lost the shared adapters"


def test_empty_registry_still_fails_closed_for_secondary(monkeypatch, profile_homes):
    """An empty registry is a "nothing registered yet" state, not "no multiplex".

    ``gateway/run.py`` passes the registry only on the multiplex path, where it
    is empty until secondary adapters finish connecting. During that window the
    default profile keeps the shared adapters (they are its own), while a
    secondary profile must still fail closed rather than borrow them.
    """
    shared_adapters = {"qq": "default-bot-adapter"}

    seen = _drive_one_multiplex_tick(
        monkeypatch,
        profile_homes=profile_homes,
        adapters=shared_adapters,
        profile_adapters={},
    )

    by_home = {home: used for home, used in seen}
    default_home = str(profile_homes[0][1])
    secondary_home = str(profile_homes[1][1])

    assert by_home[default_home] == shared_adapters
    assert not by_home[secondary_home], (
        "secondary profile borrowed the default profile's adapters while the "
        "registry was still empty"
    )
