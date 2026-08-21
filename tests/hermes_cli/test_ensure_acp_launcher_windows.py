"""Native Windows coverage for the dedicated PATH launcher copies."""

import pytest

from hermes_cli import main as hermes_main
from hermes_cli.main import _ensure_acp_launcher

pytestmark = pytest.mark.windows_only


def test_restores_dedicated_bin_launchers(tmp_path, monkeypatch):
    scripts_dir = tmp_path / "venv" / "Scripts"
    scripts_dir.mkdir(parents=True)
    launchers = {
        "hermes.exe": b"updated hermes launcher",
        "hermes-acp.exe": b"updated acp launcher",
    }
    for name, content in launchers.items():
        (scripts_dir / name).write_bytes(content)
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)

    _ensure_acp_launcher()

    for name, content in launchers.items():
        assert (tmp_path / "bin" / name).read_bytes() == content


def test_refreshes_stale_bin_launchers(tmp_path, monkeypatch):
    scripts_dir = tmp_path / "venv" / "Scripts"
    scripts_dir.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("hermes.exe", "hermes-acp.exe"):
        (scripts_dir / name).write_bytes(f"updated {name}".encode())
        (bin_dir / name).write_bytes(b"stale launcher")
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)

    _ensure_acp_launcher()

    for name in ("hermes.exe", "hermes-acp.exe"):
        assert (bin_dir / name).read_bytes() == f"updated {name}".encode()
