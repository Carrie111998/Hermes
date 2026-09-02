"""`hermes status --deep` must probe the real gateway control plane (#101195).

The deep check used to dial TCP 127.0.0.1:18789 and label the result a
gateway-port check, but no Hermes process has ever bound that port — the
gateway's control plane is a Unix domain socket / named pipe
(``gateway/control_socket.py``). That made the line wrong in both directions:

* false positive — any unrelated listener on 18789 (OpenClaw's default, or
  this repo's own ``hermes meet node run --port 18789``) read as "gateway
  likely running";
* false negative — a healthy gateway on the pipe always read as "available".

#101195 is the false positive reaching a user: they saw ``Port 18789: in
use``, found OpenClaw bound there, and filed the desktop hang as "the client
connects to the wrong endpoint (TCP 18789 vs named pipe)".
"""

import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import gateway.control_socket as control_socket
from hermes_cli.status import show_status


@pytest.fixture()
def deep_home(monkeypatch, tmp_path: Path) -> Path:
    """An isolated HERMES_HOME with the network-touching deep checks disabled."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # The OpenRouter deep check is a live HTTP call; keep it out of the test.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return home


def _deep_output(capsys) -> str:
    show_status(SimpleNamespace(all=False, deep=True))
    return capsys.readouterr().out


def test_deep_check_never_reports_the_phantom_18789_port(monkeypatch, capsys, deep_home):
    """An unrelated listener on 18789 must not surface in the gateway status.

    Binding the port is the exact #101195 environment. Skipped rather than
    failed when the port is already taken on the host — the assertion below
    is about what Hermes prints, not about who owns the port.
    """
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        try:
            squatter.bind(("127.0.0.1", 18789))
            squatter.listen(1)
        except OSError:
            pytest.skip("127.0.0.1:18789 is unavailable on this host")

        monkeypatch.setattr(control_socket, "identify_gateway", lambda home, **kw: None)
        output = _deep_output(capsys)
    finally:
        squatter.close()

    assert "18789" not in output


def test_deep_check_names_the_real_control_endpoint(monkeypatch, capsys, deep_home):
    """The endpoint is printed so nobody has to guess the transport."""
    monkeypatch.setattr(control_socket, "identify_gateway", lambda home, **kw: None)

    output = _deep_output(capsys)

    endpoint_line = next(ln for ln in output.splitlines() if ln.strip().startswith("Endpoint:"))
    if sys.platform == "win32":
        assert r"\\.\pipe\hermes-gateway-" in endpoint_line
    else:
        assert endpoint_line.strip().endswith("gateway.sock")
        assert str(deep_home) in endpoint_line


def test_deep_check_reports_no_answer_when_nothing_serves(monkeypatch, capsys, deep_home):
    monkeypatch.setattr(control_socket, "identify_gateway", lambda home, **kw: None)

    output = _deep_output(capsys)

    control_line = next(ln for ln in output.splitlines() if ln.strip().startswith("Control:"))
    assert "no answer" in control_line


def test_deep_check_reports_answering_when_the_control_plane_replies(monkeypatch, capsys, deep_home):
    """A well-formed `identify` answer IS liveness, per control_socket.py."""
    seen: list[Path] = []

    def _identify(home, **kwargs):
        seen.append(Path(home))
        return {"pid": 4242, "profile": "default", "protocol": 1}

    monkeypatch.setattr(control_socket, "identify_gateway", _identify)

    output = _deep_output(capsys)

    control_line = next(ln for ln in output.splitlines() if ln.strip().startswith("Control:"))
    assert "answering" in control_line
    # The probe must be scoped to the active HERMES_HOME, not a global default:
    # the endpoint is derived from a hash of that path.
    assert seen and seen[0].resolve() == deep_home.resolve()


def test_deep_check_reports_a_failed_probe_instead_of_going_quiet(monkeypatch, capsys, deep_home):
    """A broken probe must say so — a silent diagnostic is what caused #101195."""

    def _boom(home, **kwargs):
        raise RuntimeError("pipe handle exhausted")

    monkeypatch.setattr(control_socket, "identify_gateway", _boom)

    output = _deep_output(capsys)

    control_line = next(ln for ln in output.splitlines() if ln.strip().startswith("Control:"))
    assert "probe failed" in control_line
    assert "pipe handle exhausted" in control_line


def test_status_source_holds_no_hardcoded_gateway_port():
    """Source guard: the phantom port must not creep back in as live code.

    Token-scoped rather than a text search — the comment above the
    replacement explains the old probe and legitimately names the port.
    """
    import ast

    source = Path(__file__).resolve().parents[2] / "hermes_cli" / "status.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    literals = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == 18789
    ]

    assert not literals
