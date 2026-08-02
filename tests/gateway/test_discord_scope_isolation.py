"""Adapter-level thread-scope isolation checks.

Verifies that gateway/session.py's build_session_key() -- the single source
of truth for session identity -- produces the distinct session_keys that
hermes_scope's thread-scope filtering depends on: two Discord threads under
one channel/guild must key to different session_keys. Also verifies (and
documents a currently-open gap in) the "prospective thread" case: a channel
message that auto-threads keys to the SAME session_key as its later thread
follow-ups, but hermes_scope's own identity does NOT yet inherit that same
continuity -- see TestProspectiveThreadContinuity's KNOWN_GAP test. See
docs/design/thread-scope-isolation.md.
"""
import hermes_scope as scope
from gateway.session import Platform, SessionSource, build_session_key


def _thread_source(thread_id, chat_id="channel-99"):
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        chat_type="thread",
        thread_id=thread_id,
    )


class TestDistinctThreadsGetDistinctSessionKeys:
    def test_two_threads_same_channel_different_session_keys(self):
        key_a = build_session_key(_thread_source("thread-A"))
        key_b = build_session_key(_thread_source("thread-B"))
        assert key_a != key_b

    def test_scope_identity_derived_from_distinct_threads_is_distinct(self, tmp_path):
        identity_a = scope.normalize_scope_identity(
            profile="main", platform="discord", chat_id="channel-99", thread_id="thread-A",
        )
        identity_b = scope.normalize_scope_identity(
            profile="main", platform="discord", chat_id="channel-99", thread_id="thread-B",
        )
        scope_a = scope.create_scope(identity_a, goal="a", hermes_home=tmp_path)
        scope_b = scope.create_scope(identity_b, goal="b", hermes_home=tmp_path)
        assert scope_a["scope_id"] != scope_b["scope_id"]


class TestProspectiveThreadContinuity:
    def test_channel_initiator_and_thread_followup_share_one_session_key(self):
        # The channel-initiating message has no real thread_id yet -- only
        # the connector's prospective_thread_id (the message id that WILL
        # become the thread id once Discord auto-threads it). At the
        # session_key layer, both resolve to the SAME key -- confirmed here.
        initiator = SessionSource(
            platform=Platform.DISCORD,
            chat_id="channel-99",
            chat_type="group",
            thread_id=None,
            prospective_thread_id="msg-12345",
        )
        followup = SessionSource(
            platform=Platform.DISCORD,
            chat_id="channel-99",
            chat_type="thread",
            thread_id="msg-12345",
        )
        assert build_session_key(initiator) == build_session_key(followup)

    def test_once_the_real_thread_resolves_its_scope_identity_is_stable(self, tmp_path):
        # After Discord auto-threads (real thread_id now known), scope
        # identity keyed on that real thread_id resolves consistently across
        # repeat lookups -- exactly one scope for the thread going forward.
        identity = scope.normalize_scope_identity(
            profile="main", platform="discord", chat_id="channel-99", thread_id="msg-12345",
        )
        created = scope.create_scope(identity, goal="investigate the bug", hermes_home=tmp_path)
        resolved_again = scope.resolve_scope_id(identity, hermes_home=tmp_path)
        assert resolved_again == created["scope_id"]

    def test_KNOWN_GAP_scope_identity_does_not_yet_track_prospective_continuity(self, tmp_path):
        """Documents a real, currently-unresolved gap -- NOT a passing guarantee.

        build_session_key() treats the channel-initiating message and its
        real-thread follow-up as the SAME session (via prospective_thread_id,
        see the test above). hermes_scope's identity, however, is normalized
        from HERMES_SESSION_THREAD_ID at each call
        (hermes_scope.identity_from_session_env), which is None during the
        initiating message (only the adapter's internal prospective_thread_id
        is set, not exposed through session env) and becomes "msg-12345" only
        once the real thread exists. So a scope created (or auto-linked to)
        during the initiating-message turn and one created after the thread
        resolves are, TODAY, two different identity tuples -- unlike
        session_key, which already unifies them.

        docs/design/thread-scope-isolation.md ("Continuity across
        compaction/resume/...") calls this exact case a "per-adapter
        decision" left open for the Discord adapter; this test exists so a
        future fix flips this assertion (proving the gap is closed) instead
        of the gap being silently forgotten.
        """
        identity_during_initiator = scope.normalize_scope_identity(
            profile="main", platform="discord", chat_id="channel-99", thread_id=None,
        )
        identity_after_thread_resolves = scope.normalize_scope_identity(
            profile="main", platform="discord", chat_id="channel-99", thread_id="msg-12345",
        )
        assert scope.compute_scope_id(identity_during_initiator) != scope.compute_scope_id(
            identity_after_thread_resolves
        )
