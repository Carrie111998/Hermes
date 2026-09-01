"""#91675 (resume half): post-update cold-start must resume EVERY profile
with an installed gateway autostart entry, not just the active profile.

``_cold_start_windows_gateway_after_update`` called
``gateway_windows._spawn_detached()`` exactly once; ``_build_gateway_argv``
derives its profile from ``get_hermes_home()``, so only the updater's own
(active) profile ever came back. Worse, the idempotency guard was
``find_gateway_pids(all_profiles=True)`` while the spawn was single-profile:
if a sibling profile's gateway came up first (watchdog task, login), the
guard saw a live PID and NOTHING was cold-started — "one of N" was really
"zero or one, whichever the race decided".

These tests use a REAL temp HERMES_HOME with two profiles and REAL
Startup-folder autostart entries (fake APPDATA); only the OS boundary
(platform gate, schtasks, spawn, liveness poll) is stubbed.
"""

from __future__ import annotations

import hermes_constants
from hermes_cli import gateway as hermes_gateway
from hermes_cli import gateway_windows
from hermes_cli import main as cli_main
from hermes_cli import update_cmd


def _setup_two_profiles(tmp_path, monkeypatch):
    """Two-profile HERMES_HOME (default + beta), both with autostart installed."""
    root = tmp_path / "hermes-home"
    startup = (
        tmp_path
        / "appdata"
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )
    startup.mkdir(parents=True)
    root.mkdir()
    (root / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    beta = root / "profiles" / "beta"
    beta.mkdir(parents=True)
    (beta / "config.yaml").write_text("model: {}\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    # REAL Startup-folder entries for BOTH profiles — the enumeration under
    # test must find these via per-profile get_startup_entry_path().
    (startup / "Hermes_Gateway.vbs").write_text("' default\n", encoding="utf-8")
    (startup / "Hermes_Gateway_beta.vbs").write_text("' beta\n", encoding="utf-8")

    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    # No schtasks off-Windows: "task not found" exercises the REAL
    # Startup-folder fallback of is_installed().
    monkeypatch.setattr(
        gateway_windows, "_exec_schtasks", lambda args: (1, "", "no such task")
    )
    monkeypatch.setattr(update_cmd, "_desktop_owns_gateway_lifecycle", lambda: False)
    return root, beta


def test_cold_start_spawns_one_gateway_per_autostart_profile(
    tmp_path, monkeypatch, capsys
):
    root, beta = _setup_two_profiles(tmp_path, monkeypatch)

    spawned_homes: list[str] = []

    def fake_spawn(script_path=None):
        spawned_homes.append(str(hermes_constants.get_hermes_home()))
        return 40000 + len(spawned_homes)

    monkeypatch.setattr(gateway_windows, "_spawn_detached", fake_spawn)
    monkeypatch.setattr(hermes_gateway, "find_gateway_pids", lambda *a, **k: [])
    monkeypatch.setattr(
        gateway_windows, "_wait_for_gateway_ready", lambda *a, **k: [4242]
    )

    assert update_cmd._cold_start_windows_gateway_after_update() is True

    # BOTH profiles' homes got a spawn, each scoped to its own HERMES_HOME.
    assert sorted(spawned_homes) == sorted([str(root), str(beta)])
    out = capsys.readouterr().out
    assert "[default]" in out
    assert "[beta]" in out


def test_live_sibling_does_not_suppress_other_profiles(
    tmp_path, monkeypatch, capsys
):
    """The old ``all_profiles=True`` guard skipped EVERYTHING when any one
    gateway was alive. Per-profile guard: a live beta gateway must only skip
    beta — default still cold-starts."""
    root, beta = _setup_two_profiles(tmp_path, monkeypatch)

    spawned_homes: list[str] = []

    def fake_spawn(script_path=None):
        spawned_homes.append(str(hermes_constants.get_hermes_home()))
        return 50000 + len(spawned_homes)

    def fake_find_gateway_pids(*args, **kwargs):
        # beta's gateway already came back (e.g. its watchdog task fired).
        if str(hermes_constants.get_hermes_home()) == str(beta):
            return [7777]
        return []

    monkeypatch.setattr(gateway_windows, "_spawn_detached", fake_spawn)
    monkeypatch.setattr(hermes_gateway, "find_gateway_pids", fake_find_gateway_pids)
    monkeypatch.setattr(
        gateway_windows, "_wait_for_gateway_ready", lambda *a, **k: [5001]
    )

    assert update_cmd._cold_start_windows_gateway_after_update() is True

    assert spawned_homes == [str(root)]  # default spawned, beta skipped


def test_enumeration_failure_falls_back_to_active_profile(
    tmp_path, monkeypatch, capsys
):
    """If profile enumeration yields nothing, keep the pre-#91675 behavior:
    a single active-profile spawn (the pause phase already proved an
    autostart entry exists for this home)."""
    root, _beta = _setup_two_profiles(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli_main, "_windows_autostart_profile_homes", lambda: [], raising=False
    )

    spawned_homes: list[str] = []

    def fake_spawn(script_path=None):
        spawned_homes.append(str(hermes_constants.get_hermes_home()))
        return 60001

    monkeypatch.setattr(gateway_windows, "_spawn_detached", fake_spawn)
    monkeypatch.setattr(hermes_gateway, "find_gateway_pids", lambda *a, **k: [])
    monkeypatch.setattr(
        gateway_windows, "_wait_for_gateway_ready", lambda *a, **k: [6001]
    )

    assert update_cmd._cold_start_windows_gateway_after_update() is True
    assert spawned_homes == [str(root)]


def test_one_profile_failure_does_not_stop_the_rest(tmp_path, monkeypatch, capsys):
    """A dead spawn for one profile is aggregated and raised at the end —
    after every other profile got its start attempt."""
    import pytest

    root, beta = _setup_two_profiles(tmp_path, monkeypatch)

    spawned_homes: list[str] = []

    def fake_spawn(script_path=None):
        spawned_homes.append(str(hermes_constants.get_hermes_home()))
        return 70000 + len(spawned_homes)

    def fake_ready(*args, **kwargs):
        # default's spawn dies (job object denies breakaway); beta survives.
        if str(hermes_constants.get_hermes_home()) == str(root):
            return []
        return [7002]

    monkeypatch.setattr(gateway_windows, "_spawn_detached", fake_spawn)
    monkeypatch.setattr(hermes_gateway, "find_gateway_pids", lambda *a, **k: [])
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", fake_ready)

    with pytest.raises(RuntimeError, match="cold-start failed for: default"):
        update_cmd._cold_start_windows_gateway_after_update()

    # beta was still attempted and succeeded despite default's failure.
    assert sorted(spawned_homes) == sorted([str(root), str(beta)])
    assert "[beta]" in capsys.readouterr().out
