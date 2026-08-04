"""Tests for shared credential pools across profiles.

Provider singleton state remains profile-aware with a global-root fallback,
but ``credential_pool`` and its suppression metadata are machine-wide state.
Every profile reads and writes the one pool in the global-root ``auth.json``.
Legacy profile-local pool rows are migrated into that shared store on access.
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
# read_credential_pool — shared provider-slice reads and migration
# ---------------------------------------------------------------------------


def test_profile_entries_migrate_into_shared_pool(profile_env):
    """Legacy profile rows join the root pool instead of shadowing it."""
    from hermes_cli.auth import read_credential_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "glob-1",
            "label": "global-key",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-or-global",
        }],
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "prof-1",
            "label": "profile-key",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "sk-or-profile",
        }],
    }))

    assert [entry["id"] for entry in read_credential_pool("openrouter")] == [
        "glob-1",
        "prof-1",
    ]

    shared = json.loads((profile_env["global"] / "auth.json").read_text())
    assert [entry["id"] for entry in shared["credential_pool"]["openrouter"]] == [
        "glob-1",
        "prof-1",
    ]
    local = json.loads((profile_env["profile"] / "auth.json").read_text())
    assert "credential_pool" not in local


def test_repeated_profile_migration_deduplicates_cloned_rows(profile_env):
    """A clone-all copy of root auth state must not duplicate credentials."""
    from hermes_cli.auth import read_credential_pool

    entry = {
        "id": "shared-1",
        "label": "same-account",
        "auth_type": "oauth",
        "priority": 0,
        "source": "manual:device_code",
        "access_token": "access-token",
        "refresh_token": "refresh-token",
    }
    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openai-codex": [entry],
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        "openai-codex": [dict(entry)],
    }))

    assert [item["id"] for item in read_credential_pool("openai-codex")] == [
        "shared-1",
    ]
    assert [item["id"] for item in read_credential_pool("openai-codex")] == [
        "shared-1",
    ]


def test_profile_migration_remaps_same_id_when_secret_material_differs(profile_env):
    """An ID collision alone cannot discard a distinct migrated account."""
    from hermes_cli.auth import read_credential_pool

    shared_entry = {
        "id": "shared-1",
        "source": "manual:device_code",
        "access_token": "current-access",
        "refresh_token": "current-refresh",
    }
    stale_entry = {
        **shared_entry,
        "access_token": "stale-access",
        "refresh_token": "stale-refresh",
    }
    _write(
        profile_env["global"] / "auth.json",
        _make_auth_store(pool={"openai-codex": [shared_entry]}),
    )
    _write(
        profile_env["profile"] / "auth.json",
        _make_auth_store(pool={"openai-codex": [stale_entry]}),
    )

    entries = read_credential_pool("openai-codex")
    assert len(entries) == 2
    assert entries[0]["id"] == "shared-1"
    assert entries[0]["refresh_token"] == "current-refresh"
    assert entries[1]["id"] != "shared-1"
    assert entries[1]["refresh_token"] == "stale-refresh"


def test_profile_migration_deduplicates_legacy_singleton_alias(profile_env):
    """The old device-code/manual alias pair represents one OAuth account."""
    from hermes_cli.auth import read_credential_pool

    shared_entry = {
        "id": "shared-1",
        "label": "same-account",
        "auth_type": "oauth",
        "priority": 0,
        "source": "device_code",
        "access_token": "access-token",
        "refresh_token": "refresh-token",
    }
    profile_entry = {
        **shared_entry,
        "id": "legacy-alias",
        "source": "manual:device_code",
    }
    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openai-codex": [shared_entry],
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={
        "openai-codex": [profile_entry],
    }))

    assert [item["id"] for item in read_credential_pool("openai-codex")] == [
        "shared-1",
    ]








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


def test_shared_pool_pytest_seatbelt_uses_windows_auth_root(monkeypatch, tmp_path):
    from hermes_cli import auth

    local_appdata = tmp_path / "LocalAppData"
    monkeypatch.setattr(auth.sys, "platform", "win32")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "seatbelt")
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    assert auth._is_real_user_auth_store_under_test(
        local_appdata / "hermes" / "auth.json"
    )


@pytest.mark.parametrize(
    ("platform", "missing_env", "relative_auth_path"),
    [
        ("linux", "HOME", Path(".hermes/auth.json")),
        ("win32", "LOCALAPPDATA", Path("AppData/Local/hermes/auth.json")),
    ],
)
def test_shared_pool_pytest_seatbelt_uses_platform_fallback_when_env_missing(
    monkeypatch,
    tmp_path,
    platform,
    missing_env,
    relative_auth_path,
):
    """Missing HOME/LOCALAPPDATA must not make the real-store guard fail open."""
    from hermes_cli import auth

    monkeypatch.setattr(auth.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "seatbelt")
    monkeypatch.delenv(missing_env, raising=False)
    real_auth_path = tmp_path / relative_auth_path
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: real_auth_path)

    assert auth._is_real_user_auth_store_under_test(real_auth_path)
    with pytest.raises(RuntimeError, match="real user credential pool"):
        auth._credential_pool_auth_file_path()


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


def test_profile_logout_removes_provider_from_shared_pool(profile_env):
    from hermes_cli.auth import clear_provider_auth

    _write(
        profile_env["global"] / "auth.json",
        _make_auth_store(pool={"openai-codex": [{
            "id": "shared",
            "source": "manual:device_code",
            "access_token": "shared-access",
            "refresh_token": "shared-refresh",
        }]}),
    )
    _write(
        profile_env["profile"] / "auth.json",
        {
            **_make_auth_store(providers={
                "openai-codex": {"tokens": {"access_token": "profile-access"}},
            }),
            "active_provider": "openai-codex",
        },
    )

    assert clear_provider_auth("openai-codex") is True

    shared = json.loads(
        (profile_env["global"] / "auth.json").read_text(encoding="utf-8")
    )
    local = json.loads(
        (profile_env["profile"] / "auth.json").read_text(encoding="utf-8")
    )
    assert "openai-codex" not in shared.get("credential_pool", {})
    assert "openai-codex" not in local.get("providers", {})


def test_profile_logout_keeps_provider_retryable_when_shared_delete_fails(
    profile_env, monkeypatch
):
    """A failed shared-pool save must leave active/provider state for retry."""
    from hermes_cli import auth

    root_path = profile_env["global"] / "auth.json"
    profile_path = profile_env["profile"] / "auth.json"
    _write(
        root_path,
        _make_auth_store(pool={"openai-codex": [{
            "id": "shared",
            "source": "manual:device_code",
            "access_token": "shared-access",
            "refresh_token": "shared-refresh",
        }]}),
    )
    _write(
        profile_path,
        {
            **_make_auth_store(providers={
                "openai-codex": {"tokens": {"access_token": "profile-access"}},
            }),
            "active_provider": "openai-codex",
        },
    )

    real_save = auth._save_auth_store
    failed_once = False

    def fail_first_shared_save(store, target_path=None):
        nonlocal failed_once
        if target_path == root_path and not failed_once:
            failed_once = True
            raise OSError("injected shared save failure")
        return real_save(store, target_path=target_path)

    monkeypatch.setattr(auth, "_save_auth_store", fail_first_shared_save)

    with pytest.raises(OSError, match="injected shared save failure"):
        auth.clear_provider_auth()

    local_after_failure = json.loads(profile_path.read_text(encoding="utf-8"))
    assert local_after_failure["active_provider"] == "openai-codex"
    assert "openai-codex" in local_after_failure["providers"]

    assert auth.clear_provider_auth() is True
    local_after_retry = json.loads(profile_path.read_text(encoding="utf-8"))
    shared_after_retry = json.loads(root_path.read_text(encoding="utf-8"))
    assert local_after_retry.get("active_provider") is None
    assert "openai-codex" not in local_after_retry.get("providers", {})
    assert "openai-codex" not in shared_after_retry.get("credential_pool", {})


def test_classic_logout_deletes_provider_and_pool_in_one_save(
    classic_env, monkeypatch
):
    """Same-file provider/pool cleanup is one atomic auth.json transaction."""
    from hermes_cli import auth

    auth_path = classic_env / "auth.json"
    _write(
        auth_path,
        {
            **_make_auth_store(
                pool={"openai-codex": [{"id": "shared"}]},
                providers={"openai-codex": {"tokens": {"access_token": "active"}}},
            ),
            "active_provider": "openai-codex",
        },
    )
    real_save = auth._save_auth_store
    saves = 0

    def count_save(store, target_path=None):
        nonlocal saves
        saves += 1
        return real_save(store, target_path=target_path)

    monkeypatch.setattr(auth, "_save_auth_store", count_save)

    assert auth.clear_provider_auth() is True
    assert saves == 1
    stored = json.loads(auth_path.read_text(encoding="utf-8"))
    assert stored.get("active_provider") is None
    assert "openai-codex" not in stored.get("providers", {})
    assert "openai-codex" not in stored.get("credential_pool", {})


def test_profile_codex_reauth_updates_shared_pool_not_profile_pool(profile_env):
    from hermes_cli.auth import _save_codex_tokens

    _write(
        profile_env["global"] / "auth.json",
        _make_auth_store(pool={"openai-codex": [{
            "id": "shared",
            "source": "device_code",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
        }]}),
    )
    _write(
        profile_env["profile"] / "auth.json",
        _make_auth_store(providers={
            "openai-codex": {
                "tokens": {
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                }
            },
        }),
    )

    _save_codex_tokens(
        {"access_token": "new-access", "refresh_token": "new-refresh"},
        last_refresh="2026-08-04T00:00:00Z",
    )

    shared = json.loads(
        (profile_env["global"] / "auth.json").read_text(encoding="utf-8")
    )
    local = json.loads(
        (profile_env["profile"] / "auth.json").read_text(encoding="utf-8")
    )
    shared_entry = shared["credential_pool"]["openai-codex"][0]
    assert shared_entry["access_token"] == "new-access"
    assert shared_entry["refresh_token"] == "new-refresh"
    assert "credential_pool" not in local
    assert (
        local["providers"]["openai-codex"]["tokens"]["access_token"]
        == "new-access"
    )


@pytest.mark.parametrize("failed_save", ["shared", "profile"])
def test_profile_codex_reauth_retry_repairs_legacy_alias_after_partial_save(
    profile_env, monkeypatch, failed_save
):
    """Either named-profile save can fail once without stranding an alias."""
    from hermes_cli import auth

    root_path = profile_env["global"] / "auth.json"
    profile_path = profile_env["profile"] / "auth.json"
    _write(
        root_path,
        _make_auth_store(
            pool={
                "openai-codex": [
                    {
                        "id": "legacy-alias",
                        "source": "manual:device_code",
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                        "last_status": "dead",
                    },
                    {
                        "id": "singleton-seed",
                        "source": "device_code",
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                        "last_status": "dead",
                    },
                    {
                        "id": "independent-account",
                        "source": "manual:device_code",
                        "access_token": "independent-access",
                        "refresh_token": "independent-refresh",
                        "last_status": "exhausted",
                        "last_error_code": 429,
                    },
                ]
            }
        ),
    )
    _write(
        profile_path,
        _make_auth_store(
            providers={
                "openai-codex": {
                    "tokens": {
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                    }
                }
            }
        ),
    )
    real_save = auth._save_auth_store
    failed_once = False

    def fail_selected_save(store, target_path=None):
        nonlocal failed_once
        is_selected = (
            target_path == root_path
            if failed_save == "shared"
            else target_path is None
        )
        if is_selected and not failed_once:
            failed_once = True
            raise OSError(f"injected {failed_save} save failure")
        return real_save(store, target_path=target_path)

    monkeypatch.setattr(auth, "_save_auth_store", fail_selected_save)
    new_tokens = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
    }

    with pytest.raises(OSError, match=f"injected {failed_save} save failure"):
        auth._save_codex_tokens(new_tokens, last_refresh="2026-08-04T00:00:00Z")

    root_after_failure = json.loads(root_path.read_text(encoding="utf-8"))
    profile_after_failure = json.loads(profile_path.read_text(encoding="utf-8"))
    rows_after_failure = {
        row["id"]: row
        for row in root_after_failure["credential_pool"]["openai-codex"]
    }
    if failed_save == "shared":
        assert rows_after_failure["legacy-alias"]["access_token"] == "old-access"
        assert (
            profile_after_failure["providers"]["openai-codex"]["tokens"][
                "access_token"
            ]
            == "old-access"
        )
    else:
        assert rows_after_failure["legacy-alias"]["access_token"] == "new-access"
        assert (
            profile_after_failure["providers"]["openai-codex"]["tokens"][
                "access_token"
            ]
            == "old-access"
        )

    auth._save_codex_tokens(new_tokens, last_refresh="2026-08-04T00:00:00Z")

    root_after_retry = json.loads(root_path.read_text(encoding="utf-8"))
    profile_after_retry = json.loads(profile_path.read_text(encoding="utf-8"))
    rows = {
        row["id"]: row
        for row in root_after_retry["credential_pool"]["openai-codex"]
    }
    for alias_id in ("legacy-alias", "singleton-seed"):
        assert rows[alias_id]["access_token"] == "new-access"
        assert rows[alias_id]["refresh_token"] == "new-refresh"
        assert rows[alias_id]["last_status"] is None
    assert rows["independent-account"]["access_token"] == "independent-access"
    assert rows["independent-account"]["refresh_token"] == "independent-refresh"
    assert rows["independent-account"]["last_status"] == "exhausted"
    assert rows["independent-account"]["last_error_code"] == 429
    assert (
        profile_after_retry["providers"]["openai-codex"]["tokens"]
        == new_tokens
    )


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
# Writes and suppression metadata target the shared root
# ---------------------------------------------------------------------------


def test_write_credential_pool_targets_shared_root(profile_env):
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

    # The shared root now owns both the existing and newly-added entries.
    global_data = json.loads((profile_env["global"] / "auth.json").read_text())
    assert [entry["id"] for entry in global_data["credential_pool"]["openrouter"]] == [
        "prof-new",
        "glob-1",
    ]
    assert not (profile_env["profile"] / "auth.json").exists()
    assert [e["id"] for e in read_credential_pool("openrouter")] == [
        "prof-new",
        "glob-1",
    ]


def test_suppression_is_shared_across_profiles(profile_env):
    from hermes_cli.auth import is_source_suppressed, suppress_credential_source

    suppress_credential_source("openai-codex", "device_code")

    shared = json.loads((profile_env["global"] / "auth.json").read_text())
    assert shared["suppressed_sources"]["openai-codex"] == ["device_code"]
    assert not (profile_env["profile"] / "auth.json").exists()
    assert is_source_suppressed("openai-codex", "device_code") is True




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


def test_named_profile_runtime_pool_excludes_retained_seed_rows(profile_env):
    from hermes_cli.auth import read_runtime_credential_pool

    _write(
        profile_env["global"] / "auth.json",
        _make_auth_store(
            pool={
                "openai-codex": [
                    {
                        "id": "retained-seed",
                        "source": "device_code",
                        "access_token": "other-profile-access",
                    },
                    {
                        "id": "shared-manual",
                        "source": "manual:device_code",
                        "access_token": "shared-access",
                    },
                ]
            }
        ),
    )

    assert [
        entry["id"] for entry in read_runtime_credential_pool("openai-codex")
    ] == ["shared-manual"]


def test_classic_runtime_pool_keeps_legacy_seed_rows(classic_env):
    from hermes_cli.auth import read_runtime_credential_pool

    _write(
        classic_env / "auth.json",
        _make_auth_store(
            pool={
                "openai-codex": [
                    {
                        "id": "classic-seed",
                        "source": "device_code",
                        "access_token": "classic-access",
                    }
                ]
            }
        ),
    )

    assert [
        entry["id"] for entry in read_runtime_credential_pool("openai-codex")
    ] == ["classic-seed"]


def test_profile_migration_preserves_distinct_photon_rows(profile_env):
    from hermes_cli.auth import read_credential_pool

    _write(
        profile_env["global"] / "auth.json",
        _make_auth_store(
            pool={
                "photon_project": [
                    {
                        "id": "root-project",
                        "project_id": "root",
                        "project_secret": "root-secret",
                    }
                ],
                "photon_user": [
                    {
                        "id": "root-user",
                        "from_number": "+10000000001",
                        "user_number": "+10000000002",
                    }
                ],
            }
        ),
    )
    _write(
        profile_env["profile"] / "auth.json",
        _make_auth_store(
            pool={
                "photon_project": [
                    {
                        "id": "profile-project",
                        "project_id": "profile",
                        "project_secret": "profile-secret",
                    }
                ],
                "photon_user": [
                    {
                        "id": "profile-user",
                        "from_number": "+10000000003",
                        "user_number": "+10000000004",
                    }
                ],
            }
        ),
    )

    assert {entry["id"] for entry in read_credential_pool("photon_project")} == {
        "root-project",
        "profile-project",
    }
    assert {entry["id"] for entry in read_credential_pool("photon_user")} == {
        "root-user",
        "profile-user",
    }


def test_profile_migration_keeps_source_when_any_legacy_row_is_unsupported(
    profile_env,
):
    from hermes_cli.auth import read_credential_pool

    valid = {
        "id": "valid-row",
        "source": "manual",
        "access_token": "valid-token",
    }
    _write(
        profile_env["profile"] / "auth.json",
        _make_auth_store(pool={"openrouter": [valid, "unsupported-row"]}),
    )

    assert [entry["id"] for entry in read_credential_pool("openrouter")] == [
        "valid-row"
    ]
    profile_store = json.loads(
        (profile_env["profile"] / "auth.json").read_text(encoding="utf-8")
    )
    assert profile_store["credential_pool"]["openrouter"][1] == "unsupported-row"


def test_profile_migration_preserves_top_level_unsupported_suppressions(profile_env):
    from hermes_cli.auth import read_credential_pool

    valid = {
        "id": "valid-row",
        "source": "manual",
        "access_token": "valid-token",
    }
    profile_store = _make_auth_store(pool={"openrouter": [valid]})
    profile_store["suppressed_sources"] = "unsupported-shape"
    _write(profile_env["profile"] / "auth.json", profile_store)

    assert [entry["id"] for entry in read_credential_pool("openrouter")] == [
        "valid-row"
    ]
    preserved = json.loads(
        (profile_env["profile"] / "auth.json").read_text(encoding="utf-8")
    )
    assert preserved["suppressed_sources"] == "unsupported-shape"
    assert preserved["credential_pool"]["openrouter"][0]["id"] == "valid-row"


def test_profile_migration_preserves_nested_malformed_shared_suppression(profile_env):
    """An unsupported destination slice must not be replaced or cleaned up."""
    root_path = profile_env["global"] / "auth.json"
    profile_path = profile_env["profile"] / "auth.json"
    _write(
        root_path,
        {
            "version": 1,
            "providers": {},
            "suppressed_sources": {"openai-codex": "unsupported-shape"},
        },
    )
    _write(
        profile_path,
        {
            "version": 1,
            "providers": {},
            "suppressed_sources": {"openai-codex": ["device_code"]},
        },
    )

    from hermes_cli.auth import read_credential_pool

    assert read_credential_pool("openai-codex") == []
    root_store = json.loads(root_path.read_text(encoding="utf-8"))
    profile_store = json.loads(profile_path.read_text(encoding="utf-8"))
    assert root_store["suppressed_sources"]["openai-codex"] == "unsupported-shape"
    assert profile_store["suppressed_sources"]["openai-codex"] == ["device_code"]


def test_profile_migration_preserves_malformed_destination_elements(profile_env):
    """Unsupported nested destination values keep the profile source intact."""
    root_path = profile_env["global"] / "auth.json"
    profile_path = profile_env["profile"] / "auth.json"
    _write(
        root_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {"openrouter": ["unsupported-destination-row"]},
            "suppressed_sources": {
                "openrouter": ["env:EXISTING", {"unsupported": "source"}]
            },
        },
    )
    _write(
        profile_path,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openrouter": [
                    {
                        "id": "profile-row",
                        "source": "manual",
                        "api_key": "profile-key",
                    }
                ]
            },
            "suppressed_sources": {"openrouter": ["env:PROFILE"]},
        },
    )

    from hermes_cli.auth import read_credential_pool

    rows = read_credential_pool("openrouter")
    assert rows[0] == "unsupported-destination-row"
    assert any(isinstance(row, dict) and row.get("id") == "profile-row" for row in rows)
    root_store = json.loads(root_path.read_text(encoding="utf-8"))
    profile_store = json.loads(profile_path.read_text(encoding="utf-8"))
    assert root_store["suppressed_sources"]["openrouter"] == [
        "env:EXISTING",
        {"unsupported": "source"},
        "env:PROFILE",
    ]
    assert profile_store["credential_pool"]["openrouter"][0]["id"] == "profile-row"
    assert profile_store["suppressed_sources"]["openrouter"] == ["env:PROFILE"]


@pytest.mark.parametrize(
    "shared_pool",
    ["unsupported-top-level", {"openrouter": {"unsupported": "provider-shape"}}],
)
def test_profile_migration_preserves_malformed_shared_pool(profile_env, shared_pool):
    from hermes_cli.auth import read_credential_pool

    _write(
        profile_env["global"] / "auth.json",
        {"version": 1, "credential_pool": shared_pool},
    )
    _write(
        profile_env["profile"] / "auth.json",
        _make_auth_store(
            pool={
                "openrouter": [
                    {
                        "id": "profile-row",
                        "source": "manual",
                        "access_token": "profile-token",
                    }
                ]
            }
        ),
    )

    assert read_credential_pool("openrouter") == []
    root_store = json.loads(
        (profile_env["global"] / "auth.json").read_text(encoding="utf-8")
    )
    profile_store = json.loads(
        (profile_env["profile"] / "auth.json").read_text(encoding="utf-8")
    )
    assert root_store["credential_pool"] == shared_pool
    assert profile_store["credential_pool"]["openrouter"][0]["id"] == "profile-row"
