"""Lifecycle, concurrency, and cross-profile isolation tests for hermes_scope.

Covers the remaining mandatory scenarios from
docs/design/thread-scope-isolation.md not exercised by test_hermes_scope.py:
concurrent writers, restart/resume continuity (fresh re-derivation from live
identity, never from remembered state), and profile separation.
"""
import concurrent.futures

import hermes_scope as scope


def _identity(**overrides):
    base = dict(profile="main", platform="discord", chat_id="channel-1", thread_id="thread-A")
    base.update(overrides)
    return scope.normalize_scope_identity(**base)


class TestConcurrentWriters:
    def test_concurrent_link_calls_do_not_lose_updates(self, tmp_path):
        created = scope.create_scope(_identity(), goal="fix the bug", hermes_home=tmp_path)
        scope_id = created["scope_id"]
        values = [f"branch-{i}" for i in range(20)]

        def _link(value):
            scope.link_artifact(scope_id, "branches", value, hermes_home=tmp_path)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(_link, values))

        manifest = scope.load_scope(scope_id, hermes_home=tmp_path)
        assert sorted(manifest["owned"]["branches"]) == sorted(values)

    def test_concurrent_create_scope_is_idempotent(self, tmp_path):
        identity = _identity()

        def _create(_):
            return scope.create_scope(identity, goal="fix the bug", hermes_home=tmp_path)["scope_id"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            scope_ids = list(pool.map(_create, range(8)))

        assert len(set(scope_ids)) == 1

    def test_concurrent_create_of_distinct_scopes_all_survive_in_the_index(self, tmp_path):
        identities = [_identity(thread_id=f"thread-{i}") for i in range(12)]

        def _create(identity):
            return scope.create_scope(identity, goal="fix the bug", hermes_home=tmp_path)["scope_id"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            scope_ids = list(pool.map(_create, identities))

        assert len(set(scope_ids)) == len(identities)
        for identity, scope_id in zip(identities, scope_ids):
            assert scope.resolve_scope_id(identity, hermes_home=tmp_path) == scope_id


class TestRestartResumeContinuity:
    def test_scope_survives_reimport_of_the_module(self, tmp_path):
        """Simulates a process restart: identity is re-derived from live
        fields and the manifest is found fresh from disk, never from
        anything cached in the module."""
        identity = _identity()
        created = scope.create_scope(identity, goal="fix the bug", hermes_home=tmp_path)
        scope.link_artifact(created["scope_id"], "branches", "feat/x", hermes_home=tmp_path)

        import importlib

        reloaded = importlib.reload(scope)
        try:
            resolved_id = reloaded.resolve_scope_id(identity, hermes_home=tmp_path)
            assert resolved_id == created["scope_id"]
            assert reloaded.owns(resolved_id, "branches", "feat/x", hermes_home=tmp_path)
        finally:
            importlib.reload(scope)

    def test_clear_preserves_scope_when_thread_identity_is_stable(self, tmp_path):
        # /clear starts a new session_key but the platform's thread_id is
        # unchanged -- the scope (keyed on thread identity, not session_key)
        # must resolve to the same manifest before and after.
        identity = _identity(thread_id="thread-A")
        created = scope.create_scope(identity, goal="fix the bug", hermes_home=tmp_path)
        scope.link_artifact(created["scope_id"], "session_keys", "session-before-clear", hermes_home=tmp_path)

        # Re-derive identity exactly as a fresh turn after /clear would.
        post_clear_identity = _identity(thread_id="thread-A")
        resolved = scope.resolve_scope_id(post_clear_identity, hermes_home=tmp_path)
        assert resolved == created["scope_id"]
        scope.link_artifact(resolved, "session_keys", "session-after-clear", hermes_home=tmp_path)

        manifest = scope.load_scope(resolved, hermes_home=tmp_path)
        assert set(manifest["owned"]["session_keys"]) == {"session-before-clear", "session-after-clear"}


class TestProfileSeparation:
    def test_same_identity_different_profile_are_different_scopes(self, tmp_path):
        main_scope = scope.create_scope(_identity(profile="main"), goal="a", hermes_home=tmp_path)
        work_scope = scope.create_scope(_identity(profile="work"), goal="b", hermes_home=tmp_path)
        assert main_scope["scope_id"] != work_scope["scope_id"]

    def test_separate_hermes_home_directories_never_cross_resolve(self, tmp_path):
        home_a = tmp_path / "profile-a"
        home_b = tmp_path / "profile-b"
        identity = _identity()
        created = scope.create_scope(identity, goal="fix the bug", hermes_home=home_a)

        # Same identity tuple, but a different HERMES_HOME (as a Fleet
        # project or a different profile's root would be) must not resolve
        # -- storage isolation must hold even before identity comparison.
        assert scope.resolve_scope_id(identity, hermes_home=home_b) is None
        assert scope.load_scope(created["scope_id"], hermes_home=home_b) is None
