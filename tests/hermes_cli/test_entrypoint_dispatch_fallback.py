"""Behavior tests for the wrapped-runtime Docker entrypoint."""

import os
from pathlib import Path
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DISPATCHER = REPO_ROOT / "docker" / "entrypoint-dispatch.sh"


@pytest.mark.parametrize(
    "argv",
    [
        ["gateway"],
        ["gateway", "run"],
        ["--profile", "work", "gateway", "run"],
        ["-p", "work", "gateway"],
        ["--profile=work", "gateway", "run"],
        ["hermes", "gateway", "run"],
        ["--accept-hooks", "gateway", "run"],
        ["gateway", "--accept-hooks", "run"],
        ["gateway", "--accept-hooks"],
        ["--profile", "work", "gateway", "--accept-hooks"],
        ["chat"],
    ],
)
def test_non_pid_one_dispatch_binds_direct_process_slot(tmp_path, argv):
    """The wrapper keeps argv and receives authority bound to its exec PID."""
    stage2 = tmp_path / "stage2.sh"
    stage2.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stage2.chmod(0o755)

    record = tmp_path / "record.txt"
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$HERMES_GATEWAY_EXTERNAL_SUPERVISOR_PID" > "$RECORD"\n'
        'printf "%s\\n" "$$" >> "$RECORD"\n'
        'printf "%s\\n" "$@" >> "$RECORD"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    env = os.environ.copy()
    env.pop("HERMES_GATEWAY_EXTERNAL_SUPERVISOR", None)
    env.pop("HERMES_GATEWAY_EXTERNAL_SUPERVISOR_PID", None)
    env.update(
        {
            "HERMES_ENTRYPOINT_STAGE2": str(stage2),
            "HERMES_ENTRYPOINT_WRAPPER": str(wrapper),
            "RECORD": str(record),
        }
    )
    result = subprocess.run(
        ["sh", str(DISPATCHER), *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = record.read_text(encoding="utf-8").splitlines()
    assert lines[0] == lines[1]
    assert lines[2:] == argv
