"""Relay config reads must not bootstrap the messaging gateway."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_segments_config_does_not_mutate_cli_environment(tmp_path):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    project = tmp_path / "project"
    project.mkdir()

    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["PYTHONPATH"] = str(Path(__file__).parents[2])
    for key in ("HERMES_QUIET", "TERMINAL_CWD", "_HERMES_GATEWAY"):
        env.pop(key, None)

    probe = """
import os
import sys
from agent import relay_runtime

assert relay_runtime._segments_config() == {
    "on_compaction": False,
    "max_turns": 0,
}
assert "gateway.run" not in sys.modules
assert "HERMES_QUIET" not in os.environ
assert "TERMINAL_CWD" not in os.environ
assert "_HERMES_GATEWAY" not in os.environ
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
