"""Gateway update origins are canonical, validated profile identities."""

from pathlib import Path

import pytest


@pytest.mark.parametrize("profile", ["../escape", "work/name", "tmp"])
def test_gateway_origin_rejects_invalid_profile_env(
    monkeypatch, tmp_path: Path, profile: str
):
    import hermes_cli.update_cmd as update_cmd

    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_UPDATE_ORIGIN_PROFILE", profile)
    monkeypatch.setenv(
        "HERMES_UPDATE_ORIGIN_HOME", str((tmp_path / "profiles" / profile).resolve())
    )

    with pytest.raises(ValueError):
        update_cmd._new_update_context(gateway_mode=True)


def test_gateway_origin_rejects_profile_home_mismatch(monkeypatch, tmp_path: Path):
    import hermes_cli.update_cmd as update_cmd

    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_UPDATE_ORIGIN_PROFILE", "work")
    monkeypatch.setenv("HERMES_UPDATE_ORIGIN_HOME", str(tmp_path / "wrong"))

    with pytest.raises(ValueError, match="profile/home mismatch"):
        update_cmd._new_update_context(gateway_mode=True)
