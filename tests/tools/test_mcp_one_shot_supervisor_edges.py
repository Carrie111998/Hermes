"""Edge-case tests for the one-shot supervisor.

Covers the paths the basic happy-path tests don't:
- inner server crashes (non-zero exit) → supervisor must surface a JSON-RPC
  error, NOT silently truncate the response or hang
- inner executable missing → supervisor must emit a structured error
- large payload (multi-KiB) round-trips through the per-line dispatch
- blank/empty lines on stdin (MCP keep-alive probes) are skipped without
  spawning the inner server
- stdin EOF from hermes → clean supervisor shutdown, exit 0
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SUPERVISOR_PATH = PROJECT_ROOT / "tools" / "mcp_one_shot_supervisor.py"


def _run_supervisor_capture(
    inner_cmd: str,
    inner_args: list[str],
    stdin_payload: bytes,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        str(SUPERVISOR_PATH),
        "--inner-cmd",
        inner_cmd,
        "--label",
        "edge-test",
    ]
    for arg in inner_args:
        cmd.extend(["--inner-arg", arg])
    return subprocess.run(
        cmd,
        input=stdin_payload,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _one_request(req_id: int = 1) -> bytes:
    return (json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "method": "x", "params": {}}
    ) + "\n").encode("utf-8")


class TestSupervisorErrorHandling:
    """Supervisor must surface failures as structured JSON-RPC, not hang."""

    def test_inner_nonzero_exit_returns_jsonrpc_error(self, tmp_path):
        """Inner exits non-zero → supervisor emits JSON-RPC error object.

        Without this, hermes would silently see a stream close with no
        response and either hang on the deadline or surface a confusing
        "connection closed" error. The error must include the supervisor
        label so operators can correlate the failure back to the right
        server.
        """
        crash = tmp_path / "crash.py"
        crash.write_text(
            "import sys\nsys.stderr.write('boom\\n'); sys.exit(7)\n"
        )

        proc = _run_supervisor_capture(
            sys.executable, [str(crash)], _one_request(),
        )

        assert proc.returncode == 0, (
            f"supervisor itself crashed (rc={proc.returncode}); "
            f"stderr: {proc.stderr.decode('utf-8', 'replace')}"
        )
        out = proc.stdout.decode("utf-8", "replace").strip()
        assert out, "supervisor emitted no output on inner crash"
        # Must be parseable JSON-RPC, not a raw stack trace.
        err = json.loads(out.splitlines()[0])
        assert err.get("jsonrpc") == "2.0"
        assert "error" in err
        assert isinstance(err["error"]["code"], int)
        assert "edge-test" in err["error"]["message"], (
            f"label missing from error message: {err['error']['message']!r}"
        )
        assert "exit" in err["error"]["message"].lower()

    def test_inner_missing_executable_returns_jsonrpc_error(self):
        """Inner binary doesn't exist → structured error, no hang."""
        proc = _run_supervisor_capture(
            "C:/nonexistent/definitely-not-a-real-binary-12345.exe",
            [],
            _one_request(),
            timeout=10.0,
        )

        assert proc.returncode == 0, (
            f"supervisor should NOT propagate ENOENT as its own rc; "
            f"got rc={proc.returncode}"
        )
        out = proc.stdout.decode("utf-8", "replace").strip()
        assert out, "supervisor emitted no output on missing inner"
        err = json.loads(out.splitlines()[0])
        assert err.get("jsonrpc") == "2.0"
        assert "error" in err
        # The FileNotFoundError message should reach the operator.
        assert "not found" in err["error"]["message"].lower() or \
               "nonexistent" in err["error"]["message"].lower()

    def test_continues_after_inner_failure(self, tmp_path):
        """A single inner failure must NOT kill the supervisor.

        The supervisor is the long-lived outer process — if it dies on
        any inner failure, hermes has to spawn a brand-new supervisor
        (and the operator-visible session window comes back). Verify
        the loop survives: first request crashes, second request
        succeeds with id=2.
        """
        # One inner that crashes on odd ids, succeeds on even ids.
        crash_or_succeed = tmp_path / "crash_or_succeed.py"
        crash_or_succeed.write_text(
            "import json, sys\n"
            "req = json.loads(sys.stdin.buffer.read())\n"
            "rid = req.get('id')\n"
            "if rid % 2 == 1:\n"
            "    sys.exit(1)\n"
            "sys.stdout.buffer.write(\n"
            "    (json.dumps({'jsonrpc':'2.0','id':rid,"
            "'result':'ok'}) + '\\n').encode())\n"
        )

        payload = _one_request(1) + _one_request(2)
        proc = _run_supervisor_capture(
            sys.executable, [str(crash_or_succeed)], payload,
        )

        assert proc.returncode == 0
        lines = [
            json.loads(line) for line in
            proc.stdout.decode("utf-8").splitlines() if line.strip()
        ]
        # First call → JSON-RPC error, second call → result. Two
        # outputs, supervisor survived both.
        assert len(lines) == 2
        assert "error" in lines[0]
        assert lines[1]["result"] == "ok"
        assert lines[1]["id"] == 2


class TestSupervisorIO:
    """Wire-protocol edge cases."""

    def test_large_payload_round_trip(self, tmp_path):
        """Multi-KiB request → multi-KiB response, preserved bit-for-bit.

        MCP tool responses for codegraph_search easily exceed 10 KiB;
        a supervisor that truncates or mangles large payloads is
        useless in production. Verify with a response >8KiB.
        """
        big_inner = tmp_path / "big.py"
        big_inner.write_text(
            "import json, sys\n"
            "req = json.loads(sys.stdin.buffer.read())\n"
            "resp = {'jsonrpc':'2.0','id':req.get('id'),"
            "'result':{'size':len(sys.stdin.buffer.read() or b''),"
            "'data':'x'*8192}}\n"
            "sys.stdout.buffer.write((json.dumps(resp)+'\\n').encode())\n"
        )

        payload = (
            json.dumps({
                "jsonrpc": "2.0", "id": 42, "method": "x",
                "params": {"pad": "y" * 4000},
            }) + "\n"
        ).encode()
        proc = _run_supervisor_capture(
            sys.executable, [str(big_inner)], payload,
        )

        assert proc.returncode == 0
        line = proc.stdout.decode("utf-8").splitlines()[0]
        resp = json.loads(line)
        assert resp["id"] == 42
        assert len(resp["result"]["data"]) == 8192

    def test_blank_lines_are_skipped(self, tmp_path):
        """Blank lines on stdin must NOT trigger an inner spawn.

        MCP clients emit keep-alive probes as blank lines sometimes;
        the supervisor must skip them rather than spending an inner
        spawn on nothing.
        """
        log_path = tmp_path / "pids.log"
        log_path.write_text("")

        log_inner = tmp_path / "log_inner.py"
        log_inner.write_text(
            "import os, sys\n"
            f"open({str(log_path)!r}, 'a').write(f'{{os.getpid()}}\\n')\n"
            "sys.stdout.buffer.write(b'{\"jsonrpc\":\"2.0\",\"id\":1,"
            "\"result\":\"ok\"}\\n')\n"
        )

        # 5 blank lines + 1 real request.
        payload = b"\n" * 5 + _one_request(1)
        proc = _run_supervisor_capture(
            sys.executable, [str(log_inner)], payload,
        )

        assert proc.returncode == 0
        # Exactly one inner spawn for the one real request.
        pids = [
            int(line.strip()) for line in
            log_path.read_text("utf-8").splitlines() if line.strip()
        ]
        assert len(pids) == 1, (
            f"blank lines must not trigger inner spawns; got {pids!r}"
        )

    def test_unicode_payload_round_trip(self, tmp_path):
        """Unicode payloads (Chinese, emoji) must round-trip cleanly.

        Hermes operators run codegraph against repos with Unicode
        identifiers — a supervisor that mangles UTF-8 is a silent
        data-loss bug.
        """
        unicode_inner = tmp_path / "unicode.py"
        unicode_inner.write_text(
            "import json, sys\n"
            "req = json.loads(sys.stdin.buffer.read())\n"
            "resp = {'jsonrpc':'2.0','id':req.get('id'),"
            "'result':req.get('params')}\n"
            "sys.stdout.buffer.write((json.dumps(resp, "
            "ensure_ascii=False)+'\\n').encode('utf-8'))\n"
        )

        # Chinese + emoji + Japanese in a single payload.
        payload = (json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "x",
             "params": {"q": "搜索 代码图 🔍 グラフィックス", "name": "テスト"}},
            ensure_ascii=False,
        ) + "\n").encode("utf-8")

        proc = _run_supervisor_capture(
            sys.executable, [str(unicode_inner)], payload,
        )

        assert proc.returncode == 0
        line = proc.stdout.decode("utf-8").splitlines()[0]
        resp = json.loads(line)
        assert resp["result"]["q"] == "搜索 代码图 🔍 グラフィックス"
        assert resp["result"]["name"] == "テスト"

    def test_eof_on_stdin_returns_zero(self):
        """Closing hermes's stdin → supervisor exits 0, not crash.

        The supervisor is the long-lived outer process; hermes will
        close its stdin at shutdown. An exit code != 0 would fail the
        process ledger / spawn accounting on the hermes side.
        """
        # ``--inner-arg=-c`` (not ``--inner-arg -c``) — argparse treats
        # bare ``-c`` as a flag and complains "expected one argument".
        proc = subprocess.run(
            [
                sys.executable,
                str(SUPERVISOR_PATH),
                "--inner-cmd", sys.executable,
                "--inner-arg=-c",
                "--inner-arg=import sys",
                "--label", "eof",
            ],
            input=b"",
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert proc.returncode == 0, (
            f"clean stdin EOF must exit 0, got rc={proc.returncode}; "
            f"stderr: {proc.stderr.decode('utf-8', 'replace')}"
        )


class TestDetectionRules:
    """``_is_one_shot_stdio_server`` decision matrix."""

    def test_liftoff_only_marker_triggers(self):
        from tools.mcp_tool import _is_one_shot_stdio_server
        assert _is_one_shot_stdio_server({
            "command": "x",
            "args": ["--liftoff-only", "y"],
        }) is True

    def test_explicit_flag_triggers(self):
        from tools.mcp_tool import _is_one_shot_stdio_server
        assert _is_one_shot_stdio_server({
            "command": "x",
            "args": ["serve"],
            "one_shot_supervisor": True,
        }) is True

    def test_explicit_false_overrides_liftoff(self):
        """``one_shot_supervisor: false`` is a kill switch.

        Operators who want the old behavior (e.g. they're running a
        custom wrapper that already manages re-spawning) can opt out.
        """
        from tools.mcp_tool import _is_one_shot_stdio_server
        assert _is_one_shot_stdio_server({
            "command": "x",
            "args": ["--liftoff-only"],
            "one_shot_supervisor": False,
        }) is False

    def test_long_lived_server_is_not_detected(self):
        from tools.mcp_tool import _is_one_shot_stdio_server
        assert _is_one_shot_stdio_server({
            "command": "x",
            "args": ["--serve", "--port", "9999"],
        }) is False

    def test_empty_args_not_detected(self):
        from tools.mcp_tool import _is_one_shot_stdio_server
        assert _is_one_shot_stdio_server({"command": "x", "args": []}) is False

    def test_non_dict_config_is_false(self):
        from tools.mcp_tool import _is_one_shot_stdio_server
        assert _is_one_shot_stdio_server(None) is False
        assert _is_one_shot_stdio_server("config") is False