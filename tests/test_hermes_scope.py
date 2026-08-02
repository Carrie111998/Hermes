"""Tests for hermes_scope.py -- identity normalization, manifest storage,
fail-closed reads, and ownership queries.

See docs/design/thread-scope-isolation.md for the design this implements.
"""
import json
import stat

import pytest

import hermes_scope as scope


def _identity(**overrides):
    base = dict(
        profile="main",
        platform="discord",
        chat_id="channel-1",
        account_id="bot-1",
        guild_scope_id="guild-1",
        thread_id="thread-A",
        topic="thread-scope-isolation",
    )
    base.update(overrides)
    return scope.normalize_scope_identity(**base)


class TestIdentityNormalization:
    def test_normalizes_required_and_optional_fields(self):
        identity = _identity()
        assert identity["profile"] == "main"
        assert identity["platform"] == "discord"
        assert identity["chat_id"] == "channel-1"
        assert identity["thread_id"] == "thread-A"
        assert identity["topic"] == "thread-scope-isolation"

    def test_optional_fields_default_to_none(self):
        identity = scope.normalize_scope_identity(
            profile="main", platform="discord", chat_id="channel-1"
        )
        assert identity["account_id"] is None
        assert identity["guild_scope_id"] is None
        assert identity["thread_id"] is None
        assert identity["topic"] is None

    @pytest.mark.parametrize("field", ["profile", "platform", "chat_id"])
    def test_missing_required_field_fails_closed(self, field):
        kwargs = dict(profile="main", platform="discord", chat_id="channel-1")
        kwargs[field] = ""
        with pytest.raises(scope.ScopeIdentityError):
            scope.normalize_scope_identity(**kwargs)

    def test_whitespace_only_required_field_fails_closed(self):
        with pytest.raises(scope.ScopeIdentityError):
            scope.normalize_scope_identity(profile="   ", platform="discord", chat_id="c1")

    def test_identity_never_carries_display_name_fields(self):
        # normalize_scope_identity has no display-name parameter at all --
        # this test documents that constraint so a future edit that adds
        # e.g. `channel_name`/`user_nickname` as an identity input is caught.
        import inspect

        params = set(inspect.signature(scope.normalize_scope_identity).parameters)
        assert not (params & {"channel_name", "user_nickname", "display_name", "username"})


class TestScopeIdHashing:
    def test_same_identity_same_scope_id(self):
        assert scope.compute_scope_id(_identity()) == scope.compute_scope_id(_identity())

    def test_different_thread_different_scope_id(self):
        a = scope.compute_scope_id(_identity(thread_id="thread-A"))
        b = scope.compute_scope_id(_identity(thread_id="thread-B"))
        assert a != b

    def test_different_profile_different_scope_id(self):
        a = scope.compute_scope_id(_identity(profile="main"))
        b = scope.compute_scope_id(_identity(profile="work"))
        assert a != b

    def test_different_topic_different_scope_id(self):
        a = scope.compute_scope_id(_identity(topic="topic-a"))
        b = scope.compute_scope_id(_identity(topic="topic-b"))
        assert a != b


class TestManifestStorage:
    def test_create_scope_writes_mode_0600(self, tmp_path):
        manifest = scope.create_scope(_identity(), goal="fix the bug", hermes_home=tmp_path)
        path = scope._manifest_path(manifest["scope_id"], tmp_path)
        assert path.exists()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_create_scope_is_idempotent_by_identity(self, tmp_path):
        first = scope.create_scope(_identity(), goal="fix the bug", hermes_home=tmp_path)
        second = scope.create_scope(_identity(), goal="a different goal text", hermes_home=tmp_path)
        assert first["scope_id"] == second["scope_id"]
        assert second["goal"] == "fix the bug"  # first write wins, not silently overwritten

    def test_load_scope_roundtrips(self, tmp_path):
        created = scope.create_scope(_identity(), goal="fix the bug", hermes_home=tmp_path)
        loaded = scope.load_scope(created["scope_id"], hermes_home=tmp_path)
        assert loaded == created

    def test_load_missing_scope_returns_none(self, tmp_path):
        assert scope.load_scope("sha256:doesnotexist", hermes_home=tmp_path) is None

    def test_load_corrupt_manifest_fails_closed(self, tmp_path):
        created = scope.create_scope(_identity(), goal="fix the bug", hermes_home=tmp_path)
        path = scope._manifest_path(created["scope_id"], tmp_path)
        path.write_text("{not valid json", encoding="utf-8")
        assert scope.load_scope(created["scope_id"], hermes_home=tmp_path) is None

    def test_resolve_scope_id_finds_created_scope(self, tmp_path):
        identity = _identity()
        created = scope.create_scope(identity, goal="fix the bug", hermes_home=tmp_path)
        resolved = scope.resolve_scope_id(identity, hermes_home=tmp_path)
        assert resolved == created["scope_id"]

    def test_resolve_scope_id_unknown_identity_returns_none(self, tmp_path):
        assert scope.resolve_scope_id(_identity(thread_id="never-created"), hermes_home=tmp_path) is None

    def test_resolve_scope_id_corrupt_index_fails_closed(self, tmp_path):
        identity = _identity()
        scope.create_scope(identity, goal="fix the bug", hermes_home=tmp_path)
        scope._index_path(tmp_path).write_text("not json", encoding="utf-8")
        assert scope.resolve_scope_id(identity, hermes_home=tmp_path) is None


class TestOwnedArtifacts:
    def test_link_and_query_ownership(self, tmp_path):
        created = scope.create_scope(_identity(), goal="fix the bug", hermes_home=tmp_path)
        scope.link_artifact(created["scope_id"], "session_keys", "sess-123", hermes_home=tmp_path)
        assert scope.owns(created["scope_id"], "session_keys", "sess-123", hermes_home=tmp_path)
        assert not scope.owns(created["scope_id"], "session_keys", "sess-999", hermes_home=tmp_path)

    def test_link_is_idempotent(self, tmp_path):
        created = scope.create_scope(_identity(), goal="fix the bug", hermes_home=tmp_path)
        scope.link_artifact(created["scope_id"], "branches", "feat/x", hermes_home=tmp_path)
        manifest = scope.link_artifact(created["scope_id"], "branches", "feat/x", hermes_home=tmp_path)
        assert manifest["owned"]["branches"].count("feat/x") == 1

    def test_link_unknown_category_fails_closed(self, tmp_path):
        created = scope.create_scope(_identity(), goal="fix the bug", hermes_home=tmp_path)
        with pytest.raises(scope.ScopeIdentityError):
            scope.link_artifact(created["scope_id"], "not_a_category", "x", hermes_home=tmp_path)

    def test_link_unknown_scope_fails_closed(self, tmp_path):
        with pytest.raises(scope.ScopeIdentityError):
            scope.link_artifact("sha256:doesnotexist", "branches", "feat/x", hermes_home=tmp_path)

    def test_unlink_removes_ownership(self, tmp_path):
        created = scope.create_scope(_identity(), goal="fix the bug", hermes_home=tmp_path)
        scope.link_artifact(created["scope_id"], "prs", "https://x/1", hermes_home=tmp_path)
        scope.unlink_artifact(created["scope_id"], "prs", "https://x/1", hermes_home=tmp_path)
        assert not scope.owns(created["scope_id"], "prs", "https://x/1", hermes_home=tmp_path)

    def test_owns_unknown_scope_is_false_not_raise(self, tmp_path):
        assert scope.owns("sha256:doesnotexist", "branches", "x", hermes_home=tmp_path) is False

    def test_two_scopes_same_repo_do_not_share_ownership(self, tmp_path):
        # Two threads referencing the same repository must stay isolated --
        # sharing a repo is not sharing ownership.
        scope_a = scope.create_scope(_identity(thread_id="thread-A"), goal="a", hermes_home=tmp_path)
        scope_b = scope.create_scope(_identity(thread_id="thread-B"), goal="b", hermes_home=tmp_path)
        scope.link_artifact(scope_a["scope_id"], "branches", "shared-repo-branch", hermes_home=tmp_path)
        assert scope.owns(scope_a["scope_id"], "branches", "shared-repo-branch", hermes_home=tmp_path)
        assert not scope.owns(scope_b["scope_id"], "branches", "shared-repo-branch", hermes_home=tmp_path)

    def test_same_tmux_name_different_projects_isolated(self, tmp_path):
        scope_a = scope.create_scope(_identity(chat_id="proj-a"), goal="a", hermes_home=tmp_path)
        scope_b = scope.create_scope(_identity(chat_id="proj-b"), goal="b", hermes_home=tmp_path)
        scope.link_artifact(scope_a["scope_id"], "tmux_session_keys", "main", hermes_home=tmp_path)
        scope.link_artifact(scope_b["scope_id"], "tmux_session_keys", "main", hermes_home=tmp_path)
        assert scope.owns(scope_a["scope_id"], "tmux_session_keys", "main", hermes_home=tmp_path)
        assert scope.owns(scope_b["scope_id"], "tmux_session_keys", "main", hermes_home=tmp_path)
        # Unlinking from A must not affect B's independently-stored copy.
        scope.unlink_artifact(scope_a["scope_id"], "tmux_session_keys", "main", hermes_home=tmp_path)
        assert not scope.owns(scope_a["scope_id"], "tmux_session_keys", "main", hermes_home=tmp_path)
        assert scope.owns(scope_b["scope_id"], "tmux_session_keys", "main", hermes_home=tmp_path)


class TestDependenciesAndLifecycle:
    def test_dependencies_tracked_separately_from_owned_artifacts(self, tmp_path):
        created = scope.create_scope(_identity(), goal="fix the bug", hermes_home=tmp_path)
        manifest = scope.add_dependency(created["scope_id"], "waiting on infra team", hermes_home=tmp_path)
        assert manifest["external_dependencies"][0]["description"] == "waiting on infra team"
        assert all(not v for v in manifest["owned"].values())

    def test_lifecycle_transitions(self, tmp_path):
        created = scope.create_scope(_identity(), goal="fix the bug", hermes_home=tmp_path)
        manifest = scope.set_lifecycle(created["scope_id"], "completed", hermes_home=tmp_path)
        assert manifest["lifecycle"] == "completed"

    def test_invalid_lifecycle_fails_closed(self, tmp_path):
        created = scope.create_scope(_identity(), goal="fix the bug", hermes_home=tmp_path)
        with pytest.raises(scope.ScopeIdentityError):
            scope.set_lifecycle(created["scope_id"], "vibing", hermes_home=tmp_path)


class TestPrivacy:
    def test_manifest_contains_no_raw_command_or_env_content(self, tmp_path):
        created = scope.create_scope(_identity(), goal="fix the bug", hermes_home=tmp_path)
        raw = json.dumps(created)
        for forbidden in ("HERMES_", "os.environ", "api_key", "token", "password"):
            assert forbidden not in raw
