"""TUI/Desktop backend dispatch coverage for ``/refresh``."""

from types import SimpleNamespace
from unittest.mock import patch
import threading
import pytest


_REAL_THREAD = threading.Thread


def _call_with_deadline(fn, timeout=0.5):
    """Run a lock-sensitive probe without letting a regression hang pytest."""
    done = threading.Event()
    outcome = {}

    def run():
        try:
            outcome["value"] = fn()
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            done.set()

    _REAL_THREAD(target=run, daemon=True).start()
    assert done.wait(timeout), "operation deadlocked on production threading.Lock"
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


class _DeferredThread:
    targets = []

    def __init__(self, target=None, daemon=None):
        self.target = target
        self.daemon = daemon
        self.__class__.targets.append(target)

    def start(self):
        return None

    def is_alive(self):
        return True


def _install_public_submit_probe(monkeypatch, server, session):
    _DeferredThread.targets = []
    monkeypatch.setattr(server.threading, "Thread", _DeferredThread)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: False)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda session: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda session: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *a, **k: None)
    server._sessions["sid"] = session


def test_public_prompt_submit_reserves_refresh_without_recursive_lock(monkeypatch):
    import tui_gateway.server as server

    session = {
        "agent": object(),
        "session_key": "refresh-public-key",
        "history": [{"role": "assistant", "content": "prior answer"}],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
    }
    original_history = list(session["history"])
    server._queue_pending_refresh_note(session, "REFRESH-NOTE")
    delivered = {}
    _install_public_submit_probe(monkeypatch, server, session)

    def capture_dispatch(rid, sid, active, text, **kwargs):
        delivered.update(
            text=text,
            model_input=server._prepend_note(text, kwargs["refresh_note"]),
            reservation=kwargs["refresh_reservation"],
        )

    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        capture_dispatch,
    )
    try:
        response = _call_with_deadline(
            lambda: server._methods["prompt.submit"](
                "rid", {"session_id": "sid", "text": "hello"}
            ),
            timeout=0.75,
        )
        assert response["result"]["status"] == "streaming"
        assert len(_DeferredThread.targets) == 1
        record = session["pending_refresh_notes"][0]
        assert record["reserved"] is True

        _call_with_deadline(_DeferredThread.targets[0], timeout=0.75)
        assert delivered["reservation"]["token"] == record["token"]
        assert delivered["model_input"] == "REFRESH-NOTE\n\nhello"
        assert delivered["text"] == "hello"
        assert session["history"] == original_history
        assert all("REFRESH-NOTE" not in str(message) for message in session["history"])
    finally:
        server._sessions.pop("sid", None)


def test_public_prompt_submit_skips_malformed_refresh_records_and_claims_later_valid(monkeypatch):
    import tui_gateway.server as server

    session = {
        "agent": object(),
        "session_key": "refresh-malformed-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "pending_refresh_notes": [
            None,
            {},
            {"token": "", "note": "empty token", "reserved": False},
            {"token": "missing-note", "reserved": False},
            {"token": "empty-note", "note": "", "reserved": False},
            {"token": "bad-reserved", "note": "bad", "reserved": "no"},
            {"token": "valid-token", "note": "VALID-NOTE", "reserved": False},
            {"token": "later-token", "note": "LATER-NOTE", "reserved": False},
        ],
    }
    _install_public_submit_probe(monkeypatch, server, session)
    delivered = []
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda *args, **kwargs: delivered.append(kwargs["refresh_reservation"]),
    )
    try:
        response = server._methods["prompt.submit"](
            "rid", {"session_id": "sid", "text": "hello"}
        )
        assert response["result"]["status"] == "streaming"
        assert session["running"] is True
        _DeferredThread.targets[0]()
        assert delivered == [{"token": "valid-token", "note": "VALID-NOTE"}]
        assert session["pending_refresh_notes"][-1]["reserved"] is False
    finally:
        server._sessions.pop("sid", None)


def test_cancel_before_agent_ready_unreserves_refresh_for_retry(monkeypatch):
    import tui_gateway.server as server

    session = {
        "agent": None,
        "session_key": "refresh-cancel-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
    }
    queued = server._queue_pending_refresh_note(session, "REFRESH-RETRY")
    _install_public_submit_probe(monkeypatch, server, session)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda *a, **k: pytest.fail("cancelled turn must not dispatch"),
    )
    try:
        response = _call_with_deadline(
            lambda: server._methods["prompt.submit"](
                "rid", {"session_id": "sid", "text": "hello"}
            ),
            timeout=0.75,
        )
        assert response["result"]["status"] == "streaming"
        assert session["pending_refresh_notes"][0]["reserved"] is True
        session["_turn_cancel_requested"] = True

        _call_with_deadline(_DeferredThread.targets[0], timeout=0.75)
        assert session["running"] is False
        assert session["pending_refresh_notes"][0]["reserved"] is False
        retry = server._claim_pending_refresh_note(session)
        assert retry == {"token": queued["token"], "note": "REFRESH-RETRY"}
    finally:
        server._sessions.pop("sid", None)


def test_command_dispatch_soft_refresh_queues_session_tail_context():
    import tui_gateway.server as server

    sid = "refresh-session"
    server._sessions[sid] = {
        "session_key": "refresh-key",
        "history": [],
        "history_lock": threading.Lock(),
    }
    result = SimpleNamespace(context_note="[fresh context]", report="Refreshed. Gateway not restarted.")
    try:
        with patch("agent.session_refresh.build_soft_refresh", return_value=result):
            response = _call_with_deadline(
                lambda: server._methods["command.dispatch"](
                    1, {"session_id": sid, "name": "refresh", "arg": ""}
                )
            )

        assert response["result"] == {"type": "exec", "output": result.report}
        assert [r["note"] for r in server._sessions[sid]["pending_refresh_notes"]] == [
            result.context_note
        ]
    finally:
        server._sessions.pop(sid, None)


def test_command_dispatch_refresh_branch_reuses_client_branch_semantics():
    import tui_gateway.server as server

    sid = "refresh-branch-session"
    server._sessions[sid] = {
        "session_key": "refresh-branch-key",
        "history": [],
        "history_lock": threading.Lock(),
    }
    try:
        response = _call_with_deadline(
            lambda: server._methods["command.dispatch"](
                1, {"session_id": sid, "name": "refresh", "arg": "--branch"}
            )
        )
        assert response["result"] == {"type": "alias", "target": "branch", "arg": ""}
    finally:
        server._sessions.pop(sid, None)


def test_refresh_note_claim_is_session_scoped_and_one_shot():
    import tui_gateway.server as server

    first = {"history_lock": threading.Lock()}
    second = {"history_lock": threading.Lock()}
    def exercise():
        server._queue_pending_refresh_note(first, "[fresh A]")
        server._queue_pending_refresh_note(second, "[fresh B]")
        assert server._claim_pending_refresh_note(first)["note"] == "[fresh A]"
        assert server._claim_pending_refresh_note(first) is None
        assert server._claim_pending_refresh_note(second)["note"] == "[fresh B]"

    _call_with_deadline(exercise)


def test_refresh_note_reservation_rolls_back_before_dispatch_and_commits_once_attempted():
    import tui_gateway.server as server

    session = {"history_lock": threading.Lock()}
    def exercise():
        server._queue_pending_refresh_note(session, "[fresh]")
        reservation = server._claim_pending_refresh_note(session)
        assert reservation["note"] == "[fresh]"
        assert server._claim_pending_refresh_note(session) is None
        server._finish_pending_refresh_note(
            session, reservation["token"], attempted=False
        )
        retry = server._claim_pending_refresh_note(session)
        assert retry["note"] == "[fresh]"
        server._finish_pending_refresh_note(session, retry["token"], attempted=True)
        assert "pending_refresh_notes" not in session
        assert server._claim_pending_refresh_note(session) is None

    _call_with_deadline(exercise)


def test_second_refresh_survives_commit_of_first_reserved_record():
    import tui_gateway.server as server

    session = {"history_lock": threading.Lock()}
    def exercise():
        first = server._queue_pending_refresh_note(session, "NOTE-1")
        reserved = server._claim_pending_refresh_note(session)
        assert reserved == {"token": first["token"], "note": "NOTE-1"}
        second = server._queue_pending_refresh_note(session, "NOTE-2")
        assert server._finish_pending_refresh_note(
            session, reserved["token"], attempted=True
        )
        next_reservation = server._claim_pending_refresh_note(session)
        assert next_reservation == {"token": second["token"], "note": "NOTE-2"}
        assert not server._finish_pending_refresh_note(
            session, reserved["token"], attempted=True
        )

    _call_with_deadline(exercise)


def test_older_queued_turn_cannot_steal_refresh_note_from_later_submission():
    import tui_gateway.server as server

    session = {"history_lock": threading.Lock()}
    server._enqueue_prompt(session, "queued before refresh", object())
    server._enqueue_prompt(
        session,
        "genuine after refresh",
        object(),
        refresh_reservation={"token": "refresh-token", "note": "[fresh context]"},
    )

    assert session["queued_prompt"]["text"] == "queued before refresh"
    assert "refresh_note" not in session["queued_prompt"]
    assert session["queued_prompts"] == [
        {
            "text": "genuine after refresh",
            "transport": session["queued_prompts"][0]["transport"],
            "generation": 0,
            "refresh_reservation": {
                "token": "refresh-token",
                "note": "[fresh context]",
            },
        }
    ]


def test_multimodal_queued_submission_carries_refresh_note_on_its_own_envelope():
    import tui_gateway.server as server

    session = {"history_lock": threading.Lock()}
    server._enqueue_prompt(
        session,
        [{"type": "text", "text": "inspect this"}, {"type": "image"}],
        object(),
        image_paths=["/tmp/example.png"],
        refresh_reservation={"token": "refresh-token", "note": "[fresh context]"},
    )

    assert session["queued_prompt"]["refresh_reservation"] == {
        "token": "refresh-token",
        "note": "[fresh context]",
    }
    assert session["queued_prompt"]["image_paths"] == ["/tmp/example.png"]


def test_busy_submission_reserves_refresh_note_without_recursive_lock(monkeypatch):
    import tui_gateway.server as server

    session = {
        "history_lock": threading.Lock(),
        "running": True,
        "attached_images": [],
    }
    server._queue_pending_refresh_note(session, "[fresh]")
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")

    response = _call_with_deadline(
        lambda: server._handle_busy_submit(
            "rid", "sid", session, "after refresh", object()
        )
    )

    assert response["result"]["status"] == "queued"
    assert session["queued_prompt"]["refresh_reservation"]["note"] == "[fresh]"


def test_stale_queued_generation_rolls_back_refresh_for_retry(monkeypatch):
    import tui_gateway.server as server

    session = {
        "history_lock": threading.Lock(),
        "running": False,
        "_queued_prompt_generation": 1,
    }
    server._queue_pending_refresh_note(session, "[fresh]")
    reservation = server._claim_pending_refresh_note(session)
    server._enqueue_prompt(
        session,
        "queued turn",
        object(),
        refresh_reservation=reservation,
    )
    session["_queued_prompt_generation"] = 2
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: pytest.fail("stale turn dispatched"))

    assert _call_with_deadline(
        lambda: server._drain_queued_prompt("rid", "sid", session)
    )
    retry = server._claim_pending_refresh_note(session)
    assert retry == reservation


def test_compute_host_rejection_rolls_back_but_dispatch_ack_commits(monkeypatch):
    import tui_gateway.server as server

    session = {
        "history_lock": threading.Lock(),
        "history": [],
        "history_version": 0,
        "session_key": "key",
        "attached_images": [],
        "cols": 80,
    }
    server._queue_pending_refresh_note(session, "NOTE-1")
    first = server._claim_pending_refresh_note(session)

    class RejectingSupervisor:
        def submit_turn(self, frame, *, on_complete, on_dispatch):
            on_complete({"type": "turn.error", "request_id": frame["request_id"], "message": "busy"})

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda cfg=None: RejectingSupervisor())
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_on_compute_host_turn_done", lambda *a, **k: None)
    response = server._submit_prompt_to_compute_host(
        "rid", "sid", session, "hello", refresh_reservation=first
    )
    assert response["result"]["status"] == "streaming"
    assert server._claim_pending_refresh_note(session) == first

    # Roll the retry back, then prove an authoritative dispatch ack consumes it.
    server._finish_pending_refresh_note(session, first["token"], attempted=False)
    retry = server._claim_pending_refresh_note(session)

    class DispatchingSupervisor:
        def submit_turn(self, frame, *, on_complete, on_dispatch):
            on_dispatch({"type": "turn.dispatched", "request_id": frame["request_id"]})
            on_complete({"type": "turn.end", "request_id": frame["request_id"]})

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda cfg=None: DispatchingSupervisor())
    server._submit_prompt_to_compute_host(
        "rid-2", "sid", session, "hello", refresh_reservation=retry
    )
    assert "pending_refresh_notes" not in session


def test_legacy_compute_host_async_busy_rolls_back_instead_of_synthetic_dispatch(monkeypatch):
    import tui_gateway.server as server

    session = {"history_lock": threading.Lock(), "history": [], "history_version": 0,
               "session_key": "key", "attached_images": [], "cols": 80}
    server._queue_pending_refresh_note(session, "NOTE")
    reservation = server._claim_pending_refresh_note(session)

    class LegacySupervisor:
        def submit_turn(self, frame, *, on_complete):
            on_complete({"type": "turn.error", "request_id": frame["request_id"],
                         "message": "session busy"})

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda cfg=None: LegacySupervisor())
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_on_compute_host_turn_done", lambda *a, **k: None)
    server._submit_prompt_to_compute_host("rid", "sid", session, "hello",
                                          refresh_reservation=reservation)
    assert server._claim_pending_refresh_note(session) == reservation


@pytest.mark.parametrize("metadata", [{"model_attempted": True}, {"api_calls": 1}])
def test_legacy_compute_host_success_metadata_commits_refresh(monkeypatch, metadata):
    import tui_gateway.server as server

    session = {"history_lock": threading.Lock(), "history": [], "history_version": 0,
               "session_key": "key", "attached_images": [], "cols": 80}
    server._queue_pending_refresh_note(session, "NOTE")
    reservation = server._claim_pending_refresh_note(session)

    class LegacySupervisor:
        def submit_turn(self, frame, *, on_complete):
            on_complete({"type": "turn.end", "request_id": frame["request_id"], **metadata})

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda cfg=None: LegacySupervisor())
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_on_compute_host_turn_done", lambda *a, **k: None)
    server._submit_prompt_to_compute_host("rid", "sid", session, "hello",
                                          refresh_reservation=reservation)
    assert "pending_refresh_notes" not in session


def test_legacy_compute_host_send_failure_rolls_back_refresh(monkeypatch):
    import tui_gateway.server as server

    session = {"history_lock": threading.Lock(), "history": [], "history_version": 0,
               "session_key": "key", "attached_images": [], "cols": 80}
    server._queue_pending_refresh_note(session, "NOTE")
    reservation = server._claim_pending_refresh_note(session)

    class LegacySupervisor:
        def submit_turn(self, frame, *, on_complete):
            on_complete({"type": "turn.error", "request_id": frame["request_id"],
                         "reason": "send_failed"})
            raise OSError("pipe closed")

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda cfg=None: LegacySupervisor())
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    response = server._submit_prompt_to_compute_host("rid", "sid", session, "hello",
                                                     refresh_reservation=reservation)
    assert response["error"]["code"] == 5019
    assert server._claim_pending_refresh_note(session) == reservation


@pytest.mark.parametrize(
    "metadata",
    [
        {"api_calls": "1"},
        {"api_calls": float("nan")},
        {"api_calls": float("inf")},
        {"api_calls": object()},
        {"model_attempted": "true"},
        {"model_attempted": 1},
    ],
)
def test_legacy_compute_host_malformed_attempt_metadata_rolls_back(monkeypatch, metadata):
    import tui_gateway.server as server

    session = {"history_lock": threading.Lock(), "history": [], "history_version": 0,
               "session_key": "key", "attached_images": [], "cols": 80}
    server._queue_pending_refresh_note(session, "NOTE")
    reservation = server._claim_pending_refresh_note(session)

    class LegacySupervisor:
        def submit_turn(self, frame, *, on_complete):
            on_complete({"type": "turn.end", "request_id": frame["request_id"], **metadata})

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda cfg=None: LegacySupervisor())
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_on_compute_host_turn_done", lambda *a, **k: None)

    server._submit_prompt_to_compute_host("rid", "sid", session, "hello",
                                          refresh_reservation=reservation)

    assert server._claim_pending_refresh_note(session) == reservation
