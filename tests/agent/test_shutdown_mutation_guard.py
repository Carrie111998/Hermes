from __future__ import annotations

import pytest

from agent.request_phase import (
    guard_tool_call,
    resume_local_mutations,
    suspend_local_mutations,
)


@pytest.fixture(autouse=True)
def _reset_shutdown_mutation_guard():
    resume_local_mutations()
    yield
    resume_local_mutations()


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("skill_manage", {"action": "edit", "name": "business-insurance-audit"}),
        ("write_file", {"path": "SOUL.md", "content": "changed"}),
        ("patch", {"patch": "*** Begin Patch\n*** End Patch\n"}),
        ("terminal", {"command": "Set-Content SOUL.md changed"}),
        ("terminal", {"command": "python -c \"open('SOUL.md', 'w').write('x')\""}),
        (
            "terminal",
            {"command": "node -e \"require('fs').writeFileSync('SOUL.md','x')\""},
        ),
        ("execute_code", {"code": "Path('SOUL.md').write_text('changed')"}),
        ("execute_code", {"code": "print(Path('SOUL.md').read_text())"}),
    ],
)
def test_shutdown_blocks_local_self_improvement_writes(tool_name, arguments):
    suspend_local_mutations("gateway shutdown/drain")

    block = guard_tool_call(tool_name, arguments)

    assert block is not None
    assert "shutdown safety block" in block.lower()


def test_shutdown_does_not_hamstring_read_or_business_provider_tools():
    suspend_local_mutations("gateway shutdown/drain")

    assert guard_tool_call("terminal", {"command": "rg TODO agent"}) is None
    assert (
        guard_tool_call(
            "terrain_schedule_move",
            {"event_id": "exact-id", "scheduled_date": "2026-08-04"},
        )
        is None
    )
