"""
Tests for the Termux Gateway Keep-Alive skill (skills/devops/termux-gateway-keepalive).

Covers:
- Keep-alive status retrieval and mock gateway checks
- Self-check liveness and state freshness validation
- Script installation helper
- Shell script syntax verification
- CLI subprocess commands
"""

import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "devops" / "termux-gateway-keepalive"
SCRIPTS_DIR = SKILL_DIR / "scripts"
CLI_SCRIPT = SCRIPTS_DIR / "keepalive_cli.py"

# Import module directly
sys.path.insert(0, str(SCRIPTS_DIR))
import keepalive_cli


class TestTermuxGatewayKeepaliveCore:
    def test_get_keepalive_status_missing(self, tmp_path):
        with patch.object(keepalive_cli, "HERMES_HOME", tmp_path):
            st = keepalive_cli.get_keepalive_status()
            assert st["gateway"]["alive"] is False
            assert st["watchdog"]["running"] is False
            assert st["state_file"]["exists"] is False

    def test_run_selfcheck_unhealthy(self, tmp_path):
        with patch.object(keepalive_cli, "HERMES_HOME", tmp_path):
            res = keepalive_cli.run_selfcheck()
            assert res["healthy"] is False
            assert len(res["issues"]) >= 2

    def test_run_selfcheck_healthy(self, tmp_path):
        state_file = tmp_path / "gateway_state.json"
        state_file.write_text(
            json.dumps({"platforms": {"telegram": {"state": "connected"}}}),
            encoding="utf-8",
        )
        with patch.object(keepalive_cli, "HERMES_HOME", tmp_path), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "12345"

            res = keepalive_cli.run_selfcheck()
            assert res["healthy"] is True
            assert len(res["issues"]) == 0

    def test_install_scripts(self, tmp_path):
        target_dir = tmp_path / "scripts"
        keepalive_cli.install_scripts(target_dir)

        expected = [
            "gateway_watchdog.sh",
            "gateway_monitor.sh",
            "telegram_selfcheck.sh",
            "presence_notify.sh",
            "termux_presence.py",
        ]
        for name in expected:
            installed = target_dir / name
            assert installed.exists()
            assert installed.stat().st_mode & 0o111  # executable

    def test_bash_syntax(self):
        scripts = list(SCRIPTS_DIR.glob("*.sh"))
        assert len(scripts) >= 4
        for s in scripts:
            res = subprocess.run(["bash", "-n", str(s)], capture_output=True, text=True)
            assert res.returncode == 0, f"Syntax error in {s.name}: {res.stderr}"


class TestTermuxGatewayKeepaliveCLI:
    def test_cli_status_json(self):
        res = subprocess.run(
            [sys.executable, str(CLI_SCRIPT), "status", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(res.stdout)
        assert "gateway" in data
        assert "watchdog" in data
        assert "state_file" in data

    def test_cli_selfcheck_json(self):
        res = subprocess.run(
            [sys.executable, str(CLI_SCRIPT), "selfcheck", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(res.stdout)
        assert "healthy" in data
        assert "issues" in data
