"""Behaviour contracts for descriptor-bound Codex auth-store reads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hermes_cli.auth as auth
from agent.credential_pool import load_pool


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _store(token: str) -> dict:
    return {
        "version": 1,
        "providers": {
            "openai-codex": {
                "tokens": {"access_token": token, "refresh_token": f"{token}-refresh"}
            }
        },
        "credential_pool": {
            "openai-codex": [{
                "id": token,
                "label": token,
                "auth_type": "oauth",
                "priority": 0,
                "source": "device_code",
                "access_token": token,
                "refresh_token": f"{token}-refresh",
            }]
        },
    }


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "work"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    return root, profile


def test_isolated_policy_never_reads_root_codex_store(profile_env, monkeypatch):
    root, profile = profile_env
    _write(root / "auth.json", _store("root-only"))
    _write(profile / "auth.json", {"version": 1})
    (profile / "config.yaml").write_text(
        "auth:\n  isolated_providers: [openai-codex]\n", encoding="utf-8"
    )

    def root_read():
        pytest.fail("isolated Codex read consulted the root auth store")

    monkeypatch.setattr(auth, "_load_global_auth_store", root_read)
    assert auth.read_credential_pool("openai-codex") == []
    assert auth.get_provider_auth_state("openai-codex") is None
    assert load_pool("openai-codex").entries() == []


def test_invalid_policy_never_reads_root_codex_store(profile_env, monkeypatch):
    root, profile = profile_env
    _write(root / "auth.json", _store("root-only"))
    _write(profile / "auth.json", {"version": 1})
    (profile / "config.yaml").write_text("auth: invalid\n", encoding="utf-8")

    def root_read():
        pytest.fail("invalid Codex policy consulted the root auth store")

    monkeypatch.setattr(auth, "_load_global_auth_store", root_read)
    assert auth.read_credential_pool("openai-codex") == []
    assert auth.get_provider_auth_state("openai-codex") is None


def test_explicit_shared_policy_reads_only_selected_root_store(profile_env, monkeypatch):
    root, profile = profile_env
    root_store = _store("shared-root")
    root_store["credential_pool"]["openai-codex"][0]["auth_type"] = "api_key"
    _write(root / "auth.json", root_store)
    _write(profile / "auth.json", _store("profile-shadow"))
    (profile / "config.yaml").write_text(
        f"openai_codex:\n  shared_auth_store: {root / 'auth.json'}\n",
        encoding="utf-8",
    )

    loaded: list[Path | None] = []
    real_load = auth._load_auth_store

    def record_load(path=None):
        loaded.append(path)
        return real_load(path)

    monkeypatch.setattr(auth, "_load_auth_store", record_load)
    assert auth.read_credential_pool("openai-codex")[0]["access_token"] == "shared-root"
    assert auth.get_provider_auth_state("openai-codex")["tokens"]["access_token"] == "shared-root"
    assert loaded and all(path == root / "auth.json" for path in loaded)
    assert (root / "auth.lock").exists()
    assert not (profile / "auth.lock").exists()


def test_legacy_profile_read_preserves_root_fallback(profile_env):
    root, profile = profile_env
    _write(root / "auth.json", _store("legacy-root"))
    _write(profile / "auth.json", {"version": 1})

    assert auth.read_credential_pool("openai-codex")[0]["access_token"] == "legacy-root"
    assert auth.get_provider_auth_state("openai-codex")["tokens"]["access_token"] == "legacy-root"


def test_local_descriptor_never_adopts_root_singleton_or_persists_seed(profile_env):
    root, profile = profile_env
    _write(root / "auth.json", _store("root-only"))
    local_entry = _store("local-pool")["credential_pool"]["openai-codex"][0]
    _write(profile / "auth.json", {"version": 1, "credential_pool": {"openai-codex": [local_entry]}})
    store = auth.CodexAuthStore(profile / "auth.json")

    loaded = load_pool("openai-codex", codex_store=store)
    assert [entry.access_token for entry in loaded.entries()] == ["local-pool"]
    local = json.loads((profile / "auth.json").read_text(encoding="utf-8"))
    assert "openai-codex" not in local.get("providers", {})

    seed_only = profile / "seed-only" / "auth.json"
    seed_payload = _store("local-singleton")
    seed_payload.pop("credential_pool")
    _write(seed_only, seed_payload)
    seeded = load_pool("openai-codex", codex_store=auth.CodexAuthStore(seed_only))
    assert [entry.access_token for entry in seeded.entries()] == ["local-singleton"]
    assert "credential_pool" not in json.loads(seed_only.read_text(encoding="utf-8") )
