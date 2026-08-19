"""CLI /browser connect Camofox autodetection."""

from __future__ import annotations

import json
import os
from queue import Queue
from unittest.mock import patch

from hermes_cli.cli_commands_mixin import CLICommandsMixin


class _BareCLI(CLICommandsMixin):
    """Mixin-only stand-in so tests don't import the full cli.py module."""


class _FakeResponse:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def _bare_cli():
    cli = _BareCLI()
    cli._pending_input = Queue()
    return cli


def _camofox_urlopen(url, timeout=2.0):
    if url.endswith("/json/version"):
        raise OSError("not cdp")
    if url.endswith("/health"):
        return _FakeResponse(body=json.dumps({"ok": True}).encode())
    raise AssertionError(f"unexpected probe {url}")


def test_browser_connect_autodetects_camofox_on_9377(monkeypatch, capsys):
    cli = _bare_cli()
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv("CAMOFOX_URL", raising=False)
    monkeypatch.delenv("BROWSER_CONNECT_MODE", raising=False)
    monkeypatch.delenv("BROWSER_PREV_CAMOFOX_URL", raising=False)
    monkeypatch.delenv("BROWSER_PREV_CAMOFOX_SET", raising=False)

    with patch("urllib.request.urlopen", side_effect=_camofox_urlopen):
        cli._handle_browser_command("/browser connect localhost:9377")

    out = capsys.readouterr().out
    assert os.environ.get("CAMOFOX_URL") == "http://localhost:9377"
    assert os.environ.get("BROWSER_CONNECT_MODE") == "camofox"
    assert not os.environ.get("BROWSER_CDP_URL")
    assert "connected to Camofox" in out
    assert "Endpoint: http://localhost:9377" in out


def test_browser_status_reports_camofox_when_connected(monkeypatch, capsys):
    cli = _bare_cli()
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    monkeypatch.setenv("BROWSER_CONNECT_MODE", "camofox")
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)

    cli._handle_browser_command("/browser status")

    out = capsys.readouterr().out
    assert "Browser: connected to Camofox" in out
    assert "Endpoint: http://localhost:9377" in out


def test_browser_disconnect_restores_previous_camofox_url(monkeypatch, capsys):
    cli = _bare_cli()
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    monkeypatch.setenv("BROWSER_CONNECT_MODE", "camofox")
    monkeypatch.setenv("BROWSER_PREV_CAMOFOX_URL", "http://localhost:9999")
    monkeypatch.setenv("BROWSER_PREV_CAMOFOX_SET", "1")
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)

    cli._handle_browser_command("/browser disconnect")

    out = capsys.readouterr().out
    assert os.environ.get("CAMOFOX_URL") == "http://localhost:9999"
    assert not os.environ.get("BROWSER_CONNECT_MODE")
    assert not os.environ.get("BROWSER_PREV_CAMOFOX_URL")
    assert "Browser disconnected from Camofox" in out


def test_browser_connect_disconnect_preserves_identical_camofox_url(monkeypatch, capsys):
    """Connecting to the already configured Camofox URL must not clear it."""
    cli = _bare_cli()
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv("BROWSER_CONNECT_MODE", raising=False)
    monkeypatch.delenv("BROWSER_PREV_CAMOFOX_URL", raising=False)
    monkeypatch.delenv("BROWSER_PREV_CAMOFOX_SET", raising=False)

    with patch("urllib.request.urlopen", side_effect=_camofox_urlopen):
        cli._handle_browser_command("/browser connect http://localhost:9377")
    assert os.environ.get("CAMOFOX_URL") == "http://localhost:9377"
    assert os.environ.get("BROWSER_CONNECT_MODE") == "camofox"

    cli._handle_browser_command("/browser disconnect")
    assert os.environ.get("CAMOFOX_URL") == "http://localhost:9377"
    assert not os.environ.get("BROWSER_CONNECT_MODE")
    assert "Browser disconnected from Camofox" in capsys.readouterr().out
