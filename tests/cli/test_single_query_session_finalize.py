from types import SimpleNamespace

import pytest

import cli


@pytest.fixture(autouse=True)
def reset_single_query_finalize_state(monkeypatch):
    monkeypatch.setattr(cli, "_single_query_finalize_attempted_session_ids", set())
    monkeypatch.setattr(cli, "_cleanup_done", False)




@pytest.fixture
def session_db():
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        yield db
    finally:
        db.close()


def _fake_cli(db, agent_session_id, cli_session_id, calls):
    agent = SimpleNamespace(
        session_id=agent_session_id,
        platform="cli",
        close=lambda: calls.append("agent-close"),
    )
    return SimpleNamespace(
        _session_db=db,
        session_id=cli_session_id,
        agent=agent,
        _release_active_session=lambda: calls.append("release"),
    )


def test_finalize_single_query_ends_live_session_row(monkeypatch, session_db):
    db = session_db
    db.create_session("one-shot-session", "cli")
    calls = []
    fake_cli = _fake_cli(db, "one-shot-session", "one-shot-session", calls)

    monkeypatch.setattr(
        cli, "_notify_single_query_session_finalize", lambda _cli: calls.append("finalize")
    )
    monkeypatch.setattr(cli, "_run_cleanup", lambda **kwargs: calls.append("cleanup"))

    cli._finalize_single_query(fake_cli)

    row = db.get_session("one-shot-session")
    assert row["ended_at"] is not None
    assert row["end_reason"] == "cli_close"
    assert "agent-close" not in calls
    assert calls[-1] == "release"


def test_finalize_single_query_ends_live_agent_session_not_stale_cli_session(
    monkeypatch, session_db
):
    db = session_db
    db.create_session("stale-cli-session", "cli")
    db.create_session("live-agent-session", "cli")
    calls = []
    fake_cli = _fake_cli(db, "live-agent-session", "stale-cli-session", calls)

    monkeypatch.setattr(
        cli, "_notify_single_query_session_finalize", lambda _cli: calls.append("finalize")
    )
    monkeypatch.setattr(cli, "_run_cleanup", lambda **kwargs: calls.append("cleanup"))

    cli._finalize_single_query(fake_cli)

    live = db.get_session("live-agent-session")
    assert live["ended_at"] is not None
    assert live["end_reason"] == "cli_close"
    stale = db.get_session("stale-cli-session")
    assert stale["ended_at"] is None
    assert stale["end_reason"] is None


def test_finalize_single_query_cleanup_failure_still_ends_session_and_releases(
    monkeypatch, session_db
):
    db = session_db
    db.create_session("one-shot-session", "cli")
    calls = []
    fake_cli = _fake_cli(db, "one-shot-session", "one-shot-session", calls)

    monkeypatch.setattr(
        cli, "_notify_single_query_session_finalize", lambda _cli: calls.append("finalize")
    )

    def cleanup(**kwargs):
        calls.append("cleanup")
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(cli, "_run_cleanup", cleanup)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        cli._finalize_single_query(fake_cli)

    row = db.get_session("one-shot-session")
    assert row["ended_at"] is not None
    assert row["end_reason"] == "cli_close"
    assert calls[-1] == "release"


def test_finalize_single_query_close_failure_still_runs_cleanup_and_release(monkeypatch):
    calls = []

    class ExplodingDB:
        def end_session(self, session_id, end_reason):
            calls.append("end_session")
            raise RuntimeError("db write failed")

    fake_cli = _fake_cli(ExplodingDB(), "one-shot-session", "one-shot-session", calls)

    monkeypatch.setattr(
        cli, "_notify_single_query_session_finalize", lambda _cli: calls.append("finalize")
    )
    monkeypatch.setattr(cli, "_run_cleanup", lambda **kwargs: calls.append("cleanup"))

    cli._finalize_single_query(fake_cli)

    assert calls == ["finalize", "end_session", "cleanup", "release"]


@pytest.mark.parametrize(
    "hook_error",
    [KeyboardInterrupt(), SystemExit(23)],
    ids=["keyboard-interrupt", "system-exit"],
)
def test_finalize_single_query_hook_base_exception_still_closes_and_cleans_up(
    monkeypatch, session_db, hook_error
):
    db = session_db
    db.create_session("one-shot-session", "cli")
    calls = []
    fake_cli = _fake_cli(db, "one-shot-session", "one-shot-session", calls)

    def finalize(_cli):
        calls.append("finalize")
        raise hook_error

    monkeypatch.setattr(cli, "_notify_single_query_session_finalize", finalize)
    monkeypatch.setattr(cli, "_run_cleanup", lambda **kwargs: calls.append("cleanup"))

    with pytest.raises(type(hook_error)) as exc_info:
        cli._finalize_single_query(fake_cli)

    assert exc_info.value is hook_error
    row = db.get_session("one-shot-session")
    assert row["ended_at"] is not None
    assert row["end_reason"] == "cli_close"
    assert calls == ["finalize", "cleanup", "release"]


def test_finalize_single_query_preserves_existing_end_reason(monkeypatch, session_db):
    db = session_db
    db.create_session("one-shot-session", "cli")
    db.end_session("one-shot-session", "new_session")
    already_ended = db.get_session("one-shot-session")
    calls = []
    fake_cli = _fake_cli(db, "one-shot-session", "one-shot-session", calls)

    monkeypatch.setattr(
        cli, "_notify_single_query_session_finalize", lambda _cli: calls.append("finalize")
    )
    monkeypatch.setattr(cli, "_run_cleanup", lambda **kwargs: calls.append("cleanup"))

    cli._finalize_single_query(fake_cli)

    row = db.get_session("one-shot-session")
    assert row["end_reason"] == "new_session"
    assert row["ended_at"] == already_ended["ended_at"]
    assert calls[-1] == "release"


def test_finalize_single_query_releases_session_when_cleanup_fails(monkeypatch):
    calls = []
    fake_cli = SimpleNamespace(_release_active_session=lambda: calls.append("release"))

    def cleanup(**kwargs):
        calls.append("cleanup")
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(
        cli,
        "_notify_single_query_session_finalize",
        lambda _cli: calls.append("finalize"),
    )
    monkeypatch.setattr(cli, "_run_cleanup", cleanup)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        cli._finalize_single_query(fake_cli)

    assert calls == ["finalize", "cleanup", "release"]


def test_finalize_single_query_runs_cleanup_when_finalize_hook_fails(monkeypatch):
    calls = []
    fake_agent = SimpleNamespace(session_id="agent-session", platform="cli")
    fake_cli = SimpleNamespace(
        agent=fake_agent,
        session_id="cli-session",
        _release_active_session=lambda: calls.append("release"),
    )

    def invoke_hook(name, **kwargs):
        calls.append("finalize")
        raise RuntimeError("hook failed")

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", invoke_hook)
    monkeypatch.setattr(cli, "_run_cleanup", lambda **kwargs: calls.append("cleanup"))

    cli._finalize_single_query(fake_cli)

    assert calls == ["finalize", "cleanup", "release"]




def test_notify_single_query_session_finalize_uses_agent_session(monkeypatch):
    calls = []
    fake_agent = SimpleNamespace(session_id="agent-session", platform="cli")
    fake_cli = SimpleNamespace(agent=fake_agent, session_id="cli-session")

    def invoke_hook(name, **kwargs):
        calls.append((name, kwargs))

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", invoke_hook)

    cli._notify_single_query_session_finalize(fake_cli)

    assert calls == [
        (
            "on_session_finalize",
            {
                "session_id": "agent-session",
                "platform": "cli",
                "reason": "shutdown",
            },
        )
    ]


def test_human_single_query_main_finalizes_after_query(monkeypatch):
    calls = []

    import cli as cli_mod

    class _Console:
        def print(self, *_args, **_kwargs):
            calls.append("query-label")

    class FakeCLI:
        def __init__(self, **_kwargs):
            self.console = _Console()
            self.session_id = "single-query-session"
            self.agent = SimpleNamespace(
                session_id="single-query-session",
                platform="cli",
            )

        def _claim_active_session(self, surface, *, stderr=False):
            calls.append(("claim", surface, stderr))
            return True

        def _show_security_advisories(self):
            calls.append("advisories")

        def chat(self, query, images=None):
            calls.append(("chat", query, images))
            return "done"

        def _print_exit_summary(self, clear_screen=True):
            calls.append("summary")

    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_mod,
        "_finalize_single_query",
        lambda fake_cli: calls.append(("finalize", fake_cli.session_id)),
    )

    cli_mod.main(query="hello", quiet=False, toolsets="terminal")

    assert calls == [
        ("claim", "cli", False),
        "query-label",
        "advisories",
        ("chat", "hello", None),
        "summary",
        ("finalize", "single-query-session"),
    ]


def test_quiet_single_query_main_finalizes_while_preserving_exit_code(monkeypatch):
    calls = []

    import cli as cli_mod

    def run_conversation(*, user_message, conversation_history):
        calls.append(("run", user_message, conversation_history))
        return {
            "final_response": "",
            "error": "provider failed",
            "failed": True,
        }

    class FakeCLI:
        def __init__(self, **_kwargs):
            self.provider = "test-provider"
            self.model = "test-model"
            self.session_id = "quiet-session"
            self.conversation_history = []
            self._active_agent_route_signature = "same-route"
            self.agent = SimpleNamespace(
                session_id="quiet-session",
                platform="cli",
                quiet_mode=False,
                suppress_status_output=False,
                stream_delta_callback=object(),
                tool_gen_callback=object(),
                run_conversation=run_conversation,
            )

        def _claim_active_session(self, surface, *, stderr=False):
            calls.append(("claim", surface, stderr))
            return True

        def _ensure_runtime_credentials(self):
            calls.append("credentials")
            return True

        def _resolve_turn_agent_config(self, effective_query):
            calls.append(("resolve", effective_query))
            return {
                "signature": "same-route",
                "model": None,
                "runtime": None,
                "request_overrides": None,
            }

        def _init_agent(self, **kwargs):
            calls.append(("init", kwargs))
            return True

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_mod,
        "_finalize_single_query",
        lambda fake_cli: calls.append(("finalize", fake_cli.session_id)),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main(query="hello", quiet=True, toolsets="terminal")

    assert exc_info.value.code == 1
    assert ("claim", "cli", True) in calls
    assert ("run", "hello", []) in calls
    assert calls[-1] == ("finalize", "quiet-session")
