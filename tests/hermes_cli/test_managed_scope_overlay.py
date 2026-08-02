"""apply_managed_overlay() — the shared helper used by every standalone loader."""
import textwrap

import pytest


@pytest.fixture
def managed(tmp_path, monkeypatch):
    md = tmp_path / "managed"
    md.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(md))
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    return md


def _write(md, body):
    (md / "config.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()


def test_overlay_noop_without_scope(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "nope"))
    managed_scope.invalidate_managed_cache()
    src = {"display": {"skin": "user"}}
    assert managed_scope.apply_managed_overlay(src) == {"display": {"skin": "user"}}


def test_overlay_preserves_user_siblings(managed):
    from hermes_cli import managed_scope

    _write(managed, "display:\n  skin: charizard\n")
    out = managed_scope.apply_managed_overlay(
        {"display": {"skin": "user", "show_reasoning": True}}
    )
    assert out["display"]["skin"] == "charizard"
    assert out["display"]["show_reasoning"] is True


def test_managed_config_degradation_tracks_repair_removal_and_path_change(
    managed, tmp_path, monkeypatch
):
    from hermes_cli import managed_scope

    config_path = managed / "config.yaml"
    config_path.write_text("- malformed\n- root\n", encoding="utf-8")
    assert managed_scope.load_managed_config() == {}
    assert managed_scope.managed_config_load_degraded() is True

    config_path.write_text("openrouter:\n  zdr: false\n", encoding="utf-8")
    assert managed_scope.load_managed_config() == {"openrouter": {"zdr": False}}
    assert managed_scope.managed_config_load_degraded() is False

    config_path.unlink()
    assert managed_scope.load_managed_config() == {}
    assert managed_scope.managed_config_load_degraded() is False

    config_path.write_text("openrouter: [", encoding="utf-8")
    assert managed_scope.managed_config_load_degraded() is True
    other = tmp_path / "other-managed"
    other.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(other))
    assert managed_scope.managed_config_load_degraded() is False


