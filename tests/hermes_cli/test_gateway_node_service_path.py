import sys
from pathlib import Path

import pytest

import hermes_constants
from hermes_cli import gateway as gateway_cli


def _install_managed_node(home: Path) -> list[Path]:
    """Create the platform-native managed Node shim plus both supported dirs."""
    directories = hermes_constants.iter_hermes_node_dirs(home)
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        node = home / "node" / "node.exe"
    else:
        node = home / "node" / "bin" / "node"
    node.write_text("", encoding="utf-8")
    if sys.platform != "win32":
        node.chmod(0o755)
    return directories


def test_managed_node_tree_prevents_ambient_node_fallback(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    directories = _install_managed_node(home)

    def unexpected_which(command):
        raise AssertionError(f"ambient PATH fallback must not run for {command}")

    monkeypatch.setattr(gateway_cli.shutil, "which", unexpected_which)

    entries = []
    gateway_cli._append_node_dir_for_service(entries, home)

    assert entries == [str(path) for path in directories if path.is_dir()]


def test_empty_managed_directories_keep_ambient_node_fallback(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    directories = hermes_constants.iter_hermes_node_dirs(home)
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

    ambient_node = tmp_path / "ambient-node" / "bin" / "node"
    monkeypatch.setattr(
        gateway_cli.shutil,
        "which",
        lambda command: str(ambient_node) if command == "node" else None,
    )

    entries = []
    gateway_cli._append_node_dir_for_service(entries, home)

    expected = [str(path) for path in directories if path.is_dir()]
    expected.append(str(ambient_node.parent))
    assert entries == expected


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable-bit semantics")
def test_non_executable_managed_node_keeps_ambient_fallback(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    managed_bin = home / "node" / "bin"
    managed_bin.mkdir(parents=True)
    managed_node = managed_bin / "node"
    managed_node.write_text("", encoding="utf-8")
    managed_node.chmod(0o644)

    ambient_node = tmp_path / "ambient-node" / "bin" / "node"
    monkeypatch.setattr(
        gateway_cli.shutil,
        "which",
        lambda command: str(ambient_node) if command == "node" else None,
    )

    entries = []
    gateway_cli._append_node_dir_for_service(entries, home)

    assert str(managed_bin) in entries
    assert str(ambient_node.parent) in entries


@pytest.mark.skipif(sys.platform == "win32", reason="systemd unit generation is POSIX-only")
def test_user_systemd_unit_is_independent_of_ambient_node_when_managed_tree_exists(
    monkeypatch, tmp_path
):
    home = tmp_path / "hermes"
    directories = _install_managed_node(home)
    ambient_nodes = iter(
        [
            tmp_path / "ambient-node-a" / "bin" / "node",
            tmp_path / "ambient-node-b" / "bin" / "node",
        ]
    )

    token = hermes_constants.set_hermes_home_override(home)
    try:
        monkeypatch.setattr(
            gateway_cli.shutil,
            "which",
            lambda command: str(next(ambient_nodes)) if command == "node" else None,
        )
        unit_a = gateway_cli.generate_systemd_unit(system=False)
        unit_b = gateway_cli.generate_systemd_unit(system=False)
    finally:
        hermes_constants.reset_hermes_home_override(token)

    assert unit_a == unit_b
    assert "ambient-node-a" not in unit_a
    assert "ambient-node-b" not in unit_a
    assert any(str(directory) in unit_a for directory in directories)


@pytest.mark.skipif(sys.platform == "win32", reason="systemd unit generation is POSIX-only")
def test_systemd_current_ignores_ambient_node_drift_when_managed_tree_exists(
    monkeypatch, tmp_path
):
    home = tmp_path / "hermes"
    _install_managed_node(home)
    unit_path = tmp_path / "hermes-gateway.service"
    ambient = {"path": tmp_path / "ambient-node-a" / "bin" / "node"}

    monkeypatch.setattr(
        gateway_cli.shutil,
        "which",
        lambda command: str(ambient["path"]) if command == "node" else None,
    )
    monkeypatch.setattr(
        gateway_cli,
        "get_systemd_unit_path",
        lambda system=False: unit_path,
    )
    monkeypatch.setattr(
        gateway_cli,
        "_sync_hermes_home_from_systemd_unit",
        lambda system=False: None,
    )

    token = hermes_constants.set_hermes_home_override(home)
    try:
        installed = gateway_cli.generate_systemd_unit(system=False)
        unit_path.write_text(installed, encoding="utf-8")
        ambient["path"] = tmp_path / "ambient-node-b" / "bin" / "node"

        assert gateway_cli.systemd_unit_is_current(system=False) is True
    finally:
        hermes_constants.reset_hermes_home_override(token)
