"""Durable settlement proof for agent-backed cron runs."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_state import SessionDB


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"role": "assistant", "content": "done", "finish_reason": "stop"}, "complete"),
        ({"role": "assistant", "content": "[SILENT]", "finish_reason": "stop"}, "silent"),
        ({"role": "assistant", "content": "", "finish_reason": "stop"}, "incomplete"),
        ({"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}, "incomplete"),
        ({"role": "assistant", "content": "partial", "finish_reason": "incomplete"}, "incomplete"),
        ({"role": "tool", "content": "ok"}, "incomplete"),
        ({"role": "user", "content": "run it"}, "incomplete"),
        (None, "incomplete"),
    ],
)
def test_cron_terminal_message_requires_persisted_assistant_text(message, expected):
    from cron.scheduler import _classify_persisted_cron_final_message

    assert _classify_persisted_cron_final_message(message) == expected


def test_real_sessiondb_verification_reads_only_the_persisted_tail(tmp_path):
    from cron.scheduler import _verify_persisted_cron_final_message

    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("cron-tool-tail", source="cron")
        db.append_message("cron-tool-tail", "user", "run it")
        db.append_message(
            "cron-tool-tail",
            "assistant",
            None,
            tool_calls=[{"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}],
            finish_reason="tool_calls",
        )
        db.append_message("cron-tool-tail", "tool", "ok", tool_call_id="c1")

        db.create_session("cron-complete", source="cron")
        db.append_message("cron-complete", "user", "run it")
        db.append_message("cron-complete", "assistant", "done", finish_reason="stop")

        db.create_session("cron-silent", source="cron")
        db.append_message("cron-silent", "user", "check it")
        db.append_message("cron-silent", "assistant", "[SILENT]", finish_reason="stop")

        assert _verify_persisted_cron_final_message(db, "cron-tool-tail") == "incomplete"
        assert _verify_persisted_cron_final_message(db, "cron-complete") == "complete"
        assert _verify_persisted_cron_final_message(db, "cron-silent") == "silent"
    finally:
        db.close()


def test_run_one_job_consumes_incomplete_settlement_before_delivery(monkeypatch):
    import cron.scheduler as scheduler

    events: list[tuple[str, object]] = []

    def fake_run_job(job, *, settlement=None, **_kwargs):
        assert settlement is not None
        settlement.update(
            {
                "session_id": "cron-exact-fire",
                "status": "incomplete",
                "end_reason": "cron_incomplete_no_output",
                "error": "Cron run ended without a persisted final assistant message.",
            }
        )
        return True, "normal output", "visible answer", None

    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(
        scheduler,
        "mark_execution_running",
        lambda execution_id: events.append(("running", execution_id)),
    )
    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: None)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_args, **_kwargs: events.append(("delivery", _args[1])))
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda *args, **kwargs: events.append(("mark", (args, kwargs))),
    )
    monkeypatch.setattr(
        scheduler,
        "finish_execution",
        lambda execution_id, **kwargs: events.append(("finish", (execution_id, kwargs))),
    )

    assert scheduler.run_one_job({"id": "job-incomplete", "execution_id": "exec-incomplete"}) is True

    marked = next(value for kind, value in events if kind == "mark")
    assert marked[0][1] is False
    finished = next(value for kind, value in events if kind == "finish")
    assert finished[1]["success"] is False
    assert "persisted final assistant" in finished[1]["error"]
    delivered = next(value for kind, value in events if kind == "delivery")
    assert "visible answer" not in delivered


def test_run_one_job_keeps_exact_silent_settlement_successful(monkeypatch):
    import cron.scheduler as scheduler

    events: list[tuple[str, object]] = []

    def fake_run_job(job, *, settlement=None, **_kwargs):
        assert settlement is not None
        settlement.update(
            {
                "session_id": "cron-silent-fire",
                "status": "silent",
                "end_reason": "cron_complete",
            }
        )
        return True, "audit output", "[SILENT]", None

    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "mark_execution_running", lambda *_args: None)
    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: None)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_args, **_kwargs: events.append(("delivery", True)))
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *args, **kwargs: events.append(("mark", (args, kwargs))))
    monkeypatch.setattr(scheduler, "finish_execution", lambda execution_id, **kwargs: events.append(("finish", kwargs)))

    assert scheduler.run_one_job({"id": "job-silent", "execution_id": "exec-silent"}) is True

    marked = next(value for kind, value in events if kind == "mark")
    assert marked[0][1] is True
    finished = next(value for kind, value in events if kind == "finish")
    assert finished["success"] is True
    assert not any(kind == "delivery" for kind, _value in events)


def test_failure_reporting_accepts_legacy_string_schedule():
    from cron.scheduler import _failure_streak_nudge

    assert _failure_streak_nudge({"id": "legacy", "schedule": "every 5m"}) == ""


class _RecordingSessionDB:
    next_tail = [{"role": "tool", "content": "ok"}]
    probe_error = None

    def __init__(self, *args, **kwargs):
        self.ended: list[tuple[str, str]] = []

    def set_session_title(self, *args, **kwargs):
        return True

    def get_compression_tip(self, session_id):
        return session_id

    def get_messages(self, *args, **kwargs):
        if type(self).probe_error is not None:
            raise type(self).probe_error
        return list(type(self).next_tail)

    def end_session(self, session_id, reason):
        self.ended.append((session_id, reason))

    def close(self):
        pass


class _FakeCronAgent:
    def __init__(self, *args, **kwargs):
        pass

    def run_conversation(self, prompt):
        return {
            "completed": True,
            "failed": False,
            "final_response": "done",
            "turn_exit_reason": "",
        }

    def close(self):
        pass


class _FailingCronAgent(_FakeCronAgent):
    def run_conversation(self, prompt):
        raise RuntimeError("provider failed")


def _run_job_with_persisted_tail(
    monkeypatch, tmp_path, tail, *, probe_error=None, agent_cls=_FakeCronAgent
):
    import hermes_state
    import run_agent
    import cron.scheduler as scheduler

    _RecordingSessionDB.next_tail = tail
    _RecordingSessionDB.probe_error = probe_error
    instances: list[_RecordingSessionDB] = []
    real_init = _RecordingSessionDB.__init__

    def capture_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        instances.append(self)

    monkeypatch.setattr(_RecordingSessionDB, "__init__", capture_init)
    monkeypatch.setattr(hermes_state, "SessionDB", _RecordingSessionDB)
    monkeypatch.setattr(run_agent, "AIAgent", agent_cls)
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(scheduler, "get_fallback_chain", lambda _cfg: [])
    monkeypatch.setattr(scheduler, "_guard_job_credential_exfil", lambda _job: None)
    monkeypatch.setattr(
        "hermes_constants.resolve_reasoning_config", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", lambda: [])
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "api_key": "test-key",
            "base_url": None,
            "provider": "test-provider",
            "api_mode": None,
            "command": None,
            "args": None,
        },
    )

    result = scheduler.run_job(
        {
            "id": "job-tail-proof",
            "name": "Tail proof",
            "prompt": "Do the thing",
            "schedule_display": "manual",
        }
    )
    return result, instances


def test_run_job_downgrades_a_tool_tail_and_probe_failure(monkeypatch, tmp_path):
    result, instances = _run_job_with_persisted_tail(
        monkeypatch, tmp_path, [{"role": "tool", "content": "ok"}]
    )

    assert result[0] is True
    assert len(instances[0].ended) == 1
    assert instances[0].ended[0][1] == "cron_incomplete_no_output"

    result, instances = _run_job_with_persisted_tail(
        monkeypatch,
        tmp_path,
        [{"role": "tool", "content": "ok"}],
        probe_error=RuntimeError("db busy"),
    )
    assert result[0] is True
    assert instances[0].ended[0][1] == "cron_unverified"


def test_run_job_failure_never_books_cron_complete(monkeypatch, tmp_path):
    result, instances = _run_job_with_persisted_tail(
        monkeypatch,
        tmp_path,
        [{"role": "tool", "content": "ok"}],
        agent_cls=_FailingCronAgent,
    )

    assert result[0] is False
    assert instances[0].ended[0][1] == "cron_failed"


def test_run_job_accepts_exact_silent_assistant_tail(monkeypatch, tmp_path):
    result, instances = _run_job_with_persisted_tail(
        monkeypatch, tmp_path, [{"role": "assistant", "content": "[SILENT]", "finish_reason": "stop"}]
    )

    assert result[0] is True
    assert instances[0].ended[0][1] == "cron_complete"
