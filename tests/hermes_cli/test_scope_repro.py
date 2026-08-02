"""Reproduction of the thread-scope-isolation root-cause incident.

A progress question asked in one Discord thread was answered using work
that belonged to a *different* thread in the same channel/repo. This test
reconstructs the minimal shape of that failure at the ownership layer: two
scopes derived from two threads under one channel, artifacts (a tmux
session and a delegation) belong only to the thread that actually did the
work, and asking "does scope A own this artifact" must say no for anything
that belongs to scope B -- even though both threads share every other
identity field (profile, platform, guild, channel).

See docs/design/thread-scope-isolation.md for the full root-cause writeup.
"""
import hermes_scope as scope


def _thread_identity(thread_id):
    return scope.normalize_scope_identity(
        profile="main",
        platform="discord",
        account_id="bot-1",
        guild_scope_id="guild-42",
        chat_id="channel-99",
        thread_id=thread_id,
    )


class TestCrossThreadProgressContamination:
    def test_work_in_one_thread_is_not_visible_as_progress_in_another(self, tmp_path):
        thread_a = scope.create_scope(
            _thread_identity("thread-A"),
            goal="investigate the SSL cert bug",
            hermes_home=tmp_path,
        )
        thread_b = scope.create_scope(
            _thread_identity("thread-B"),
            goal="unrelated feature request",
            hermes_home=tmp_path,
        )

        # Thread A did real work: a tmux session and a background delegation.
        scope.link_artifact(thread_a["scope_id"], "tmux_session_keys", "agent:main:discord:thread:guild-42:channel-99:thread-A", hermes_home=tmp_path)
        scope.link_artifact(thread_a["scope_id"], "delegation_ids", "deleg-abc123", hermes_home=tmp_path)
        scope.link_artifact(thread_a["scope_id"], "branches", "fix/ssl-cert-bug", hermes_home=tmp_path)

        # A progress question asked from thread B must not see thread A's work,
        # even though both threads share profile/platform/guild/channel identity.
        assert not scope.owns(thread_b["scope_id"], "tmux_session_keys", "agent:main:discord:thread:guild-42:channel-99:thread-A", hermes_home=tmp_path)
        assert not scope.owns(thread_b["scope_id"], "delegation_ids", "deleg-abc123", hermes_home=tmp_path)
        assert not scope.owns(thread_b["scope_id"], "branches", "fix/ssl-cert-bug", hermes_home=tmp_path)

        # And thread A's own status check does see it.
        assert scope.owns(thread_a["scope_id"], "tmux_session_keys", "agent:main:discord:thread:guild-42:channel-99:thread-A", hermes_home=tmp_path)
        assert scope.owns(thread_a["scope_id"], "delegation_ids", "deleg-abc123", hermes_home=tmp_path)
        assert scope.owns(thread_a["scope_id"], "branches", "fix/ssl-cert-bug", hermes_home=tmp_path)

    def test_scopes_resolve_independently_from_live_identity_not_remembered_ids(self, tmp_path):
        # Re-derive both scope_ids fresh, exactly as a live progress-question
        # handler would (never trusting a scope_id parsed out of prior
        # conversation/compaction text).
        identity_a = _thread_identity("thread-A")
        identity_b = _thread_identity("thread-B")
        created_a = scope.create_scope(identity_a, goal="investigate the SSL cert bug", hermes_home=tmp_path)
        scope.link_artifact(created_a["scope_id"], "branches", "fix/ssl-cert-bug", hermes_home=tmp_path)

        resolved_a = scope.resolve_scope_id(identity_a, hermes_home=tmp_path)
        resolved_b = scope.resolve_scope_id(identity_b, hermes_home=tmp_path)

        assert resolved_a == created_a["scope_id"]
        assert resolved_b is None  # thread B never created a scope -- must not fall back to A's
        assert scope.owns(resolved_a, "branches", "fix/ssl-cert-bug", hermes_home=tmp_path)
