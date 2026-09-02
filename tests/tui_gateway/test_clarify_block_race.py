"""Regression tests for the clarify timeout / session.activate race.

Issue #96173: "Question from agent to user disappears".

The desktop / TUI client could lose the prompt box for an in-flight
clarifying question when the following interleaving happened:

  1. Agent calls ``_block("clarify.request", ...)``.
  2. Timeout fires while a reconnecting client is in the middle of
     calling ``session.activate``.
  3. ``_block`` pops ``_pending_prompt_payloads[rid]`` *before*
     emitting ``clarify.expire`` (so a fresh ``session.activate``
     returns ``pending_clarify = None``).
  4. ``clarify.expire`` then arrives at a client that has no matching
     request id, gets dropped, and the question UI is gone with no
     way to answer it.

The fix is to make sure ``clarify.expire`` is emitted *before* the
prompt payload is removed from the registry, so any session.activate
read either sees the snapshot AND the expire (consistent) or sees
neither (also consistent).
"""

from __future__ import annotations

import importlib
import json
import threading
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest


class TrackingDict(dict):
    """A dict subclass that records every ``pop`` call so we can assert
    the snapshot-pop happens *after* the corresponding expire emit.

    Python's ``dict.pop`` is a C-implemented method and cannot be
    patched with ``unittest.mock.patch.object`` (it raises
    "attribute is read-only"). Subclassing lets us install a Python
    wrapper that is itself a regular method and trivial to assert on.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pop_log: list[str] = []
        self._lock = threading.Lock()

    def pop(self, key, *args, **kwargs):
        with self._lock:
            self.pop_log.append(key)
        return super().pop(key, *args, **kwargs)


@pytest.fixture()
def server():
    """Import a fresh tui_gateway.server with the standard stub mocks."""
    with patch.dict(
        "sys.modules",
        {
            "hermes_constants": MagicMock(
                get_hermes_home=MagicMock(return_value="/tmp/hermes_test"),
            ),
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
            "hermes_state": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")

    methods = dict(mod._methods)
    real_stdout = mod._real_stdout
    real_pending_payloads = mod._pending_prompt_payloads
    real_pending = mod._pending
    real_answers = mod._answers
    real_batch = mod._batch_clarify
    yield mod
    mod._methods.clear()
    mod._methods.update(methods)
    mod._real_stdout = real_stdout
    for sid in list(mod._sessions):
        mod._close_session_by_id(sid, end_reason="test_cleanup")
    # Restore the original (possibly replaced) module-level dicts.
    mod._pending_prompt_payloads = real_pending_payloads
    mod._pending = real_pending
    mod._answers = real_answers
    mod._batch_clarify = real_batch


def _seed_session(server, sid: str) -> None:
    """Register a session id so ``_pending_clarify_request_payload`` matches it."""
    server._sessions.setdefault(sid, {})


def _install_tracking_payloads(server) -> TrackingDict:
    """Swap ``_pending_prompt_payloads`` for a TrackingDict on the module."""
    tracking = TrackingDict()
    server._pending_prompt_payloads = tracking
    # Ensure the helper code in the rest of the module looks at the swapped
    # name (it does — the name is resolved at call time via the module
    # globals).
    return tracking


def test_pending_clarify_snapshot_visible_after_block_timeout_until_expire_emitted(server):
    """The session.activate-visible snapshot must outlive the moment the
    expire notification is delivered. If a client reconnects at the same
    instant the timeout fires, the snapshot and the expire must be
    delivered in a coherent order:

      * snapshot still in _pending_prompt_payloads -> client sees the
        question, then the expire clears it
      * snapshot already gone -> no snapshot to restore, expire is a no-op

    The pre-fix code pops the snapshot BEFORE emitting expire, which
    breaks the first leg of that contract.
    """
    sid = "sess-race"
    _seed_session(server, sid)
    tracking = _install_tracking_payloads(server)

    timeline: list[tuple[str, str | None]] = []
    timeline_lock = threading.Lock()
    real_emit = server._emit

    def tracked_emit(event, sid_arg, payload=None):
        if event.endswith(".expire"):
            with timeline_lock:
                # Record whether the snapshot was still present when expire
                # was emitted. If the bug is present, the snapshot has
                # already been popped by this point.
                snapshot_still_present = (payload or {}).get("request_id") in tracking
                timeline.append(("expire", (payload or {}).get("request_id"), snapshot_still_present))
        return real_emit(event, sid_arg, payload)

    with patch.object(server, "_emit", side_effect=tracked_emit):
        # Drive _block on a worker thread with a tiny timeout so we hit
        # the expire path deterministically.
        worker = threading.Thread(
            target=server._block,
            args=(
                "clarify.request",
                sid,
                {"question": "Pick one", "choices": ["a", "b"]},
            ),
            kwargs={"timeout": 0.05},
        )
        worker.start()
        # Wait for the timeout to fire and _block to enter the post-finally
        # code path.
        worker.join(timeout=2.0)
        assert not worker.is_alive(), "_block did not return after timeout"

    # The post-fix invariant: at the moment ``clarify.expire`` was emitted,
    # the snapshot was still in the registry. Pre-fix, the pop in the
    # ``finally`` block ran first, so the snapshot was already gone.
    expire_entries = [t for t in timeline if t[0] == "expire"]
    assert len(expire_entries) == 1, timeline
    _, expire_rid, snapshot_still_present = expire_entries[0]
    assert expire_rid in tracking.pop_log, (
        "expire should have been emitted for the same rid that was popped"
    )
    assert snapshot_still_present, (
        f"expire was emitted for rid={expire_rid!r} AFTER the snapshot "
        f"was popped; a reconnecting client reads pending_clarify=None "
        f"and the expire drops as an orphan"
    )


def test_session_activate_reads_snapshot_when_timeout_just_fired(server):
    """A session.activate that races the timeout must see the pending
    snapshot until the expire has been durably emitted.

    Pre-fix: the snapshot is popped first, so a racing reader returns
    ``pending_clarify=None`` even though the client never received the
    expire (request-correlation drops orphan expires).
    """
    sid = "sess-activate-race"
    _seed_session(server, sid)
    tracking = _install_tracking_payloads(server)

    expire_emitted = threading.Event()
    release_expire = threading.Event()
    real_emit = server._emit

    def gated_emit(event, sid_arg, payload=None):
        if event.endswith(".expire"):
            expire_emitted.set()
            # Hold the emit thread here so a concurrent reader can still
            # observe the pre-pop snapshot in the dict.
            release_expire.wait(timeout=2.0)
        return real_emit(event, sid_arg, payload)

    with patch.object(server, "_emit", side_effect=gated_emit):
        worker = threading.Thread(
            target=server._block,
            args=(
                "clarify.request",
                sid,
                {"question": "Pick one", "choices": ["a", "b"]},
            ),
            kwargs={"timeout": 0.05},
        )
        worker.start()
        # Wait until expire would be emitted (gated_emit blocks).
        assert expire_emitted.wait(timeout=2.0), "expire was never emitted"
        # While expire is being emitted, a session.activate should still be
        # able to read the snapshot.
        snapshot = server._pending_clarify_request_payload(sid)
        # Now release the expire thread so it can finish the pop.
        release_expire.set()
        worker.join(timeout=2.0)
        assert not worker.is_alive()

    assert snapshot is not None, (
        "session.activate saw no pending snapshot even though the expire "
        "had not been delivered yet; the client will never get a chance "
        "to learn the question existed"
    )


def test_block_timeout_emits_expire_with_matching_request_id(server):
    """Sanity check the post-fix ordering for the non-batch case: when
    the timeout fires and no answer is present, the expire notification
    carries the same rid that was registered, and is emitted before the
    pop."""
    sid = "sess-single"
    _seed_session(server, sid)
    tracking = _install_tracking_payloads(server)

    captured: dict[str, object] = {}
    snapshot_at_emit: list[bool] = []
    real_emit = server._emit

    def capturing_emit(event, sid_arg, payload=None):
        if event == "clarify.expire":
            captured["event"] = event
            captured["sid"] = sid_arg
            captured["payload"] = dict(payload or {})
            rid = (payload or {}).get("request_id")
            snapshot_at_emit.append(rid in tracking)
        return real_emit(event, sid_arg, payload)

    with patch.object(server, "_emit", side_effect=capturing_emit):
        result = server._block(
            "clarify.request",
            sid,
            {"question": "Free form?", "choices": None},
            timeout=0.05,
        )

    assert result == "", "_block should return empty on timeout"
    assert captured.get("event") == "clarify.expire"
    rid = (captured.get("payload") or {}).get("request_id")
    assert rid and isinstance(rid, str)
    # The snapshot must still be visible at the instant expire was emitted.
    assert snapshot_at_emit == [True], (
        "expire was emitted AFTER the snapshot was popped; a reconnecting "
        "client sees no pending and the expire drops as an orphan"
    )


def test_block_does_not_emit_expire_when_user_answered(server):
    """When the user answered in time, no expire must be emitted and the
    snapshot can be popped immediately."""
    sid = "sess-answered"
    _seed_session(server, sid)

    expire_seen = threading.Event()
    real_emit = server._emit

    def watch_expire(event, sid_arg, payload=None):
        if event == "clarify.expire":
            expire_seen.set()
        return real_emit(event, sid_arg, payload)

    with patch.object(server, "_emit", side_effect=watch_expire):
        def resolver():
            # Wait for the entry to appear, then resolve with a real answer.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with server._prompt_lock:
                    for rid, (owner_sid, ev) in server._pending.items():
                        if owner_sid == sid:
                            server._answers[rid] = "alpha"
                            ev.set()
                            return
                time.sleep(0.01)

        t = threading.Thread(target=resolver)
        t.start()
        result = server._block(
            "clarify.request",
            sid,
            {"question": "Pick", "choices": ["alpha", "beta"]},
            timeout=2.0,
        )
        t.join(timeout=2.0)

    assert result == "alpha"
    assert not expire_seen.is_set(), "expire must not fire when the user answered"


def test_block_batch_timeout_emits_expire_before_pop(server):
    """Same ordering guarantee for the batch-clarify path: on a timeout
    with partial answers, expire must be emitted BEFORE the snapshot is
    popped from the registry."""
    sid = "sess-batch"
    _seed_session(server, sid)
    tracking = _install_tracking_payloads(server)

    snapshot_at_emit: list[bool] = []
    real_emit = server._emit

    def tracked_emit(event, sid_arg, payload=None):
        if event.endswith(".expire"):
            rid = (payload or {}).get("request_id")
            snapshot_at_emit.append(rid in tracking)
        return real_emit(event, sid_arg, payload)

    qids = [uuid.uuid4().hex[:8], uuid.uuid4().hex[:8]]
    with patch.object(server, "_emit", side_effect=tracked_emit):
        # No answers locked — should fire expire on timeout.
        result = server._block(
            "clarify.request",
            sid,
            {"questions": [{"qid": qids[0], "question": "Q1"}, {"qid": qids[1], "question": "Q2"}]},
            timeout=0.05,
            batch_qids=qids,
        )

    # The batch result is a JSON string with timed_out=True.
    parsed = json.loads(result)
    assert parsed.get("timed_out") is True

    assert snapshot_at_emit == [True], (
        "batch path: expire was emitted AFTER the snapshot was popped; a "
        "reconnecting client sees no pending and the expire drops as an "
        "orphan (issue #96173)"
    )
