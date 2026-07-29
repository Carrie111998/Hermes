"""Hermetic coverage for the optional containerized noVNC desktop."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_DIR = REPO_ROOT / "hermes-desktop"


def test_compose_publishes_desktop_services_on_loopback_only():
    compose = yaml.safe_load(
        (DESKTOP_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    )
    desktop = compose["services"]["desktop"]

    assert set(desktop["ports"]) == {
        "127.0.0.1:5901:5901",
        "127.0.0.1:6080:6080",
    }
    assert all(port.startswith("127.0.0.1:") for port in desktop["ports"])
    assert "22" not in {port.rsplit(":", 1)[-1] for port in desktop["ports"]}


def test_configured_driver_wrapper_is_resolved(tmp_path, monkeypatch):
    from tools.computer_use import cua_backend

    driver = tmp_path / "cua-driver-docker"
    driver.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    driver.chmod(0o755)
    monkeypatch.delenv("HERMES_CUA_DRIVER_CMD", raising=False)

    with patch(
        "hermes_cli.config.load_config",
        return_value={"computer_use": {"driver_command": str(driver)}},
    ):
        assert cua_backend.resolve_cua_driver_cmd() == str(driver)


def test_driver_wrapper_uses_compose_stdio_transport(tmp_path):
    wrapper = DESKTOP_DIR / "cua-driver-docker"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "docker-args"
    docker = fake_bin / "docker"
    docker.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE_FILE"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "CAPTURE_FILE": str(capture),
    }

    manifest = subprocess.run(
        [str(wrapper), "manifest"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert manifest.returncode != 0
    assert not capture.exists()

    subprocess.run([str(wrapper), "mcp"], env=env, check=True)
    args = capture.read_text(encoding="utf-8").splitlines()
    assert args[-7:] == [
        "exec",
        "-T",
        "--user",
        "hermes",
        "desktop",
        "cua-driver-desktop",
        "mcp",
    ]
