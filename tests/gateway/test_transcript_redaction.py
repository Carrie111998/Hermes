"""Regression tests for credential capture in gateway JSONL transcripts.

Phase 9 / Packet B4.

Pins a deliberate asymmetry: the JSONL transcript is redacted, the SQLite
write is not. state.db `messages` is the resume source of truth, so redacting
it would strip credentials out of a running agent's working context. The JSONL
is a redundant mirror that `load_transcript` only prefers when it holds MORE
messages than the DB (strict `>`), which no session on this machine does.

Constructed via __new__ rather than __init__ on purpose: SessionStore.__init__
builds a real SessionDB against the live HERMES_HOME state.db, and these tests
must not touch it.
"""

import json
import threading

import pytest

from gateway.session import SessionStore


CANARY_PREFIXED = "sk-proj-CANARYaaaabbbbccccddddeeeeffff"
CANARY_OPAQUE = "Zq7Z4mKp2Wf9Lx3Rv8Tn1Yb6Hd5Gs0Jc"


class _RecordingDB:
    """Captures what the SQLite branch was handed, without a database."""

    def __init__(self):
        self.appended = []
        self.replaced = []

    def append_message(self, **kwargs):
        self.appended.append(kwargs)

    def replace_messages(self, session_id, messages):
        self.replaced.append((session_id, messages))


@pytest.fixture
def store(tmp_path):
    s = SessionStore.__new__(SessionStore)
    s.sessions_dir = tmp_path
    s._lock = threading.Lock()
    s._db = _RecordingDB()
    return s


def _read_jsonl(store, session_id):
    return store.get_transcript_path(session_id).read_text()


class TestAppendToTranscript:
    def test_recognisable_credential_redacted_in_jsonl(self, store):
        store.append_to_transcript(
            "s1", {"role": "user", "content": f"my key is {CANARY_PREFIXED}"}
        )
        assert CANARY_PREFIXED not in _read_jsonl(store, "s1")

    def test_sensitive_key_redacted_in_jsonl(self, store):
        store.append_to_transcript(
            "s1",
            {"role": "tool", "tool_name": "http", "content": {"api_key": CANARY_OPAQUE}},
        )
        assert CANARY_OPAQUE not in _read_jsonl(store, "s1")

    def test_jsonl_remains_valid_and_structured(self, store):
        store.append_to_transcript("s1", {"role": "user", "content": "hello"})
        store.append_to_transcript("s1", {"role": "assistant", "content": "hi"})

        lines = [json.loads(l) for l in _read_jsonl(store, "s1").splitlines() if l.strip()]
        assert [m["role"] for m in lines] == ["user", "assistant"]
        assert lines[0]["content"] == "hello"

    def test_sqlite_branch_receives_UNREDACTED_content(self, store):
        """The deliberate asymmetry. If this ever starts failing because the
        DB write got redacted too, resume fidelity has been broken -- read
        _redacted_for_transcript before 'fixing' it."""
        store.append_to_transcript(
            "s1", {"role": "user", "content": f"my key is {CANARY_PREFIXED}"}
        )
        assert store._db.appended[0]["content"] == f"my key is {CANARY_PREFIXED}"

    def test_skip_db_still_redacts_jsonl(self, store):
        store.append_to_transcript(
            "s1", {"role": "user", "content": f"key {CANARY_PREFIXED}"}, skip_db=True
        )
        assert store._db.appended == []
        assert CANARY_PREFIXED not in _read_jsonl(store, "s1")

    def test_caller_message_not_mutated(self, store):
        message = {"role": "user", "content": f"key {CANARY_PREFIXED}"}
        store.append_to_transcript("s1", message)
        assert message["content"] == f"key {CANARY_PREFIXED}"


class TestRewriteTranscript:
    """/retry, /undo and /compress rewrite the whole file from in-memory
    history. Without redaction here, one rewrite would silently undo the
    containment on every previously-redacted line."""

    def test_rewrite_redacts_jsonl(self, store):
        store.rewrite_transcript(
            "s1",
            [
                {"role": "user", "content": f"key {CANARY_PREFIXED}"},
                {"role": "assistant", "content": "ok"},
            ],
        )
        raw = _read_jsonl(store, "s1")
        assert CANARY_PREFIXED not in raw
        assert "ok" in raw

    def test_rewrite_does_not_undo_prior_redaction(self, store):
        store.append_to_transcript(
            "s1", {"role": "user", "content": f"key {CANARY_PREFIXED}"}
        )
        store.rewrite_transcript(
            "s1", [{"role": "user", "content": f"key {CANARY_PREFIXED}"}]
        )
        assert CANARY_PREFIXED not in _read_jsonl(store, "s1")

    def test_rewrite_sqlite_branch_receives_UNREDACTED(self, store):
        messages = [{"role": "user", "content": f"key {CANARY_PREFIXED}"}]
        store.rewrite_transcript("s1", messages)
        _, passed = store._db.replaced[0]
        assert passed[0]["content"] == f"key {CANARY_PREFIXED}"


class TestNoDatabase:
    def test_jsonl_still_redacted_when_db_unavailable(self, tmp_path):
        """SessionStore falls back to JSONL-only when SQLite is unavailable.
        That fallback must not be an unredacted path."""
        s = SessionStore.__new__(SessionStore)
        s.sessions_dir = tmp_path
        s._lock = threading.Lock()
        s._db = None

        s.append_to_transcript("s1", {"role": "user", "content": f"key {CANARY_PREFIXED}"})
        assert CANARY_PREFIXED not in s.get_transcript_path("s1").read_text()
