"""Runtime authority tests for the bounded Blocker-A repair."""

import json
from types import SimpleNamespace

import pytest


def test_deny_policy_blocks_real_file_and_terminal_mutations(tmp_path):
    from agent.session_write_policy import SessionWritePolicy, session_write_policy_scope
    from tools.file_tools import write_file_tool
    from tools.terminal_tool import terminal_tool

    file_sentinel = tmp_path / "file-sentinel.txt"
    terminal_sentinel = tmp_path / "terminal-sentinel.txt"
    policy = SessionWritePolicy.deny_all(session_id="protected", origin="test")

    with session_write_policy_scope(policy):
        file_result = json.loads(write_file_tool(str(file_sentinel), "blocked", task_id="policy-test"))
        terminal_result = json.loads(
            terminal_tool(
                command=f"touch {terminal_sentinel}",
                task_id="policy-test",
                workdir=str(tmp_path),
            )
        )

    assert "Session write policy denied" in file_result["error"]
    assert terminal_result["exit_code"] == -1
    assert not file_sentinel.exists()
    assert not terminal_sentinel.exists()


def test_normal_policy_preserves_real_file_mutation(tmp_path):
    from agent.session_write_policy import SessionWritePolicy, session_write_policy_scope
    from tools.file_tools import write_file_tool

    sentinel = tmp_path / "normal-sentinel.txt"
    with session_write_policy_scope(SessionWritePolicy.normal(session_id="normal")):
        result = json.loads(write_file_tool(str(sentinel), "allowed", task_id="policy-test"))

    assert result.get("error") in (None, "")
    assert sentinel.read_text(encoding="utf-8") == "allowed"


def test_protected_missing_or_invalid_retained_policy_refuses_dispatch():
    from agent.session_write_policy import require_turn_policy

    missing = require_turn_policy(None, protected=True, session_id="protected")
    invalid = require_turn_policy(object(), protected=True, session_id="protected")
    normal = require_turn_policy(None, protected=False, session_id="normal")

    assert missing is None
    assert invalid is None
    assert normal is not None


@pytest.mark.parametrize("replacement", [None, object()], ids=["missing", "invalid"])
def test_real_turn_boundary_rejects_broken_protected_retained_policy(replacement, monkeypatch):
    """A broken retained protected policy reaches the real run_agent boundary.

    The body is the production conversation-loop seam; it must remain untouched
    when authority validation rejects the turn.
    """
    from agent import relay_runtime
    from hermes_cli.observability import relay_shared_metrics
    from run_agent import AIAgent
    from agent.session_write_policy import SessionWritePolicy
    import agent.conversation_loop as conversation_loop

    agent = AIAgent(
        api_key="test",
        base_url="http://example.test/v1",
        model="test",
        session_id="protected",
        session_write_policy=SessionWritePolicy.deny_all(session_id="protected"),
        skip_memory=True,
        skip_background_review=True,
    )
    agent.session_write_policy = replacement
    dispatched = []
    lifecycle = {"start": 0, "finish": 0, "logical_finish": [], "end": []}

    def body(*_args, **_kwargs):
        dispatched.append(True)
        return {"completed": True}

    monkeypatch.setattr(conversation_loop, "run_conversation", body)
    monkeypatch.setattr(relay_shared_metrics, "start_task_run", lambda **_kwargs: lifecycle.__setitem__("start", lifecycle["start"] + 1))
    monkeypatch.setattr(relay_shared_metrics, "finish_task_run", lambda **_kwargs: lifecycle.__setitem__("finish", lifecycle["finish"] + 1))
    coordinator = relay_runtime.SESSION_COORDINATOR
    monkeypatch.setattr(coordinator, "acquire_conversation", lambda **_kwargs: object())
    monkeypatch.setattr(coordinator, "begin_turn", lambda *_args, **_kwargs: SimpleNamespace(relay_enabled=True))
    monkeypatch.setattr(coordinator, "finish_logical_calls", lambda _turn, *, outcome: lifecycle["logical_finish"].append(outcome))
    monkeypatch.setattr(coordinator, "end_turn", lambda _turn, *, outcome: lifecycle["end"].append(outcome))
    monkeypatch.setattr(coordinator, "release_conversation", lambda _lease: None)
    with pytest.raises(RuntimeError, match="Protected session write policy"):
        agent.run_conversation("test")
    assert dispatched == []
    assert lifecycle == {"start": 1, "finish": 1, "logical_finish": ["failed"], "end": ["failed"]}


def test_real_turn_boundary_binds_explicit_normal_policy_and_dispatches_once(monkeypatch):
    """The real authority branch scopes a valid NORMAL policy before dispatch."""
    from agent import relay_runtime
    from hermes_cli.observability import relay_shared_metrics
    from run_agent import AIAgent
    from agent.session_write_policy import SessionWritePolicy, get_current_session_write_policy
    import agent.conversation_loop as conversation_loop

    agent = AIAgent(
        api_key="test",
        base_url="http://example.test/v1",
        model="test",
        session_write_policy=SessionWritePolicy.normal(session_id="normal"),
        skip_memory=True,
        skip_background_review=True,
    )
    dispatched = []
    lifecycle = {"start": 0, "finish": 0, "logical_finish": [], "end": []}

    def body(*_args, **_kwargs):
        dispatched.append(get_current_session_write_policy().mode.value)
        return {"completed": True}

    monkeypatch.setattr(conversation_loop, "run_conversation", body)
    monkeypatch.setattr(relay_shared_metrics, "start_task_run", lambda **_kwargs: lifecycle.__setitem__("start", lifecycle["start"] + 1))
    monkeypatch.setattr(relay_shared_metrics, "finish_task_run", lambda **_kwargs: lifecycle.__setitem__("finish", lifecycle["finish"] + 1))
    coordinator = relay_runtime.SESSION_COORDINATOR
    monkeypatch.setattr(coordinator, "acquire_conversation", lambda **_kwargs: object())
    monkeypatch.setattr(coordinator, "begin_turn", lambda *_args, **_kwargs: SimpleNamespace(relay_enabled=True))
    monkeypatch.setattr(coordinator, "finish_logical_calls", lambda _turn, *, outcome: lifecycle["logical_finish"].append(outcome))
    monkeypatch.setattr(coordinator, "end_turn", lambda _turn, *, outcome: lifecycle["end"].append(outcome))
    monkeypatch.setattr(coordinator, "release_conversation", lambda _lease: None)

    assert agent.run_conversation("test") == {"completed": True}
    assert dispatched == ["NORMAL"]
    assert lifecycle == {"start": 1, "finish": 1, "logical_finish": ["success"], "end": ["success"]}


def test_deny_policy_blocks_real_patch_replace_and_every_v4a_mutation(tmp_path):
    """Every patch_tool mutation path must stop before its real filesystem write."""
    from agent.session_write_policy import SessionWritePolicy, session_write_policy_scope
    from tools.file_tools import patch_tool

    replace_target = tmp_path / "replace.txt"
    update_target = tmp_path / "update.txt"
    add_target = tmp_path / "added.txt"
    delete_target = tmp_path / "delete.txt"
    move_source = tmp_path / "move-source.txt"
    move_destination = tmp_path / "move-destination.txt"
    replace_target.write_text("before\n", encoding="utf-8")
    update_target.write_text("before\n", encoding="utf-8")
    delete_target.write_text("before\n", encoding="utf-8")
    move_source.write_text("before\n", encoding="utf-8")

    patch_cases = {
        "replace": dict(
            mode="replace", path=str(replace_target), old_string="before", new_string="after"
        ),
        "update": dict(
            mode="patch", patch=(
                "*** Begin Patch\n"
                f"*** Update File: {update_target}\n"
                "@@\n-before\n+after\n"
                "*** End Patch\n"
            ),
        ),
        "add": dict(
            mode="patch", patch=(
                "*** Begin Patch\n"
                f"*** Add File: {add_target}\n"
                "+after\n"
                "*** End Patch\n"
            ),
        ),
        "delete": dict(
            mode="patch", patch=(
                "*** Begin Patch\n"
                f"*** Delete File: {delete_target}\n"
                "*** End Patch\n"
            ),
        ),
        "move": dict(
            mode="patch", patch=(
                "*** Begin Patch\n"
                f"*** Move File: {move_source} -> {move_destination}\n"
                "*** End Patch\n"
            ),
        ),
    }

    with session_write_policy_scope(SessionWritePolicy.deny_all(session_id="protected")):
        results = {name: json.loads(patch_tool(**kwargs)) for name, kwargs in patch_cases.items()}

    assert all("Session write policy denied" in result["error"] for result in results.values())
    assert replace_target.read_text(encoding="utf-8") == "before\n"
    assert update_target.read_text(encoding="utf-8") == "before\n"
    assert not add_target.exists()
    assert delete_target.read_text(encoding="utf-8") == "before\n"
    assert move_source.read_text(encoding="utf-8") == "before\n"
    assert not move_destination.exists()


@pytest.mark.parametrize(
    "command",
    [
        "cat /dev/null > {target}",
        "git status && touch {target}",
        "git status ; touch {target}",
        "git status || touch {target}",
        "printf x >> {target}",
        "echo x | tee {target}",
    ],
    ids=["redirection", "and", "semicolon", "or", "append", "pipe"],
)
def test_deny_policy_blocks_mutating_shell_syntax(tmp_path, command):
    from agent.session_write_policy import SessionWritePolicy, session_write_policy_scope
    from tools.terminal_tool import terminal_tool

    target = tmp_path / "terminal-sentinel.txt"
    with session_write_policy_scope(SessionWritePolicy.deny_all(session_id="protected")):
        result = json.loads(terminal_tool(command=command.format(target=target), workdir=str(tmp_path)))

    assert result["exit_code"] == -1
    assert "Session write policy denied" in result["error"]
    assert not target.exists()


@pytest.mark.parametrize("command", ["pwd", "git status", "git diff --stat"])
def test_deny_policy_preserves_explicit_readonly_terminal_controls(tmp_path, command):
    from agent.session_write_policy import SessionWritePolicy, session_write_policy_scope
    from tools.terminal_tool import terminal_tool

    with session_write_policy_scope(SessionWritePolicy.deny_all(session_id="protected")):
        result = json.loads(terminal_tool(command=command, workdir=str(tmp_path)))

    assert result["exit_code"] != -1 or "Session write policy denied" not in (result.get("error") or "")


@pytest.mark.parametrize(
    "retained",
    [
        pytest.param(
            __import__("agent.session_write_policy", fromlist=["SessionWritePolicy"]).SessionWritePolicy(
                session_id="live", mode="DENY_ALL", protected=True
            ),
            id="string-mode",
        ),
        pytest.param(
            __import__("agent.session_write_policy", fromlist=["SessionWritePolicy"]).SessionWritePolicy(
                session_id="live", mode=object(), protected=True
            ),
            id="arbitrary-mode",
        ),
        pytest.param(
            __import__("agent.session_write_policy", fromlist=["SessionWritePolicy"]).SessionWritePolicy.deny_all(
                session_id="stale"
            ),
            id="stale-session",
        ),
        pytest.param(SimpleNamespace(session_id="live", mode="DENY_ALL", protected=True), id="plain-object"),
        pytest.param(None, id="missing"),
    ],
)
def test_protected_turn_rejects_malformed_or_stale_retained_policy(retained, monkeypatch, tmp_path):
    from agent import relay_runtime
    from agent.session_write_policy import SessionWritePolicy
    from hermes_cli.observability import relay_shared_metrics
    from run_agent import AIAgent
    import agent.conversation_loop as conversation_loop

    agent = AIAgent(
        api_key="test", base_url="http://example.test/v1", model="test", session_id="live",
        session_write_policy=SessionWritePolicy.deny_all(session_id="live"),
        skip_memory=True, skip_background_review=True,
    )
    agent.session_write_policy = retained
    dispatched = []
    sentinel = tmp_path / "must-not-write.txt"
    lifecycle = {"start": 0, "finish": 0, "logical_finish": [], "end": []}

    def body(*_args, **_kwargs):
        dispatched.append(True)
        sentinel.write_text("mutated", encoding="utf-8")
        return {"completed": True}

    monkeypatch.setattr(conversation_loop, "run_conversation", body)
    monkeypatch.setattr(relay_shared_metrics, "start_task_run", lambda **_kwargs: lifecycle.__setitem__("start", lifecycle["start"] + 1))
    monkeypatch.setattr(relay_shared_metrics, "finish_task_run", lambda **_kwargs: lifecycle.__setitem__("finish", lifecycle["finish"] + 1))
    coordinator = relay_runtime.SESSION_COORDINATOR
    monkeypatch.setattr(coordinator, "acquire_conversation", lambda **_kwargs: object())
    monkeypatch.setattr(coordinator, "begin_turn", lambda *_args, **_kwargs: SimpleNamespace(relay_enabled=True))
    monkeypatch.setattr(coordinator, "finish_logical_calls", lambda _turn, *, outcome: lifecycle["logical_finish"].append(outcome))
    monkeypatch.setattr(coordinator, "end_turn", lambda _turn, *, outcome: lifecycle["end"].append(outcome))
    monkeypatch.setattr(coordinator, "release_conversation", lambda _lease: None)

    with pytest.raises(RuntimeError, match="Protected session write policy"):
        agent.run_conversation("test")
    assert dispatched == []
    assert not sentinel.exists()
    assert lifecycle == {"start": 1, "finish": 1, "logical_finish": ["failed"], "end": ["failed"]}
