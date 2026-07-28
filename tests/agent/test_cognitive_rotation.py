"""Pure behavior tests for the session-scoped cognitive-rotation controller."""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest

from agent.cognitive_rotation import (
    CognitiveRotationConfig,
    CognitiveRotationController,
)


def test_mutation_budget_counts_only_successes_and_activates_at_exact_boundary():
    controller = CognitiveRotationController(
        CognitiveRotationConfig(enabled=True, mutation_budget=2)
    )

    assert controller.observe_tool_result("patch", failed=True) is None
    assert controller.successful_mutations == 0
    assert controller.observe_tool_result("write_file", failed=False) is None
    assert controller.successful_mutations == 1
    assert controller.before_call("execute_code").allows_execution

    notice = controller.observe_tool_result("execute_code", failed=False)

    assert controller.successful_mutations == 2
    assert notice is not None
    assert "mutation_budget" in notice
    blocked = controller.before_call("patch")
    assert not blocked.allows_execution
    assert blocked.reason == "mutation_budget"
    assert controller.observe_tool_result("write_file", failed=False) is None


def test_successful_compaction_after_a_mutation_activates_rotation():
    controller = CognitiveRotationController(
        CognitiveRotationConfig(enabled=True, mutation_budget=0)
    )
    controller.observe_tool_result("write_file", failed=False)

    notice = controller.observe_compaction(made_progress=True, committed=True)

    assert notice is not None
    assert "compaction" in notice
    blocked = controller.before_call("patch")
    assert not blocked.allows_execution
    assert blocked.reason == "compaction"


def test_mixed_delegation_batch_blocks_direct_mutator_without_permanent_activation():
    controller = CognitiveRotationController(
        CognitiveRotationConfig(enabled=True, mutation_budget=0)
    )

    blocked = controller.before_call(
        "write_file",
        batch_has_delegation=True,
    )

    assert not blocked.allows_execution
    assert blocked.reason == "mixed_delegation_batch"
    assert controller.active_reason == ""
    assert controller.before_call("write_file").allows_execution


def test_default_disabled_controller_is_a_complete_noop():
    controller = CognitiveRotationController()

    assert controller.before_call(
        "write_file", batch_has_delegation=True
    ).allows_execution
    assert controller.observe_tool_result("write_file", failed=False) is None
    assert controller.observe_tool_result("delegate_task", failed=False) is None
    assert controller.observe_compaction(made_progress=True, committed=True) is None
    assert controller.successful_mutations == 0
    assert controller.active_reason == ""


def test_malformed_config_values_fall_back_to_safe_defaults():
    config = CognitiveRotationConfig.from_mapping({
        "enabled": "sometimes",
        "mutation_budget": object(),
        "rotate_after_compaction": [],
        "lock_after_delegation": {"invalid": True},
    })

    assert config == CognitiveRotationConfig()
    malformed_mapping = cast(Any, ["not", "a", "mapping"])
    assert CognitiveRotationConfig.from_mapping(malformed_mapping) == (
        CognitiveRotationConfig()
    )


def test_failed_delegation_does_not_activate_rotation():
    controller = CognitiveRotationController(CognitiveRotationConfig(enabled=True))

    assert controller.observe_tool_result("delegate_task", failed=True) is None
    assert controller.active_reason == ""
    assert controller.before_call("write_file").allows_execution


def test_successful_delegation_activates_rotation_when_enabled():
    controller = CognitiveRotationController(CognitiveRotationConfig(enabled=True))

    notice = controller.observe_tool_result("delegate_task", failed=False)

    assert notice is not None
    assert "delegation" in notice
    assert controller.active_reason == "delegation"


@pytest.mark.parametrize("mutation_budget", [0, -1])
def test_non_positive_budget_does_not_activate_from_mutations(mutation_budget):
    controller = CognitiveRotationController(
        CognitiveRotationConfig(enabled=True, mutation_budget=mutation_budget)
    )

    for _ in range(3):
        assert controller.observe_tool_result("patch", failed=False) is None

    assert controller.successful_mutations == 3
    assert controller.active_reason == ""
    assert controller.before_call("write_file").allows_execution


@pytest.mark.parametrize("mutation_budget", [0, -1])
def test_non_positive_budget_preserves_delegation_trigger(mutation_budget):
    controller = CognitiveRotationController(
        CognitiveRotationConfig(enabled=True, mutation_budget=mutation_budget)
    )

    notice = controller.observe_tool_result("delegate_task", failed=False)

    assert notice is not None
    assert controller.active_reason == "delegation"


def test_compaction_before_a_successful_mutation_does_not_activate():
    controller = CognitiveRotationController(CognitiveRotationConfig(enabled=True))

    assert controller.observe_compaction(made_progress=True, committed=True) is None
    assert controller.active_reason == ""


def test_no_progress_compaction_does_not_activate():
    controller = CognitiveRotationController(
        CognitiveRotationConfig(enabled=True, mutation_budget=0)
    )
    controller.observe_tool_result("write_file", failed=False)

    assert controller.observe_compaction(made_progress=False, committed=True) is None
    assert controller.active_reason == ""


def test_uncommitted_compaction_does_not_activate():
    controller = CognitiveRotationController(
        CognitiveRotationConfig(enabled=True, mutation_budget=0)
    )
    controller.observe_tool_result("write_file", failed=False)

    assert controller.observe_compaction(made_progress=True, committed=False) is None
    assert controller.active_reason == ""


def test_disabled_compaction_trigger_does_not_activate():
    controller = CognitiveRotationController(
        CognitiveRotationConfig(
            enabled=True,
            mutation_budget=0,
            rotate_after_compaction=False,
        )
    )
    controller.observe_tool_result("write_file", failed=False)

    assert controller.observe_compaction(made_progress=True, committed=True) is None
    assert controller.active_reason == ""


@pytest.mark.parametrize(
    "tool_name",
    ["read_file", "search_files", "terminal", "run_tests", "delegate_task"],
)
def test_non_mutating_work_remains_permitted_after_activation(tool_name):
    controller = CognitiveRotationController(CognitiveRotationConfig(enabled=True))
    controller.observe_tool_result("delegate_task", failed=False)

    assert controller.before_call(tool_name).allows_execution


def test_activation_notice_is_returned_exactly_once():
    controller = CognitiveRotationController(CognitiveRotationConfig(enabled=True))

    first_notice = controller.observe_tool_result("delegate_task", failed=False)
    repeated_notice = controller.observe_tool_result("delegate_task", failed=False)
    later_compaction_notice = controller.observe_compaction(
        made_progress=True,
        committed=True,
    )

    assert first_notice is not None
    assert repeated_notice is None
    assert later_compaction_notice is None


def test_rotation_budget_admission_is_atomic_across_threads():
    controller = CognitiveRotationController(
        CognitiveRotationConfig(enabled=True, mutation_budget=1)
    )
    start = threading.Barrier(3)

    def admit():
        start.wait()
        return controller.before_call("write_file")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(admit) for _ in range(2)]
        start.wait()
        decisions = [future.result() for future in futures]

    admitted = [decision for decision in decisions if decision.allows_execution]
    blocked = [decision for decision in decisions if not decision.allows_execution]
    assert len(admitted) == 1
    assert len(blocked) == 1
    assert blocked[0].reason == "mutation_budget"
    assert controller.successful_mutations == 0
    assert controller.pending_mutation_reservations == 1

    assert admitted[0].reservation_id is not None
    assert (
        controller.observe_tool_result(
            "write_file",
            failed=True,
            reservation_id=admitted[0].reservation_id,
        )
        is None
    )
    assert controller.pending_mutation_reservations == 0
