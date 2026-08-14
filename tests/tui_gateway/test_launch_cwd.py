from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_tui_entry_pins_local_launch_cwd_before_config_reload(tmp_path):
    home = tmp_path / "hermes"
    launch = tmp_path / "launch-project"
    configured = tmp_path / "configured-workspace"
    for path in (home, launch, configured):
        path.mkdir()
    (home / "config.yaml").write_text(
        "terminal:\n"
        "  backend: local\n"
        f"  cwd: {configured.as_posix()}\n",
        encoding="utf-8",
    )

    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.update(
        HERMES_HOME=str(home),
        HERMES_CWD=str(launch),
        HERMES_PYTHON_SRC_ROOT=str(repo_root),
        TERMINAL_ENV="local",
        TERMINAL_CWD=str(configured),
    )
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{repo_root}{os.pathsep}{pythonpath}" if pythonpath else str(repo_root)
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys; import tui_gateway.entry; "
            "print('TUI_CWD=' + os.environ['TERMINAL_CWD'], file=sys.stderr)",
        ],
        cwd=launch,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    assert f"TUI_CWD={launch}" in result.stderr.splitlines()
