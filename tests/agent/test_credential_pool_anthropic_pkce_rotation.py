"""Regression tests for Hermes-managed Anthropic PKCE rotation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.credential_pool import (
    AUTH_TYPE_OAUTH,
    STATUS_EXHAUSTED,
    CredentialPool,
    PooledCredential,
    _upsert_entry,
)


def _pkce_entry(**overrides):
    values = {
        "provider": "anthropic",
        "id": "pkce-1",
        "label": "Hermes PKCE",
        "auth_type": AUTH_TYPE_OAUTH,
        "priority": 0,
        "source": "hermes_pkce",
        "access_token": "fresh-access",
        "refresh_token": "fresh-refresh",
        "expires_at_ms": 2_000,
    }
    values.update(overrides)
    return PooledCredential(**values)


def test_singleton_seed_cannot_regress_rotated_pkce_token():
    entry = _pkce_entry(
        last_status=STATUS_EXHAUSTED,
        last_status_at=10.0,
        last_error_code=401,
        last_error_reason="token_revoked",
    )
    entries = [entry]

    changed = _upsert_entry(
        entries,
        "anthropic",
        "hermes_pkce",
        {
            "source": "hermes_pkce",
            "auth_type": AUTH_TYPE_OAUTH,
            "access_token": "stale-access",
            "refresh_token": "stale-refresh",
            "expires_at_ms": 2_000,
        },
    )

    assert changed is False
    assert entries == [entry]


def test_newer_singleton_token_replaces_exhausted_pkce_entry():
    entries = [
        _pkce_entry(
            expires_at_ms=2_000,
            last_status=STATUS_EXHAUSTED,
            last_error_code=401,
        )
    ]

    changed = _upsert_entry(
        entries,
        "anthropic",
        "hermes_pkce",
        {
            "source": "hermes_pkce",
            "auth_type": AUTH_TYPE_OAUTH,
            "access_token": "new-login-access",
            "refresh_token": "new-login-refresh",
            "expires_at_ms": 4_000,
        },
    )

    assert changed is True
    assert entries[0].refresh_token == "new-login-refresh"
    assert entries[0].expires_at_ms == 4_000
    assert entries[0].last_status is None
    assert entries[0].last_error_code is None


def test_pkce_refresh_persists_rotated_singleton(tmp_path, monkeypatch):
    oauth_file = tmp_path / ".anthropic_oauth.json"
    entry = _pkce_entry(expires_at_ms=1_000)
    pool = CredentialPool("anthropic", [entry])

    monkeypatch.setattr(
        "agent.anthropic_adapter.refresh_anthropic_oauth_pure",
        lambda refresh_token, *, use_json: {
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "expires_at_ms": 3_000,
        },
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter._get_hermes_oauth_file", lambda: oauth_file
    )
    monkeypatch.setattr(pool, "_persist", lambda **_kwargs: None)

    updated = pool._refresh_entry_impl(entry, force=True)

    assert updated is not None
    assert updated.refresh_token == "rotated-refresh"
    assert json.loads(oauth_file.read_text(encoding="utf-8")) == {
        "accessToken": "rotated-access",
        "refreshToken": "rotated-refresh",
        "expiresAt": 3_000,
    }


def test_terminal_refresh_failure_adopts_newer_canonical_row(monkeypatch):
    stale = _pkce_entry(
        access_token="stale-access",
        refresh_token="stale-refresh",
        expires_at_ms=1_000,
    )
    winner = _pkce_entry(
        access_token="winner-access",
        refresh_token="winner-refresh",
        expires_at_ms=3_000,
        last_status=STATUS_EXHAUSTED,
    )
    pool = CredentialPool("anthropic", [stale])

    def fail_refresh(_refresh_token, *, use_json):
        raise ValueError("invalid_grant")

    monkeypatch.setattr(
        "agent.anthropic_adapter.refresh_anthropic_oauth_pure", fail_refresh
    )
    monkeypatch.setattr(
        "agent.credential_pool.read_credential_pool", lambda _provider: [winner.to_dict()]
    )
    monkeypatch.setattr(pool, "_persist", lambda **_kwargs: None)

    updated = pool._refresh_entry_impl(stale, force=True)

    assert updated is not None
    assert updated.refresh_token == "winner-refresh"
    assert updated.last_status == "ok"


def test_singleton_seed_collapses_legacy_dashboard_mirror(monkeypatch):
    from agent.credential_pool import _seed_from_singletons

    legacy = _pkce_entry(
        id="dashboard",
        source="manual:dashboard_pkce",
        access_token="rotated-access",
        refresh_token="rotated-refresh",
        expires_at_ms=3_000,
    )
    entries = [legacy]
    written = []

    monkeypatch.setattr(
        "hermes_cli.auth.is_provider_explicitly_configured", lambda _provider: True
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.read_hermes_oauth_credentials",
        lambda: {
            "accessToken": "stale-access",
            "refreshToken": "stale-refresh",
            "expiresAt": 2_000,
        },
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.read_claude_code_credentials", lambda: None
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter._write_hermes_oauth_credentials",
        lambda access, refresh, expires: written.append((access, refresh, expires)),
    )

    changed, active_sources = _seed_from_singletons("anthropic", entries)

    assert changed is True
    assert active_sources == {"hermes_pkce"}
    assert len(entries) == 1
    assert entries[0].source == "hermes_pkce"
    assert entries[0].access_token == "rotated-access"
    assert entries[0].refresh_token == "rotated-refresh"
    assert written == [("rotated-access", "rotated-refresh", 3_000)]


_CHILD_REFRESH = r"""
import json
import os
import time
import urllib.request
from pathlib import Path

from agent import anthropic_adapter
from agent.credential_pool import load_pool

endpoint = os.environ["FAKE_REFRESH_ENDPOINT"]
ready = Path(os.environ["READY_FILE"])
barrier = Path(os.environ["BARRIER_FILE"])


def fake_refresh(refresh_token, *, use_json):
    request = urllib.request.Request(
        endpoint,
        data=json.dumps({"refresh_token": refresh_token}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


anthropic_adapter.refresh_anthropic_oauth_pure = fake_refresh
pool = load_pool("anthropic")
entry = next(item for item in pool.entries() if item.source == "hermes_pkce")
ready.write_text("ready", encoding="utf-8")
while not barrier.exists():
    time.sleep(0.01)
updated = pool._refresh_entry(entry, force=True)
print(json.dumps({
    "access_token": updated.access_token if updated else None,
    "refresh_token": updated.refresh_token if updated else None,
    "last_status": updated.last_status if updated else None,
}))
"""


def test_two_processes_share_one_pkce_refresh_authority(tmp_path, monkeypatch):
    """Only one process redeems a rotating grant; the waiter adopts it."""
    from agent.anthropic_adapter import _write_hermes_oauth_credentials
    from agent.credential_pool import read_credential_pool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    initial = _pkce_entry(
        access_token="stale-access",
        refresh_token="stale-refresh",
        expires_at_ms=0,
    )
    CredentialPool("anthropic", [initial])._persist()
    _write_hermes_oauth_credentials("stale-access", "stale-refresh", 0)

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            calls.append(payload["refresh_token"])
            body = json.dumps(
                {
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                    "expires_at_ms": 4_000_000_000_000,
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    barrier = tmp_path / "release-workers"
    ready_files = [tmp_path / f"worker-{index}.ready" for index in range(2)]
    env_base = os.environ.copy()
    env_base["FAKE_REFRESH_ENDPOINT"] = (
        f"http://127.0.0.1:{server.server_address[1]}/refresh"
    )
    env_base["BARRIER_FILE"] = str(barrier)
    processes = []
    try:
        for ready in ready_files:
            env = dict(env_base)
            env["READY_FILE"] = str(ready)
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", _CHILD_REFRESH],
                    cwd=Path(__file__).parents[2],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )

        deadline = time.monotonic() + 10
        while not all(path.exists() for path in ready_files):
            assert time.monotonic() < deadline, "workers did not reach refresh barrier"
            time.sleep(0.02)
        barrier.write_text("go", encoding="utf-8")
        outputs = [process.communicate(timeout=15) for process in processes]
    finally:
        server.shutdown()
        server.server_close()
        for process in processes:
            if process.poll() is None:
                process.kill()

    assert [(process.returncode, stderr) for process, (_, stderr) in zip(processes, outputs)] == [
        (0, ""),
        (0, ""),
    ]
    results = [json.loads(stdout) for stdout, _ in outputs]
    assert calls == ["stale-refresh"]
    assert {result["refresh_token"] for result in results} == {"rotated-refresh"}
    assert {result["last_status"] for result in results} == {"ok"}

    persisted = next(
        item for item in read_credential_pool("anthropic") if item["id"] == initial.id
    )
    singleton = json.loads((tmp_path / ".anthropic_oauth.json").read_text("utf-8"))
    assert persisted["refresh_token"] == "rotated-refresh"
    assert singleton == {
        "accessToken": "rotated-access",
        "refreshToken": "rotated-refresh",
        "expiresAt": 4_000_000_000_000,
    }
