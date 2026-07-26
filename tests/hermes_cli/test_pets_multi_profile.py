"""Multi-profile pet CLI tests (PR1 / Layer 1).

Covers the per-profile isolation of ``hermes pets`` commands:

- ``pets off`` disables ONLY the current (bound) profile.
- ``pets off --all`` disables every profile, skipping unreadable ones with a
  warning instead of aborting.
- ``pets select <slug> --profile <name>`` (global ``--profile`` binds
  HERMES_HOME) writes the named profile's config, not the launch profile's.
- ``pets list --profiles`` reports per-profile status.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    """Default home + a profiles root under a mocked ``Path.home()``.

    ``_get_profiles_root()`` is HOME-anchored (``~/.hermes/profiles``), so the
    profile machinery only resolves inside the temp dir when ``Path.home()`` is
    mocked alongside ``HERMES_HOME``.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "profiles").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _make_profile(profile_env: Path, name: str, *, enabled: bool, slug: str = "boba") -> Path:
    """Create a named profile dir with a ``display.pet`` config; return its home."""
    home = profile_env / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    cfg = {"display": {"pet": {"enabled": enabled, "slug": slug}}}
    (home / "config.yaml").write_text(yaml.dump(cfg), encoding="utf-8")
    return home


def _read_pet(home: Path) -> dict:
    cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    return cfg.get("display", {}).get("pet", {})


def _write_default_pet(profile_env: Path, *, enabled: bool, slug: str = "boba") -> None:
    cfg = {"display": {"pet": {"enabled": enabled, "slug": slug}}}
    (profile_env / "config.yaml").write_text(yaml.dump(cfg), encoding="utf-8")


class _Args:
    """Minimal argparse.Namespace stand-in for the pet subcommand handlers."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_pets_off_disables_only_current_profile(profile_env):
    """``pets off`` (no --all) touches ONLY the bound profile (test 3)."""
    from hermes_cli.pets import _cmd_off

    _write_default_pet(profile_env, enabled=True)
    apollo = _make_profile(profile_env, "apollo", enabled=True)

    assert _cmd_off(_Args(all=False)) == 0

    # Launch/default profile disabled...
    assert _read_pet(profile_env)["enabled"] is False
    # ...but the named profile is untouched.
    assert _read_pet(apollo)["enabled"] is True


def test_pets_off_all_disables_every_profile(profile_env):
    """``pets off --all`` disables default + all named profiles (test 4)."""
    from hermes_cli.pets import _cmd_off

    _write_default_pet(profile_env, enabled=True)
    apollo = _make_profile(profile_env, "apollo", enabled=True)
    nova = _make_profile(profile_env, "nova", enabled=True)

    assert _cmd_off(_Args(all=True)) == 0

    assert _read_pet(profile_env)["enabled"] is False
    assert _read_pet(apollo)["enabled"] is False
    assert _read_pet(nova)["enabled"] is False


def test_pets_off_all_warns_but_does_not_abort_on_unreadable(profile_env, monkeypatch):
    """An unreadable profile is skipped with a warning, not fatal (test 4)."""
    from hermes_cli import pets as pets_mod

    _write_default_pet(profile_env, enabled=True)
    apollo = _make_profile(profile_env, "apollo", enabled=True)

    # Make the default profile's save path blow up; the named profile must still
    # be processed (the run does not abort on the first failure).
    real_set_enabled = pets_mod._set_enabled
    calls = {"count": 0}

    def flaky_set_enabled(enabled):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("config unreadable")
        return real_set_enabled(enabled)

    monkeypatch.setattr(pets_mod, "_set_enabled", flaky_set_enabled)

    assert pets_mod._cmd_off(_Args(all=True)) == 0

    # The first profile failed but the second was still disabled.
    assert _read_pet(apollo)["enabled"] is False


def test_pets_select_with_profile_writes_named_profile(profile_env):
    """Global ``--profile`` binds HERMES_HOME → select writes THAT profile (test 2).

    The global ``--profile`` flag is applied by ``main.py`` via the
    ``set_hermes_home_override`` contextvar before the subcommand runs; we bind
    the same override here to exercise the isolation directly.
    """
    from hermes_cli.pets import _cmd_select
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    _write_default_pet(profile_env, enabled=True, slug="default-pet")
    apollo = _make_profile(profile_env, "apollo", enabled=False, slug="")

    # The named profile needs the pet installed for select to resolve it.
    from agent.pet import store

    token = set_hermes_home_override(apollo)
    try:
        # Install a minimal pet into Apollo's pets dir so select can resolve it.
        from PIL import Image
        from agent.pet.constants import FRAME_H, FRAME_W

        pet_dir = store.pets_dir() / "boba"
        pet_dir.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (FRAME_W * 8, FRAME_H * 9), (0, 0, 0, 0)).save(pet_dir / "spritesheet.webp")
        (pet_dir / "pet.json").write_text(
            '{"id":"boba","displayName":"Boba","description":"d","spritesheetPath":"spritesheet.webp"}'
        )

        assert _cmd_select(_Args(slug="boba")) == 0
    finally:
        reset_hermes_home_override(token)

    # Apollo got the pet; the default profile is unchanged.
    assert _read_pet(apollo)["slug"] == "boba"
    assert _read_pet(apollo)["enabled"] is True
    assert _read_pet(profile_env)["slug"] == "default-pet"


def test_pets_list_profiles_reports_per_profile_status(profile_env, capsys):
    """``pets list --profiles`` prints a per-profile status line (test 1/2)."""
    from hermes_cli.pets import _cmd_list

    _write_default_pet(profile_env, enabled=True, slug="boba")
    _make_profile(profile_env, "apollo", enabled=False, slug="")

    assert _cmd_list(_Args(profiles=True, installed=False, query="", limit=0)) == 0

    out = capsys.readouterr().out
    assert "default" in out
    assert "apollo" in out
    assert "enabled=True" in out
    assert "enabled=False" in out
