"""Adapter-level thread-scope isolation checks.

Verifies that gateway/session.py's build_session_key() -- the single source
of truth for session identity -- produces the distinct session_keys that
hermes_scope's thread-scope filtering depends on: two Discord threads under
one channel/guild must key to different sessions, while a channel message
that auto-threads (the "prospective thread" case) must key to the SAME
session as its later thread follow-ups, so it stays one scope rather than
splitting into two. See docs/design/thread-scope-isolation.md.
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
        # become the thread id once Discord auto-threads it).
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

    def test_prospective_continuity_means_one_scope_not_two(self, tmp_path):
        # Because both messages resolve to the same session_key, they persist
        # as the SAME session_id/session row (upsert-by-session_key), which
        # carries one thread_id once enriched -- so exactly one scope covers
        # both the kickoff message and its threaded follow-ups, never two.
        initiator = SessionSource(
            platform=Platform.DISCORD, chat_id="channel-99", chat_type="group",
            thread_id=None, prospective_thread_id="msg-12345",
        )
        followup = SessionSource(
            platform=Platform.DISCORD, chat_id="channel-99", chat_type="thread",
            thread_id="msg-12345",
        )
        assert build_session_key(initiator) == build_session_key(followup)

        # Once the real thread exists, scope identity is keyed on the real
        # (now-resolved) thread_id -- exactly one scope for this thread.
        identity = scope.normalize_scope_identity(
            profile="main", platform="discord", chat_id="channel-99", thread_id="msg-12345",
        )
        created = scope.create_scope(identity, goal="investigate the bug", hermes_home=tmp_path)
        resolved_again = scope.resolve_scope_id(identity, hermes_home=tmp_path)
        assert resolved_again == created["scope_id"]
