import sqlite3
import time

from hermes_cli.execution_boundary import PayloadContract
from hermes_cli.objective_adapters import ActionExecutorRegistry
from hermes_cli.objective_runtime import ExecutionOutcome


def test_registry_rejects_unknown_tool_and_stale_state_before_handler():
    calls = []
    registry = ActionExecutorRegistry()
    registry.register(
        "deploy",
        lambda payload: calls.append(payload) or ExecutionOutcome("succeeded", {}),
        contract=PayloadContract(
            required={"system": str, "target_resource": str},
            optional={
                "observed_state_at": int,
                "max_state_age_seconds": int,
            },
        ),
    )
    missing = registry.execute("invented.tool", {"system": "fiction"})
    stale = registry.execute(
        "deploy",
        {
            "system": "git",
            "target_resource": "main",
            "observed_state_at": int(time.time()) - 100,
            "max_state_age_seconds": 5,
        },
    )
    assert missing.status == "failed"
    assert "no executor registered" in missing.result["error"]
    assert stale.status == "failed"
    assert "stale" in stale.result["error"]
    assert calls == []


def test_active_change_freeze_prevents_modification():
    registry = ActionExecutorRegistry()
    registry.register("deploy", lambda _: ExecutionOutcome("succeeded", {}))
    outcome = registry.execute(
        "deploy",
        {
            "system": "git",
            "change_freeze": {"active": True, "reason": "tax close"},
        },
    )
    assert outcome.status == "failed"
    assert "tax close" in outcome.result["error"]
