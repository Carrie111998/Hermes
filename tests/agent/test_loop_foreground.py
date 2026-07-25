from __future__ import annotations


class Agent:
    api_mode = "chat_completions"
    reasoning_config = {"enabled": True, "effort": "ultra"}
    valid_tool_names = {
        "delegate_task",
        "kanban_show",
        "kanban_complete",
        "kanban_comment",
    }
    _foreground_loop_routed = False


def test_ultra_substantive_foreground_request_routes_without_prompt_mutation(monkeypatch):
    from agent.loop_foreground import decide_foreground_loop_route

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    decision = decide_foreground_loop_route(
        Agent(),
        "Implement the migration, define file boundaries, and verify the rollout.",
        config={"loop": {"enabled": True, "foreground_routing": "ultra"}},
    )

    assert decision.route is True
    assert decision.reason == "canonical_ultra_substantive_foreground_request"


def test_foreground_loop_policy_has_explicit_bypasses(monkeypatch):
    from agent.loop_foreground import decide_foreground_loop_route

    cases = [
        ("hello", "trivial_or_single_step"),
        ("read the migration file", "trivial_or_single_step"),
        ("Implement this without a durable loop", "request_opt_out"),
        ("Which approach should we use?", "clarification_dependency"),
    ]
    for message, reason in cases:
        decision = decide_foreground_loop_route(
            Agent(),
            message,
            config={"loop": {"enabled": True, "foreground_routing": "ultra"}},
        )
        assert decision.route is False
        assert decision.reason == reason

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    decision = decide_foreground_loop_route(
        Agent(),
        "Implement the migration and verify the rollout.",
        config={"loop": {"enabled": True, "foreground_routing": "ultra"}},
    )
    assert decision.reason == "dispatcher_worker"


def test_foreground_loop_policy_respects_effort_and_profile_config():
    from agent.loop_foreground import decide_foreground_loop_route

    medium = Agent()
    medium.reasoning_config = {"enabled": True, "effort": "high"}
    assert decide_foreground_loop_route(
        medium,
        "Implement the migration and verify the rollout.",
        config={"loop": {"foreground_routing": "ultra"}},
    ).reason == "effort_not_ultra"

    assert decide_foreground_loop_route(
        Agent(),
        "Implement the migration and verify the rollout.",
        config={"loop": {"foreground_routing": "off"}},
    ).reason == "foreground_routing_disabled"


def test_foreground_loop_policy_bypasses_active_workflows_and_missing_tools():
    from agent.loop_foreground import decide_foreground_loop_route
    from gateway.session_context import set_current_workflow_id

    try:
        set_current_workflow_id("workflow-1")
        assert decide_foreground_loop_route(
            Agent(),
            "Implement the migration and verify the rollout.",
            config={"loop": {"foreground_routing": "ultra"}},
        ).reason == "internal_wake_or_active_workflow"
    finally:
        set_current_workflow_id("")

    no_loop_tools = Agent()
    no_loop_tools.valid_tool_names = {"delegate_task"}
    assert decide_foreground_loop_route(
        no_loop_tools,
        "Implement the migration and verify the rollout.",
        config={"loop": {"foreground_routing": "ultra"}},
    ).reason == "loop_tools_unavailable"


def test_foreground_loop_policy_bypasses_delegated_children():
    from agent.delegation_context import delegated_child_context
    from agent.loop_foreground import decide_foreground_loop_route

    with delegated_child_context():
        decision = decide_foreground_loop_route(
            Agent(),
            "Implement the migration and verify the rollout.",
            config={"loop": {"foreground_routing": "ultra"}},
        )
    assert decision.reason == "delegated_child"


def test_named_tool_choice_is_provider_native():
    from agent.loop_foreground import foreground_loop_tool_choice

    assert foreground_loop_tool_choice(Agent()) == {
        "type": "function",
        "function": {"name": "delegate_task"},
    }

    anthropic = Agent()
    anthropic.api_mode = "anthropic_messages"
    assert foreground_loop_tool_choice(anthropic) == {
        "type": "tool",
        "name": "delegate_task",
    }


def test_loop_plan_requires_mode_and_context_with_acceptance_criteria():
    from agent.loop_foreground import validate_loop_plan_arguments

    valid = {
        "goal": "Implement the migration",
        "mode": "loop",
        "context": "Boundaries: src only. Acceptance criteria: focused tests pass.",
    }
    assert validate_loop_plan_arguments(valid) == (True, "")
    assert validate_loop_plan_arguments({"goal": "Implement it"})[0] is False
    assert validate_loop_plan_arguments({"goal": "Implement it", "mode": "single"})[0] is False
    assert validate_loop_plan_arguments({**valid, "mode": "durable"})[0] is False

    tasks = [
        {
            "goal": "Review the migration",
            "context": "Boundaries: tests only. Acceptance criteria: report findings.",
        }
    ]
    assert validate_loop_plan_arguments({"mode": "loop", "tasks": tasks}) == (True, "")


def test_default_loop_policy_exposes_foreground_routing():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["loop"]["foreground_routing"] == "ultra"


def test_foreground_loop_plan_enforcement_is_single_and_fail_closed(monkeypatch):
    from agent.loop_foreground import enforce_foreground_loop_plan

    calls = []

    def fake_delegate_task(**kwargs):
        calls.append(kwargs)
        return '{"status":"dispatched","mode":"loop","workflow_id":"wf-1"}'

    monkeypatch.setattr("tools.delegate_tool.delegate_task", fake_delegate_task)
    agent = Agent()
    assert enforce_foreground_loop_plan(agent, "Implement and verify this") == (True, "")
    assert len(calls) == 1
    assert calls[0]["mode"] == "loop"
    assert calls[0]["parent_agent"] is agent
    assert enforce_foreground_loop_plan(agent, "Implement and verify this")[0] is False
    assert len(calls) == 1

    monkeypatch.setattr(
        "tools.delegate_tool.delegate_task",
        lambda **kwargs: '{"error":"auto-decompose unavailable"}',
    )
    failed_agent = Agent()
    ok, error = enforce_foreground_loop_plan(failed_agent, "Implement and verify this")
    assert ok is False
    assert "rejected" in error
