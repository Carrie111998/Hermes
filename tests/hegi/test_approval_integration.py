from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hegi.approval import process_pending_approvals
from hegi.config import load_config
from hegi.gateway_plugin import intercept_telegram_approval
from hegi.memory import DraftGate
from hegi.models import MeetingMinutes
from hegi.state import StateStore


class RecordingMemory:
    def __init__(self, *, matches: list[dict] | None = None):
        self.searches: list[str] = []
        self.drafts: list[dict] = []
        self.matches = matches or []

    def search(self, query: str, limit: int = 5):
        self.searches.append(query)
        return {"results": self.matches}

    def create_draft(self, arguments):
        self.drafts.append(arguments)
        return {"draft_id": "draft-1", "status": "pending"}


class RecordingApproval:
    def __init__(self, *, fail_operation: str = "", live_state: str = "pending"):
        self.calls: list[str] = []
        self.fail_operation = fail_operation
        self.live_state = live_state
        self.commits = 0

    def show(self, draft_id: str):
        self.calls.append(f"show:{draft_id}")
        return {
            "ok": True,
            "state": self.live_state,
            "draft": {
                "draft_id": draft_id,
                "title": "매체미학 연구회의",
                "observed_facts": "인공자연을 매체적 조건으로 정의",
                "current_judgment": "논문 장에 반영",
                "forest_result": (
                    {
                        "ok": True,
                        "sha256": "memory-sha",
                        "path": "04 stm/media-aesthetics/memory.md",
                    }
                    if self.live_state == "committed"
                    else None
                ),
            },
        }

    def approve(self, draft_id: str, *, note: str):
        self.calls.append(f"approve:{draft_id}")
        return {"ok": True, "draft_id": draft_id, "state": "approved"}

    def commit(self, draft_id: str):
        self.calls.append(f"commit:{draft_id}")
        self.commits += 1
        return {
            "ok": True,
            "draft_id": draft_id,
            "state": "committed",
            "forest_result": {
                "ok": True,
                "sha256": "memory-sha",
                "path": "04 stm/media-aesthetics/memory.md",
            },
        }

    def maintenance(self, operation: str):
        self.calls.append(operation)
        if operation == self.fail_operation:
            raise RuntimeError(f"{operation} failed")
        return {"ok": True, "operation": operation}


def _minutes() -> MeetingMinutes:
    return MeetingMinutes(
        meeting_id="meeting-1",
        title="매체미학 연구회의",
        background="교수와 세 에이전트가 개념을 검토함",
        agenda=["인공자연 개념"],
        discussion_flow=[],
        agent_positions=[],
        professor_positions=["기존 장과 연결"],
        agreements=["인공자연을 매체적 조건으로 정의"],
        disagreements=[],
        unresolved_questions=["사례 범위"],
        new_concepts=[],
        evidence_and_sources=[],
        research_direction=["논문 장에 반영"],
        action_items=[],
        memory_evaluation=None,
        confidence=0.9,
        warnings=[],
    )


def _config(tmp_path, monkeypatch):
    home = tmp_path / "runtime"
    hegi = home / "hegi"
    hegi.mkdir(parents=True)
    env_path = home / ".env"
    env_path.write_text("TELEGRAM_BOT_TOKEN=fake-token\n", encoding="utf-8")
    config_path = hegi / "config.yaml"
    config_path.write_text(
        f"""
enabled: true
state_db: "{hegi / 'state.db'}"
telegram:
  chat_id: "-1001"
  curator_env: "{env_path}"
  enabled: true
agents: []
archive:
  local_spool: "{hegi / 'archive'}"
memory:
  enabled: true
  read_server: memory-forest-read
  draft_server: memory-forest-curator-draft
  auto_commit: false
  auto_draft: false
  require_professor_approval: true
  professor_user_ids: ["42"]
  default_project: media_aesthetics
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "hegi.gateway_plugin._ensure_pipeline_worker",
        lambda _config: None,
    )
    config = load_config(config_path)
    state = StateStore(config.state_db)
    state.save_episode(
        "meeting-1", "hash-1", {"meeting_id": "meeting-1"}, "reported"
    )
    state.update_episode(
        "meeting-1", status="reported", minutes=_minutes().to_dict()
    )
    state.record_delivery(
        "meeting-1",
        4,
        "digest",
        status="sent",
        platform_message_id="400",
    )
    return config, state


def _enqueue(state: StateStore, *, text: str, message_id: str, user_id: str = "42"):
    gate = DraftGate(state, RecordingMemory(), professor_user_ids=["42"])
    command = gate.approve(
        meeting_id="meeting-1",
        text=text,
        user_id=user_id,
        platform_message_id=message_id,
    )
    assert state.enqueue_approval_job(
        meeting_id="meeting-1",
        platform_message_id=message_id,
        project="media_aesthetics",
    )
    return command


def test_remember_runs_full_professor_authorized_workflow(tmp_path, monkeypatch):
    config, state = _config(tmp_path, monkeypatch)
    assert _enqueue(state, text="@헤기 기억해", message_id="500") == "remember"
    memory = RecordingMemory()
    approval = RecordingApproval()
    sent: list[str] = []

    result = process_pending_approvals(
        config,
        backend=memory,
        approval_backend=approval,
        sender=lambda _token, _chat, text, **_kwargs: sent.append(text),
    )

    assert result[0]["status"] == "completed"
    assert result[0]["memory_id"] == "memory-sha"
    assert approval.calls == [
        "show:draft-1",
        "show:draft-1",
        "approve:draft-1",
        "commit:draft-1",
        "validate",
        "audit",
        "index",
        "backup",
    ]
    job = state.approval_job_for_meeting("meeting-1")
    assert job and job["idempotency_key"]
    assert state.approval_transitions(int(job["id"]))[-6:] == [
        "committed",
        "validated",
        "audited",
        "indexed",
        "backed_up",
        "completed",
    ]
    assert any("🌲 기억 저장 완료" in item for item in sent)


def test_draft_only_stops_pending(tmp_path, monkeypatch):
    config, state = _config(tmp_path, monkeypatch)
    assert _enqueue(state, text="@헤기 초안 만들어", message_id="501") == "draft"
    approval = RecordingApproval()

    result = process_pending_approvals(
        config,
        backend=RecordingMemory(),
        approval_backend=approval,
        sender=lambda *_args, **_kwargs: None,
    )

    assert result[0]["status"] == "draft_created"
    assert approval.calls == ["show:draft-1"]
    assert approval.commits == 0


def test_gateway_starts_embedded_pipeline_worker_once(monkeypatch):
    import hegi.gateway_plugin as plugin

    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.alive = False

        def start(self):
            self.alive = True
            started.append(self.kwargs)

        def is_alive(self):
            return self.alive

    monkeypatch.setattr(plugin.threading, "Thread", FakeThread)
    monkeypatch.setattr(plugin, "_PIPELINE_THREAD", None)
    config = SimpleNamespace(section=lambda _name: {"poll_seconds": 60})

    plugin._ensure_pipeline_worker(config)
    plugin._ensure_pipeline_worker(config)

    assert len(started) == 1
    assert started[0]["daemon"] is True
    assert started[0]["name"] == "hegi-gateway-pipeline"


def test_embedded_pipeline_shares_standalone_daemon_lock(tmp_path, monkeypatch):
    import hegi.gateway_plugin as plugin

    if plugin.fcntl is None:
        pytest.skip("POSIX daemon lock is unavailable")
    state_db = tmp_path / "hegi" / "state.db"
    config = SimpleNamespace(state_db=state_db)
    calls = []
    pipeline = SimpleNamespace(
        run_once=lambda **kwargs: calls.append(("pipeline", kwargs)),
        state=SimpleNamespace(add_dead_letter=lambda *_args: None),
    )
    monkeypatch.setattr(
        plugin,
        "process_pending_approvals",
        lambda _config: calls.append(("approval", {})),
    )
    lock_path = state_db.parent / "daemon.lock"
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("a+", encoding="ascii") as standalone_lock:
        plugin.fcntl.flock(
            standalone_lock.fileno(),
            plugin.fcntl.LOCK_EX | plugin.fcntl.LOCK_NB,
        )
        assert plugin._run_pipeline_cycle(config, pipeline) is False
        assert calls == []
        plugin.fcntl.flock(standalone_lock.fileno(), plugin.fcntl.LOCK_UN)

    assert plugin._run_pipeline_cycle(config, pipeline) is True
    assert calls == [
        ("pipeline", {"dry_run": False}),
        ("approval", {}),
    ]


def test_gateway_starts_embedded_pipeline_for_plain_discussion(
    tmp_path, monkeypatch
):
    _config(tmp_path, monkeypatch)
    started = []
    monkeypatch.setattr(
        "hegi.gateway_plugin._ensure_pipeline_worker",
        lambda config: started.append(config.chat_id),
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value={"message_id": "ack"}))
    gateway = SimpleNamespace(_adapter_for_source=lambda _source: adapter)
    source = SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        chat_id="-1001",
        user_id="42",
    )
    event = SimpleNamespace(
        text="마찰의 투명성을 논의합시다.",
        message_id="discussion",
        reply_to_message_id=None,
        source=source,
    )

    assert intercept_telegram_approval(
        event=event, gateway=gateway, session_store=None
    ) is None
    assert started == ["-1001"]


def test_plugin_registration_starts_worker_in_gateway_process(
    tmp_path, monkeypatch
):
    _config(tmp_path, monkeypatch)
    started = []
    registered = []
    monkeypatch.setattr(
        "hegi.gateway_plugin._ensure_pipeline_worker",
        lambda config: started.append(config.chat_id),
    )
    context = SimpleNamespace(
        register_hook=lambda name, callback: registered.append((name, callback))
    )

    from hegi.gateway_plugin import register

    register(context)

    assert started == ["-1001"]
    assert registered[0][0] == "pre_gateway_dispatch"


def test_unauthorized_user_cannot_create_draft_or_job(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    state = StateStore(tmp_path / "runtime" / "hegi" / "state.db")
    gate = DraftGate(state, RecordingMemory(), professor_user_ids=["42"])

    with pytest.raises(PermissionError):
        gate.approve(
            meeting_id="meeting-1",
            text="기억해",
            user_id="99",
            platform_message_id="502",
        )

    assert state.approval_job_counts() == {}


def test_duplicate_telegram_approval_is_idempotent(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    state = StateStore(tmp_path / "runtime" / "hegi" / "state.db")
    gate = DraftGate(state, RecordingMemory(), professor_user_ids=["42"])
    assert gate.approve(
        meeting_id="meeting-1",
        text="기억해",
        user_id="42",
        platform_message_id="503",
    )
    with pytest.raises(ValueError, match="이미 처리"):
        gate.approve(
            meeting_id="meeting-1",
            text="기억해",
            user_id="42",
            platform_message_id="503",
        )


def test_one_active_approval_job_per_meeting(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    state = StateStore(tmp_path / "runtime" / "hegi" / "state.db")
    assert state.enqueue_approval_job(
        meeting_id="meeting-1",
        platform_message_id="active-1",
        project="media_aesthetics",
    )
    assert not state.enqueue_approval_job(
        meeting_id="meeting-1",
        platform_message_id="active-2",
        project="media_aesthetics",
    )


def test_multiple_pending_drafts_require_manual_selection(tmp_path, monkeypatch):
    config, state = _config(tmp_path, monkeypatch)
    for message_id, draft_id in (("draft-a", "draft-a"), ("draft-b", "draft-b")):
        _enqueue(state, text="초안 만들어", message_id=message_id)
        job = state.claim_approval_job()
        assert job
        state.update_approval_workflow(
            int(job["id"]), "draft_validated", draft_id=draft_id
        )
        state.complete_approval_job(
            int(job["id"]),
            status="completed",
            result={"draft_id": draft_id, "status": "draft_created"},
        )
    _enqueue(state, text="승인하고 저장해", message_id="approve-ambiguous")
    approval = RecordingApproval()

    result = process_pending_approvals(
        config,
        backend=RecordingMemory(),
        approval_backend=approval,
        sender=lambda *_args, **_kwargs: None,
    )

    assert result[0]["status"] == "manual_review_required"
    assert approval.commits == 0


def test_high_duplicate_requires_manual_review_without_commit(tmp_path, monkeypatch):
    config, state = _config(tmp_path, monkeypatch)
    _enqueue(state, text="기억해", message_id="504")
    approval = RecordingApproval()

    result = process_pending_approvals(
        config,
        backend=RecordingMemory(
            matches=[
                {
                    "memory_id": "existing-1",
                    "title": "기존 기억",
                    "relation": "높은 중복",
                }
            ]
        ),
        approval_backend=approval,
        sender=lambda *_args, **_kwargs: None,
    )

    assert result[0]["status"] == "duplicate_memory"
    assert approval.commits == 0


def test_restart_after_approved_resumes_at_commit(tmp_path, monkeypatch):
    config, state = _config(tmp_path, monkeypatch)
    _enqueue(state, text="기억해", message_id="505")
    job = state.claim_approval_job()
    assert job
    state.update_approval_workflow(
        int(job["id"]),
        "approved",
        draft_id="draft-1",
        idempotency_key="stable-key",
    )
    state.complete_approval_job(int(job["id"]), status="failed", error="crash")
    approval = RecordingApproval()

    result = process_pending_approvals(
        config,
        backend=RecordingMemory(),
        approval_backend=approval,
        sender=lambda *_args, **_kwargs: None,
    )

    assert result[0]["status"] == "completed"
    assert "approve:draft-1" not in approval.calls
    assert approval.commits == 1


def test_committed_meeting_never_commits_again(tmp_path, monkeypatch):
    config, state = _config(tmp_path, monkeypatch)
    _enqueue(state, text="기억해", message_id="506")
    first = RecordingApproval()
    process_pending_approvals(
        config,
        backend=RecordingMemory(),
        approval_backend=first,
        sender=lambda *_args, **_kwargs: None,
    )
    _enqueue(state, text="기억해", message_id="507")
    second = RecordingApproval()

    result = process_pending_approvals(
        config,
        backend=RecordingMemory(),
        approval_backend=second,
        sender=lambda *_args, **_kwargs: None,
    )

    assert result[0]["status"] == "duplicate_memory"
    assert second.commits == 0


def test_external_commit_crash_window_recovers_without_recommit(tmp_path, monkeypatch):
    config, state = _config(tmp_path, monkeypatch)
    _enqueue(state, text="기억해", message_id="507b")
    approval = RecordingApproval(live_state="committed")

    result = process_pending_approvals(
        config,
        backend=RecordingMemory(),
        approval_backend=approval,
        sender=lambda *_args, **_kwargs: None,
    )

    assert result[0]["status"] == "completed"
    assert approval.commits == 0
    assert result[0]["memory_id"] == "memory-sha"


def test_post_commit_failure_retries_audit_without_recommit(tmp_path, monkeypatch):
    config, state = _config(tmp_path, monkeypatch)
    _enqueue(state, text="기억해", message_id="508")
    failing = RecordingApproval(fail_operation="audit")

    first = process_pending_approvals(
        config,
        backend=RecordingMemory(),
        approval_backend=failing,
        sender=lambda *_args, **_kwargs: None,
    )
    assert first[0]["status"] == "post_commit_failed"
    assert failing.commits == 1

    retry = RecordingApproval()
    second = process_pending_approvals(
        config,
        backend=RecordingMemory(),
        approval_backend=retry,
        sender=lambda *_args, **_kwargs: None,
    )
    assert second[0]["status"] == "completed"
    assert retry.commits == 0
    assert "validate" not in retry.calls
    assert "audit" in retry.calls


@pytest.mark.asyncio
async def test_gateway_requires_reply_and_accepts_telemetry_kwargs(
    tmp_path, monkeypatch
):
    _config(tmp_path, monkeypatch)
    processed = []
    monkeypatch.setattr(
        "hegi.gateway_plugin._process_pending_background",
        lambda config: processed.append(config.chat_id),
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value={"message_id": "ack"}))
    gateway = SimpleNamespace(_adapter_for_source=lambda _source: adapter)
    event = SimpleNamespace(
        text="초안 만들어",
        message_id="509",
        reply_to_message_id="400",
        source=SimpleNamespace(
            platform=SimpleNamespace(value="telegram"),
            chat_id="-1001",
            user_id="42",
        ),
    )

    decision = intercept_telegram_approval(
        event=event,
        gateway=gateway,
        session_store=None,
        telemetry_schema_version="v1",
    )
    await asyncio.sleep(0.05)

    assert decision == {"action": "skip", "reason": "hegi-approval-queued"}
    adapter.send.assert_awaited()
    assert processed == ["-1001"]
