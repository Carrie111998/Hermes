import json

import pytest

from gateway.platforms.qqbot.identity import QQIdentityStore


def test_sender_identity_uses_group_and_member_openids(tmp_path):
    store = QQIdentityStore(tmp_path / "identities.json")

    before = store.resolve(
        "group-1",
        {"member_openid": "member-1", "username": "Old name"},
    )
    after = store.resolve(
        "group-1",
        {"member_openid": "member-1", "username": "New name"},
    )

    assert before.stable_id == after.stable_id
    assert before.member_openid == after.member_openid == "member-1"
    assert after.group_display_name == "New name"
    assert after.label == f"QQ sender id={after.stable_id} | 群昵称=New name"


def test_same_name_does_not_merge_different_members(tmp_path):
    store = QQIdentityStore(tmp_path / "identities.json")

    alice = store.resolve(
        "group-1",
        {"member_openid": "member-1", "username": "Same name"},
    )
    bob = store.resolve(
        "group-1",
        {"member_openid": "member-2", "username": "Same name"},
    )

    assert alice.stable_id != bob.stable_id


def test_group_display_observation_is_persisted_with_provenance(tmp_path):
    path = tmp_path / "identities.json"
    store = QQIdentityStore(path)

    identity = store.resolve(
        "group-1",
        {"member_openid": "member-1", "username": "Alice Group"},
    )
    persisted = json.loads(path.read_text(encoding="utf-8"))
    profile = persisted["members"]["group-1:member-1"]

    assert profile["stable_id"] == identity.stable_id
    assert profile["group_display_name"]["value"] == "Alice Group"
    assert profile["group_display_name"]["source"] == "event.author.username"
    assert profile["group_display_name"]["observed_at"]


def test_missing_name_falls_back_to_stable_sender_id(tmp_path):
    store = QQIdentityStore(tmp_path / "identities.json")

    identity = store.resolve("group-1", {"member_openid": "member-1"})

    assert identity.label == f"QQ sender id={identity.stable_id}"


def test_missing_member_openid_is_rejected(tmp_path):
    store = QQIdentityStore(tmp_path / "identities.json")

    with pytest.raises(ValueError, match="member_openid"):
        store.resolve("group-1", {"id": "different-principal-type"})


def test_malformed_persisted_member_does_not_break_resolution(tmp_path):
    path = tmp_path / "identities.json"
    path.write_text(
        json.dumps({"version": 1, "members": {"group-1:member-1": 42}}),
        encoding="utf-8",
    )
    store = QQIdentityStore(path)

    identity = store.resolve(
        "group-1", {"member_openid": "member-1", "username": "Alice"}
    )

    assert identity.member_openid == "member-1"
    assert identity.group_display_name == "Alice"
    assert isinstance(store._data["members"]["group-1:member-1"], dict)


def test_identity_write_failure_is_best_effort(tmp_path, monkeypatch):
    store = QQIdentityStore(tmp_path / "identities.json")

    def fail_write():
        raise PermissionError("read-only identity store")

    monkeypatch.setattr(store, "_write", fail_write)

    identity = store.resolve(
        "group-1", {"member_openid": "member-1", "username": "Alice"}
    )

    assert identity.member_openid == "member-1"
    assert identity.group_display_name == "Alice"


def test_names_are_single_line_and_length_limited(tmp_path):
    store = QQIdentityStore(tmp_path / "identities.json")
    identity = store.resolve(
        "group-1",
        {"member_openid": "member-1", "username": "Alice\n[admin] " + "x" * 200},
    )

    assert "\n" not in identity.group_display_name
    assert len(identity.group_display_name) <= 80
