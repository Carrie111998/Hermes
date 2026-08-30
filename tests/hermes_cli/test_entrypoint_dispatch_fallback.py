"""Behavior tests for the wrapped-runtime Docker entrypoint."""

import os
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
DISPATCHER = REPO_ROOT / "docker" / "entrypoint-dispatch.sh"


def test_non_pid_one_dispatch_marks_external_supervisor(tmp_path):
    """The direct-bootstrap fallback must identify its supervising runtime."""
    stage2 = tmp_path / "stage2.sh"
    stage2.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stage2.chmod(0o755)

    record = tmp_path / "record.txt"
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$HERMES_GATEWAY_EXTERNAL_SUPERVISOR" > "$RECORD"\n'
        'printf "%s\\n" "$@" >> "$RECORD"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    env = os.environ.copy()
    env.pop("HERMES_GATEWAY_EXTERNAL_SUPERVISOR", None)
    env.update(
        {
            "HERMES_ENTRYPOINT_STAGE2": str(stage2),
            "HERMES_ENTRYPOINT_WRAPPER": str(wrapper),
            "RECORD": str(record),
        }
    )
    result = subprocess.run(
        ["sh", str(DISPATCHER), "gateway", "run"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert record.read_text(encoding="utf-8").splitlines() == [
        "1",
        "gateway",
        "run",
    ]