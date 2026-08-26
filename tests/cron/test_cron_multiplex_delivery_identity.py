"""Regression tests: multiplexed cron deliveries must use the OWNING profile.

Two defects shipped together in the multiplexed gateway's cron path:

1. ``InProcessCronScheduler`` received only the ACTIVE profile's flat adapter
   map (``runner.adapters``); every secondary profile's scheduled delivery
   therefore resolved whichever OTHER bot held the platform slot — e.g. one
   profile's DM-bound brief sent as another profile's Discord bot, rejected
   server-side with 403 50001 Missing Access because the sender is not a DM
   participant. The fix threads ``profile_adapters`` (profile name → own
   adapter map) through ``start()`` → ``_start_multiplex()`` → each profile's
   ``tick(adapters=...)``, and ``cron.scheduler._adapters_for_profile``
   resolves ONLY the owning profile's map.

2. ``run_one_job`` resets the job's secret scope BEFORE ``_deliver_result``
   runs. The standalone delivery fallback then resolved platform tokens from
   process-global ``os.environ`` — under a multiplexer that is whatever
   profile STARTED the process, again the wrong bot. The fix re-installs the
   owning profile's secret scope around delivery
   (``cron.scheduler._delivery_secret_scope``), gated on multiplexing being
   active so single-profile behavior is untouched.
"""

import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture()
def profile_home(tmp_path):
    """A minimal profile home with its own DISCORD_BOT_TOKEN in .env."""
    home = tmp_path / "profiles" / "coach"
    home.mkdir(parents=True)
    (home / ".env").write_text(
        'DISCORD_BOT_TOKEN="tok-coach-from-profile-env"\n',
        encoding="utf-8",
    )
    return home


@pytest.fixture()
def multiplex_on():
    """Turn multiplex mode on for the test, always restoring it."""
    from agent import secret_scope

    secret_scope.set_multiplex_active(True)
    try:
        yield True
    finally:
        secret_scope.set_multiplex_active(False)


# ───────────────────────── _adapters_for_profile ─────────────────────────


class TestAdaptersForProfile:
    def test_active_profile_gets_base_map(self):
        from cron.scheduler import _adapters_for_profile

        base = {"discord": object()}
        result = _adapters_for_profile(base, {"default": base}, "default")
        assert result is base

    def test_secondary_profile_gets_only_its_own_map(self):
        from cron.scheduler import _adapters_for_profile

        base = {"discord": "wrong-bot"}
        own = {"discord": "rickclawin-adapter"}
        result = _adapters_for_profile(base, {"coach": own}, "coach")
        assert result is own

    def test_unknown_secondary_resolves_empty_map(self):
        """A secondary whose adapters are not connected must resolve NO live
        transport — never another profile's adapter."""
        from cron.scheduler import _adapters_for_profile

        base = {"discord": "wrong-bot"}
        result = _adapters_for_profile(base, {"coach": {}}, "hdops")
        assert result == {}
        assert result is not base

    def test_no_profile_adapters_is_legacy_passthrough(self):
        """Without profile_adapters (non-multiplex callers, older providers),
        the base map is handed through unchanged."""
        from cron.scheduler import _adapters_for_profile

        base = {"discord": object()}
        assert _adapters_for_profile(base, None, "coach") is base
        assert _adapters_for_profile(base, {}, "coach") is base

    def test_none_profile_name_gets_base(self):
        """String-only profile_homes entries (no name tuple) keep old shape."""
        from cron.scheduler import _adapters_for_profile

        base = {"discord": object()}
        assert _adapters_for_profile(base, {"coach": {}}, None) is base


# ─────────────────────── _delivery_secret_scope ──────────────────────────


class TestDeliverySecretScope:
    def test_scope_reinstalled_from_profile_env_when_multiplex_on(
        self, profile_home, multiplex_on, monkeypatch
    ):
        import cron.scheduler as scheduler
        from agent.secret_scope import current_secret_scope, get_secret

        # Simulate a mux ticker thread scoped to this profile's home.
        monkeypatch.setattr(scheduler, "_hermes_home", profile_home)

        # No ambient scope before delivery.
        assert current_secret_scope() is None

        with scheduler._delivery_secret_scope():
            assert current_secret_scope() is not None
            # The standalone fallback resolves the OWNING profile's token,
            # not whatever sits in os.environ / the launching profile's env.
            assert (
                get_secret("DISCORD_BOT_TOKEN")
                == "tok-coach-from-profile-env"
            )

        # Scope is gone again once delivery finishes.
        assert current_secret_scope() is None

    def test_no_scope_installed_when_multiplex_off(self, profile_home, monkeypatch):
        """Single-profile processes keep legacy behavior: no scope, plain
        os.environ resolution."""
        import cron.scheduler as scheduler
        from agent.secret_scope import current_secret_scope, get_secret

        monkeypatch.setattr(scheduler, "_hermes_home", profile_home)
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok-from-process-env")

        with scheduler._delivery_secret_scope():
            assert current_secret_scope() is None
            assert get_secret("DISCORD_BOT_TOKEN") == "tok-from-process-env"

    def test_scoped_read_wins_over_process_env(self, profile_home, multiplex_on, monkeypatch):
        """Under an active multiplexer the profile scope is authoritative."""
        import cron.scheduler as scheduler
        from agent.secret_scope import get_secret

        monkeypatch.setattr(scheduler, "_hermes_home", profile_home)
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok-wrong-process-bot")

        with scheduler._delivery_secret_scope():
            assert get_secret("DISCORD_BOT_TOKEN") == "tok-coach-from-profile-env"

    def test_wrapper_passes_through_and_restores_scope(
        self, profile_home, multiplex_on, monkeypatch
    ):
        import cron.scheduler as scheduler
        from agent.secret_scope import current_secret_scope

        monkeypatch.setattr(scheduler, "_hermes_home", profile_home)

        seen = {}

        def fake_deliver(job, content, adapters=None, loop=None):
            seen["job"] = job
            seen["content"] = content
            seen["scope_active"] = current_secret_scope() is not None
            return None

        monkeypatch.setattr(scheduler, "_deliver_result", fake_deliver)

        job = {"id": "j1"}
        result = scheduler._deliver_result_with_profile_scope(
            job, "body", adapters={"discord": object()}, loop=None
        )
        assert result is None
        assert seen["content"] == "body"
        assert seen["scope_active"] is True
        assert current_secret_scope() is None


# ─────────────── provider-level: per-profile tick routing ────────────────


class TestMultiplexTickRoutesPerProfileAdapters:
    def test_each_profile_tick_receives_its_own_adapter_map(
        self, tmp_path, monkeypatch
    ):
        """End-to-end wiring: start(profile_homes=..., profile_adapters=...)
        hands each profile's tick ONLY that profile's live-adapter map."""
        from cron.scheduler_provider import InProcessCronScheduler

        base_map = {"discord": "active-profile-adapter"}
        coach_map = {"discord": "coach-profile-adapter"}
        profile_adapters = {"default": base_map, "coach": coach_map}

        homes = [
            ("default", tmp_path / "default"),
            ("coach", tmp_path / "profiles" / "coach"),
            ("hdops", tmp_path / "profiles" / "hdops"),  # adapters missing
        ]
        for _, home in homes:
            home.mkdir(parents=True)

        # Store-side noise is irrelevant here; stub it hermetically.
        for name in (
            "record_ticker_heartbeat",
            "record_ticker_error",
            "clear_ticker_error",
        ):
            monkeypatch.setattr(f"cron.jobs.{name}", lambda *a, **k: None)

        received = []
        stop_event = threading.Event()

        def fake_tick(*args, **kwargs):
            received.append(kwargs.get("adapters"))
            if len(received) >= len(homes):
                stop_event.set()

        monkeypatch.setattr("cron.scheduler.tick", fake_tick)

        provider = InProcessCronScheduler()
        provider.recover_interrupted = lambda *a, **k: 0

        provider.start(
            stop_event,
            adapters=base_map,
            loop=None,
            interval=3600,
            profile_homes=homes,
            profile_adapters=profile_adapters,
        )

        assert len(received) == len(homes)
        # Active profile keeps the runner's flat map (by reference).
        assert received[0] is base_map
        # Coach resolves ONLY its own map.
        assert received[1] is coach_map
        # Unconnected secondary resolves NO live transport — crucially NOT
        # another profile's adapter.
        assert received[2] == {}
