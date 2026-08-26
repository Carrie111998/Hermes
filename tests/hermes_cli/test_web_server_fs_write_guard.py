"""Regression tests for the fs write-text sensitive-target guard (#95306).

``POST /api/fs/write-text`` overwrote arbitrary resolvable paths with no
``_is_sensitive_path()`` check while every read surface applied it — an
authenticated dashboard session could rewrite the Hermes credential and
config stores (``auth.json``, ``.env*``, ``config.yaml``) through the spot
editor, which is the config-injection chain: a rewritten ``config.yaml``
registers an attacker-controlled MCP stdio server executed on next tool
resolution. The fix applies the read-side guard to the write surface
(minimal half; the configurable write-root allowlist is left to maintainer
discussion).
"""

import pytest

from hermes_cli import web_server

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        if previous_auth_required is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous_auth_required


@pytest.mark.parametrize(
    "sensitive_name", [".env", "auth.json", "config.yaml"]
)
def test_write_text_rejects_sensitive_targets(client, tmp_path, sensitive_name):
    target = tmp_path / sensitive_name
    target.write_text("original")

    response = client.post(
        "/api/fs/write-text", json={"path": str(target), "content": "tampered"}
    )

    assert response.status_code == 403
    assert target.read_text() == "original", (
        "the guard must fire before any staging or replace touches the file"
    )


def test_write_text_rejects_credential_directory_target(client, tmp_path):
    cred_dir = tmp_path / "mcp-tokens"
    cred_dir.mkdir()
    target = cred_dir / "server.json"
    target.write_text("{}")

    response = client.post(
        "/api/fs/write-text", json={"path": str(target), "content": "tampered"}
    )

    assert response.status_code == 403
    assert target.read_text() == "{}"


def test_write_text_rejects_sensitive_creation(client, tmp_path):
    """The guard must also block CREATING a sensitive store that does not
    exist yet (config.yaml in a fresh home) — not only overwrites."""
    target = tmp_path / "config.yaml"

    response = client.post(
        "/api/fs/write-text", json={"path": str(target), "content": "malicious: on"}
    )

    assert response.status_code == 403
    assert not target.exists()


def test_write_text_serves_ordinary_files(client, tmp_path):
    """The spot-editor flow for ordinary workspace files is unaffected."""
    target = tmp_path / "notes.md"
    target.write_text("old")

    response = client.post(
        "/api/fs/write-text", json={"path": str(target), "content": "new"}
    )

    assert response.status_code == 200
    assert target.read_text() == "new"
