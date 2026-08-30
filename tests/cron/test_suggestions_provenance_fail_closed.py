"""Runtime regression coverage for suggestions provenance failures."""

import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants
    import cron.suggestions as suggestions

    importlib.reload(hermes_constants)
    importlib.reload(suggestions)
    return suggestions


def _add(store, key="k1"):
    return store.add_suggestion(
        title="Test",
        description="desc",
        source="usage",
        job_spec={"prompt": "do it", "schedule": "0 9 * * *", "name": "Test"},
        dedup_key=key,
    )


def _provenance_failure():
    raise RuntimeError("provenance unavailable")


def test_background_add_fails_closed_when_provenance_lookup_raises(store, monkeypatch):
    monkeypatch.setattr(store, "is_background_review", _provenance_failure, raising=False)
    monkeypatch.setattr(store, "get_self_improvement_decision", lambda: type("D", (), {"allow": True})(), raising=False)

    assert _add(store) is None
    assert store.load_suggestions() == []


def test_background_dismiss_fails_closed_when_provenance_lookup_raises(store, monkeypatch):
    monkeypatch.setattr(store, "is_background_review", lambda: False, raising=False)
    record = _add(store)
    assert record is not None

    monkeypatch.setattr(store, "is_background_review", _provenance_failure, raising=False)
    monkeypatch.setattr(store, "get_self_improvement_decision", lambda: type("D", (), {"allow": True})(), raising=False)

    assert store.dismiss_suggestion(record["id"]) is False
    assert store.get_suggestion(record["id"])["status"] == "pending"


def _run_bound_turn(agent, monkeypatch, body):
    """Drive the production turn scope while replacing only the LLM loop body."""
    from agent import relay_runtime
    from hermes_cli.observability import relay_shared_metrics
    import agent.conversation_loop as conversation_loop

    monkeypatch.setattr(conversation_loop, "run_conversation", body)
    monkeypatch.setattr(relay_shared_metrics, "start_task_run", lambda **_kwargs: None)
    monkeypatch.setattr(relay_shared_metrics, "finish_task_run", lambda **_kwargs: None)
    coordinator = relay_runtime.SESSION_COORDINATOR
    monkeypatch.setattr(coordinator, "acquire_conversation", lambda **_kwargs: object())
    monkeypatch.setattr(coordinator, "begin_turn", lambda *_args, **_kwargs: SimpleNamespace(relay_enabled=True))
    monkeypatch.setattr(coordinator, "finish_logical_calls", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(coordinator, "end_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(coordinator, "release_conversation", lambda *_args, **_kwargs: None)
    return agent.run_conversation("test")


def _agent(*, skip_background_review: bool):
    from run_agent import AIAgent

    return AIAgent(
        api_key="test", base_url="http://example.test/v1", model="test",
        session_id="self-improvement-test", skip_memory=True,
        skip_background_review=skip_background_review,
    )


def test_real_turn_binding_uses_captured_deny_after_environment_changes(store, monkeypatch):
    """An initial deny remains authoritative after process environment changes."""
    from tools.skill_provenance import reset_current_write_origin, set_current_write_origin

    agent = _agent(skip_background_review=True)
    assert agent.self_improvement_decision.allow is False
    monkeypatch.setenv("HERMES_SELF_IMPROVEMENT_DISABLED", "0")

    def body(*_args, **_kwargs):
        token = set_current_write_origin("background_review")
        try:
            assert _add(store) is None
        finally:
            reset_current_write_origin(token)
        return {"completed": True}

    assert _run_bound_turn(agent, monkeypatch, body) == {"completed": True}
    assert store.load_suggestions() == []


def test_real_turn_binding_uses_captured_allow_after_environment_changes(store, monkeypatch):
    """A valid captured allow is not replaced by a later restrictive environment."""
    from tools.skill_provenance import reset_current_write_origin, set_current_write_origin

    agent = _agent(skip_background_review=False)
    assert agent.self_improvement_decision.allow is True
    monkeypatch.setenv("HERMES_SELF_IMPROVEMENT_DISABLED", "1")

    def body(*_args, **_kwargs):
        token = set_current_write_origin("background_review")
        try:
            record = _add(store)
            assert record is not None
            assert store.dismiss_suggestion(record["id"]) is True
        finally:
            reset_current_write_origin(token)
        return {"completed": True}

    assert _run_bound_turn(agent, monkeypatch, body) == {"completed": True}
    assert len(store.load_suggestions()) == 1


def test_provenance_failure_denies_captured_allow_in_real_turn(store, monkeypatch):
    agent = _agent(skip_background_review=False)
    monkeypatch.setattr(store, "is_background_review", _provenance_failure)

    def body(*_args, **_kwargs):
        assert _add(store) is None
        return {"completed": True}

    assert _run_bound_turn(agent, monkeypatch, body) == {"completed": True}
    assert store.load_suggestions() == []


def test_invalid_retained_decision_falls_back_to_deny_without_failing_turn(store, monkeypatch):
    from tools.skill_provenance import reset_current_write_origin, set_current_write_origin

    agent = _agent(skip_background_review=False)
    agent.self_improvement_decision = object()

    def body(*_args, **_kwargs):
        token = set_current_write_origin("background_review")
        try:
            assert _add(store) is None
        finally:
            reset_current_write_origin(token)
        return {"completed": True}

    assert _run_bound_turn(agent, monkeypatch, body) == {"completed": True}
    assert store.load_suggestions() == []


def test_real_turn_decision_scope_restores_after_exception(monkeypatch):
    from agent.self_improvement_decision_context import get_self_improvement_decision

    agent = _agent(skip_background_review=False)
    assert agent.self_improvement_decision.allow is True

    def body(*_args, **_kwargs):
        assert get_self_improvement_decision().allow is True
        raise RuntimeError("turn failure")

    with pytest.raises(RuntimeError, match="turn failure"):
        _run_bound_turn(agent, monkeypatch, body)
    assert get_self_improvement_decision().allow is False
