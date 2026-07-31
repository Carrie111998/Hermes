"""Tests for the Douglas/Hermes environment + home-directory compatibility
chain implemented in hermes_bootstrap.py (normalize_douglas_env() and its
helpers). See douglas/README.md, section "Cadena canonica de resolucion
Douglas/Hermes", for the chain these tests verify.

Uses tmp_path + monkeypatch throughout. Never touches the real system home
or the real HERMES_HOME/DOUGLAS_HOME environment.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import hermes_bootstrap
import hermes_constants

_DOUGLAS_HERMES_VARS = [k for k in os.environ if k.startswith(("DOUGLAS_", "HERMES_"))]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip every DOUGLAS_*/HERMES_* var and LOCALAPPDATA before each test.

    normalize_douglas_env() derives everything from os.environ, so a leaked
    var from the real environment (or a previous test) would make these
    tests flaky depending on where they run.
    """
    for key in _DOUGLAS_HERMES_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point Path.home() at an empty tmp_path subdirectory for this test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def _set_platform(monkeypatch, *, windows: bool) -> None:
    """Flip hermes_bootstrap's cached platform flag.

    hermes_bootstrap._IS_WINDOWS is computed once at import time from
    sys.platform, so patching sys.platform alone would not affect it —
    patch the module's own flag directly instead.
    """
    monkeypatch.setattr(hermes_bootstrap, "_IS_WINDOWS", windows)


# ── Env var priority: DOUGLAS_HOME / HERMES_HOME ────────────────────────────


def test_douglas_home_env_wins_over_everything(monkeypatch, fake_home):
    _set_platform(monkeypatch, windows=False)
    douglas_target = str(fake_home / "custom-douglas-target")
    hermes_target = str(fake_home / "custom-hermes-target")
    monkeypatch.setenv("DOUGLAS_HOME", douglas_target)
    monkeypatch.setenv("HERMES_HOME", hermes_target)
    # Both candidate directories exist on disk too — env still wins over disk.
    (fake_home / ".douglas").mkdir()
    (fake_home / ".hermes").mkdir()

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_HOME"] == douglas_target


def test_hermes_home_env_respected_when_douglas_unset(monkeypatch, fake_home):
    _set_platform(monkeypatch, windows=False)
    hermes_target = str(fake_home / "custom-hermes-target")
    monkeypatch.setenv("HERMES_HOME", hermes_target)

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_HOME"] == hermes_target


def test_both_set_douglas_wins(monkeypatch, fake_home):
    _set_platform(monkeypatch, windows=False)
    douglas_target = str(fake_home / "d")
    hermes_target = str(fake_home / "h")
    monkeypatch.setenv("DOUGLAS_HOME", douglas_target)
    monkeypatch.setenv("HERMES_HOME", hermes_target)

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_HOME"] == douglas_target
    assert os.environ["HERMES_HOME"] != hermes_target


def test_empty_douglas_home_does_not_override_hermes_home(monkeypatch, fake_home):
    """An empty-string DOUGLAS_HOME must be treated as unset, matching how
    hermes_constants treats HERMES_HOME=""."""
    _set_platform(monkeypatch, windows=False)
    hermes_target = str(fake_home / "h")
    monkeypatch.setenv("DOUGLAS_HOME", "")
    monkeypatch.setenv("HERMES_HOME", hermes_target)

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_HOME"] == hermes_target


# ── Directory-existence chain (POSIX) ───────────────────────────────────────


def test_douglas_dir_exists_posix_is_used(monkeypatch, fake_home):
    _set_platform(monkeypatch, windows=False)
    (fake_home / ".douglas").mkdir()
    # No .hermes — should still resolve to .douglas regardless.

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_HOME"] == str(fake_home / ".douglas")


def test_only_hermes_dir_exists_posix_is_used(monkeypatch, fake_home):
    """Previous Hermes install, no Douglas install yet: must not be orphaned."""
    _set_platform(monkeypatch, windows=False)
    (fake_home / ".hermes").mkdir()

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_HOME"] == str(fake_home / ".hermes")


def test_douglas_wins_over_hermes_when_both_exist_posix(monkeypatch, fake_home):
    _set_platform(monkeypatch, windows=False)
    (fake_home / ".douglas").mkdir()
    (fake_home / ".hermes").mkdir()

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_HOME"] == str(fake_home / ".douglas")


def test_neither_exists_defaults_to_douglas_posix(monkeypatch, fake_home):
    """Fresh install: neither directory exists yet, default target is
    ~/.douglas (the function does not create it — it only selects the path;
    Hermes creates HERMES_HOME lazily on first write, same as today)."""
    _set_platform(monkeypatch, windows=False)

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_HOME"] == str(fake_home / ".douglas")
    assert not (fake_home / ".douglas").exists()


# ── Directory-existence chain (Windows) ─────────────────────────────────────


def test_windows_douglas_appdata_wins(monkeypatch, fake_home, tmp_path):
    _set_platform(monkeypatch, windows=True)
    local_appdata = tmp_path / "AppData" / "Local"
    local_appdata.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    (local_appdata / "douglas").mkdir()
    (local_appdata / "hermes").mkdir()

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_HOME"] == str(local_appdata / "douglas")


def test_windows_hermes_appdata_used_when_no_douglas(monkeypatch, fake_home, tmp_path):
    _set_platform(monkeypatch, windows=True)
    local_appdata = tmp_path / "AppData" / "Local"
    local_appdata.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    (local_appdata / "hermes").mkdir()

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_HOME"] == str(local_appdata / "hermes")


def test_windows_legacy_home_hermes_used_as_last_resort(monkeypatch, fake_home, tmp_path):
    """Neither %LOCALAPPDATA%\\douglas nor %LOCALAPPDATA%\\hermes exist, but
    a pre-LOCALAPPDATA-era ~/.hermes does — mirrors main.ts's existing
    legacy special case exactly."""
    _set_platform(monkeypatch, windows=True)
    local_appdata = tmp_path / "AppData" / "Local"
    local_appdata.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    (fake_home / ".hermes").mkdir()

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_HOME"] == str(fake_home / ".hermes")


def test_windows_fresh_install_defaults_to_douglas_appdata(monkeypatch, fake_home, tmp_path):
    _set_platform(monkeypatch, windows=True)
    local_appdata = tmp_path / "AppData" / "Local"
    local_appdata.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_HOME"] == str(local_appdata / "douglas")


def test_windows_without_localappdata_falls_back_to_home_style(monkeypatch, fake_home):
    """LOCALAPPDATA unset on Windows is an edge case, but must still resolve
    sensibly rather than crash — falls back to the ~/.douglas / ~/.hermes
    chain, same as POSIX."""
    _set_platform(monkeypatch, windows=True)
    (fake_home / ".hermes").mkdir()

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_HOME"] == str(fake_home / ".hermes")


# ── Generic DOUGLAS_<X> -> HERMES_<X> variable aliasing ─────────────────────


def test_generic_douglas_var_aliases_to_hermes_var(monkeypatch, fake_home):
    _set_platform(monkeypatch, windows=False)
    monkeypatch.setenv("DOUGLAS_API_KEY", "douglas-secret")

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_API_KEY"] == "douglas-secret"


def test_generic_douglas_var_wins_when_both_set(monkeypatch, fake_home):
    _set_platform(monkeypatch, windows=False)
    monkeypatch.setenv("DOUGLAS_MODEL", "douglas-model")
    monkeypatch.setenv("HERMES_MODEL", "hermes-model")

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_MODEL"] == "douglas-model"


def test_hermes_var_untouched_when_no_douglas_equivalent(monkeypatch, fake_home):
    _set_platform(monkeypatch, windows=False)
    monkeypatch.setenv("HERMES_LANGUAGE", "es")

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_LANGUAGE"] == "es"


def test_empty_douglas_var_does_not_override_hermes_var(monkeypatch, fake_home):
    _set_platform(monkeypatch, windows=False)
    monkeypatch.setenv("DOUGLAS_PROFILE", "")
    monkeypatch.setenv("HERMES_PROFILE", "work")

    hermes_bootstrap.normalize_douglas_env()

    assert os.environ["HERMES_PROFILE"] == "work"


# ── ContextVar profile override still wins ──────────────────────────────────


def test_context_override_wins_over_normalized_env(monkeypatch, fake_home):
    """hermes_cli/profiles.py scopes a task to a profile via
    set_hermes_home_override(); that must keep winning over whatever
    normalize_douglas_env() put in os.environ["HERMES_HOME"], since the
    override is checked first and unconditionally inside get_hermes_home()."""
    _set_platform(monkeypatch, windows=False)
    monkeypatch.setenv("DOUGLAS_HOME", str(fake_home / "process-level-home"))
    hermes_bootstrap.normalize_douglas_env()
    assert os.environ["HERMES_HOME"] == str(fake_home / "process-level-home")

    profile_dir = fake_home / "profiles" / "acme-brand"
    token = hermes_constants.set_hermes_home_override(str(profile_dir))
    try:
        assert hermes_constants.get_hermes_home() == profile_dir
    finally:
        hermes_constants.reset_hermes_home_override(token)

    # With the override cleared, the process-level (normalized) value is
    # visible again.
    assert hermes_constants.get_hermes_home() == Path(os.environ["HERMES_HOME"])
