import json
import logging
import os
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_cli.smart_orchestrator import (
    ROUTE_AMBIGUOUS,
    ROUTE_DEPENDENT,
    ROUTE_INDEPENDENT,
    ROUTE_RELATED,
    SmartRouteDecision,
)


def _decision(route):
    return SmartRouteDecision(
        route=route, confidence=0.95, reason=f"reason-{route}", source="classifier"
    )


def _cli():
    from cli import HermesCLI

    instance = object.__new__(HermesCLI)
    instance._agent_running = True
    instance._pending_input = queue.Queue()
    instance._interrupt_queue = queue.Queue()
    instance._smart_cli_input_queue = queue.Queue()
    instance._smart_cli_worker_lock = MagicMock()
    instance.agent = MagicMock()
    instance.agent.steer.return_value = True
    instance._smart_cli_turn_lock = threading.Lock()
    instance._smart_cli_turn_generation = 7
    instance._smart_cli_active_turn = (7, "Fix the gateway", instance.agent)
    instance.agent.get_activity_summary.return_value = {"current_tool": "terminal"}
    return instance


def test_cli_smart_related_steers_without_interrupt_or_next_turn_queue(monkeypatch):
    cli = _cli()
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_RELATED), "add this requirement"),
    )

    route = cli._route_smart_cli_input("add this requirement")

    assert route == ROUTE_RELATED
    cli.agent.steer.assert_called_once_with("add this requirement")
    assert cli._pending_input.empty()
    assert cli._interrupt_queue.empty()


def test_cli_smart_route_diagnostic_is_metadata_only(caplog, monkeypatch):
    cli = _cli()
    cli.agent.session_id = "private-session"
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (
            SmartRouteDecision(
                route=ROUTE_RELATED,
                confidence=0.987,
                reason="private-reason",
                source="classifier",
            ),
            "private-payload",
        ),
    )

    with caplog.at_level(logging.INFO, logger="cli"):
        cli._route_smart_cli_input("private-payload")

    rendered = caplog.text
    assert "smart_route" in rendered
    for private_value in (
        "confidence",
        "0.987",
        "private-payload",
        "private-reason",
        "private-session",
    ):
        assert private_value not in rendered


def test_cli_smart_independent_injects_directive_without_false_parallel_ack(monkeypatch):
    cli = _cli()
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_INDEPENDENT), "research another market"),
    )

    route = cli._route_smart_cli_input("research another market")

    assert route == ROUTE_RELATED
    injected = cli.agent.steer.call_args.args[0]
    assert "SMART ORCHESTRATOR" in injected
    assert "delegate_task" in injected
    assert "research another market" in injected
    assert cli._pending_input.empty()
    assert cli._interrupt_queue.empty()


def test_cli_smart_classifier_receives_immutable_active_prompt(monkeypatch):
    cli = _cli()
    seen = {}

    def classify(**kwargs):
        seen.update(kwargs)
        return _decision(ROUTE_DEPENDENT), "next"

    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        classify,
    )

    cli._route_smart_cli_input("next")

    assert seen["active_goal"] == "Fix the gateway"


def test_cli_smart_reused_agent_generation_change_queues_old_decision(monkeypatch):
    cli = _cli()

    def classify(**_kwargs):
        # The same cached agent starts turn N+1 while turn N's classification
        # is in flight. Identity still matches, but ownership does not.
        cli._smart_cli_turn_generation = 8
        cli._smart_cli_active_turn = (8, "New turn", cli.agent)
        return _decision(ROUTE_RELATED), "stale correction"

    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        classify,
    )

    route = cli._route_smart_cli_input("stale correction")

    assert route == ROUTE_AMBIGUOUS
    cli.agent.steer.assert_not_called()
    assert cli._pending_input.get_nowait() == "stale correction"


def test_cli_smart_dependent_and_ambiguous_queue_losslessly(monkeypatch):
    for route in (ROUTE_DEPENDENT, ROUTE_AMBIGUOUS):
        cli = _cli()
        original = f"request-{route}"
        monkeypatch.setattr(
            "hermes_cli.smart_orchestrator.classify_smart_message",
            lambda route=route, **_kwargs: (_decision(route), original),
        )

        returned = cli._route_smart_cli_input(original)

        assert returned == route
        assert cli._pending_input.get_nowait() == original
        assert cli._interrupt_queue.empty()
        cli.agent.interrupt.assert_not_called()


def test_cli_smart_steer_race_falls_back_to_next_turn_queue(monkeypatch):
    cli = _cli()
    cli.agent.steer.return_value = False
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_RELATED), "late correction"),
    )

    route = cli._route_smart_cli_input("late correction")

    assert route == ROUTE_AMBIGUOUS
    assert cli._pending_input.get_nowait() == "late correction"
    assert cli._interrupt_queue.empty()


def test_cli_smart_no_longer_active_queues_without_attempting_steer(monkeypatch):
    cli = _cli()
    cli._agent_running = False
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_RELATED), "arrived at boundary"),
    )

    route = cli._route_smart_cli_input("arrived at boundary")

    assert route == ROUTE_AMBIGUOUS
    cli.agent.steer.assert_not_called()
    assert cli._pending_input.get_nowait() == "arrived at boundary"
    assert cli._interrupt_queue.empty()


def test_cli_smart_enqueue_captures_turn_context_at_admission():
    """FIFO wait must not recapture a cached agent's later turn generation."""
    cli = _cli()
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True

    snapshot = cli._begin_smart_cli_turn("turn N")
    cli._enqueue_smart_cli_input("message admitted during N")

    job = cli._smart_cli_input_queue.get_nowait()
    assert job.text == "message admitted during N"
    assert job.route_context.turn_snapshot is snapshot
    assert job.route_context.agent is cli.agent


def test_cli_smart_queue_rejects_33rd_classifier_job_without_losing_fifo():
    cli = _cli()
    cli._smart_cli_input_queue = queue.Queue(maxsize=32)
    cli._smart_cli_queue_lock = threading.Lock()
    cli._smart_cli_queued_bytes = 0
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True

    assert all(cli._enqueue_smart_cli_input(f"item-{index}") for index in range(32))
    assert cli._enqueue_smart_cli_input("overflow") is False
    assert cli._smart_cli_input_queue.qsize() == 32
    assert [cli._smart_cli_input_queue.get_nowait().text for _ in range(32)] == [
        f"item-{index}" for index in range(32)
    ]


def test_cli_smart_queue_rejects_oversized_classifier_job():
    cli = _cli()
    cli._smart_cli_queue_lock = threading.Lock()
    cli._smart_cli_queued_bytes = 0

    assert cli._enqueue_smart_cli_input("x" * (1024 * 1024 + 1)) is False
    assert cli._smart_cli_input_queue.empty()


def test_cli_smart_editor_rejection_keeps_composer_without_volatile_fallback(
    monkeypatch,
):
    cli = _cli()
    cli.busy_input_mode = "smart"
    cli._app = MagicMock()
    buffer = MagicMock()
    buffer.text = "keep this exact draft"
    monkeypatch.setattr(cli, "_enqueue_smart_cli_input", MagicMock(return_value=False))

    cli._submit_editor_buffer(buffer)

    assert buffer.text == "keep this exact draft"
    buffer.reset.assert_not_called()
    assert cli._pending_input.empty()


def test_cli_smart_enter_rejection_keeps_composer_without_volatile_fallback(
    monkeypatch,
):
    cli = _cli()
    cli.busy_input_mode = "smart"
    cli._attached_images = []
    cli._sudo_state = None
    cli._secret_state = None
    cli._approval_state = None
    cli._slash_confirm_state = None
    cli._model_picker_state = None
    cli._clarify_state = None
    cli._clarify_freetext = False
    cli._should_handle_model_command_inline = MagicMock(return_value=False)
    cli._should_handle_steer_command_inline = MagicMock(return_value=False)
    monkeypatch.setattr(cli, "_enqueue_smart_cli_input", MagicMock(return_value=False))
    monkeypatch.setattr("agent.onboarding.is_seen", lambda *_args, **_kwargs: True)

    buffer = MagicMock()
    buffer.text = "keep primary composer draft"
    app = MagicMock()
    app.current_buffer = buffer
    event = MagicMock()
    event.app = app

    cli._submit_normal_input(event)

    assert buffer.text == "keep primary composer draft"
    buffer.reset.assert_not_called()
    assert cli._pending_input.empty()


def test_cli_interrupt_enter_does_not_write_private_payload_debug_file(
    tmp_path, monkeypatch
):
    cli = _cli()
    cli.busy_input_mode = "interrupt"
    cli._attached_images = []
    cli._sudo_state = None
    cli._secret_state = None
    cli._approval_state = None
    cli._slash_confirm_state = None
    cli._model_picker_state = None
    cli._clarify_state = None
    cli._clarify_freetext = False
    cli._should_handle_model_command_inline = MagicMock(return_value=False)
    cli._should_handle_steer_command_inline = MagicMock(return_value=False)
    monkeypatch.setattr("cli._hermes_home", tmp_path)
    monkeypatch.setattr("agent.onboarding.is_seen", lambda *_args, **_kwargs: True)

    private_payload = "private customer payload must stay out of logs"
    buffer = MagicMock()
    buffer.text = private_payload
    app = MagicMock()
    app.current_buffer = buffer
    event = MagicMock()
    event.app = app

    cli._submit_normal_input(event)

    assert cli._interrupt_queue.get_nowait() == private_payload
    assert not (tmp_path / "interrupt_debug.log").exists()


def test_cli_smart_durable_append_serializes_independent_session_owners(
    tmp_path, monkeypatch
):
    """Two owners must not lose either receipt in load-modify-replace."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    first = _cli()
    second = _cli()
    session_id = "shared-session"
    barrier = threading.Barrier(2)

    def delay_after_load(owner):
        original = owner._load_smart_cli_durable_jobs_locked

        def load(candidate_session_id=None):
            jobs = original(candidate_session_id)
            try:
                barrier.wait(timeout=0.25)
            except threading.BrokenBarrierError:
                pass
            return jobs

        monkeypatch.setattr(owner, "_load_smart_cli_durable_jobs_locked", load)

    delay_after_load(first)
    delay_after_load(second)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                first._append_smart_cli_durable_job,
                "receipt-first",
                "first",
                session_id,
            ),
            executor.submit(
                second._append_smart_cli_durable_job,
                "receipt-second",
                "second",
                session_id,
            ),
        ]
        for future in futures:
            future.result(timeout=2)

    jobs = first._load_smart_cli_durable_jobs_locked(session_id)
    assert {job["id"] for job in jobs} == {"receipt-first", "receipt-second"}


def test_cli_smart_durable_claim_allows_exactly_one_owner_to_cross_effect(
    tmp_path, monkeypatch
):
    """Two live owners racing one accepted receipt cannot both call steer."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    session_id = "shared-claim-session"
    durable_id = "shared-claim-receipt"
    effects = []
    classifier_barrier = threading.Barrier(2)

    class ClaimingAgent:
        provider = None
        model = None

        def __init__(self, owner):
            self.owner = owner
            self.session_id = session_id

        def get_activity_summary(self):
            return {"current_tool": "terminal"}

        def get_steer_generation(self):
            return 73

        def supports_steer_consumption_ack(self):
            return True

        def steer(self, _payload, **_kwargs):
            effects.append(self.owner)
            return True

    owners = []
    for owner_name in ("first", "second"):
        owner = _cli()
        owner.agent = ClaimingAgent(owner_name)
        owner._smart_cli_active_turn = (7, "active turn", owner.agent)
        owners.append(owner)

    owners[0]._append_smart_cli_durable_job(
        durable_id,
        "apply exactly once",
        session_id,
    )

    def classify(**_kwargs):
        classifier_barrier.wait(timeout=2)
        return _decision(ROUTE_RELATED), "apply exactly once"

    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        classify,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                owner._route_smart_cli_input,
                "apply exactly once",
                owner._capture_smart_cli_route_context(),
                durable_id,
                session_id,
            )
            for owner in owners
        ]
        for future in futures:
            future.result(timeout=3)

    assert len(effects) == 1
    assert owners[0]._load_smart_cli_durable_jobs_locked(session_id) == [
        {
            "id": durable_id,
            "text": "apply exactly once",
            "state": "transferring",
        }
    ]


@pytest.mark.parametrize(
    "predecessor_state",
    ["accepted", "processing", "transferring", "uncertain"],
)
def test_cli_smart_durable_claim_is_blocked_by_every_live_predecessor(
    tmp_path, monkeypatch, predecessor_state
):
    from cli import SmartCliDurableDisposition

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    session_id = "predecessor-fence-session"
    cli.agent.session_id = session_id
    cli._append_smart_cli_durable_job("receipt-a", "first", session_id)
    cli._append_smart_cli_durable_job("receipt-b", "second", session_id)
    if predecessor_state != "accepted":
        assert cli._set_smart_cli_durable_job_state(
            "receipt-a",
            predecessor_state,
            session_id,
            expected_state="accepted",
        ) is True

    disposition = cli._claim_smart_cli_durable_job(
        "receipt-b",
        "processing",
        session_id,
        owner_session_id=session_id,
    )

    assert disposition is SmartCliDurableDisposition.BLOCKED_BY_PREDECESSOR
    assert cli._load_smart_cli_durable_jobs_locked(session_id) == [
        {"id": "receipt-a", "text": "first", "state": predecessor_state},
        {"id": "receipt-b", "text": "second", "state": "accepted"},
    ]


def test_cli_smart_claim_attempt_token_fences_late_callback_after_requeue(
    tmp_path, monkeypatch
):
    """A callback from attempt N cannot commit the same receipt at attempt N+1."""
    from cli import SmartCliDurableDisposition

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    session_id = "attempt-aba-session"
    cli.agent.session_id = session_id
    cli._append_smart_cli_durable_job("receipt-a", "payload-a", session_id)

    first_token = "a" * 32
    second_token = "b" * 32
    assert (
        cli._claim_smart_cli_durable_job(
            "receipt-a",
            "transferring",
            session_id,
            owner_session_id=session_id,
            claim_token=first_token,
        )
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )
    assert (
        cli._requeue_unconsumed_smart_cli_steer(
            "receipt-a",
            session_id,
            "payload-a",
            owner_session_id=session_id,
            claim_token=first_token,
        )
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )
    assert (
        cli._claim_smart_cli_durable_job(
            "receipt-a",
            "transferring",
            session_id,
            owner_session_id=session_id,
            claim_token=second_token,
        )
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )

    assert (
        cli._finalize_consumed_smart_cli_steer(
            "receipt-a",
            session_id,
            owner_session_id=session_id,
            claim_token=first_token,
        )
        is SmartCliDurableDisposition.STALE
    )
    state = cli._load_smart_cli_durable_state_locked(session_id)
    assert state["jobs"] == [
        {"id": "receipt-a", "text": "payload-a", "state": "transferring"}
    ]
    assert state["attempts"]["receipt-a"] == {
        "epoch": 2,
        "token": second_token,
    }

    assert (
        cli._finalize_consumed_smart_cli_steer(
            "receipt-a",
            session_id,
            owner_session_id=session_id,
            claim_token=second_token,
        )
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )
    assert cli._load_smart_cli_durable_jobs_locked(session_id) == []


def test_cli_smart_durable_empty_tombstone_prevents_revision_aba(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    owner_a = _cli()
    owner_b = _cli()
    owner_c = _cli()
    session_id = "aba-session"

    owner_a._append_smart_cli_durable_job("old", "old payload", session_id)
    path = owner_a._smart_cli_durable_path(session_id)
    first_state = json.loads(path.read_text(encoding="utf-8"))
    assert first_state["version"] == 4
    assert first_state["revision"] > 0
    assert isinstance(first_state["incarnation"], str)
    assert first_state["incarnation"]

    assert owner_b._ack_smart_cli_durable_job("old", session_id) is True
    empty_state = json.loads(path.read_text(encoding="utf-8"))
    assert empty_state["jobs"] == []
    assert empty_state["revision"] > first_state["revision"]
    assert empty_state["incarnation"] == first_state["incarnation"]

    owner_c._append_smart_cli_durable_job("fresh", "fresh payload", session_id)
    owner_a._append_smart_cli_durable_job("a-new", "new payload", session_id)

    final_state = json.loads(path.read_text(encoding="utf-8"))
    assert [job["id"] for job in final_state["jobs"]] == ["fresh", "a-new"]
    assert final_state["revision"] > empty_state["revision"]
    assert final_state["incarnation"] == first_state["incarnation"]


def test_cli_smart_processing_crash_is_preserved_as_uncertain(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    cli.agent.session_id = "processing-session"
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True

    assert cli._enqueue_smart_cli_input("run once") is True
    job = cli._smart_cli_input_queue.get_nowait()
    assert cli._set_smart_cli_durable_job_state(
        job.durable_id,
        "processing",
        job.durable_session_id,
    ) is True

    restarted = _cli()
    restarted.agent.session_id = "processing-session"
    assert restarted._restore_smart_cli_durable_inputs() == 0
    assert restarted._smart_cli_restore_error is True
    assert restarted._pending_input.empty()
    recovered = restarted._load_smart_cli_durable_jobs_locked("processing-session")
    assert recovered == [
        {"id": job.durable_id, "text": "run once", "state": "uncertain"}
    ]


def test_cli_smart_steer_crash_window_is_recovered_as_uncertain_not_duplicated(
    tmp_path, monkeypatch
):
    """Persist intent before steer so a crash cannot replay an executed update."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()

    class CrashAfterFenceAgent:
        session_id = "session-steer-crash"
        provider = None
        model = None

        def get_activity_summary(self):
            return {"current_tool": "terminal"}

        def get_steer_generation(self):
            return 31

        def supports_steer_consumption_ack(self):
            return True

        def steer(self, _payload, **_kwargs):
            jobs = cli._load_smart_cli_durable_jobs_locked(self.session_id)
            assert jobs[0]["state"] == "transferring"
            raise KeyboardInterrupt("simulated process death")

    cli.agent = CrashAfterFenceAgent()
    cli._smart_cli_active_turn = (7, "Fix the gateway", cli.agent)
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True
    assert cli._enqueue_smart_cli_input("related update") is True
    job = cli._smart_cli_input_queue.get_nowait()
    decision = SmartRouteDecision(
        route=ROUTE_RELATED,
        confidence=0.99,
        reason="same mission",
        source="test",
    )
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (decision, "related update"),
    )

    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        cli._route_smart_cli_input(
            job.text,
            job.route_context,
            durable_id=job.durable_id,
            durable_session_id=job.durable_session_id,
        )

    restored = _cli()
    restored.agent.session_id = "session-steer-crash"
    assert restored._restore_smart_cli_durable_inputs() == 0
    assert restored._smart_cli_restore_error is True
    jobs = restored._load_smart_cli_durable_jobs_locked("session-steer-crash")
    assert jobs[0]["text"] == "related update"
    assert jobs[0]["state"] == "uncertain"


def test_cli_smart_accepted_classifier_job_survives_process_recreation(
    tmp_path, monkeypatch
):
    """A positive admission cannot disappear with its daemon classifier thread."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    cli.agent.session_id = "session-smart-recovery"
    cli._smart_cli_input_queue = queue.Queue(maxsize=32)
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True

    assert cli._enqueue_smart_cli_input("accepted before shutdown") is True

    recreated = _cli()
    recreated.agent.session_id = "session-smart-recovery"
    restored = recreated._restore_smart_cli_durable_inputs()

    assert restored == 1
    recovered = recreated._pending_input.get_nowait()
    assert recovered.payload == "accepted before shutdown"
    assert recovered.durable_id


def test_cli_smart_restore_uses_controller_session_before_agent_exists(
    tmp_path, monkeypatch
):
    """Startup restore must not wait for agent construction to discover scope."""
    from cli import DurableCliInput

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    session_id = "lazy-restore-session"
    owner = _cli()
    owner.agent.session_id = session_id
    owner._append_smart_cli_durable_job(
        "receipt-lazy", "accepted before agent construction", session_id
    )

    restarted = _cli()
    restarted.session_id = session_id
    restarted.agent = None

    assert restarted._restore_smart_cli_durable_inputs() == 1
    recovered = restarted._pending_input.get_nowait()
    assert recovered == DurableCliInput(
        "receipt-lazy",
        session_id,
        "accepted before agent construction",
    )


def test_cli_smart_persistence_failure_rejects_before_positive_admission(monkeypatch):
    cli = _cli()
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True
    monkeypatch.setattr(
        cli,
        "_append_smart_cli_durable_job",
        MagicMock(side_effect=OSError("disk unavailable")),
        raising=False,
    )

    assert cli._enqueue_smart_cli_input("must not be accepted") is False
    assert cli._smart_cli_input_queue.empty()


def test_cli_smart_durable_ack_requires_explicit_chat_terminal_success(monkeypatch):
    from cli import SmartCliDurableDisposition

    cli = _cli()
    finalize = MagicMock(
        return_value=SmartCliDurableDisposition.COMMITTED_CURRENT
    )
    monkeypatch.setattr(cli, "_finalize_smart_cli_input_after_chat", finalize)
    commit = getattr(cli, "_ack_smart_cli_input_after_chat", None)
    assert callable(commit)

    cli._last_chat_turn_terminal_success = False
    assert commit("receipt", "session", claim_token="a" * 32) is False
    finalize.assert_called_once_with(
        "receipt", "session", claim_token="a" * 32
    )

    finalize.reset_mock()
    cli._last_chat_turn_terminal_success = True
    assert commit("receipt", "session", claim_token="b" * 32) is True
    finalize.assert_called_once_with(
        "receipt", "session", claim_token="b" * 32
    )


def test_cli_smart_clean_full_turn_commits_head_then_promotes_successor(
    tmp_path, monkeypatch
):
    from cli import DurableCliInput, SmartCliDurableDisposition

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    cli.agent.session_id = "full-turn-fifo"
    cli._append_smart_cli_durable_job("receipt-a", "payload-a", "full-turn-fifo")
    cli._append_smart_cli_durable_job("receipt-b", "payload-b", "full-turn-fifo")
    claim_token = "c" * 32
    assert (
        cli._claim_smart_cli_durable_job(
            "receipt-a",
            "processing",
            "full-turn-fifo",
            owner_session_id="full-turn-fifo",
            claim_token=claim_token,
        )
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )
    cli._last_chat_turn_terminal_success = True

    finalize = getattr(cli, "_finalize_smart_cli_input_after_chat", None)
    assert callable(finalize)
    assert (
        finalize("receipt-a", "full-turn-fifo", claim_token=claim_token)
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )

    queued = cli._pending_input.get_nowait()
    assert isinstance(queued, DurableCliInput)
    assert queued.durable_id == "receipt-b"
    assert cli._load_smart_cli_durable_jobs_locked("full-turn-fifo") == [
        {"id": "receipt-b", "text": "payload-b", "state": "accepted"}
    ]


def test_cli_smart_dirty_result_marks_processing_receipt_uncertain_without_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    session_id = "dirty-result-session"
    cli.agent.session_id = session_id
    cli._append_smart_cli_durable_job("dirty-receipt", "run once", session_id)
    assert cli._set_smart_cli_durable_job_state(
        "dirty-receipt",
        "processing",
        session_id,
        expected_state="accepted",
    ) is True
    cli._last_chat_turn_terminal_success = False
    claim_token = cli._load_smart_cli_durable_state_locked(session_id)["attempts"][
        "dirty-receipt"
    ]["token"]

    assert (
        cli._ack_smart_cli_input_after_chat(
            "dirty-receipt", session_id, claim_token=claim_token
        )
        is False
    )

    assert cli._load_smart_cli_durable_jobs_locked(session_id) == [
        {"id": "dirty-receipt", "text": "run once", "state": "uncertain"}
    ]


def test_cli_smart_dependent_job_stays_durable_until_next_turn_ack(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    cli.agent.session_id = "session-dependent"
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_DEPENDENT), "run after this turn"),
    )

    assert cli._enqueue_smart_cli_input("run after this turn") is True
    job = cli._smart_cli_input_queue.get_nowait()
    cli._smart_cli_input_queue.put(job)
    cli._smart_cli_input_queue.put(None)
    cli._smart_cli_worker_loop()

    recovered = cli._pending_input.get_nowait()
    assert recovered.durable_id == job.durable_id
    assert recovered.payload == "run after this turn"
    assert cli._smart_cli_durable_path().exists()
    assert cli._ack_smart_cli_durable_job(job.durable_id) is True
    assert cli._load_smart_cli_durable_jobs_locked("session-dependent") == []


def test_cli_smart_related_steer_keeps_durable_record_until_consumption_ack(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()

    class AckingAgent:
        session_id = "session-related"
        provider = None
        model = None

        def __init__(self):
            self.consumed = None
            self.unconsumed = None
            self.uncertain = None

        def get_activity_summary(self):
            return {"current_tool": "terminal"}

        def get_steer_generation(self):
            return 17

        def supports_steer_consumption_ack(self):
            return True

        def steer(
            self,
            _text,
            *,
            run_generation=None,
            on_consumed=None,
            on_unconsumed=None,
            on_uncertain=None,
        ):
            assert run_generation == 17
            self.consumed = on_consumed
            self.unconsumed = on_unconsumed
            self.uncertain = on_uncertain
            return True

    agent = AckingAgent()
    cli.agent = agent
    cli._smart_cli_active_turn = (7, "Fix the gateway", agent)
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_RELATED), "apply correction"),
    )

    assert cli._enqueue_smart_cli_input("apply correction") is True
    job = cli._smart_cli_input_queue.get_nowait()
    cli._smart_cli_input_queue.put(job)
    cli._smart_cli_input_queue.put(None)
    cli._smart_cli_worker_loop()

    jobs = cli._load_smart_cli_durable_jobs_locked("session-related")
    assert jobs == [
        {"id": job.durable_id, "text": "apply correction", "state": "transferring"}
    ]
    assert callable(agent.consumed)
    assert callable(agent.uncertain)
    assert cli._pending_input.empty()

    agent.consumed()

    assert cli._load_smart_cli_durable_jobs_locked("session-related") == []


def test_cli_smart_consumed_callback_commits_current_then_promotes_successor(
    tmp_path, monkeypatch
):
    from cli import SmartCliDurableDisposition

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()

    class AckingAgent:
        session_id = "session-consumed-successor"
        provider = None
        model = None

        def __init__(self):
            self.consumed = None

        def get_activity_summary(self):
            return {"current_tool": "terminal"}

        def get_steer_generation(self):
            return 19

        def supports_steer_consumption_ack(self):
            return True

        def steer(self, _text, **kwargs):
            self.consumed = kwargs["on_consumed"]
            return True

    agent = AckingAgent()
    cli.agent = agent
    cli._smart_cli_active_turn = (7, "Current work", agent)
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_RELATED), "receipt-a"),
    )

    assert cli._enqueue_smart_cli_input("receipt-a") is True
    job_a = cli._smart_cli_input_queue.get_nowait()
    cli._route_smart_cli_input(
        job_a.text,
        job_a.route_context,
        durable_id=job_a.durable_id,
        durable_session_id=job_a.durable_session_id,
    )
    assert callable(agent.consumed)
    cli._append_smart_cli_durable_job(
        "receipt-b",
        "next turn",
        agent.session_id,
    )

    disposition = agent.consumed()

    assert disposition is SmartCliDurableDisposition.COMMITTED_CURRENT
    promoted = cli._pending_input.get_nowait()
    assert promoted.durable_id == "receipt-b"
    assert promoted.payload == "next turn"
    assert cli._load_smart_cli_durable_jobs_locked(agent.session_id) == [
        {"id": "receipt-b", "text": "next turn", "state": "accepted"}
    ]


def test_cli_smart_stale_consumed_finalizer_cannot_promote_successor(
    tmp_path, monkeypatch
):
    from cli import SmartCliDurableDisposition

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    cli.agent.session_id = "stale-finalizer"
    cli._append_smart_cli_durable_job("receipt-a", "payload-a", "stale-finalizer")
    cli._append_smart_cli_durable_job("receipt-b", "payload-b", "stale-finalizer")
    claim_token = "d" * 32
    assert (
        cli._claim_smart_cli_durable_job(
            "receipt-a",
            "transferring",
            "stale-finalizer",
            owner_session_id="stale-finalizer",
            claim_token=claim_token,
        )
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )
    assert (
        cli._mark_uncertain_smart_cli_steer(
            "receipt-a", "stale-finalizer", claim_token=claim_token
        )
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )

    assert (
        cli._finalize_consumed_smart_cli_steer(
            "receipt-a", "stale-finalizer", claim_token=claim_token
        )
        is SmartCliDurableDisposition.STALE
    )
    assert cli._pending_input.empty()
    assert cli._load_smart_cli_durable_jobs_locked("stale-finalizer") == [
        {"id": "receipt-a", "text": "payload-a", "state": "uncertain"},
        {"id": "receipt-b", "text": "payload-b", "state": "accepted"},
    ]


def test_cli_smart_unconsumed_steer_returns_same_receipt_to_durable_queue(
    tmp_path, monkeypatch
):
    from cli import DurableCliInput, SmartCliDurableDisposition

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()

    class AckingAgent:
        session_id = "session-unconsumed"
        provider = None
        model = None

        def __init__(self):
            self.unconsumed = None

        def get_activity_summary(self):
            return {"current_tool": "terminal"}

        def get_steer_generation(self):
            return 23

        def supports_steer_consumption_ack(self):
            return True

        def steer(self, _text, **kwargs):
            self.unconsumed = kwargs["on_unconsumed"]
            return True

    agent = AckingAgent()
    cli.agent = agent
    cli._smart_cli_active_turn = (8, "Current work", agent)
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_RELATED), "late correction"),
    )

    assert cli._enqueue_smart_cli_input("late correction") is True
    job = cli._smart_cli_input_queue.get_nowait()
    cli._smart_cli_input_queue.put(job)
    cli._smart_cli_input_queue.put(None)
    cli._smart_cli_worker_loop()
    assert callable(agent.unconsumed)

    assert (
        agent.unconsumed()
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )

    assert cli._load_smart_cli_durable_jobs_locked("session-unconsumed") == [
        {"id": job.durable_id, "text": "late correction", "state": "accepted"}
    ]
    queued = cli._pending_input.get_nowait()
    assert queued == DurableCliInput(
        job.durable_id,
        "session-unconsumed",
        "late correction",
    )
    assert cli._pending_input.empty()


def test_cli_smart_unconsumed_head_stays_before_already_accepted_successor(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()

    class AckingAgent:
        session_id = "session-unconsumed-fifo"
        provider = None
        model = None

        def __init__(self):
            self.unconsumed = None

        def get_activity_summary(self):
            return {"current_tool": "terminal"}

        def get_steer_generation(self):
            return 47

        def supports_steer_consumption_ack(self):
            return True

        def steer(self, _text, **kwargs):
            self.unconsumed = kwargs["on_unconsumed"]
            return True

    agent = AckingAgent()
    cli.agent = agent
    cli._smart_cli_active_turn = (10, "Current work", agent)
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True

    assert cli._enqueue_smart_cli_input("receipt-a") is True
    job_a = cli._smart_cli_input_queue.get_nowait()
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_RELATED), "receipt-a"),
    )
    cli._route_smart_cli_input(
        job_a.text,
        job_a.route_context,
        durable_id=job_a.durable_id,
        durable_session_id=job_a.durable_session_id,
    )
    assert callable(agent.unconsumed)

    assert cli._enqueue_smart_cli_input("receipt-b") is True
    job_b = cli._smart_cli_input_queue.get_nowait()
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_DEPENDENT), "receipt-b"),
    )
    cli._route_smart_cli_input(
        job_b.text,
        job_b.route_context,
        durable_id=job_b.durable_id,
        durable_session_id=job_b.durable_session_id,
    )

    agent.unconsumed()

    queued = [cli._pending_input.get_nowait(), cli._pending_input.get_nowait()]
    assert [item.durable_id for item in queued] == [
        job_a.durable_id,
        job_b.durable_id,
    ]
    assert cli._load_smart_cli_durable_jobs_locked(agent.session_id) == [
        {"id": job_a.durable_id, "text": "receipt-a", "state": "accepted"},
        {"id": job_b.durable_id, "text": "receipt-b", "state": "accepted"},
    ]


def test_cli_smart_restart_executes_unconsumed_head_before_accepted_successor(
    tmp_path, monkeypatch
):
    from cli import DurableCliInput, SmartCliDurableDisposition

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    owner = _cli()
    owner.agent.session_id = "session-restart-fifo"
    owner._append_smart_cli_durable_job("receipt-a", "payload-a", "session-restart-fifo")
    owner._append_smart_cli_durable_job("receipt-b", "payload-b", "session-restart-fifo")
    owner_claim_token = "e" * 32
    assert (
        owner._claim_smart_cli_durable_job(
            "receipt-a",
            "transferring",
            "session-restart-fifo",
            owner_session_id="session-restart-fifo",
            claim_token=owner_claim_token,
        )
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )
    assert (
        owner._requeue_unconsumed_smart_cli_steer(
            "receipt-a",
            "session-restart-fifo",
            "payload-a",
            claim_token=owner_claim_token,
        )
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )

    resumed = _cli()
    resumed.agent.session_id = "session-restart-fifo"
    assert resumed._restore_smart_cli_durable_inputs() == 2
    executed = []
    for expected_id, claim_token in (
        ("receipt-a", "f" * 32),
        ("receipt-b", "0" * 32),
    ):
        item = resumed._pending_input.get_nowait()
        assert isinstance(item, DurableCliInput)
        assert item.durable_id == expected_id
        assert (
            resumed._claim_smart_cli_durable_job(
                item.durable_id,
                "processing",
                item.durable_session_id,
                owner_session_id="session-restart-fifo",
                claim_token=claim_token,
            )
            is SmartCliDurableDisposition.COMMITTED_CURRENT
        )
        executed.append(item.payload)
        resumed._last_chat_turn_terminal_success = True
        assert (
            resumed._finalize_smart_cli_input_after_chat(
                item.durable_id,
                item.durable_session_id,
                claim_token=claim_token,
            )
            is SmartCliDurableDisposition.COMMITTED_CURRENT
        )

    assert executed == ["payload-a", "payload-b"]
    assert resumed._load_smart_cli_durable_jobs_locked("session-restart-fifo") == []
    assert resumed._pending_input.empty()


def test_cli_smart_dirty_injected_steer_marks_same_receipt_uncertain(
    tmp_path, monkeypatch
):
    from cli import SmartCliDurableDisposition

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()

    class AckingAgent:
        session_id = "session-uncertain"
        provider = None
        model = None

        def __init__(self):
            self.uncertain = None

        def get_activity_summary(self):
            return {"current_tool": "terminal"}

        def get_steer_generation(self):
            return 29

        def supports_steer_consumption_ack(self):
            return True

        def steer(self, _text, **kwargs):
            self.uncertain = kwargs.get("on_uncertain")
            return True

    agent = AckingAgent()
    cli.agent = agent
    cli._smart_cli_active_turn = (9, "Current work", agent)
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_RELATED), "durable correction"),
    )

    assert cli._enqueue_smart_cli_input("durable correction") is True
    job = cli._smart_cli_input_queue.get_nowait()
    cli._smart_cli_input_queue.put(job)
    cli._smart_cli_input_queue.put(None)
    cli._smart_cli_worker_loop()
    assert callable(agent.uncertain)
    assert cli._load_smart_cli_durable_jobs_locked("session-uncertain") == [
        {"id": job.durable_id, "text": "durable correction", "state": "transferring"}
    ]

    assert (
        agent.uncertain()
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )

    assert cli._load_smart_cli_durable_jobs_locked("session-uncertain") == [
        {"id": job.durable_id, "text": "durable correction", "state": "uncertain"}
    ]


def test_cli_smart_late_steer_exception_cannot_rollback_uncertain_receipt(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()

    class DirtyThenRaiseAgent:
        session_id = "session-late-finalizer"
        provider = None
        model = None

        def __init__(self):
            self.uncertain_outcome = None

        def get_activity_summary(self):
            return {"current_tool": "terminal"}

        def get_steer_generation(self):
            return 31

        def supports_steer_consumption_ack(self):
            return True

        def steer(self, _text, **kwargs):
            self.uncertain_outcome = kwargs["on_uncertain"]()
            raise RuntimeError("late transport failure")

    agent = DirtyThenRaiseAgent()
    cli.agent = agent
    cli._smart_cli_active_turn = (11, "Current work", agent)
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_RELATED), "durable correction"),
    )

    assert cli._enqueue_smart_cli_input("durable correction") is True
    job = cli._smart_cli_input_queue.get_nowait()
    cli._route_smart_cli_input(
        job.text,
        route_context=job.route_context,
        durable_id=job.durable_id,
        durable_session_id=job.durable_session_id,
    )

    from cli import SmartCliDurableDisposition

    assert (
        agent.uncertain_outcome
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )
    assert cli._load_smart_cli_durable_jobs_locked("session-late-finalizer") == [
        {"id": job.durable_id, "text": "durable correction", "state": "uncertain"}
    ]
    assert cli._pending_input.empty()


def test_cli_smart_durable_ack_uses_admission_session_after_agent_rotates(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()

    class RotatingAckAgent:
        session_id = "session-at-admission"
        provider = None
        model = None

        def __init__(self):
            self.consumed = None

        def get_activity_summary(self):
            return {"current_tool": "terminal"}

        def get_steer_generation(self):
            return 41

        def supports_steer_consumption_ack(self):
            return True

        def steer(self, _payload, **kwargs):
            self.consumed = kwargs["on_consumed"]
            return True

    agent = RotatingAckAgent()
    cli.agent = agent
    cli._smart_cli_active_turn = (7, "Fix the gateway", agent)
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True
    monkeypatch.setattr(
        "hermes_cli.smart_orchestrator.classify_smart_message",
        lambda **_kwargs: (_decision(ROUTE_RELATED), "apply correction"),
    )

    assert cli._enqueue_smart_cli_input("apply correction") is True
    job = cli._smart_cli_input_queue.get_nowait()
    original_path = cli._smart_cli_durable_path("session-at-admission")
    assert original_path.exists()
    cli.agent.session_id = "session-after-rotation"
    cli._smart_cli_input_queue.put(job)
    cli._smart_cli_input_queue.put(None)
    cli._smart_cli_worker_loop()

    # A successful mailbox stage is not terminal consumption.  Rotation must
    # not make the callback target the agent's newer session implicitly.
    assert original_path.exists()
    assert callable(agent.consumed)
    agent.consumed()

    assert original_path.exists()
    assert cli._load_smart_cli_durable_jobs_locked("session-at-admission") == []


def test_cli_smart_continuation_rotation_restores_parent_receipt_once(
    tmp_path, monkeypatch
):
    """A continuation owns the parent's receipt without copy/delete replay gaps."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    parent = _cli()
    parent.agent.session_id = "continuation-parent"
    parent._smart_cli_worker = MagicMock()
    parent._smart_cli_worker.is_alive.return_value = True

    assert parent._enqueue_smart_cli_input("survive continuation rotation") is True
    admitted = parent._smart_cli_input_queue.get_nowait()
    assert parent._migrate_smart_cli_session_scope(
        "continuation-parent", "continuation-child"
    ) is True

    resumed = _cli()
    resumed.agent.session_id = "continuation-child"
    assert resumed._smart_cli_durable_job_count() == 1
    assert resumed._restore_smart_cli_durable_inputs() == 1
    assert resumed._restore_smart_cli_durable_inputs() == 0

    recovered = resumed._pending_input.get_nowait()
    assert recovered.durable_id == admitted.durable_id
    assert recovered.durable_session_id == "continuation-parent"
    assert recovered.payload == "survive continuation rotation"
    assert resumed._pending_input.empty()


def test_cli_smart_rotation_crash_gap_recovers_compression_parent_from_db(
    tmp_path, monkeypatch
):
    """A child created before alias publication can still recover its parent scope."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    parent = _cli()
    parent.agent.session_id = "crash-gap-parent"
    parent._append_smart_cli_durable_job(
        "crash-gap-receipt",
        "accepted before compression",
        "crash-gap-parent",
    )

    rows = {
        "crash-gap-child": {
            "id": "crash-gap-child",
            "parent_session_id": "crash-gap-parent",
            "model_config": None,
        },
        "crash-gap-parent": {
            "id": "crash-gap-parent",
            "parent_session_id": None,
            "end_reason": "compression",
        },
    }
    resumed = _cli()
    resumed.agent.session_id = "crash-gap-child"
    resumed._session_db = MagicMock()
    resumed._session_db.get_session.side_effect = rows.get

    assert resumed._restore_smart_cli_durable_inputs() == 1
    recovered = resumed._pending_input.get_nowait()
    assert recovered.durable_id == "crash-gap-receipt"
    assert recovered.durable_session_id == "crash-gap-parent"
    assert recovered.payload == "accepted before compression"


def test_cli_smart_rotation_chain_preserves_fifo_ack_scope_and_no_replay(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    cli._append_smart_cli_durable_job("receipt-a", "parent payload", "segment-a")
    assert cli._migrate_smart_cli_session_scope("segment-a", "segment-b") is True
    cli._append_smart_cli_durable_job("receipt-b", "middle payload", "segment-b")
    assert cli._migrate_smart_cli_session_scope("segment-b", "segment-c") is True
    cli._append_smart_cli_durable_job("receipt-c", "child payload", "segment-c")

    resumed = _cli()
    resumed.agent.session_id = "segment-c"
    assert resumed._restore_smart_cli_durable_inputs() == 3
    recovered = [resumed._pending_input.get_nowait() for _ in range(3)]
    assert [(item.durable_id, item.durable_session_id) for item in recovered] == [
        ("receipt-a", "segment-a"),
        ("receipt-b", "segment-b"),
        ("receipt-c", "segment-c"),
    ]

    for item in recovered:
        assert resumed._set_smart_cli_durable_job_state(
            item.durable_id,
            "processing",
            item.durable_session_id,
            expected_state="accepted",
        ) is True
        claim_token = resumed._load_smart_cli_durable_state_locked(
            item.durable_session_id
        )["attempts"][item.durable_id]["token"]
        assert resumed._ack_smart_cli_durable_job(
            item.durable_id,
            item.durable_session_id,
            expected_state="processing",
            claim_token=claim_token,
        ) is True

    restarted = _cli()
    restarted.agent.session_id = "segment-c"
    assert restarted._restore_smart_cli_durable_inputs() == 0
    assert restarted._pending_input.empty()

    stale_parent = _cli()
    stale_parent.agent.session_id = "segment-b"
    assert stale_parent._restore_smart_cli_durable_inputs() == 0
    assert stale_parent._smart_cli_restore_error is True
    assert stale_parent._pending_input.empty()


def test_cli_smart_late_parent_ack_cannot_delete_same_id_in_child_scope(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    cli._append_smart_cli_durable_job("shared-receipt", "parent", "ack-parent")
    assert cli._migrate_smart_cli_session_scope("ack-parent", "ack-child") is True
    cli._append_smart_cli_durable_job("shared-receipt", "child", "ack-child")

    assert cli._set_smart_cli_durable_job_state(
        "shared-receipt",
        "processing",
        "ack-parent",
        expected_state="accepted",
    ) is True
    claim_token = cli._load_smart_cli_durable_state_locked("ack-parent")[
        "attempts"
    ]["shared-receipt"]["token"]
    assert cli._ack_smart_cli_durable_job(
        "shared-receipt",
        "ack-parent",
        expected_state="processing",
        claim_token=claim_token,
    ) is True

    assert cli._load_smart_cli_durable_jobs_locked("ack-parent") == []
    assert cli._load_smart_cli_durable_jobs_locked("ack-child") == [
        {"id": "shared-receipt", "state": "accepted", "text": "child"}
    ]


def test_cli_smart_durable_state_accepts_max_payload_after_json_escaping(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    cli.agent.session_id = "session-json-escape"
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True
    payload = "\x00" * (1024 * 1024)

    assert cli._enqueue_smart_cli_input(payload) is True

    recreated = _cli()
    recreated.agent.session_id = "session-json-escape"
    assert recreated._restore_smart_cli_durable_inputs() == 1
    assert recreated._pending_input.get_nowait().payload == payload


def test_cli_smart_durable_state_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    cli.agent.session_id = "session-private-file"
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True

    assert cli._enqueue_smart_cli_input("private accepted message") is True

    if os.name != "nt":
        assert cli._smart_cli_durable_path().stat().st_mode & 0o777 == 0o600
        assert cli._smart_cli_durable_path().parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
def test_cli_smart_restore_repairs_private_directory_state_and_lock_modes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    cli.agent.session_id = "session-repair-private-modes"
    cli._append_smart_cli_durable_job(
        "private-receipt",
        "private accepted message",
        cli.agent.session_id,
    )
    state_path = cli._smart_cli_durable_path()
    lock_path = state_path.with_suffix(".lock")
    os.chmod(state_path.parent, 0o755)
    os.chmod(state_path, 0o644)
    os.chmod(lock_path, 0o644)

    restarted = _cli()
    restarted.agent.session_id = cli.agent.session_id
    assert restarted._restore_smart_cli_durable_inputs() == 1

    assert state_path.parent.stat().st_mode & 0o777 == 0o700
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert lock_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink contract")
def test_cli_smart_restore_rejects_symlinked_state_without_reading_target(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    session_id = "session-symlink-state"
    cli._append_smart_cli_durable_job("symlink-receipt", "outside payload", session_id)
    state_path = cli._smart_cli_durable_path(session_id)
    outside_path = tmp_path / "outside-state.json"
    state_path.replace(outside_path)
    state_path.symlink_to(outside_path)

    restarted = _cli()
    restarted.agent.session_id = session_id
    assert restarted._restore_smart_cli_durable_inputs() == 0
    assert restarted._smart_cli_restore_error is True
    assert restarted._pending_input.empty()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership contract")
def test_cli_smart_restore_rejects_state_not_owned_by_effective_user(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    session_id = "session-foreign-owner"
    cli._append_smart_cli_durable_job("foreign-receipt", "private payload", session_id)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)

    restarted = _cli()
    restarted.agent.session_id = session_id
    assert restarted._restore_smart_cli_durable_inputs() == 0
    assert restarted._smart_cli_restore_error is True
    assert restarted._pending_input.empty()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and mode contract")
def test_cli_smart_rotation_alias_is_private_and_symlink_fails_closed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    source = "private-parent-session"
    target = "private-child-session"
    assert cli._migrate_smart_cli_session_scope(source, target) is True
    alias_path = cli._smart_cli_rotation_path(source, target)
    os.chmod(alias_path.parent, 0o755)
    os.chmod(alias_path, 0o644)

    assert cli._load_smart_cli_rotation_edges() == [(source, target)]
    assert alias_path.parent.stat().st_mode & 0o777 == 0o700
    assert alias_path.stat().st_mode & 0o777 == 0o600

    outside_path = tmp_path / "outside-alias.json"
    alias_path.replace(outside_path)
    alias_path.symlink_to(outside_path)
    with pytest.raises(RuntimeError, match="rotation record"):
        cli._load_smart_cli_rotation_edges()


def test_cli_smart_rotation_publication_rejects_second_target_for_source(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()

    assert cli._migrate_smart_cli_session_scope("parent", "child-a") is True
    assert cli._migrate_smart_cli_session_scope("parent", "child-a") is True
    assert cli._migrate_smart_cli_session_scope("parent", "child-b") is False
    assert cli._load_smart_cli_rotation_edges() == [("parent", "child-a")]


def test_cli_smart_branch_session_does_not_publish_continuation_alias(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    cli.session_id = "parent"

    class BranchDB:
        def get_session(self, session_id):
            if session_id == "branch":
                return {
                    "parent_session_id": "parent",
                    "model_config": {"_branched_from": "parent"},
                }
            if session_id == "parent":
                return {"end_reason": "branched"}
            return None

    cli._session_db = BranchDB()

    assert cli._adopt_smart_cli_continuation_session("parent", "branch") is False
    assert cli._load_smart_cli_rotation_edges() == []
    assert cli.session_id == "parent"


def test_cli_smart_rotation_publication_rejects_duplicate_target_and_cycle(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    duplicate_target = _cli()
    assert duplicate_target._migrate_smart_cli_session_scope("source-a", "target")
    assert not duplicate_target._migrate_smart_cli_session_scope(
        "source-b", "target"
    )
    assert duplicate_target._load_smart_cli_rotation_edges() == [
        ("source-a", "target")
    ]

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "cycle-profile"))
    cycle = _cli()
    assert cycle._migrate_smart_cli_session_scope("session-a", "session-b")
    assert not cycle._migrate_smart_cli_session_scope("session-b", "session-a")
    assert cycle._load_smart_cli_rotation_edges() == [
        ("session-a", "session-b")
    ]


def test_cli_smart_verified_compression_publishes_continuation_alias(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    source = "compression-parent"
    target = "compression-child"
    cli = _cli()
    cli.session_id = source
    cli._session_db = MagicMock()
    cli._session_db.get_session.side_effect = lambda session_id: {
        source: {
            "id": source,
            "parent_session_id": None,
            "end_reason": "compression",
            "model_config": {},
        },
        target: {
            "id": target,
            "parent_session_id": source,
            "end_reason": None,
            "model_config": {},
        },
    }.get(session_id)

    assert cli._adopt_smart_cli_continuation_session(source, target) is True
    assert cli.session_id == target
    assert cli._load_smart_cli_rotation_edges() == [(source, target)]


def test_cli_smart_generation_snapshot_diagnostic_is_metadata_only(caplog):
    cli = _cli()
    private_error = "tenant-secret /customers/acme/private.json session-private-123"

    class PrivateGenerationFailure(RuntimeError):
        pass

    class Agent:
        def get_steer_generation(self):
            raise PrivateGenerationFailure(private_error)

    agent = Agent()
    cli.agent = agent
    cli._smart_cli_active_turn = (7, "private active prompt", agent)
    caplog.set_level("DEBUG", logger="cli")

    context = cli._capture_smart_cli_route_context()

    assert context.steer_generation is None
    assert private_error not in caplog.text
    assert "Traceback" not in caplog.text
    assert "PrivateGenerationFailure" in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="POSIX private paste contract")
def test_cli_paste_reference_is_opaque_private_repairs_modes_and_round_trips(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    payload = "private line one\nprivate line two\nprivate line three"

    placeholder = cli._store_private_paste_reference(
        payload,
        display_index=7,
        line_count=3,
    )

    assert isinstance(placeholder, str)
    assert payload not in placeholder
    assert str(tmp_path) not in placeholder
    match = re.search(r"→ paste:([0-9a-f]{32})\]$", placeholder)
    assert match is not None
    paste_path = tmp_path / "profile" / "pastes" / f"{match.group(1)}.txt"
    assert paste_path.read_text(encoding="utf-8") == payload
    assert paste_path.parent.stat().st_mode & 0o777 == 0o700
    assert paste_path.stat().st_mode & 0o777 == 0o600

    os.chmod(paste_path.parent, 0o755)
    os.chmod(paste_path, 0o644)
    assert cli._expand_paste_references(placeholder) == payload
    assert cli._expand_paste_references(placeholder) == placeholder
    assert paste_path.parent.stat().st_mode & 0o777 == 0o700
    assert not paste_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX private paste contract")
def test_cli_paste_expansion_rejects_paths_and_symlinks_without_identifier_logs(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    paste_dir = tmp_path / "profile" / "pastes"
    paste_dir.mkdir(parents=True, mode=0o700)
    outside_path = tmp_path / "customer-private-paste.txt"
    private_payload = "private customer paste must not cross ownership boundary"
    outside_path.write_text(private_payload, encoding="utf-8")
    opaque_id = "a" * 32
    (paste_dir / f"{opaque_id}.txt").symlink_to(outside_path)
    legacy = f"[Pasted text #1: 1 lines → {outside_path}]"
    opaque = f"[Pasted text #2: 1 lines → paste:{opaque_id}]"
    caplog.set_level("WARNING", logger="cli")

    assert cli._expand_paste_references(legacy) == legacy
    assert cli._expand_paste_references(opaque) == opaque
    assert private_payload not in caplog.text
    assert str(outside_path) not in caplog.text
    assert opaque_id not in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="POSIX private paste contract")
def test_cli_paste_store_rejects_symlinked_private_directory_without_leaking(
    tmp_path, monkeypatch, caplog
):
    profile = tmp_path / "profile"
    profile.mkdir()
    outside_dir = tmp_path / "outside-pastes"
    outside_dir.mkdir()
    (profile / "pastes").symlink_to(outside_dir, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    cli = _cli()
    private_payload = "private paste payload for symlink attack"
    caplog.set_level("WARNING", logger="cli")

    assert cli._store_private_paste_reference(
        private_payload,
        display_index=1,
        line_count=1,
    ) is None
    assert list(outside_dir.iterdir()) == []
    assert private_payload not in caplog.text
    assert str(outside_dir) not in caplog.text


def test_cli_paste_store_rejects_payload_over_private_artifact_limit(
    tmp_path, monkeypatch, caplog
):
    profile = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(profile))
    cli = _cli()
    oversized = "x" * (1024 * 1024 + 1)
    caplog.set_level("WARNING", logger="cli")

    assert cli._store_private_paste_reference(
        oversized,
        display_index=1,
        line_count=1,
    ) is None
    assert list((profile / "pastes").glob("*.txt")) == []
    assert oversized[:128] not in caplog.text


def test_cli_private_paste_busy_smart_bypasses_classifier_and_queues_exact_body(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(profile))
    cli = _cli()
    cli.agent.session_id = "private-paste-session"
    cli._agent_running = True
    cli.busy_input_mode = "smart"
    cli._attached_images = []
    cli._smart_cli_worker = MagicMock()
    cli._smart_cli_worker.is_alive.return_value = True
    private_body = "private customer body\nsecond line"
    placeholder = cli._store_private_paste_reference(
        private_body,
        display_index=1,
        line_count=2,
    )
    assert placeholder is not None
    buffer = MagicMock()
    buffer.text = placeholder
    app = MagicMock()
    app.current_buffer = buffer
    event = MagicMock()
    event.app = app

    cli._submit_normal_input(event)

    assert cli._smart_cli_input_queue.empty()
    queued = cli._pending_input.get_nowait()
    assert queued.payload == private_body
    assert cli._load_smart_cli_durable_jobs_locked(cli.agent.session_id) == [
        {"id": queued.durable_id, "text": private_body, "state": "accepted"}
    ]
    assert list((profile / "pastes").glob("*.txt")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-fd TOCTOU contract")
def test_cli_paste_expansion_uses_the_validated_directory_descriptor(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    original_payload = "original private paste"
    replacement_payload = "replacement injected after validation"
    placeholder = cli._store_private_paste_reference(
        original_payload,
        display_index=1,
        line_count=1,
    )
    assert placeholder is not None
    match = re.search(r"paste:([0-9a-f]{32})", placeholder)
    assert match is not None
    opaque_id = match.group(1)
    paste_dir = tmp_path / "profile" / "pastes"
    held_dir = tmp_path / "profile" / "pastes-held"
    real_os_open = os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        fd = real_os_open(path, flags, mode, dir_fd=dir_fd)
        if (
            not swapped
            and dir_fd is None
            and Path(path) == paste_dir
            and flags & getattr(os, "O_DIRECTORY", 0)
        ):
            paste_dir.rename(held_dir)
            paste_dir.mkdir(mode=0o700)
            replacement = paste_dir / f"{opaque_id}.txt"
            replacement.write_text(replacement_payload, encoding="utf-8")
            os.chmod(replacement, 0o600)
            swapped = True
        return fd

    monkeypatch.setattr(os, "open", racing_open)

    assert cli._expand_paste_references(placeholder) == original_payload


def test_cli_smart_worker_uses_admission_context_instead_of_recapturing(monkeypatch):
    from cli import SmartCliRouteContext, SmartCliTurnSnapshot

    cli = _cli()
    admitted_context = SmartCliRouteContext(
        turn_snapshot=SmartCliTurnSnapshot(7, "turn N", object()),
        agent=object(),
        steer_generation=17,
        supports_generation=True,
    )
    routed = []
    cli._smart_cli_input_queue.put(("follow-up", admitted_context))
    cli._smart_cli_input_queue.put(None)
    monkeypatch.setattr(
        cli,
        "_route_smart_cli_input",
        lambda text, *, route_context=None: routed.append((text, route_context)),
    )

    cli._smart_cli_worker_loop()

    assert routed == [("follow-up", admitted_context)]


def test_explicit_queue_public_submit_persists_before_typed_wakeup(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    cli._attached_images = []
    cli.agent.session_id = "explicit-queue-order"
    observed = []
    real_sync = cli._sync_smart_cli_pending_inputs_from_ledger

    def assert_persisted_before_sync(session_id):
        jobs = cli._load_smart_cli_durable_jobs_locked(session_id)
        assert len(jobs) == 1
        assert jobs[0]["text"] == "follow the durable path"
        assert jobs[0]["state"] == "accepted"
        observed.append("persisted")
        return real_sync(session_id)

    monkeypatch.setattr(
        cli,
        "_sync_smart_cli_pending_inputs_from_ledger",
        assert_persisted_before_sync,
    )
    buffer = MagicMock()
    buffer.text = "/queue follow the durable path"
    app = MagicMock()
    app.current_buffer = buffer
    event = MagicMock()
    event.app = app

    cli._submit_normal_input(event)

    assert observed == ["persisted"]
    queued = cli._pending_input.get_nowait()
    assert queued.payload == "follow the durable path"
    assert queued.durable_session_id == "explicit-queue-order"
    buffer.reset.assert_called_once_with(append_to_history=True)


def test_explicit_queue_admission_io_failure_keeps_composer_and_has_no_receipt(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    cli._attached_images = []
    cli.agent.session_id = "explicit-queue-failure"
    monkeypatch.setattr(
        cli,
        "_append_smart_cli_durable_job",
        MagicMock(side_effect=OSError("disk unavailable")),
    )
    buffer = MagicMock()
    buffer.text = "/queue keep this draft"
    app = MagicMock()
    app.current_buffer = buffer
    event = MagicMock()
    event.app = app
    messages = []
    monkeypatch.setattr("cli._cprint", messages.append)

    cli._submit_normal_input(event)

    assert buffer.text == "/queue keep this draft"
    buffer.reset.assert_not_called()
    assert cli._pending_input.empty()
    output = "\n".join(messages)
    assert "Persisted for the next turn" not in output
    assert "admission failed before persistence" in output


def test_uncertain_head_fences_explicit_queue_and_steer(tmp_path, monkeypatch):
    from cli import SmartCliDurableDisposition

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    session_id = "explicit-fenced"
    cli.agent.session_id = session_id
    cli._append_smart_cli_durable_job("head", "possibly delivered", session_id)
    token = "8" * 32
    assert (
        cli._claim_smart_cli_durable_job(
            "head",
            "transferring",
            session_id,
            owner_session_id=session_id,
            claim_token=token,
        )
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )
    assert (
        cli._mark_uncertain_smart_cli_steer(
            "head",
            session_id,
            owner_session_id=session_id,
            claim_token=token,
        )
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )

    assert cli._admit_explicit_cli_queue("queued successor") is True
    assert cli._admit_explicit_cli_steer("steer successor") is True

    assert cli._pending_input.empty()
    jobs = cli._load_smart_cli_durable_jobs_locked(session_id)
    assert [job["state"] for job in jobs] == [
        "uncertain",
        "accepted",
        "accepted",
    ]


def test_uncertain_retry_invalidates_old_claim_callback(tmp_path, monkeypatch):
    from cli import SmartCliDurableDisposition

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    session_id = "uncertain-retry"
    cli.agent.session_id = session_id
    cli._append_smart_cli_durable_job("receipt", "payload", session_id)
    token_a = "9" * 32
    token_b = "a" * 32
    assert (
        cli._claim_smart_cli_durable_job(
            "receipt",
            "transferring",
            session_id,
            owner_session_id=session_id,
            claim_token=token_a,
        )
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )
    assert (
        cli._mark_uncertain_smart_cli_steer(
            "receipt",
            session_id,
            owner_session_id=session_id,
            claim_token=token_a,
        )
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )
    assert (
        cli._resolve_uncertain_smart_cli_head("retry", session_id)
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )
    assert (
        cli._claim_smart_cli_durable_job(
            "receipt",
            "transferring",
            session_id,
            owner_session_id=session_id,
            claim_token=token_b,
        )
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )

    assert (
        cli._finalize_consumed_smart_cli_steer(
            "receipt",
            session_id,
            owner_session_id=session_id,
            claim_token=token_a,
        )
        is SmartCliDurableDisposition.STALE
    )
    assert cli._load_smart_cli_durable_jobs_locked(session_id)[0]["state"] == "transferring"
    assert (
        cli._finalize_consumed_smart_cli_steer(
            "receipt",
            session_id,
            owner_session_id=session_id,
            claim_token=token_b,
        )
        is SmartCliDurableDisposition.COMMITTED_CURRENT
    )
    assert cli._load_smart_cli_durable_jobs_locked(session_id) == []


def _busy_submit_event(text):
    buffer = MagicMock()
    buffer.text = text
    app = MagicMock()
    app.current_buffer = buffer
    event = MagicMock()
    event.app = app
    return event, buffer


def _prepare_busy_submit(cli, mode):
    cli.busy_input_mode = mode
    cli._agent_running = True
    cli._should_handle_model_command_inline = MagicMock(return_value=False)
    cli._should_handle_steer_command_inline = MagicMock(return_value=False)


def test_busy_queue_mode_uses_durable_ledger_instead_of_raw_queue(tmp_path, monkeypatch):
    from cli import DurableCliInput

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    cli = _cli()
    cli.agent.session_id = "busy-queue-durable"
    cli._attached_images = []
    _prepare_busy_submit(cli, "queue")
    event, buffer = _busy_submit_event("durable follow-up")

    cli._submit_normal_input(event)

    queued = cli._pending_input.get_nowait()
    assert isinstance(queued, DurableCliInput)
    assert queued.payload == "durable follow-up"
    assert cli._load_smart_cli_durable_jobs_locked("busy-queue-durable") == [
        {
            "id": queued.durable_id,
            "text": "durable follow-up",
            "state": "accepted",
        }
    ]
    buffer.reset.assert_called_once_with(append_to_history=True)


def test_busy_queue_io_failure_retains_draft():
    cli = _cli()
    cli._attached_images = []
    _prepare_busy_submit(cli, "queue")
    cli._admit_explicit_cli_queue = MagicMock(return_value=False)
    event, buffer = _busy_submit_event("keep this draft")

    cli._submit_normal_input(event)

    cli._admit_explicit_cli_queue.assert_called_once_with("keep this draft")
    buffer.reset.assert_not_called()
    assert cli._pending_input.empty()


def test_busy_steer_mode_uses_durable_admission_not_direct_agent_call():
    cli = _cli()
    cli._attached_images = []
    _prepare_busy_submit(cli, "steer")
    cli._admit_explicit_cli_steer = MagicMock(return_value=True)
    cli.agent.steer = MagicMock(return_value=True)
    event, buffer = _busy_submit_event("durable correction")

    cli._submit_normal_input(event)

    cli._admit_explicit_cli_steer.assert_called_once_with("durable correction")
    cli.agent.steer.assert_not_called()
    buffer.reset.assert_called_once_with(append_to_history=True)


def test_busy_attachment_rejection_preserves_draft_and_attachment(tmp_path):
    cli = _cli()
    attachment = tmp_path / "private.png"
    cli._attached_images = [attachment]
    _prepare_busy_submit(cli, "queue")
    event, buffer = _busy_submit_event("inspect this")

    cli._submit_normal_input(event)

    assert buffer.text == "inspect this"
    buffer.reset.assert_not_called()
    assert cli._attached_images == [attachment]
    assert cli._pending_input.empty()
