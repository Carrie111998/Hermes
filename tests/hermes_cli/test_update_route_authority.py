from __future__ import annotations

from pathlib import Path

from hermes_cli import update_cmd


def _manager(path: Path, exit_code: int) -> Path:
    path.write_text(
        "import sys\nprint('authority-manager-ran')\nsys.exit(%d)\n" % exit_code,
        encoding="utf-8",
    )
    return path


def test_update_applies_configured_route_authority(monkeypatch, tmp_path: Path):
    authority = tmp_path / "authority"
    authority.mkdir()
    manager = _manager(authority / "manage.py", 0)
    (authority / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"routing": {"authority_dir": str(authority)}},
    )

    assert update_cmd._apply_route_authority_after_update(tmp_path) is True


def test_update_stops_when_route_authority_fails(monkeypatch, tmp_path: Path):
    authority = tmp_path / "authority"
    authority.mkdir()
    _manager(authority / "manage.py", 7)
    (authority / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"routing": {"authority_dir": str(authority)}},
    )

    assert update_cmd._apply_route_authority_after_update(tmp_path) is False


def test_update_without_authority_configuration_is_unchanged(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"routing": {}})

    assert update_cmd._apply_route_authority_after_update(tmp_path) is True
