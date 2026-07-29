import json


def _fresh_goal_home(monkeypatch, tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_cli.goals as goals

    goals._DB_CACHE.clear()
    return home


def test_goal_queue_fifo_survives_restart_and_promotes_only_on_terminal(monkeypatch, tmp_path):
    _fresh_goal_home(monkeypatch, tmp_path)
    from hermes_cli.goals import GoalManager

    manager = GoalManager("session-a")
    manager.set("active goal")
    queued_state = manager.set("queued one")
    assert queued_state.goal == "active goal"
    assert manager.queue_depth() == 1
    assert "queue: 1" in manager.status_line()

    restarted = GoalManager("session-a")
    assert restarted.state.goal == "active goal"
    assert restarted.queue_depth() == 1

    restarted.pause("waiting")
    assert GoalManager("session-a").state.goal == "active goal"
    assert GoalManager("session-a").queue_depth() == 1

    restarted.resume()
    restarted.mark_done("complete")
    promoted = GoalManager("session-a")
    assert promoted.state.goal == "queued one"
    assert promoted.state.status == "active"
    assert promoted.queue_depth() == 0


def test_goal_queue_migrates_with_compression_session(monkeypatch, tmp_path):
    _fresh_goal_home(monkeypatch, tmp_path)
    from hermes_cli.goals import GoalManager, migrate_goal_to_session

    parent = GoalManager("parent-session")
    parent.set("parent goal")
    parent.set("queued next")

    assert migrate_goal_to_session("parent-session", "child-session", reason="compression") is True

    child = GoalManager("child-session")
    assert child.state.goal == "parent goal"
    assert child.queue_depth() == 1
    child.mark_done("done")
    assert GoalManager("child-session").state.goal == "queued next"


def test_orchestrator_checkpoint_persists_genuine_ids_and_sanitized_dirty_summary(monkeypatch, tmp_path):
    _fresh_goal_home(monkeypatch, tmp_path)
    from hermes_cli.orchestrator_state import OrchestratorStateStore

    store = OrchestratorStateStore("session-a")
    state = store.update(
        active_job_id="job-1",
        attempt_id="attempt-1",
        worker_id="worker-1",
        provider_session_id=None,
    )
    assert state.provider_session_id is None

    checkpoint = store.record_checkpoint(
        goal="ship the fix",
        plan_paths=["docs/plan.md"],
        evidence_paths=["reports/evidence.md"],
        dirty_summary=[{"path": "agent/provider_route_policy.py", "status": "M", "contents": "sentinel-access-token"}],
        process_ids=[123],
        session_ids=["worker-session"],
        next_action="run tests",
        verification={"status": "failed", "command": "pytest", "output": "sentinel-refresh-token"},
    )
    raw = store._db.get_meta("orchestrator:session-a")
    assert "sentinel-access-token" not in raw
    assert "sentinel-refresh-token" not in raw
    assert checkpoint.dirty_summary == [{"path": "agent/provider_route_policy.py", "status": "M"}]

    restarted = OrchestratorStateStore("session-a")
    assert restarted.load().checkpoint.next_action == "run tests"
    assert restarted.load().checkpoint.process_ids == [123]

    reverted = restarted.clear_state()
    assert reverted.active_job_id is None
    assert restarted.load().checkpoint is None


def test_orchestrator_state_migrates_on_compression(monkeypatch, tmp_path):
    _fresh_goal_home(monkeypatch, tmp_path)
    from hermes_cli.orchestrator_state import OrchestratorStateStore, migrate_orchestrator_state_to_session

    OrchestratorStateStore("old").update(active_job_id="job-1", attempt_id="attempt-1")
    assert migrate_orchestrator_state_to_session("old", "new", reason="compression") is True
    assert OrchestratorStateStore("new").load().active_job_id == "job-1"
    assert OrchestratorStateStore("old").load().status == "migrated"
