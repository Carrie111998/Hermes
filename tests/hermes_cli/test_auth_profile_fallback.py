"""Tests for cross-profile auth fallback.

When ``HERMES_HOME`` points to a named profile, ``read_credential_pool()``
and ``get_provider_auth_state()`` fall back to the global-root
``auth.json`` per-provider when the profile has no entries for that
provider. Explicit profile writes stay local; runtime status/refresh writes on
an inherited row follow the global store that owns it.

See the #18594 follow-up report: profile workers couldn't see providers
authenticated only at the global root.
"""

from __future__ import annotations

import json
import multiprocessing
import os
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


def _concurrent_write_worker(auth_root: str, entry_id: str, start, results) -> None:
    os.environ["HERMES_HOME"] = auth_root
    try:
        from hermes_cli.auth import write_credential_pool

        start.wait(10)
        write_credential_pool("openrouter", [{
            "id": entry_id,
            "label": entry_id,
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "fixture-" + entry_id,
        }])
        results.put(None)
    except Exception as exc:  # pragma: no cover - reported in parent
        results.put("%s:%s" % (type(exc).__name__, exc))


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


def test_runtime_failure_on_global_fallback_never_materializes_profile_shadow(
    profile_env,
):
    """Runtime status writes follow the store that owns the inherited grant.

    A profile with no local Codex pool reads the global grant. Before this
    regression fix, a 401 status update persisted that inherited entry into
    the profile's auth.json. The resulting local row then shadowed every later
    repair to the global pool.
    """
    from agent.credential_pool import STATUS_DEAD, load_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openai-codex": [{
            "id": "glob-codex",
            "label": "global-codex",
            "auth_type": "oauth",
            "priority": 0,
            "source": "manual:device_code",
            "access_token": "global-access",
            "refresh_token": "global-refresh",
        }],
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={}))

    pool = load_pool("openai-codex")
    selected = pool.select()
    assert selected is not None
    assert selected.id == "glob-codex"
    pool.mark_exhausted_and_rotate(
        status_code=401,
        error_context={"reason": "token_invalidated"},
    )

    profile_store = json.loads(
        (profile_env["profile"] / "auth.json").read_text()
    )
    assert profile_store.get("credential_pool", {}).get("openai-codex", []) == []

    global_store = json.loads(
        (profile_env["global"] / "auth.json").read_text()
    )
    persisted = global_store["credential_pool"]["openai-codex"][0]
    assert persisted["id"] == "glob-codex"
    assert persisted["last_status"] == STATUS_DEAD
    assert persisted["last_error_reason"] == "token_invalidated"


def test_profile_add_shadows_global_without_copying_inherited_secret(profile_env):
    """An explicit profile add starts a local pool; it never copies globals."""
    from agent.credential_pool import (
        AUTH_TYPE_API_KEY,
        PooledCredential,
        SOURCE_MANUAL,
        load_pool,
    )

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "glob-openrouter",
            "label": "global",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "global-secret",
        }],
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={}))

    pool = load_pool("openrouter")
    pool.add_entry(PooledCredential(
        provider="openrouter",
        id="profile-openrouter",
        label="profile",
        auth_type=AUTH_TYPE_API_KEY,
        priority=0,
        source=SOURCE_MANUAL,
        access_token="profile-secret",
    ))

    profile_store = json.loads(
        (profile_env["profile"] / "auth.json").read_text()
    )
    rows = profile_store["credential_pool"]["openrouter"]
    assert [row["id"] for row in rows] == ["profile-openrouter"]
    assert "global-secret" not in (profile_env["profile"] / "auth.json").read_text()

    global_store = json.loads(
        (profile_env["global"] / "auth.json").read_text()
    )
    assert [
        row["id"] for row in global_store["credential_pool"]["openrouter"]
    ] == ["glob-openrouter"]


def test_profile_remove_returns_distinct_global_owner_refusal(profile_env):
    from agent.credential_pool import CredentialOwnershipRefused, load_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openrouter": [{
            "id": "glob-openrouter",
            "label": "global",
            "auth_type": "api_key",
            "priority": 0,
            "source": "manual",
            "access_token": "global-secret",
        }],
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={}))

    pool = load_pool("openrouter")
    with pytest.raises(
        CredentialOwnershipRefused,
        match="INHERITED-CREDENTIAL-OWNER-REFUSAL",
    ):
        pool.remove_index(1)
    assert json.loads(
        (profile_env["profile"] / "auth.json").read_text()
    ).get("credential_pool", {}).get("openrouter", []) == []


def test_persistence_suppression_makes_canary_write_paths_read_only(
    profile_env, monkeypatch
):
    from hermes_cli.auth import write_credential_pool

    original = _make_auth_store(pool={
        "openrouter": [_pool_entry(id="original", access_token="original-secret")],
    })
    _write(profile_env["profile"] / "auth.json", original)
    before = (profile_env["profile"] / "auth.json").read_bytes()
    monkeypatch.setenv("HERMES_CREDENTIAL_PERSISTENCE_SUPPRESSED", "1")
    write_credential_pool(
        "openrouter",
        [_pool_entry(id="replacement", access_token="replacement-secret")],
    )
    assert (profile_env["profile"] / "auth.json").read_bytes() == before


def test_recovery_suppression_raises_credential_brake(profile_env, monkeypatch):
    from agent.credential_pool import CredentialBrakeRequired, load_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "openai-codex": [{
            "id": "glob-codex",
            "label": "global",
            "auth_type": "oauth",
            "priority": 0,
            "source": "manual:device_code",
            "access_token": "expired-access",
            "refresh_token": "must-not-use",
            "expires_at_ms": 1,
        }],
    }))
    _write(profile_env["profile"] / "auth.json", _make_auth_store(pool={}))
    monkeypatch.setenv("HERMES_CREDENTIAL_RECOVERY_SUPPRESSED", "1")
    pool = load_pool("openai-codex")
    with pytest.raises(CredentialBrakeRequired, match="CREDENTIAL-BRAKE-REQUIRED"):
        pool.try_refresh_matching(credential_id="glob-codex")




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


def test_concurrent_owner_store_writers_are_serialized_and_merged(classic_env):
    """Two processes cannot tear or last-writer-drop the owner auth store."""
    _write(classic_env / "auth.json", _make_auth_store(pool={"openrouter": []}))
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_write_worker,
            args=(str(classic_env), entry_id, start, results),
        )
        for entry_id in ("writer-a", "writer-b")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    assert [results.get(timeout=2), results.get(timeout=2)] == [None, None]
    persisted = json.loads((classic_env / "auth.json").read_text())
    assert {row["id"] for row in persisted["credential_pool"]["openrouter"]} == {
        "writer-a",
        "writer-b",
    }


def test_crash_before_atomic_replace_preserves_complete_owner_store(
    classic_env, monkeypatch
):
    """A crash after tmpfile fsync but before rename leaves old JSON intact."""
    import hermes_cli.auth as auth

    original = _make_auth_store(pool={"openrouter": [_pool_entry(id="original")]})
    _write(classic_env / "auth.json", original)
    before = (classic_env / "auth.json").read_bytes()

    def crash_before_replace(_source, _destination):
        raise RuntimeError("fixture-crash-before-rename")

    monkeypatch.setattr(auth, "atomic_replace", crash_before_replace)
    with pytest.raises(RuntimeError, match="fixture-crash-before-rename"):
        auth.write_credential_pool(
            "openrouter", [_pool_entry(id="replacement")]
        )
    assert (classic_env / "auth.json").read_bytes() == before
    assert list(classic_env.glob("auth.json.tmp.*")) == []
