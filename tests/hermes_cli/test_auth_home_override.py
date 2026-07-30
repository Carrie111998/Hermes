from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_constants import (
    get_hermes_auth_home,
    get_hermes_auth_home_override,
    get_hermes_home,
)


_AUTH_WORKER = r"""
import json
import sys
import time
from pathlib import Path

from hermes_constants import get_hermes_home
from hermes_cli.auth import (
    _auth_store_lock,
    _load_auth_store,
    _read_shared_nous_state,
    _save_auth_store,
    _store_provider_state,
    _write_shared_nous_state,
    clear_provider_auth,
    get_provider_auth_state,
)

home = get_hermes_home()
home.mkdir(parents=True, exist_ok=True)
(home / "process.marker").write_text(home.name, encoding="utf-8")

for line in sys.stdin:
    command = json.loads(line)
    operation = command["operation"]
    if operation == "put":
        with _auth_store_lock():
            store = _load_auth_store()
            if command.get("signal_path"):
                Path(command["signal_path"]).touch()
            time.sleep(command.get("hold_lock_seconds", 0))
            _store_provider_state(
                store,
                command["provider"],
                command["state"],
                set_active=False,
            )
            _save_auth_store(store)
        result = True
    elif operation == "get":
        result = get_provider_auth_state(command["provider"])
    elif operation == "remove":
        result = clear_provider_auth(command["provider"])
    elif operation == "shared_read":
        result = _read_shared_nous_state()
    elif operation == "shared_write":
        _write_shared_nous_state(command["state"])
        result = True
    else:
        raise ValueError(operation)
    print(json.dumps(result), flush=True)
"""


def test_auth_home_defaults_to_runtime_home_and_validates_override(
    monkeypatch, tmp_path
):
    runtime_home = tmp_path / "runtime"
    residence = tmp_path / "auth-residence"
    monkeypatch.setenv("HERMES_HOME", str(runtime_home))

    assert get_hermes_auth_home_override() is None
    assert get_hermes_auth_home() == get_hermes_home() == runtime_home

    monkeypatch.setenv("HERMES_AUTH_HOME", str(residence))
    assert get_hermes_auth_home_override() == residence.resolve()
    assert get_hermes_auth_home() == residence.resolve()

    monkeypatch.setenv("HERMES_AUTH_HOME", "")
    with pytest.raises(ValueError, match="must not be empty"):
        get_hermes_auth_home()

    monkeypatch.setenv("HERMES_AUTH_HOME", "relative/auth")
    with pytest.raises(ValueError, match="absolute path"):
        get_hermes_auth_home()


def test_override_routes_current_credential_paths_and_guards(
    monkeypatch, tmp_path
):
    runtime_home = tmp_path / "runtime"
    residence = tmp_path / "auth-residence"
    global_home = tmp_path / "operator" / ".hermes"
    global_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    monkeypatch.setenv("HERMES_HOME", str(runtime_home))
    monkeypatch.setenv("HERMES_AUTH_HOME", str(residence))

    from agent.anthropic_adapter import (
        _get_hermes_oauth_file,
        read_hermes_oauth_credentials,
    )
    from agent.auxiliary_client import _auth_json_path as auxiliary_auth_json_path
    from agent.credential_sources import _remove_hermes_pkce
    from agent.file_safety import get_read_block_error, is_write_denied
    from hermes_cli.auth import (
        _auth_file_path,
        _auth_lock_path,
        _auth_store_lock,
        _global_auth_file_path,
        _load_auth_store,
        _read_shared_nous_state,
        _save_auth_store,
        _store_provider_state,
        _write_shared_nous_state,
        get_provider_auth_state,
    )
    from hermes_cli.models import _credential_fingerprint
    from hermes_cli.web_server import _save_anthropic_oauth_creds
    from plugins.platforms.photon.auth import (
        _auth_json_path as photon_auth_json_path,
        load_photon_token,
        store_photon_token,
    )
    from tools.managed_tool_gateway import auth_json_path as managed_auth_json_path
    from tools.xai_http import has_xai_credentials

    assert _auth_file_path() == residence / "auth.json"
    assert _auth_lock_path() == residence / "auth.lock"
    assert _get_hermes_oauth_file() == residence / ".anthropic_oauth.json"
    assert auxiliary_auth_json_path() == residence / "auth.json"
    assert photon_auth_json_path() == residence / "auth.json"
    assert managed_auth_json_path() == residence / "auth.json"
    assert _global_auth_file_path() is None

    for path in (
        residence / "auth.json",
        residence / "auth.lock",
        residence / ".anthropic_oauth.json",
    ):
        assert get_read_block_error(str(path)) is not None
        assert is_write_denied(str(path))

    global_home.joinpath("auth.json").write_text(
        json.dumps({"providers": {"global-only": {"value": "must-not-leak"}}}),
        encoding="utf-8",
    )
    shared_path = global_home / "shared" / "nous_auth.json"
    shared_path.parent.mkdir()
    shared_payload = json.dumps(
        {"access_token": "shared-access", "refresh_token": "shared-refresh"}
    )
    shared_path.write_text(shared_payload, encoding="utf-8")

    with _auth_store_lock():
        auth = _load_auth_store()
        _store_provider_state(
            auth,
            "xai-oauth",
            {
                "tokens": {
                    "access_token": "residence-access",
                    "refresh_token": "residence-refresh",
                }
            },
            set_active=False,
        )
        _save_auth_store(auth)
    assert get_provider_auth_state("global-only") is None
    assert has_xai_credentials()

    _write_shared_nous_state(
        {"access_token": "new-access", "refresh_token": "new-refresh"}
    )
    assert _read_shared_nous_state() is None
    assert shared_path.read_text(encoding="utf-8") == shared_payload

    store_photon_token("photon-token")
    assert load_photon_token() == "photon-token"
    assert not (runtime_home / "auth.json").exists()

    from hermes_cli.config import invalidate_env_cache
    from hermes_cli.credential_lifecycle import remove_provider_env_credential

    runtime_home.mkdir(parents=True, exist_ok=True)
    runtime_home.joinpath(".env").write_text(
        "ZAI_API_KEY=runtime-key\n",
        encoding="utf-8",
    )
    invalidate_env_cache()
    with _auth_store_lock():
        auth = _load_auth_store()
        auth.setdefault("credential_pool", {})["zai"] = [
            {
                "id": "env-entry",
                "source": "env:ZAI_API_KEY",
                "access_token": "runtime-key",
            }
        ]
        _save_auth_store(auth)
    result = remove_provider_env_credential("ZAI_API_KEY")
    assert result["found"]
    assert "zai" not in _load_auth_store().get("credential_pool", {})
    assert "ZAI_API_KEY" not in runtime_home.joinpath(".env").read_text(
        encoding="utf-8"
    )

    fingerprint = _credential_fingerprint("xai-oauth")
    (runtime_home / "auth.json").write_text("{}", encoding="utf-8")
    assert _credential_fingerprint("xai-oauth") == fingerprint
    auth_stat = (residence / "auth.json").stat()
    os.utime(
        residence / "auth.json",
        ns=(auth_stat.st_atime_ns, auth_stat.st_mtime_ns + 1_000_000),
    )
    assert _credential_fingerprint("xai-oauth") != fingerprint

    residence_oauth = residence / ".anthropic_oauth.json"
    _save_anthropic_oauth_creds(
        "residence-anthropic",
        "residence-refresh",
        1,
    )
    runtime_oauth = runtime_home / ".anthropic_oauth.json"
    runtime_oauth.write_text(
        json.dumps({"accessToken": "runtime-must-not-be-read"}),
        encoding="utf-8",
    )
    assert read_hermes_oauth_credentials()["accessToken"] == "residence-anthropic"
    result = _remove_hermes_pkce("anthropic", None)
    assert result.cleaned
    assert not residence_oauth.exists()
    assert runtime_oauth.exists()


def test_two_runtime_processes_share_only_the_auth_residence(tmp_path):
    operator_home = tmp_path / "operator"
    global_home = operator_home / ".hermes"
    global_home.mkdir(parents=True)
    global_auth = global_home / "auth.json"
    global_auth.write_text(
        json.dumps({"providers": {"global-only": {"value": "must-not-leak"}}}),
        encoding="utf-8",
    )
    shared_path = global_home / "shared" / "nous_auth.json"
    shared_path.parent.mkdir()
    shared_payload = json.dumps(
        {"access_token": "shared-access", "refresh_token": "shared-refresh"}
    )
    shared_path.write_text(shared_payload, encoding="utf-8")

    residence = tmp_path / "auth-residence"
    homes = (tmp_path / "runtime-a", tmp_path / "runtime-b")
    processes: list[subprocess.Popen[str]] = []

    def start(home: Path) -> subprocess.Popen[str]:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(operator_home),
                "HERMES_HOME": str(home),
                "HERMES_AUTH_HOME": str(residence),
                "PYTHONUNBUFFERED": "1",
            }
        )
        process = subprocess.Popen(
            [sys.executable, "-c", _AUTH_WORKER],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        return process

    def send(process: subprocess.Popen[str], payload: dict) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()

    def receive(process: subprocess.Popen[str]):
        assert process.stdout is not None
        line = process.stdout.readline()
        assert line, process.stderr.read() if process.stderr is not None else ""
        return json.loads(line)

    def request(process: subprocess.Popen[str], payload: dict):
        send(process, payload)
        return receive(process)

    first = start(homes[0])
    second = start(homes[1])
    try:
        assert request(first, {"operation": "get", "provider": "global-only"}) is None
        assert request(second, {"operation": "shared_read"}) is None
        assert request(
            first,
            {
                "operation": "shared_write",
                "state": {
                    "access_token": "must-not-write",
                    "refresh_token": "must-not-write",
                },
            },
        )

        first_has_lock = tmp_path / "first-has-lock"
        send(
            first,
            {
                "operation": "put",
                "provider": "first",
                "state": {"value": "one"},
                "hold_lock_seconds": 0.2,
                "signal_path": str(first_has_lock),
            },
        )
        deadline = time.monotonic() + 2
        while not first_has_lock.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert first_has_lock.exists()
        send(
            second,
            {
                "operation": "put",
                "provider": "second",
                "state": {"value": "two"},
            },
        )
        assert receive(first)
        assert receive(second)
        assert request(second, {"operation": "get", "provider": "first"}) == {
            "value": "one"
        }
        assert request(first, {"operation": "get", "provider": "second"}) == {
            "value": "two"
        }

        assert request(second, {"operation": "remove", "provider": "first"})
        assert request(first, {"operation": "get", "provider": "first"}) is None
    finally:
        for process in processes:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=10)
            assert process.returncode == 0, (
                process.stderr.read() if process.stderr is not None else ""
            )

    persisted = json.loads((residence / "auth.json").read_text(encoding="utf-8"))
    assert persisted["providers"]["second"] == {"value": "two"}
    assert "first" not in persisted["providers"]
    assert not any((home / "auth.json").exists() for home in homes)
    assert all((home / "process.marker").is_file() for home in homes)
    assert global_auth.read_text(encoding="utf-8") == json.dumps(
        {"providers": {"global-only": {"value": "must-not-leak"}}}
    )
    assert shared_path.read_text(encoding="utf-8") == shared_payload
    if os.name != "nt":
        assert (residence / "auth.json").stat().st_mode & 0o777 == 0o600
