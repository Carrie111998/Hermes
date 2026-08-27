"""#93608 — port bind conflict must be machine-readable, not a bare exit 1.

The reporter's repro: something already listens on the serve port (another
``hermes serve``, the gateway, anything) → ``hermes serve`` printed only
uvicorn's ``ERROR: [Errno 98/10048] error while attempting to bind on
address`` and exited 1 — indistinguishable from a broken backend for the
desktop spawn and for scripts.

Contract under test:

* conflict → single stdout sentinel ``BACKEND_PORT_IN_USE port=<port>`` +
  a human hint line + exit code 75 (EX_TEMPFAIL, the repo's existing
  transient-condition convention) — and NO ``HERMES_BACKEND_READY``.
* free explicit port → boots and announces ``HERMES_BACKEND_READY`` exactly
  as before (contract untouched).
* ``--port 0`` (ephemeral) → probe skipped, boots, announces the
  OS-assigned port.
"""

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX serve-runner path under test"
)


# ---------------------------------------------------------------------------
# Unit: the probe itself
# ---------------------------------------------------------------------------


def test_probe_detects_held_socket():
    from hermes_cli.web_server import _port_bind_conflict

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        assert _port_bind_conflict("127.0.0.1", port) is True
    finally:
        holder.close()


def test_probe_free_port_is_clean():
    from hermes_cli.web_server import _port_bind_conflict

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert _port_bind_conflict("127.0.0.1", port) is False


def test_probe_skips_ephemeral_port_zero():
    from hermes_cli.web_server import _port_bind_conflict

    # port 0 can never conflict — must short-circuit False, never bind.
    assert _port_bind_conflict("127.0.0.1", 0) is False


def test_addr_in_use_error_classification():
    import errno

    from hermes_cli.web_server import _is_addr_in_use_error

    assert _is_addr_in_use_error(OSError(errno.EADDRINUSE, "in use")) is True
    assert _is_addr_in_use_error(OSError(98, "linux")) is True
    assert _is_addr_in_use_error(OSError(10048, "winsock")) is True
    assert _is_addr_in_use_error(OSError(errno.EACCES, "denied")) is False


def test_exit_code_is_distinct_tempfail():
    from hermes_cli.web_server import PORT_IN_USE_EXIT_CODE

    assert PORT_IN_USE_EXIT_CODE == 75  # EX_TEMPFAIL — repo convention
    assert PORT_IN_USE_EXIT_CODE != 1


# ---------------------------------------------------------------------------
# E2E: real start_server in a subprocess (temp HERMES_HOME)
# ---------------------------------------------------------------------------


def _spawn_serve(port: int, tmp_path: Path) -> subprocess.Popen:
    home = tmp_path / "hermes_home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        HERMES_HOME=str(home),
        HERMES_SERVE_HEADLESS="1",
        PYTHONUNBUFFERED="1",
    )
    # Ensure a stray desktop-parent env can't arm the watchdog/reaper paths.
    for k in ("HERMES_DESKTOP", "HERMES_PARENT_PID", "HERMES_PARENT_START_MARKER",
              "HERMES_PARENT_NONCE"):
        env.pop(k, None)
    code = (
        "from hermes_cli.web_server import start_server\n"
        f"start_server(host='127.0.0.1', port={port}, open_browser=False, "
        "headless=True)\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _read_until(proc: subprocess.Popen, token: str, timeout: float = 120.0):
    """Collect stdout lines until ``token`` appears, process exits, or timeout."""
    lines: list[str] = []
    hit = threading.Event()

    def _pump():
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if token in line:
                hit.set()
                return

    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    hit.wait(timeout)
    return hit.is_set(), lines


def test_conflict_emits_sentinel_and_exit_75(tmp_path):
    """Reporter's exact repro shape: a real held LISTEN socket on the port."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        proc = _spawn_serve(port, tmp_path)
        try:
            out, _ = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            proc.kill()
            out = ""
            pytest.fail("serve did not exit on a port conflict")
    finally:
        holder.close()

    assert proc.returncode == 75, f"exit={proc.returncode}\n{out}"
    assert f"BACKEND_PORT_IN_USE port={port}" in out
    assert f"Port {port}" in out  # human hint line
    assert "HERMES_BACKEND_READY" not in out  # never claimed ready
    # exactly one machine sentinel line
    assert out.count("BACKEND_PORT_IN_USE") == 1


def test_free_port_boots_and_announces_ready(tmp_path):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    proc = _spawn_serve(port, tmp_path)
    try:
        ready, lines = _read_until(proc, "HERMES_BACKEND_READY")
        out = "".join(lines)
        assert ready, f"no READY sentinel; output:\n{out}"
        assert f"HERMES_BACKEND_READY port={port}" in out
        assert "BACKEND_PORT_IN_USE" not in out
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_ephemeral_port_zero_unaffected(tmp_path):
    proc = _spawn_serve(0, tmp_path)
    try:
        ready, lines = _read_until(proc, "HERMES_BACKEND_READY")
        out = "".join(lines)
        assert ready, f"no READY sentinel; output:\n{out}"
        # OS-assigned non-zero port announced
        ready_line = next(l for l in lines if "HERMES_BACKEND_READY" in l)
        announced = int(ready_line.strip().rsplit("port=", 1)[1])
        assert announced > 0
        assert "BACKEND_PORT_IN_USE" not in out
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_ready_sentinel_on_real_stdout_not_stderr(tmp_path):
    """The READY sentinel must land on REAL stdout (fd 1), never stderr.

    ``start_server`` imports ``tui_gateway.server`` (via
    ``install_exit_flush_signal_handlers``), and that module's import does a
    process-global ``sys.stdout = sys.stderr`` to reserve fd 1 for ACP JSON-RPC.
    A plain ``print()`` of the sentinel would therefore land on stderr, and the
    Desktop's port-announcement resolver — which reads ``child.stdout`` (fd 1)
    only — would time out after 90s (the regression introduced by commit
    6d4e851d80 "fix(serve): bounded flush-on-SIGTERM"). This test uses SEPARATE
    stdout/stderr pipes (stderr is NOT merged into stdout) to catch that split.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    home = tmp_path / "hermes_home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        HERMES_HOME=str(home),
        HERMES_SERVE_HEADLESS="1",
        PYTHONUNBUFFERED="1",
    )
    # No HERMES_DESKTOP: this asserts the sentinel reaches fd 1 for *any*
    # serve consumer, not just desktop-spawned ones.
    for k in ("HERMES_DESKTOP", "HERMES_PARENT_PID", "HERMES_PARENT_START_MARKER",
              "HERMES_PARENT_NONCE"):
        env.pop(k, None)
    code = (
        "from hermes_cli.web_server import start_server\n"
        f"start_server(host='127.0.0.1', port={port}, open_browser=False, "
        "headless=True)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,  # SEPARATE pipe — the Desktop reads stdout only
        text=True,
    )
    try:
        assert proc.stdout is not None and proc.stderr is not None
        stdout_lines: list[str] = []
        hit = threading.Event()

        def _pump_stdout():
            for line in proc.stdout:
                stdout_lines.append(line)
                if "HERMES_BACKEND_READY" in line:
                    hit.set()
                    return

        def _drain_stderr():
            # Keep the stderr pipe from filling and blocking the child.
            for _ in proc.stderr:
                pass

        threading.Thread(target=_pump_stdout, daemon=True).start()
        threading.Thread(target=_drain_stderr, daemon=True).start()
        hit.wait(120)
        out = "".join(stdout_lines)
        assert hit.is_set(), f"READY sentinel never reached stdout; got:\n{out}"
        assert f"HERMES_BACKEND_READY port={port}" in out
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_port_in_use_sentinel_on_real_stdout(tmp_path):
    """BACKEND_PORT_IN_USE sentinel must also reach REAL stdout (fd 1).

    Same fd-1 contract as the READY sentinel: `_report_port_in_use` runs after
    `tui_gateway.server`'s import has redirected ``sys.stdout`` to stderr, so a
    plain ``print()`` would hide the conflict sentinel from the Desktop (which
    reads ``child.stdout`` only). Uses SEPARATE pipes to catch the split.
    """
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        home = tmp_path / "hermes_home"
        home.mkdir(exist_ok=True)
        env = dict(os.environ)
        env.update(
            HERMES_HOME=str(home),
            HERMES_SERVE_HEADLESS="1",
            PYTHONUNBUFFERED="1",
        )
        for k in ("HERMES_DESKTOP", "HERMES_PARENT_PID", "HERMES_PARENT_START_MARKER",
                  "HERMES_PARENT_NONCE"):
            env.pop(k, None)
        code = (
            "from hermes_cli.web_server import start_server\n"
            f"start_server(host='127.0.0.1', port={port}, open_browser=False, "
            "headless=True)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # SEPARATE pipe
            text=True,
        )
        try:
            assert proc.stdout is not None and proc.stderr is not None
            # Process self-exits 75 after reporting the conflict.
            stdout, _stderr = proc.communicate(timeout=180)
            assert proc.returncode == 75, f"exit={proc.returncode}\n{stdout}"
            assert f"BACKEND_PORT_IN_USE port={port}" in stdout
        finally:
            if proc.poll() is None:
                proc.kill()
    finally:
        holder.close()
