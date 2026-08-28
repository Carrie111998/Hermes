"""Tests for cross-profile auth fallback.

When ``HERMES_HOME`` points to a named profile, ``read_credential_pool()``
and ``get_provider_auth_state()`` fall back to the global-root
``auth.json`` per-provider when the profile has no entries for that
provider.  Writes still target the profile only.

See the #18594 follow-up report: profile workers couldn't see providers
authenticated only at the global root.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

import pytest


def _make_auth_store(pool: dict | None = None, providers: dict | None = None) -> dict:
    store: dict = {"version": 1}
    if pool is not None:
        store["credential_pool"] = pool
    if providers is not None:
        store["providers"] = providers
    return store


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Set up a global root + an active profile under Path.home()/.hermes/profiles/coder.

    * Path.home() -> tmp_path
    * Global root -> tmp_path/.hermes            (has its own auth.json fixture)
    * Profile     -> tmp_path/.hermes/profiles/coder   (active, HERMES_HOME points here)

    This mirrors the real "named profile mounted under the default root"
    layout that profile users actually have on disk.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    global_root = tmp_path / ".hermes"
    global_root.mkdir()
    profile_dir = global_root / "profiles" / "coder"
    profile_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    return {"global": global_root, "profile": profile_dir}


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# Explicit named-profile auth source
# ---------------------------------------------------------------------------


def test_explicit_auth_source_profile_shares_reads_writes_and_lock(profile_env):
    from hermes_cli import auth

    source_profile = profile_env["global"] / "profiles" / "work"
    source_profile.mkdir(parents=True)
    _write(source_profile / "auth.json", _make_auth_store(
        pool={
            "openai-codex": [{
                "id": "shared-1",
                "label": "shared",
                "auth_type": "oauth",
                "priority": 0,
                "source": "manual:device_code",
                "access_token": "shared-token",
            }],
        },
        providers={"nous": {"access_token": "source-provider"}},
    ))
    (profile_env["profile"] / "config.yaml").write_text(
        "auth:\n  source_profile: work\n"
    )
    _write(profile_env["global"] / "auth.json", _make_auth_store(
        pool={"anthropic": [{"id": "must-not-fallback"}]},
        providers={"nous": {"access_token": "must-not-fallback"}},
    ))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        "openai-codex": [{"id": "stale-local"}],
    }))

    assert auth._auth_file_path() == source_profile / "auth.json"
    assert auth._auth_lock_path() == source_profile / "auth.lock"
    assert auth._global_auth_file_path() is None
    assert auth.read_credential_pool("openai-codex")[0]["id"] == "shared-1"
    assert auth.read_credential_pool("anthropic") == []
    assert auth.get_provider_auth_state("nous") == {"access_token": "source-provider"}
    provider_state, provider_source = auth._load_provider_state_with_source(
        auth._load_auth_store(), "nous"
    )
    assert provider_state == {"access_token": "source-provider"}
    assert provider_source == source_profile / "auth.json"

    auth.write_credential_pool("openai-codex", [{
        "id": "shared-1",
        "label": "shared",
        "auth_type": "oauth",
        "priority": 0,
        "source": "manual:device_code",
        "access_token": "refreshed-token",
    }])

    source_data = json.loads((source_profile / "auth.json").read_text())
    local_data = json.loads((profile_env["profile"] / "auth.json").read_text())
    assert source_data["credential_pool"]["openai-codex"][0]["access_token"] == "refreshed-token"
    assert local_data["credential_pool"]["openai-codex"][0]["id"] == "stale-local"


def test_explicit_auth_source_profile_rejects_path_traversal(profile_env):
    from hermes_cli import auth

    (profile_env["profile"] / "config.yaml").write_text(
        "auth:\n  source_profile: ../work\n"
    )
    with pytest.raises(RuntimeError, match="simple named profile"):
        auth._auth_file_path()


def test_explicit_auth_source_profile_rejects_symlink_escape(profile_env, tmp_path):
    from hermes_cli import auth

    outside = tmp_path / "outside-auth"
    outside.mkdir()
    (profile_env["global"] / "profiles" / "escaped").symlink_to(
        outside, target_is_directory=True
    )
    (profile_env["profile"] / "config.yaml").write_text(
        "auth:\n  source_profile: escaped\n"
    )

    with pytest.raises(RuntimeError, match="escapes profiles root"):
        auth._auth_file_path()


def test_explicit_auth_source_profile_rejects_self_reference(profile_env):
    from hermes_cli import auth

    (profile_env["profile"] / "config.yaml").write_text(
        "auth:\n  source_profile: coder\n"
    )

    with pytest.raises(RuntimeError, match="must not reference the active profile"):
        auth._auth_file_path()


def test_explicit_auth_source_profile_rejects_auth_file_symlink(profile_env, tmp_path):
    from hermes_cli import auth

    source_profile = profile_env["global"] / "profiles" / "work"
    source_profile.mkdir()
    outside_auth = tmp_path / "outside-auth.json"
    _write(outside_auth, _make_auth_store(pool={"openai-codex": [{"id": "outside"}]}))
    (source_profile / "auth.json").symlink_to(outside_auth)
    (profile_env["profile"] / "config.yaml").write_text(
        "auth:\n  source_profile: work\n"
    )

    with pytest.raises(RuntimeError, match="source store must not be a symlink"):
        auth._auth_file_path()
    assert json.loads(outside_auth.read_text())["credential_pool"]["openai-codex"][0]["id"] == "outside"


def test_explicit_auth_source_profile_rejects_real_profile_during_pytest(
    profile_env, tmp_path, monkeypatch
):
    from hermes_cli import auth

    declared_home = tmp_path / "declared-real-home"
    real_profile_auth = declared_home / ".hermes" / "profiles" / "work" / "auth.json"
    real_profile_auth.parent.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(declared_home))
    monkeypatch.setattr(auth, "_configured_auth_source_path", lambda: real_profile_auth)

    with pytest.raises(RuntimeError, match="Refusing to touch real user auth store"):
        auth._auth_file_path()


def test_explicit_auth_source_profile_fails_closed_on_malformed_config(profile_env):
    from hermes_cli import auth

    _write(profile_env["global"] / "auth.json", _make_auth_store(
        pool={"anthropic": [{"id": "forbidden-global"}]},
        providers={"nous": {"access_token": "forbidden-global"}},
    ))
    (profile_env["profile"] / "config.yaml").write_text("auth: [")

    with pytest.raises(RuntimeError, match="active profile config is unreadable"):
        auth._auth_file_path()
    with pytest.raises(RuntimeError, match="active profile config is unreadable"):
        auth._global_auth_file_path()


def test_explicit_auth_source_profile_fails_closed_on_unreadable_config(
    profile_env, monkeypatch
):
    from hermes_cli import auth

    _write(profile_env["global"] / "auth.json", _make_auth_store(
        pool={"anthropic": [{"id": "forbidden-global"}]},
        providers={"nous": {"access_token": "forbidden-global"}},
    ))
    monkeypatch.setattr(
        auth,
        "read_user_config_raw",
        lambda: (_ for _ in ()).throw(OSError("permission denied")),
    )

    with pytest.raises(RuntimeError, match="active profile config is unreadable"):
        auth._auth_file_path()
    with pytest.raises(RuntimeError, match="active profile config is unreadable"):
        auth._global_auth_file_path()


def test_auth_authority_is_pinned_across_credential_pool_read(profile_env, monkeypatch):
    from hermes_cli import auth

    source = profile_env["global"] / "profiles" / "source"
    source.mkdir()
    _write(source / "auth.json", _make_auth_store(
        pool={"openai-codex": [{"id": "SOURCE"}]},
    ))
    _write(profile_env["global"] / "auth.json", _make_auth_store(
        pool={"anthropic": [{"id": "ROOT-FORBIDDEN"}]},
    ))
    resolutions = iter([source / "auth.json", None])
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    pool = auth.read_credential_pool()

    assert pool == {"openai-codex": [{"id": "SOURCE"}]}
    assert len(calls) == 1


def test_auth_authority_is_pinned_across_credential_pool_write(profile_env, monkeypatch):
    from hermes_cli import auth

    stores = []
    for name in ("b", "c", "d"):
        profile = profile_env["global"] / "profiles" / name
        profile.mkdir()
        path = profile / "auth.json"
        _write(path, _make_auth_store(pool={"openai-codex": [{"id": name.upper()}]}))
        stores.append(path)
    resolutions = iter(stores)
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    auth.write_credential_pool("openai-codex", [{"id": "NEW", "source": "manual"}])

    assert len(calls) == 1
    assert json.loads(stores[0].read_text())["credential_pool"]["openai-codex"][0]["id"] == "NEW"
    assert json.loads(stores[1].read_text())["credential_pool"]["openai-codex"][0]["id"] == "C"
    assert json.loads(stores[2].read_text())["credential_pool"]["openai-codex"][0]["id"] == "D"
    assert stores[0].with_suffix(".lock").exists()
    assert not stores[1].with_suffix(".lock").exists()
    assert not stores[2].with_suffix(".lock").exists()


def test_load_pool_pins_authority_across_read_normalize_and_write(
    profile_env, monkeypatch
):
    from agent import credential_pool
    from hermes_cli import auth

    stores = []
    for name in ("b", "c"):
        profile = profile_env["global"] / "profiles" / name
        profile.mkdir()
        path = profile / "auth.json"
        _write(path, _make_auth_store(pool={
            "zai": [{
                "id": name.upper(), "label": name.upper(), "source": "manual",
                "auth_type": "api_key", "access_token": f"{name}-token", "priority": 0,
            }],
        }))
        stores.append(path)
    original_c = stores[1].read_bytes()
    resolutions = iter(stores)
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    monkeypatch.setattr(credential_pool, "_seed_from_singletons", lambda *_: (False, set()))
    monkeypatch.setattr(credential_pool, "_seed_from_env", lambda *_: (False, set()))
    monkeypatch.setattr(credential_pool, "_normalize_pool_priorities", lambda *_: True)

    pool = credential_pool.load_pool("zai")

    assert pool._entries[0].id == "B"
    assert len(calls) == 1
    assert json.loads(stores[0].read_text())["credential_pool"]["zai"][0]["id"] == "B"
    assert stores[1].read_bytes() == original_c
    assert stores[0].with_suffix(".lock").exists()
    assert not stores[1].with_suffix(".lock").exists()


def test_codex_read_pins_source_and_never_reopens_root(profile_env, monkeypatch):
    from hermes_cli import auth

    source = profile_env["global"] / "profiles" / "codex-source"
    source.mkdir()
    _write(source / "auth.json", _make_auth_store())
    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={
        "openai-codex": {"tokens": {
            "access_token": "ROOT-FORBIDDEN-ACCESS",
            "refresh_token": "ROOT-FORBIDDEN-REFRESH",
        }},
    }))
    resolutions = iter([source / "auth.json", None])
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    with pytest.raises(auth.AuthError, match="No Codex credentials stored"):
        auth._read_codex_tokens()
    assert len(calls) == 1


def test_zai_endpoint_cache_pins_read_probe_and_write(profile_env, monkeypatch):
    from hermes_cli import auth

    stores = []
    for name in ("b", "c"):
        profile = profile_env["global"] / "profiles" / name
        profile.mkdir()
        path = profile / "auth.json"
        _write(path, _make_auth_store(providers={"zai": {"marker": name.upper()}}))
        stores.append(path)
    original_c = stores[1].read_bytes()
    resolutions = iter(stores)
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    monkeypatch.setattr(auth, "detect_zai_endpoint", lambda *_: {
        "base_url": "https://detected.example/v1", "id": "test", "label": "test", "model": "glm",
    })
    result = auth._resolve_zai_base_url("test-key", "https://default.example/v1", "")

    assert result == "https://detected.example/v1"
    assert len(calls) == 1
    assert json.loads(stores[0].read_text())["providers"]["zai"]["marker"] == "B"
    assert json.loads(stores[0].read_text())["providers"]["zai"]["detected_endpoint"]["base_url"] == result
    assert stores[1].read_bytes() == original_c
    assert stores[0].with_suffix(".lock").exists()
    assert not stores[1].with_suffix(".lock").exists()


def test_persist_nous_pins_singleton_and_pool_sync(profile_env, monkeypatch):
    from hermes_cli import auth

    stores = []
    for name in ("b", "c"):
        profile = profile_env["global"] / "profiles" / name
        profile.mkdir()
        path = profile / "auth.json"
        _write(path, _make_auth_store())
        stores.append(path)
    original_c = stores[1].read_bytes()
    resolutions = iter(stores)
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    monkeypatch.setattr(auth, "_write_shared_nous_state", lambda *_: None)
    auth.persist_nous_credentials({
        "access_token": "B-NEW-ACCESS", "refresh_token": "B-NEW-REFRESH",
        "portal_base_url": "https://portal.example", "inference_base_url": "https://inference.example/v1",
    })

    stored_b = json.loads(stores[0].read_text())
    assert stored_b["providers"]["nous"]["access_token"] == "B-NEW-ACCESS"
    assert stored_b["credential_pool"]["nous"]
    assert stores[1].read_bytes() == original_c
    assert len(calls) == 1
    assert stores[0].with_suffix(".lock").exists()
    assert not stores[1].with_suffix(".lock").exists()


def test_logout_pins_active_provider_selection_and_clear(profile_env, monkeypatch):
    from argparse import Namespace
    from hermes_cli import auth

    stores = []
    for name, active in (("b", "nous"), ("c", "spotify")):
        profile = profile_env["global"] / "profiles" / name
        profile.mkdir()
        path = profile / "auth.json"
        store = _make_auth_store(providers={
            "nous": {"access_token": f"{name}-nous"},
            "spotify": {"access_token": f"{name}-spotify"},
        })
        store["active_provider"] = active
        _write(path, store)
        stores.append(path)
    original_c = stores[1].read_bytes()
    resolutions = iter(stores)
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    auth.logout_command(Namespace(provider=None))

    stored_b = json.loads(stores[0].read_text())
    assert "nous" not in stored_b["providers"]
    assert "spotify" in stored_b["providers"]
    assert stores[1].read_bytes() == original_c
    assert len(calls) == 1


def test_api_key_runtime_resolution_pins_pool_and_zai_cache(profile_env, monkeypatch):
    from hermes_cli import auth

    stores = []
    for name in ("b", "c"):
        profile = profile_env["global"] / "profiles" / name
        profile.mkdir()
        path = profile / "auth.json"
        pool = {"zai": [{
            "id": name.upper(), "label": name.upper(), "source": "manual",
            "auth_type": "api_key", "access_token": f"{name}-key", "priority": 0,
        }]}
        _write(path, _make_auth_store(pool=pool))
        stores.append(path)
    original_c = stores[1].read_bytes()
    resolutions = iter(stores)
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    monkeypatch.setattr(auth, "detect_zai_endpoint", lambda *_: {
        "base_url": "https://zai.example/v1", "id": "test", "label": "test", "model": "glm",
    })
    result = auth.resolve_api_key_provider_credentials("zai")

    assert result["api_key"] == "b-key"
    stored_b = json.loads(stores[0].read_text())
    assert stored_b["providers"]["zai"]["detected_endpoint"]["base_url"] == "https://zai.example/v1"
    assert stores[1].read_bytes() == original_c
    assert len(calls) == 1


def test_minimax_runtime_refresh_pins_read_and_persistence(profile_env, monkeypatch):
    from hermes_cli import auth

    stores = []
    for name in ("b", "c"):
        profile = profile_env["global"] / "profiles" / name
        profile.mkdir()
        path = profile / "auth.json"
        _write(path, _make_auth_store(providers={"minimax-oauth": {
            "access_token": f"{name}-old", "refresh_token": f"{name}-refresh",
            "portal_base_url": "https://portal.example", "inference_base_url": "https://api.example/v1",
        }}))
        stores.append(path)
    original_c = stores[1].read_bytes()
    resolutions = iter(stores)
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    def fake_refresh(state, **_kwargs):
        updated = dict(state, access_token="B-ROTATED")
        auth._minimax_save_auth_state(updated)
        return updated

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    monkeypatch.setattr(auth, "_refresh_minimax_oauth_state", fake_refresh)
    result = auth.resolve_minimax_oauth_runtime_credentials()

    assert result["api_key"] == "B-ROTATED"
    assert json.loads(stores[0].read_text())["providers"]["minimax-oauth"]["access_token"] == "B-ROTATED"
    assert stores[1].read_bytes() == original_c
    assert len(calls) == 1


def test_custom_model_prune_pins_pool_read_and_write(profile_env, monkeypatch):
    from agent import credential_pool
    from hermes_cli import auth
    from hermes_cli.model_setup_flows import _prune_replaced_custom_model_config_credentials

    stores = []
    for name in ("b", "c"):
        profile = profile_env["global"] / "profiles" / name
        profile.mkdir()
        path = profile / "auth.json"
        _write(path, _make_auth_store(pool={
            "custom:old": [
                {"id": f"{name}-model", "source": "model_config", "access_token": f"{name}-old"},
                {"id": f"{name}-manual", "source": "manual", "access_token": f"{name}-keep"},
            ],
        }))
        stores.append(path)
    original_c = stores[1].read_bytes()
    resolutions = iter(stores)
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    monkeypatch.setattr(credential_pool, "get_custom_provider_pool_key", lambda *_args, **_kwargs: "custom:new")
    _prune_replaced_custom_model_config_credentials("https://new.example/v1")

    entries_b = json.loads(stores[0].read_text())["credential_pool"]["custom:old"]
    assert [entry["id"] for entry in entries_b] == ["b-manual"]
    assert stores[1].read_bytes() == original_c
    assert len(calls) == 1


@pytest.mark.parametrize(
    "provider,method",
    [
        ("openai-codex", "_sync_codex_entry_from_auth_store"),
        ("xai-oauth", "_sync_xai_oauth_entry_from_auth_store"),
    ],
)
def test_oauth_pool_sync_pins_auth_read_and_pool_persist(
    profile_env, monkeypatch, provider, method
):
    from agent.credential_pool import CredentialPool, PooledCredential
    from hermes_cli import auth

    stores = []
    for name in ("b", "c"):
        profile = profile_env["global"] / "profiles" / name
        profile.mkdir()
        path = profile / "auth.json"
        _write(path, _make_auth_store(
            providers={provider: {"tokens": {
                "access_token": f"{name}-new", "refresh_token": f"{name}-new-r",
            }, "last_refresh": f"{name}-time"}},
            pool={provider: [{
                "id": "row", "label": "row", "auth_type": "oauth", "priority": 0,
                "source": "device_code", "access_token": f"{name}-old",
                "refresh_token": f"{name}-old-r", "last_status": "exhausted",
            }]},
        ))
        stores.append(path)
    original_c = stores[1].read_bytes()
    resolutions = iter(stores)
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    entry = PooledCredential(
        provider=provider, id="row", label="row", auth_type="oauth", priority=0,
        source="device_code", access_token="b-old", refresh_token="b-old-r",
        last_status="exhausted",
    )
    pool = CredentialPool(provider, [entry])
    updated = getattr(pool, method)(entry)

    assert updated.access_token == "b-new"
    assert json.loads(stores[0].read_text())["credential_pool"][provider][0]["access_token"] == "b-new"
    assert stores[1].read_bytes() == original_c
    assert len(calls) == 1


def test_loaded_pool_remains_bound_for_later_mutation(profile_env, monkeypatch):
    from agent import credential_pool
    from hermes_cli import auth

    stores = []
    for name in ("b", "c"):
        profile = profile_env["global"] / "profiles" / name
        profile.mkdir()
        path = profile / "auth.json"
        _write(path, _make_auth_store(pool={"zai": [{
            "id": name.upper(), "label": name.upper(), "source": "manual",
            "auth_type": "api_key", "access_token": f"{name}-key", "priority": 0,
        }]}))
        stores.append(path)
    original_c = stores[1].read_bytes()
    resolutions = iter(stores)
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    monkeypatch.setattr(credential_pool, "_seed_from_singletons", lambda *_: (False, set()))
    monkeypatch.setattr(credential_pool, "_seed_from_env", lambda *_: (False, set()))
    pool = credential_pool.load_pool("zai")
    removed = pool.remove_index(1)

    assert removed.id == "B"
    assert json.loads(stores[0].read_text())["credential_pool"]["zai"] == []
    assert stores[1].read_bytes() == original_c
    assert len(calls) == 1


def test_oauth_refresh_uses_loaded_pool_authority_for_singleton_and_pool(
    profile_env, monkeypatch
):
    from dataclasses import replace
    from agent import credential_pool
    from hermes_cli import auth

    provider = "openai-codex"
    stores = []
    for name in ("b", "c"):
        profile = profile_env["global"] / "profiles" / name
        profile.mkdir()
        path = profile / "auth.json"
        _write(path, _make_auth_store(
            providers={provider: {"tokens": {
                "access_token": f"{name}-old", "refresh_token": f"{name}-refresh",
            }}},
            pool={provider: [{
                "id": "row", "label": name, "source": "device_code",
                "auth_type": "oauth", "access_token": f"{name}-old",
                "refresh_token": f"{name}-refresh", "priority": 0,
            }]},
        ))
        stores.append(path)
    original_c = stores[1].read_bytes()
    resolutions = iter(stores)
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    monkeypatch.setattr(credential_pool, "_seed_from_singletons", lambda *_: (False, set()))
    monkeypatch.setattr(credential_pool, "_seed_from_env", lambda *_: (False, set()))
    pool = credential_pool.load_pool(provider)

    def fake_refresh_impl(self, entry, *, force):
        updated = replace(entry, access_token="ROTATED", refresh_token="ROTATED-r")
        self._replace_entry(entry, updated)
        self._sync_device_code_entry_to_auth_store(updated)
        self._persist()
        return updated

    monkeypatch.setattr(credential_pool.CredentialPool, "_refresh_entry_impl", fake_refresh_impl)
    refreshed = pool.try_refresh_matching(credential_id="row")

    assert refreshed.access_token == "ROTATED"
    b_store = json.loads(stores[0].read_text())
    assert b_store["providers"][provider]["tokens"]["access_token"] == "ROTATED"
    assert b_store["credential_pool"][provider][0]["access_token"] == "ROTATED"
    assert stores[1].read_bytes() == original_c
    assert len(calls) == 1


def test_auth_remove_pins_pool_cleanup_and_suppression(profile_env, monkeypatch):
    from types import SimpleNamespace
    from agent import credential_pool
    from hermes_cli import auth
    from hermes_cli.auth_commands import auth_remove_command

    provider = "openai-codex"
    stores = []
    for name in ("b", "c"):
        profile = profile_env["global"] / "profiles" / name
        profile.mkdir()
        path = profile / "auth.json"
        _write(path, _make_auth_store(
            providers={provider: {"tokens": {
                "access_token": f"{name}-access", "refresh_token": f"{name}-refresh",
            }}},
            pool={provider: [{
                "id": "row", "label": name, "source": "device_code",
                "auth_type": "oauth", "access_token": f"{name}-access",
                "refresh_token": f"{name}-refresh", "priority": 0,
            }]},
        ))
        stores.append(path)
    original_c = stores[1].read_bytes()
    resolutions = iter(stores)
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    monkeypatch.setattr(credential_pool, "_seed_from_singletons", lambda *_: (False, set()))
    monkeypatch.setattr(credential_pool, "_seed_from_env", lambda *_: (False, set()))
    auth_remove_command(SimpleNamespace(provider=provider, target="1"))

    b_store = json.loads(stores[0].read_text())
    assert b_store["credential_pool"][provider] == []
    assert provider not in b_store.get("providers", {})
    assert "device_code" in b_store["suppressed_sources"][provider]
    assert stores[1].read_bytes() == original_c
    assert len(calls) == 1


def test_xai_oauth_read_pins_source_and_never_reopens_root(profile_env, monkeypatch):
    from hermes_cli import auth

    source = profile_env["global"] / "profiles" / "xai-source"
    source.mkdir()
    _write(source / "auth.json", _make_auth_store(providers={
        "xai-oauth": {"tokens": {"access_token": "SOURCE-ACCESS", "refresh_token": "SOURCE-REFRESH"}},
    }))
    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={
        "xai-oauth": {"tokens": {"access_token": "ROOT-ACCESS", "refresh_token": "ROOT-REFRESH"}},
    }))
    resolutions = iter([source / "auth.json", None])
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    result = auth._read_xai_oauth_tokens()

    assert result["tokens"]["access_token"] == "SOURCE-ACCESS"
    assert result["tokens"]["refresh_token"] == "SOURCE-REFRESH"
    assert len(calls) == 1


def test_nous_status_pins_authority_across_initial_and_refreshed_reads(
    profile_env, monkeypatch
):
    from hermes_cli import auth

    stores = []
    for name in ("b", "c"):
        profile = profile_env["global"] / "profiles" / name
        profile.mkdir()
        path = profile / "auth.json"
        _write(path, _make_auth_store(providers={
            "nous": {
                "access_token": f"{name.upper()}-ACCESS",
                "refresh_token": f"{name.upper()}-REFRESH",
                "portal_base_url": f"https://{name}.example",
                "inference_base_url": f"https://{name}.example/v1",
            },
        }))
        stores.append(path)
    resolutions = iter(stores)
    calls = []

    def changing_source():
        calls.append(True)
        return next(resolutions)

    monkeypatch.setattr(auth, "_configured_auth_source_path", changing_source)
    monkeypatch.setattr(auth, "resolve_nous_runtime_credentials", lambda: {
        "base_url": "https://runtime.example/v1", "source": "test", "key_id": "test-key",
    })
    result = auth._compute_nous_auth_status()

    assert result["portal_base_url"] == "https://b.example"
    assert result["access_token"] == "B-ACCESS"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# read_credential_pool — provider-slice reads
# ---------------------------------------------------------------------------








def test_missing_global_auth_file_is_safe(profile_env):
    """Profile processes that never had a global auth.json still work."""
    from hermes_cli.auth import read_credential_pool

    # No global auth.json written at all.
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "prof-1",
            "label": "profile",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-profile",
        }],
    }))

    assert read_credential_pool("openrouter")[0]["id"] == "prof-1"
    assert read_credential_pool("anthropic") == []


def test_malformed_global_auth_file_does_not_break_profile_read(profile_env):
    (profile_env["global"] / "auth.json").write_text("{not valid json")
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "prof-1",
            "label": "profile",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-profile",
        }],
    }))

    from hermes_cli.auth import read_credential_pool

    # Profile reads still work; malformed global is silently ignored.
    assert read_credential_pool("openrouter")[0]["id"] == "prof-1"
    # And no fallback for anthropic since global is unreadable.
    assert read_credential_pool("anthropic") == []


# ---------------------------------------------------------------------------
# read_credential_pool — whole-pool reads (provider_id=None)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_provider_auth_state — singleton fallback
# ---------------------------------------------------------------------------


def test_provider_auth_state_falls_back_to_global_when_profile_has_none(profile_env):
    from hermes_cli.auth import get_provider_auth_state

    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={
        "nous": {"access_token": "nous-global", "refresh_token": "rt-global"},
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={}))

    state = get_provider_auth_state("nous")
    assert state is not None
    assert state["access_token"] == "nous-global"


def test_provider_auth_state_returns_none_when_neither_has_it(profile_env):
    from hermes_cli.auth import get_provider_auth_state

    _write(profile_env["global"] / "auth.json", _make_auth_store(providers={}))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(providers={}))

    assert get_provider_auth_state("nous") is None


# ---------------------------------------------------------------------------
# _load_provider_state — internal global fallback (issue #18594 follow-up)
#
# Several runtime helpers (notably ``resolve_nous_runtime_credentials`` and
# ``resolve_nous_access_token``) call ``_load_provider_state`` directly with
# a profile-loaded auth store rather than going through
# ``get_provider_auth_state``. Without the fallback wired into
# ``_load_provider_state`` itself, those helpers raise ``"Hermes is not
# logged into Nous Portal"`` even though the user has a valid global Nous
# login. These tests pin the per-provider shadowing into the helper.
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Classic mode — no fallback path should ever trigger
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Writes stay scoped to the profile
# ---------------------------------------------------------------------------


def test_write_credential_pool_targets_profile_not_global(profile_env):
    from hermes_cli.auth import read_credential_pool, write_credential_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "glob-1",
            "label": "global",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-global",
        }],
    }))

    write_credential_pool("openrouter", [{
        "id": "prof-new",
        "label": "profile-new",
        "auth_type": "api_key",
        "priority": 0,
        "source": "manual",
        "access_token": "sk-profile-new",
    }])

    # Global auth.json unchanged.
    global_data = json.loads((profile_env["global"] / "auth.json").read_text())
    assert global_data["credential_pool"]["openrouter"][0]["id"] == "glob-1"

    # Profile auth.json holds the new entry.
    profile_data = json.loads((profile_env["profile"] / "auth.json").read_text())
    assert profile_data["credential_pool"]["openrouter"][0]["id"] == "prof-new"

    # Subsequent read returns profile (shadows global).
    assert [e["id"] for e in read_credential_pool("openrouter")] == ["prof-new"]




def test_auth_lock_reentrancy_is_scoped_after_profile_context_switch(profile_env):
    """Changing profile context cannot inherit another store's lock depth."""
    import hermes_cli.auth as auth
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    profile_b = profile_env["global"] / "profiles" / "reviewer"
    profile_b.mkdir(parents=True)
    profile_b_lock = profile_b / "auth.lock"

    with auth._auth_store_lock():
        holder_a = auth._auth_lock_holder_for(profile_env["profile"] / "auth.json")
        assert getattr(holder_a, "depth", 0) == 1

        token = set_hermes_home_override(profile_b)
        try:
            holder_b = auth._auth_lock_holder_for(profile_b / "auth.json")
            assert holder_b is not holder_a
            assert getattr(holder_b, "depth", 0) == 0
            assert not profile_b_lock.exists()

            with auth._auth_store_lock():
                assert profile_b_lock.exists()
                assert getattr(holder_b, "depth", 0) == 1
        finally:
            reset_hermes_home_override(token)

    assert getattr(holder_a, "depth", 0) == 0


# ---------------------------------------------------------------------------
# write_credential_pool — stale-snapshot cooldown merge
# ---------------------------------------------------------------------------


@pytest.fixture()
def classic_env(tmp_path, monkeypatch):
    """Classic single-root layout (HERMES_HOME != ~/.hermes, no profiles)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    hermes_home = tmp_path / "classic"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


def _pool_entry(**overrides) -> dict:
    entry = {
        "id": "cred-x",
        "label": "key-x",
        "auth_type": "api_key",
        "priority": 0,
        "source": "manual",
        "access_token": "sk-x",
    }
    entry.update(overrides)
    return entry




def test_write_pool_never_merges_cooldown_onto_reauthed_entry(classic_env):
    """A token change means re-auth: the old cooldown must never carry over.

    A fresh login intentionally clears the entry's status; resurrecting the
    stale cooldown onto the new credentials would bench a just-authorized key.
    """
    from hermes_cli.auth import write_credential_pool

    _write(classic_env / "auth.json", _make_auth_store(pool={
        "openrouter": [_pool_entry(
            access_token="sk-old",
            last_status="exhausted",
            last_status_at=time.time() - 60,  # newer AND unexpired
            last_error_code=429,
        )],
    }))

    # Same entry id, freshly re-authed with a new token and cleared status.
    write_credential_pool("openrouter", [_pool_entry(access_token="sk-new")])

    data = json.loads((classic_env / "auth.json").read_text())
    persisted = data["credential_pool"]["openrouter"][0]
    assert persisted["access_token"] == "sk-new"
    assert persisted.get("last_status") != "exhausted"
    assert persisted.get("last_error_code") is None
