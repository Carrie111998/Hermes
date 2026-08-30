"""Behavioral contract for the auth-store shard and corruption recovery."""

from __future__ import annotations

import json

import pytest

import hermes_cli.auth as auth


def _malformed_store(path):
    original = b'{"providers": {"openai-codex": '
    path.write_bytes(original)
    return original


def test_legacy_systems_store_migrates_on_ordinary_save(tmp_path):
    primary = tmp_path / "auth.json"
    primary.write_text(
        json.dumps(
            {
                "systems": {
                    "nous_portal": {"api_key": "preserved"},
                    "unused_system": {"state": "ignored"},
                }
            }
        ),
        encoding="utf-8",
    )

    store = auth._load_auth_store(primary)
    assert store == {
        "version": auth.AUTH_STORE_VERSION,
        "providers": {"nous": {"api_key": "preserved"}},
        "active_provider": "nous",
    }
    store["providers"]["nous"]["rotated"] = True

    auth._save_auth_store(store, primary)

    disk_store = json.loads(primary.read_text(encoding="utf-8"))
    assert disk_store["version"] == auth.AUTH_STORE_VERSION
    assert disk_store["providers"] == {
        "nous": {"api_key": "preserved", "rotated": True}
    }
    assert disk_store["active_provider"] == "nous"
    assert "systems" not in disk_store
    assert isinstance(disk_store["updated_at"], str)




@pytest.mark.parametrize(
    "canonical_section",
    [
        {"providers": {"other": {"state": "must-survive"}}},
        {"credential_pool": {"other": [{"state": "must-survive"}]}},
    ],
    ids=["providers", "credential-pool"],
)
def test_hybrid_legacy_and_canonical_store_is_rejected_and_preserved(
    tmp_path, canonical_section
):
    primary = tmp_path / "auth.json"
    original = json.dumps(
        {
            "version": auth.AUTH_STORE_VERSION,
            "systems": {"nous_portal": {"api_key": "legacy"}},
            **canonical_section,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    primary.write_bytes(original)

    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    assert caught.value.preserved is True
    assert caught.value.corrupt_path.read_bytes() == original
    with pytest.raises(auth.AuthStoreRecoveryRequired):
        auth._save_auth_store(
            {"version": auth.AUTH_STORE_VERSION, "providers": {"new": {}}},
            primary,
        )
    assert primary.read_bytes() == original


def test_corruption_is_read_only_and_preserved(tmp_path, monkeypatch):
    primary = tmp_path / "auth.json"
    original = _malformed_store(primary)

    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    state = caught.value
    assert state.path == primary
    assert state.preserved is True
    assert state.corrupt_path == tmp_path / "auth.json.corrupt"
    assert state.corrupt_path.read_bytes() == original

    with pytest.raises(auth.AuthStoreRecoveryRequired):
        auth._save_auth_store({"version": auth.AUTH_STORE_VERSION, "providers": {}}, primary)
    assert primary.read_bytes() == original


def test_failed_preservation_is_a_hard_stop(tmp_path, monkeypatch):
    primary = tmp_path / "auth.json"
    original = _malformed_store(primary)

    import hermes_cli.auth_store as auth_store

    def fail_copy(*args, **kwargs):
        raise OSError("synthetic preservation failure")

    monkeypatch.setattr(auth_store, "_write_corrupt_sidecar", fail_copy)
    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    state = caught.value
    assert state.path == primary
    assert state.preserved is False
    assert state.corrupt_path is None
    assert not (tmp_path / "auth.json.corrupt").exists()
    with pytest.raises(auth.AuthStoreRecoveryRequired):
        auth._save_auth_store({"version": auth.AUTH_STORE_VERSION, "providers": {}}, primary)
    assert primary.read_bytes() == original


def test_explicit_recovery_import_is_the_only_replacement_path(tmp_path):
    primary = tmp_path / "auth.json"
    _malformed_store(primary)
    replacement = {
        "version": auth.AUTH_STORE_VERSION,
        "active_provider": "openai-codex",
        "providers": {"openai-codex": {"source": "explicit-recovery"}},
    }

    with pytest.raises(auth.AuthStoreCorruptionError) as caught:
        auth._load_auth_store(primary)

    auth.recover_auth_store(
        replacement,
        primary,
        expected_corrupt_sha256=caught.value.corrupt_sha256,
        expected_corrupt_path=primary,
    )

    assert json.loads(primary.read_text(encoding="utf-8")) == {
        **replacement,
        "updated_at": json.loads(primary.read_text(encoding="utf-8"))["updated_at"],
    }


def test_auth_store_shard_owns_persistence_callables():
    import hermes_cli.auth_store as owner

    for name in (
        "_auth_file_path",
        "_global_auth_file_path",
        "_auth_lock_path",
        "_same_path",
        "_auth_lock_holder_for",
        "_file_lock",
        "_auth_store_lock",
        "_load_auth_store",
        "_save_auth_store",
        "_load_provider_state_with_source",
        "_provider_state_transaction",
        "_load_provider_state",
        "_save_provider_state",
        "_save_provider_state_to_source",
        "_store_provider_state",
        "_persist_provider_state_to_store",
    ):
        assert getattr(auth, name) is owner._public(name)
