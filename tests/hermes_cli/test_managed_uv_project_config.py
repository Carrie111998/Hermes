"""Real-uv regression for managed runtime project-config discovery."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def test_candidate_sync_uses_checkout_tool_uv_config(tmp_path, monkeypatch):
    """Locked sync must discover checkout-local ``[tool.uv]`` settings.

    ``exclude-newer`` is recorded in ``uv.lock``. Ignoring the project's
    configuration, or inheriting an explicit ambient config file, makes a real
    ``uv sync --locked`` reject that otherwise-current lockfile.
    """
    uv_bin = shutil.which("uv")
    if uv_bin is None:
        pytest.skip("real uv binary is required for config-discovery regression")

    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        """\
[project]
name = "managed-uv-config-fixture"
version = "0.0.0"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
all = []

[tool.uv]
exclude-newer = "2020-01-01T00:00:00Z"
""",
        encoding="utf-8",
    )

    real_run = subprocess.run
    lock_env = dict(os.environ)
    lock_env.pop("UV_CONFIG_FILE", None)
    lock_env.pop("UV_NO_CONFIG", None)
    locked = real_run(
        [uv_bin, "lock"],
        cwd=root,
        env=lock_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert locked.returncode == 0, locked.stderr

    poison = tmp_path / "ambient-uv.toml"
    poison.write_text(
        'exclude-newer = "2019-01-01T00:00:00Z"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("UV_CONFIG_FILE", str(poison))
    monkeypatch.setenv("UV_NO_CONFIG", "1")

    generation = root / ".hermes-runtime" / "python" / "generation-test"
    generation.mkdir(parents=True)

    def run_with_real_sync(cmd, **kwargs):
        if len(cmd) > 1 and cmd[1] == "venv":
            # The config-discovery contract belongs to the locked sync. Build
            # its candidate with the test interpreter so CI does not need a
            # separately downloaded managed Python generation.
            candidate = Path(cmd[2])
            return real_run(
                [
                    uv_bin,
                    "venv",
                    str(candidate),
                    "--python",
                    sys.executable,
                    "--no-python-downloads",
                    "--no-config",
                ],
                cwd=kwargs["cwd"],
                env=kwargs["env"],
                capture_output=True,
                text=True,
                check=False,
            )
        return real_run(cmd, **kwargs)

    from hermes_cli.managed_uv import _stage_candidate_venv

    with patch(
        "hermes_cli.managed_uv.subprocess.run", side_effect=run_with_real_sync
    ), patch(
        "hermes_cli.managed_uv._smoke_candidate_venv",
        return_value=(True, "", None),
    ):
        candidate = _stage_candidate_venv(
            uv_bin,
            project_root=root,
            generation=generation,
            python=Path(sys.executable),
        )

    assert candidate is not None
    assert (candidate / "pyvenv.cfg").is_file()
