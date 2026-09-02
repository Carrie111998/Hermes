"""Regression tests: a cron run must notify session lifecycle hooks.

``run_job`` closes its SQLite session row via ``end_session``, but that call
does not reach plugin lifecycle observers. Before this was fixed, every cron
run emitted ``on_session_start`` with no matching ``on_session_finalize``:
measured against a plugin that posts session boundaries to an external store,
51/51 cron sessions were opened and never closed, while the CLI and gateway
paths matched every day.

The three properties pinned here:

1. ``run_job`` calls ``lifecycle.finalize_session`` for its cron session.
2. It passes the *resolved* session id. Compression can rotate the live agent
   onto a continuation mid-run, so the raw id captured before ``AIAgent``
   started would name a session the hook cannot resolve.
3. A plugin that raises cannot skip the SQLite teardown that follows — that
   teardown is what stops a long-lived gateway from leaking one fd per job
   until it hits EMFILE.
"""

from __future__ import annotations

import pytest

import cron.scheduler as cron_scheduler
from hermes_cli import lifecycle


class _RecordingSessionDB:
    """SessionDB stub that records teardown order for assertions."""

    instances: list["_RecordingSessionDB"] = []

    def __init__(self, *args, **kwargs):
        self.ended: tuple | None = None
        self.closed = False
        self.compression_tip: str | None = None
        _RecordingSessionDB.instances.append(self)

    def get_compression_tip(self, _session_id):
        return self.compression_tip

    def set_session_title(self, *args, **kwargs):
        pass

    def get_next_title_in_lineage(self, base):
        return base

    def end_session(self, session_id, reason):
        self.ended = (session_id, reason)

    def close(self):
        self.closed = True


class _FakeCronAgent:
    def __init__(self, *args, **kwargs):
        self.session_id = kwargs.get("session_id")

    def run_conversation(self, _prompt):
        return {
            "completed": True,
            "failed": False,
            "final_response": "done",
            "turn_exit_reason": "",
        }

    def close(self):
        pass


@pytest.fixture
def cron_env(monkeypatch, tmp_path):
    """Wire run_job to in-memory doubles: no network, no real SessionDB."""
    _RecordingSessionDB.instances.clear()
    monkeypatch.setenv("HERMES_MODEL", "test-model")
    monkeypatch.setattr("hermes_state.SessionDB", _RecordingSessionDB)
    monkeypatch.setattr("run_agent.AIAgent", _FakeCronAgent)
    monkeypatch.setattr(
        "hermes_constants.resolve_reasoning_config", lambda *_a, **_kw: None
    )
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
    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", lambda: [])
    monkeypatch.setattr(cron_scheduler, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(cron_scheduler, "get_fallback_chain", lambda _cfg: [])
    monkeypatch.setattr(
        cron_scheduler, "_guard_job_credential_exfil", lambda _job: None
    )
    return tmp_path


def _job(job_id: str) -> dict:
    return {
        "id": job_id,
        "name": f"Job {job_id}",
        "prompt": "Run safely",
        "schedule_display": "manual",
    }


def test_run_job_fires_session_finalize(cron_env, monkeypatch):
    """A completed cron run notifies on_session_finalize for its session."""
    calls: list[dict] = []
    monkeypatch.setattr(
        lifecycle, "finalize_session", lambda **kw: calls.append(kw) or []
    )

    success, _output, _final, error = cron_scheduler.run_job(_job("finalize-basic"))

    assert success is True
    assert error is None
    assert len(calls) == 1
    assert calls[0]["session_id"].startswith("cron_finalize-basic_")
    assert calls[0]["platform"] == "cron"
    assert calls[0]["reason"]

    db = _RecordingSessionDB.instances[-1]
    # The hook is an addition, not a replacement: the DB row still closes.
    assert db.ended is not None
    assert db.ended[0] == calls[0]["session_id"]
    assert db.closed is True


def test_run_job_finalizes_compression_continuation(cron_env, monkeypatch):
    """When compression rotates the session, the continuation id is reported.

    The raw ``cron_<job>_<ts>`` id is assigned before AIAgent starts. If a
    long run is compressed onto a continuation, finalizing the stale id would
    hand observers a session that no longer exists.
    """
    calls: list[dict] = []
    monkeypatch.setattr(
        lifecycle, "finalize_session", lambda **kw: calls.append(kw) or []
    )

    original_init = _RecordingSessionDB.__init__

    def _init_with_tip(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.compression_tip = "cron_rotated_continuation"

    monkeypatch.setattr(_RecordingSessionDB, "__init__", _init_with_tip)

    success, _output, _final, _error = cron_scheduler.run_job(_job("finalize-rot"))

    assert success is True
    assert len(calls) == 1
    assert calls[0]["session_id"] == "cron_rotated_continuation"
    assert _RecordingSessionDB.instances[-1].ended[0] == "cron_rotated_continuation"


def test_finalize_hook_failure_does_not_skip_sqlite_teardown(cron_env, monkeypatch):
    """A raising plugin cannot take the job down or leak the SQLite handle."""
    calls: list[dict] = []

    def _boom(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("plugin exploded")

    monkeypatch.setattr(lifecycle, "finalize_session", _boom)

    success, _output, _final, error = cron_scheduler.run_job(_job("finalize-boom"))

    assert len(calls) == 1
    assert success is True
    assert error is None

    db = _RecordingSessionDB.instances[-1]
    assert db.ended is not None
    assert db.closed is True
