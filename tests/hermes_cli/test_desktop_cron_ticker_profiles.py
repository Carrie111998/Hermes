"""Desktop cron ticker: every local profile's store must be ticked.

The desktop app pools per-profile backends and reaps them after ~10 idle
minutes, so a secondary profile's in-backend ticker dies with its backend and
that profile's cron jobs silently stop firing until the user next opens the
profile. The PRIMARY desktop backend outlives the pool, so its ticker must own
every profile's store — the desktop sibling of the multiplex-gateway fix for
#69377.
"""

from pathlib import Path
import threading
import time

import pytest

import hermes_cli.web_server as ws


class _RecordingBuiltin:
    """Stands in for InProcessCronScheduler; records start() kwargs."""

    name = "builtin"

    def __init__(self):
        self.start_kwargs = None

    def start(self, stop_event, **kwargs):
        self.start_kwargs = kwargs


class _RecordingExternal:
    """External provider double — must NOT receive profile_homes."""

    name = "chronos-test"

    def __init__(self):
        self.start_kwargs = None

    def start(self, stop_event, **kwargs):
        self.start_kwargs = kwargs

    def stop(self):
        return None


@pytest.fixture()
def _providers(monkeypatch):
    import cron.scheduler_provider as sp

    builtin = _RecordingBuiltin()
    # isinstance(provider, InProcessCronScheduler) gate: register our double
    # as that class for the module under test.
    monkeypatch.setattr(ws, "_log", ws._log)
    monkeypatch.setattr(sp, "resolve_cron_scheduler", lambda: builtin)
    monkeypatch.setattr(sp, "InProcessCronScheduler", _RecordingBuiltin)
    return sp, builtin


def test_multi_profile_homes_passed_to_builtin(monkeypatch, _providers, tmp_path):
    _sp, builtin = _providers
    homes = [
        ("default", tmp_path / "root"),
        ("coder", tmp_path / "profiles" / "coder"),
    ]
    import hermes_cli.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "profiles_to_serve", lambda **_kw: list(homes))

    ws._start_desktop_cron_ticker(threading.Event(), interval=7)

    assert builtin.start_kwargs is not None
    assert builtin.start_kwargs["interval"] == 7
    assert builtin.start_kwargs["profile_homes"] == homes


def test_single_profile_still_uses_explicit_ownership(monkeypatch, _providers, tmp_path):
    _sp, builtin = _providers
    import hermes_cli.profiles as profiles_mod

    homes = [("default", tmp_path / "root")]
    monkeypatch.setattr(profiles_mod, "profiles_to_serve", lambda **_kw: homes)

    ws._start_desktop_cron_ticker(threading.Event(), interval=9)

    assert builtin.start_kwargs["interval"] == 9
    assert builtin.start_kwargs["profile_homes"] == homes
    assert builtin.start_kwargs["owner_kind"] == "desktop-fallback"
    assert builtin.start_kwargs["runtime_id"].startswith("desktop:")


def test_enumeration_failure_fails_closed(monkeypatch, _providers):
    """Unknown profile scope must not fall back to an unowned active store."""
    _sp, builtin = _providers
    import hermes_cli.profiles as profiles_mod

    def _boom(**_kw):
        raise RuntimeError("profiles dir unreadable")

    monkeypatch.setattr(profiles_mod, "profiles_to_serve", _boom)

    ws._start_desktop_cron_ticker(threading.Event(), interval=11)

    assert builtin.start_kwargs is None


def test_desktop_passes_explicit_fallback_owner_identity(monkeypatch, _providers, tmp_path):
    _sp, builtin = _providers
    import hermes_cli.profiles as profiles_mod

    homes = [("default", tmp_path / "root"), ("brand", tmp_path / "brand")]
    monkeypatch.setattr(profiles_mod, "profiles_to_serve", lambda **_kw: homes)

    ws._start_desktop_cron_ticker(threading.Event(), interval=17)

    assert builtin.start_kwargs["owner_kind"] == "desktop-fallback"
    assert builtin.start_kwargs["runtime_id"].startswith("desktop:")
    assert builtin.start_kwargs["profile_homes"] == homes


def test_desktop_profile_enumeration_failure_fails_closed(monkeypatch, _providers):
    _sp, builtin = _providers
    import hermes_cli.profiles as profiles_mod

    monkeypatch.setattr(
        profiles_mod,
        "profiles_to_serve",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("profiles unreadable")),
    )

    ws._start_desktop_cron_ticker(threading.Event(), interval=19)

    assert builtin.start_kwargs is None


def test_external_provider_never_gets_profile_homes(monkeypatch, tmp_path):
    """External registries are not profile-scoped; keep single-store semantics."""
    import cron.scheduler_provider as sp

    external = _RecordingExternal()
    monkeypatch.setattr(sp, "resolve_cron_scheduler", lambda: external)

    import hermes_cli.profiles as profiles_mod

    monkeypatch.setattr(
        profiles_mod,
        "profiles_to_serve",
        lambda **_kw: [("default", tmp_path / "a"), ("b", tmp_path / "b")],
    )

    stop = threading.Event()
    thread = threading.Thread(
        target=ws._start_desktop_cron_ticker,
        args=(stop,),
        kwargs={"interval": 13},
    )
    thread.start()
    deadline = time.monotonic() + 2
    while external.start_kwargs is None and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert external.start_kwargs == {"adapters": None, "loop": None, "interval": 13}
    assert "profile_homes" not in external.start_kwargs
