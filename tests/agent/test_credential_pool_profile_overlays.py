"""Profile-local singleton/env seeds stay runtime-only over the shared pool."""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent import credential_pool as CP


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _profile_layout(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    profile_a = root / "profiles" / "a"
    profile_b = root / "profiles" / "b"
    profile_a.mkdir(parents=True)
    profile_b.mkdir(parents=True)
    return root, profile_a, profile_b


def test_same_env_source_is_profile_local_overlay_not_shared_row(
    tmp_path, monkeypatch
):
    root, profile_a, profile_b = _profile_layout(tmp_path, monkeypatch)
    root_path = root / "auth.json"
    _write(
        root_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openrouter": [
                    {
                        "id": "shared-manual",
                        "label": "shared",
                        "source": "manual",
                        "auth_type": "api_key",
                        "priority": 0,
                        "access_token": "shared-manual-key",
                    },
                    {
                        "id": "legacy-env-ref",
                        "label": "OPENROUTER_API_KEY",
                        "source": "env:OPENROUTER_API_KEY",
                        "auth_type": "api_key",
                        "priority": 1,
                        "secret_fingerprint": "sha256:legacy-reference",
                    },
                ]
            },
        },
    )
    profile_keys = {
        str(profile_a): "profile-a-key",
        str(profile_b): "profile-b-key",
    }

    def profile_env_value(key: str) -> str:
        if key != "OPENROUTER_API_KEY":
            return ""
        return profile_keys[os.environ["HERMES_HOME"]]

    monkeypatch.setattr(CP, "get_env_prefer_dotenv", profile_env_value)

    monkeypatch.setenv("HERMES_HOME", str(profile_a))
    pool_a = CP.load_pool("openrouter")
    env_a = next(e for e in pool_a.entries() if e.source.startswith("env:"))

    monkeypatch.setenv("HERMES_HOME", str(profile_b))
    pool_b = CP.load_pool("openrouter")
    env_b = next(e for e in pool_b.entries() if e.source.startswith("env:"))

    assert env_a.access_token == "profile-a-key"
    assert env_b.access_token == "profile-b-key"
    assert env_a.id != "legacy-env-ref"
    assert env_b.id != "legacy-env-ref"
    stored = json.loads(root_path.read_text(encoding="utf-8"))
    rows = stored["credential_pool"]["openrouter"]
    assert next(row for row in rows if row["id"] == "shared-manual")[
        "access_token"
    ] == "shared-manual-key"
    assert next(row for row in rows if row["id"] == "legacy-env-ref")[
        "secret_fingerprint"
    ] == "sha256:legacy-reference"


def test_same_singleton_source_is_profile_local_overlay_not_shared_row(
    tmp_path, monkeypatch
):
    root, profile_a, profile_b = _profile_layout(tmp_path, monkeypatch)
    root_path = root / "auth.json"
    _write(
        root_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "legacy-singleton-seed",
                        "label": "legacy",
                        "source": "device_code",
                        "auth_type": "oauth",
                        "priority": 0,
                        "access_token": "legacy-access",
                        "refresh_token": "legacy-refresh",
                    },
                    {
                        "id": "shared-account",
                        "label": "shared",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "priority": 1,
                        "access_token": "shared-access",
                        "refresh_token": "shared-refresh",
                    },
                ]
            },
        },
    )
    for profile, suffix in ((profile_a, "a"), (profile_b, "b")):
        _write(
            profile / "auth.json",
            {
                "version": 1,
                "providers": {
                    "openai-codex": {
                        "tokens": {
                            "access_token": f"profile-{suffix}-access",
                            "refresh_token": f"profile-{suffix}-refresh",
                        }
                    }
                },
            },
        )
    monkeypatch.setattr(CP, "get_env_prefer_dotenv", lambda _key: "")

    monkeypatch.setenv("HERMES_HOME", str(profile_a))
    pool_a = CP.load_pool("openai-codex")
    singleton_a = next(e for e in pool_a.entries() if e.source == "device_code")

    monkeypatch.setenv("HERMES_HOME", str(profile_b))
    pool_b = CP.load_pool("openai-codex")
    singleton_b = next(e for e in pool_b.entries() if e.source == "device_code")

    assert singleton_a.access_token == "profile-a-access"
    assert singleton_b.access_token == "profile-b-access"
    assert singleton_a.id != "legacy-singleton-seed"
    assert singleton_b.id != "legacy-singleton-seed"
    stored = json.loads(root_path.read_text(encoding="utf-8"))
    rows = stored["credential_pool"]["openai-codex"]
    assert next(row for row in rows if row["id"] == "legacy-singleton-seed")[
        "refresh_token"
    ] == "legacy-refresh"
    assert next(row for row in rows if row["id"] == "shared-account")[
        "refresh_token"
    ] == "shared-refresh"


def test_remove_manual_entry_never_persists_runtime_singleton_overlay(
    tmp_path, monkeypatch
):
    root, profile_a, _profile_b = _profile_layout(tmp_path, monkeypatch)
    root_path = root / "auth.json"
    _write(
        root_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "shared-account",
                        "label": "shared",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "priority": 0,
                        "access_token": "shared-access",
                        "refresh_token": "shared-refresh",
                    }
                ]
            },
        },
    )
    _write(
        profile_a / "auth.json",
        {
            "version": 1,
            "providers": {
                "openai-codex": {
                    "tokens": {
                        "access_token": "profile-access",
                        "refresh_token": "profile-refresh",
                    }
                }
            },
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(profile_a))
    monkeypatch.setattr(CP, "get_env_prefer_dotenv", lambda _key: "")

    pool = CP.load_pool("openai-codex")
    manual_index = next(
        index
        for index, entry in enumerate(pool.entries(), start=1)
        if entry.source.startswith("manual:")
    )
    assert pool.remove_index(manual_index) is not None

    stored = json.loads(root_path.read_text(encoding="utf-8"))
    assert stored["credential_pool"]["openai-codex"] == []
    assert "profile-access" not in root_path.read_text(encoding="utf-8")


def test_manual_codex_refresh_never_adopts_profile_singleton(tmp_path, monkeypatch):
    root, profile_a, _profile_b = _profile_layout(tmp_path, monkeypatch)
    _write(
        root / "auth.json",
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "manual-account",
                        "label": "manual",
                        "source": "manual:device_code",
                        "auth_type": "oauth",
                        "priority": 0,
                        "access_token": "manual-access",
                        "refresh_token": "manual-refresh",
                    }
                ]
            },
        },
    )
    _write(
        profile_a / "auth.json",
        {
            "version": 1,
            "providers": {
                "openai-codex": {
                    "tokens": {
                        "access_token": "singleton-access",
                        "refresh_token": "singleton-refresh",
                    }
                }
            },
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(profile_a))
    monkeypatch.setattr(CP, "get_env_prefer_dotenv", lambda _key: "")
    observed = {}

    def fake_refresh(access_token, refresh_token):
        observed.update(access_token=access_token, refresh_token=refresh_token)
        return {
            "access_token": "manual-access-rotated",
            "refresh_token": "manual-refresh-rotated",
        }

    monkeypatch.setattr(CP.auth_mod, "refresh_codex_oauth_pure", fake_refresh)
    pool = CP.load_pool("openai-codex")
    manual = next(
        entry for entry in pool.entries() if entry.source == "manual:device_code"
    )

    refreshed = pool._refresh_entry(manual, force=True)

    assert refreshed is not None
    assert observed == {
        "access_token": "manual-access",
        "refresh_token": "manual-refresh",
    }
    stored = json.loads((root / "auth.json").read_text(encoding="utf-8"))
    row = stored["credential_pool"]["openai-codex"][0]
    assert row["refresh_token"] == "manual-refresh-rotated"
