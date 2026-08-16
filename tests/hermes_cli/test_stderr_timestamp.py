"""Tests for hermes_cli.stderr_timestamp."""

import json
import re
import sys

from hermes_cli import stderr_timestamp


def test_main_timestamps_each_stderr_line(tmp_path):
    log_path = tmp_path / "gateway.error.log"
    code = (
        "import sys\n"
        "sys.stderr.write('first failure\\n')\n"
        "sys.stderr.write('second failure without newline\\n')\n"
        "sys.stderr.write('2026-07-15 12:34:56,789 already timestamped')\n"
        "sys.exit(7)\n"
    )

    rc = stderr_timestamp.main(
        [
            "--error-log",
            str(log_path),
            "--",
            sys.executable,
            "-c",
            code,
        ]
    )

    assert rc == 7
    lines = log_path.read_text(encoding="utf-8").splitlines()
    timestamp = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}"
    assert len(lines) == 3
    assert re.fullmatch(f"{timestamp} first failure", lines[0])
    assert re.fullmatch(f"{timestamp} second failure without newline", lines[1])
    assert lines[2] == "2026-07-15 12:34:56,789 already timestamped"


def test_main_exports_supervisor_marker_to_grandchild(tmp_path, monkeypatch):
    """launchd's XPC_SERVICE_NAME must survive the wrapper into the grandchild.

    launchd sets the job label only on its DIRECT child (this wrapper). The
    real gateway runs as a grandchild where macOS 26 reports the
    interactive-shell sentinel "0" — losing the supervisor marker wedges the
    gateway's self-conflict guard into a KeepAlive respawn loop. The wrapper
    must forward the marker via HERMES_GATEWAY_EXTERNAL_SUPERVISOR.
    """
    log_path = tmp_path / "gateway.error.log"
    out_path = tmp_path / "grandchild_env.json"
    code = (
        "import json, os\n"
        f"open({str(out_path)!r}, 'w').write("
        "json.dumps({k: os.environ.get(k) for k in "
        "['XPC_SERVICE_NAME', 'HERMES_GATEWAY_EXTERNAL_SUPERVISOR']}))\n"
    )

    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.hermes.gateway")
    monkeypatch.delenv("HERMES_GATEWAY_EXTERNAL_SUPERVISOR", raising=False)

    rc = stderr_timestamp.main(
        [
            "--error-log",
            str(log_path),
            "--",
            sys.executable,
            "-c",
            code,
        ]
    )

    assert rc == 0
    env = json.loads(out_path.read_text(encoding="utf-8"))
    assert env["HERMES_GATEWAY_EXTERNAL_SUPERVISOR"] == "1"


def test_main_leaves_shell_sentinel_alone(tmp_path, monkeypatch):
    """Interactive shells (XPC sentinel "0") must NOT gain a supervisor marker."""
    log_path = tmp_path / "gateway.error.log"
    out_path = tmp_path / "grandchild_env.json"
    code = (
        "import json, os\n"
        f"open({str(out_path)!r}, 'w').write("
        "json.dumps({'HERMES_GATEWAY_EXTERNAL_SUPERVISOR': "
        "os.environ.get('HERMES_GATEWAY_EXTERNAL_SUPERVISOR')}))\n"
    )

    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    monkeypatch.delenv("HERMES_GATEWAY_EXTERNAL_SUPERVISOR", raising=False)

    rc = stderr_timestamp.main(
        [
            "--error-log",
            str(log_path),
            "--",
            sys.executable,
            "-c",
            code,
        ]
    )

    assert rc == 0
    env = json.loads(out_path.read_text(encoding="utf-8"))
    assert env["HERMES_GATEWAY_EXTERNAL_SUPERVISOR"] is None
