"""The POSIX update hand-off must strip interpreter-poisoning env vars.

The Desktop spawns `scripts/desktop-update/posix.sh` detached, and the script
re-execs itself through an setsid/nohup chain before running `hermes update`
from INSTALL_ROOT's venv. Whatever environment the Electron process chain
carries is copied along the whole way. If that environment contains
__PYVENV_LAUNCHER__, PYTHONHOME, or PYTHONPATH, every interpreter under the
hand-off resolves against a foreign prefix: "Could not find platform dependent
libraries <exec_prefix>" from the shim, then ModuleNotFoundError from
venv/bin/hermes — twice per attempt, because the retry inherits the same
poison (observed 2026-08-22..24 on macOS: Desktop-initiated updates died exit
1 on both tries while terminal `hermes update` kept working).

This drives the REAL posix.sh end-to-end against a stub install root whose
`venv/bin/hermes` records the environment it received, under a deliberately
poisoned parent env, and asserts none of the poison reaches it.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "desktop-update" / "posix.sh"

requires_posix_handoff = pytest.mark.skipif(
    not (os.path.exists("/bin/bash") and os.path.exists("/usr/bin/python3")),
    reason="posix.sh detaches through /bin/bash and /usr/bin/python3",
)

POISON_VARS = {
    "__PYVENV_LAUNCHER__": "/poisoned/venv/bin/python",
    "PYTHONHOME": "/poisoned/python-home",
    "PYTHONPATH": "/poisoned/site-packages",
}


def _make_stub_install_root(tmp_path: Path) -> tuple[Path, Path]:
    """A minimal INSTALL_ROOT whose `hermes` dumps the env it was given."""
    install_root = tmp_path / "hermes-agent"
    bin_dir = install_root / "venv" / "bin"
    bin_dir.mkdir(parents=True)

    env_dump = tmp_path / "stub-env.txt"
    stub = bin_dir / "hermes"
    stub.write_text(
        "#!/bin/bash\n"
        f'env | sort > "{env_dump}"\n'
        'echo "stub update ran"\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return install_root, env_dump


def _wait_for_result(result_path: Path, timeout_s: float = 60.0) -> dict:
    """posix.sh publishes `.hermes-update-result.json` when the job settles."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if result_path.exists():
            return json.loads(result_path.read_text())
        time.sleep(0.25)
    raise AssertionError(f"update result file never appeared at {result_path}")


@requires_posix_handoff
def test_handoff_strips_interpreter_poison_before_update(tmp_path):
    assert SCRIPT.exists()

    install_root, env_dump = _make_stub_install_root(tmp_path)

    # A desktop pid already gone: the hand-off waits it out (up to 30s) and
    # proceeds as soon as `kill -0` stops finding it.
    victim = subprocess.Popen(["sleep", "0.5"])
    victim.wait()

    env = dict(os.environ)
    env.update(POISON_VARS)

    # First invocation daemonizes (re-execs detached) and returns immediately;
    # the real work happens in the re-parented orchestrator.
    subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--install-root",
            str(install_root),
            "--branch",
            "main",
            "--desktop-pid",
            str(victim.pid),
            "--no-ui",
        ],
        env=env,
        cwd=str(tmp_path),
        timeout=90,
        check=False,
    )

    result = _wait_for_result(tmp_path / ".hermes-update-result.json")

    assert result.get("ok") is True, result
    assert result.get("exit_code") == 0, result

    received: dict[str, str] = {}
    for line in env_dump.read_text().splitlines():
        key, _, value = line.partition("=")
        received[key] = value

    for key in POISON_VARS:
        assert key not in received, (
            f"{key} leaked into the `hermes update` child environment: "
            "the venv interpreter will resolve against a foreign prefix and "
            "every Desktop-initiated update dies with ModuleNotFoundError "
            "before pulling anything"
        )
