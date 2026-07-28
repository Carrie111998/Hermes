"""Lifecycle coverage for isolated-worker workspace authority bindings."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from run_agent import AIAgent
from tools import terminal_tool


@pytest.fixture(autouse=True)
def _restore_workspace_authority_registry():
    with terminal_tool._workspace_lease_authorities_lock:
        original_authorities = dict(
            terminal_tool._workspace_lease_authorities
        )
        original_owners = {
            runtime_id: set(owners)
            for runtime_id, owners in (
                terminal_tool._workspace_lease_authority_owners.items()
            )
        }
        terminal_tool._workspace_lease_authorities.clear()
        terminal_tool._workspace_lease_authority_owners.clear()
    yield
    with terminal_tool._workspace_lease_authorities_lock:
        terminal_tool._workspace_lease_authorities.clear()
        terminal_tool._workspace_lease_authorities.update(
            original_authorities
        )
        terminal_tool._workspace_lease_authority_owners.clear()
        terminal_tool._workspace_lease_authority_owners.update(
            original_owners
        )


def _minimal_agent(
    *,
    owner_id: str = "owner-a",
    session_id: str = "session-a",
    authority: str = "root-session",
) -> AIAgent:
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = session_id
    agent._workspace_lease_authority = authority
    agent._workspace_lease_persistent_runtime_ids = set()
    agent._workspace_lease_runtime_ids = ()
    agent._runtime_resource_task_ids = set()
    agent._workspace_lease_authority_lock = threading.RLock()
    agent._workspace_lease_binding_owner_id = owner_id
    agent._isolated_worker_backend_selected = True
    agent._owns_runtime_resources = True
    agent._close_started = False
    return agent


def _prepare_for_close(agent: AIAgent) -> None:
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    agent._end_session_on_close = False
    agent.client = None


def _claim(agent: AIAgent, *runtime_ids: str) -> None:
    agent._track_workspace_lease_persistent_runtime_ids(runtime_ids)
    terminal_tool.register_workspace_lease_authorities(
        runtime_ids,
        agent._workspace_lease_authority,
        owner_id=agent._workspace_lease_binding_owner_id,
    )


def test_one_owner_release_cannot_remove_another_live_owner():
    first = _minimal_agent(owner_id="owner-a")
    second = _minimal_agent(owner_id="owner-b")
    _claim(first, "session-a", "compression-child")
    _claim(second, "session-a", "compression-child")

    released = first.release_workspace_lease_authority_bindings(
        reset_authority=True
    )

    assert released == ("compression-child", "session-a")
    with terminal_tool._workspace_lease_authorities_lock:
        assert terminal_tool._workspace_lease_authorities == {
            "session-a": "root-session",
            "compression-child": "root-session",
        }
        assert terminal_tool._workspace_lease_authority_owners == {
            "session-a": {"owner-b"},
            "compression-child": {"owner-b"},
        }

    second.release_workspace_lease_authority_bindings(
        reset_authority=True
    )
    with terminal_tool._workspace_lease_authorities_lock:
        assert terminal_tool._workspace_lease_authorities == {}
        assert terminal_tool._workspace_lease_authority_owners == {}


def test_release_cannot_remove_a_foreign_exact_authority_binding():
    agent = _minimal_agent()
    agent._workspace_lease_persistent_runtime_ids.add("foreign-session")
    terminal_tool.register_workspace_lease_authority(
        "foreign-session",
        "other-root",
        owner_id="foreign-owner",
    )

    agent.release_workspace_lease_authority_bindings(reset_authority=True)

    with terminal_tool._workspace_lease_authorities_lock:
        assert (
            terminal_tool._workspace_lease_authorities["foreign-session"]
            == "other-root"
        )
        assert terminal_tool._workspace_lease_authority_owners[
            "foreign-session"
        ] == {"foreign-owner"}


def test_same_lineage_transition_retains_old_and_new_aliases():
    agent = _minimal_agent()
    _claim(agent, "session-a")

    agent.transition_workspace_lease_conversation(
        "compression-child",
        "root-session",
        retain_existing_aliases=True,
    )

    assert agent.session_id == "compression-child"
    assert agent._workspace_lease_authority == "root-session"
    assert agent._workspace_lease_persistent_runtime_ids == {
        "session-a",
        "compression-child",
    }
    with terminal_tool._workspace_lease_authorities_lock:
        assert terminal_tool._workspace_lease_authorities == {
            "session-a": "root-session",
            "compression-child": "root-session",
        }


def test_unrelated_transition_stages_new_claim_before_releasing_old():
    agent = _minimal_agent()
    _claim(agent, "session-a")

    agent.transition_workspace_lease_conversation(
        "other-session",
        "other-session",
        retain_existing_aliases=False,
    )

    assert agent.session_id == "other-session"
    assert agent._workspace_lease_authority == "other-session"
    assert agent._workspace_lease_persistent_runtime_ids == {
        "other-session"
    }
    with terminal_tool._workspace_lease_authorities_lock:
        assert terminal_tool._workspace_lease_authorities == {
            "other-session": "other-session"
        }


def test_unrelated_transition_reaps_old_root_after_last_owner(
    monkeypatch,
):
    agent = _minimal_agent()
    _claim(agent, "session-a", "compression-child")
    cleanup_vm = MagicMock()
    kill_all = MagicMock()
    monkeypatch.setattr(terminal_tool, "cleanup_vm", cleanup_vm)
    monkeypatch.setattr(
        "tools.process_registry.process_registry.kill_all",
        kill_all,
    )

    agent.transition_workspace_lease_conversation(
        "other-session",
        "other-session",
        retain_existing_aliases=False,
    )
    kill_all.assert_not_called()
    cleanup_vm.assert_not_called()

    agent.retire_workspace_lease_authority("root-session")
    kill_all.assert_called_once_with(task_id="root-session")
    cleanup_vm.assert_called_once_with("root-session")


def test_unrelated_transition_keeps_old_root_for_another_owner(
    monkeypatch,
):
    first = _minimal_agent(owner_id="owner-a", session_id="session-a")
    second = _minimal_agent(owner_id="owner-b", session_id="session-b")
    _claim(first, "session-a")
    _claim(second, "session-b")
    cleanup_vm = MagicMock()
    kill_all = MagicMock()
    monkeypatch.setattr(terminal_tool, "cleanup_vm", cleanup_vm)
    monkeypatch.setattr(
        "tools.process_registry.process_registry.kill_all",
        kill_all,
    )

    first.transition_workspace_lease_conversation(
        "other-session",
        "other-session",
        retain_existing_aliases=False,
    )

    kill_all.assert_not_called()
    cleanup_vm.assert_not_called()
    assert terminal_tool._workspace_lease_authorities == {
        "session-b": "root-session",
        "other-session": "other-session",
    }


def test_conflicting_transition_rolls_back_without_mutating_live_identity():
    agent = _minimal_agent()
    _claim(agent, "session-a")
    terminal_tool.register_workspace_lease_authority(
        "target-session",
        "foreign-root",
        owner_id="foreign-owner",
    )

    with pytest.raises(
        RuntimeError,
        match="workspace_lease_authority_rebind_denied",
    ):
        agent.transition_workspace_lease_conversation(
            "target-session",
            "target-session",
            retain_existing_aliases=False,
        )

    assert agent.session_id == "session-a"
    assert agent._workspace_lease_authority == "root-session"
    assert agent._workspace_lease_persistent_runtime_ids == {
        "session-a"
    }
    with terminal_tool._workspace_lease_authorities_lock:
        assert terminal_tool._workspace_lease_authorities == {
            "session-a": "root-session",
            "target-session": "foreign-root",
        }


@pytest.mark.parametrize(
    "close_order",
    [
        ("owner-a", "owner-b"),
        ("owner-b", "owner-a"),
    ],
)
def test_close_cleans_exact_authority_only_after_last_owner(
    monkeypatch,
    close_order,
):
    first = _minimal_agent(owner_id="owner-a")
    second = _minimal_agent(owner_id="owner-b")
    for agent in (first, second):
        _claim(agent, "session-a")
        _prepare_for_close(agent)

    cleanup_vm = MagicMock()
    kill_all = MagicMock()
    cleanup_browser = MagicMock()
    monkeypatch.setattr(terminal_tool, "cleanup_vm", cleanup_vm)
    monkeypatch.setattr(
        "tools.process_registry.process_registry.kill_all",
        kill_all,
    )
    monkeypatch.setattr("run_agent.cleanup_browser", cleanup_browser)
    by_owner = {
        "owner-a": first,
        "owner-b": second,
    }

    by_owner[close_order[0]].close()
    cleanup_vm.assert_not_called()
    kill_all.assert_not_called()
    assert terminal_tool._workspace_lease_authorities == {
        "session-a": "root-session"
    }

    by_owner[close_order[1]].close()
    cleanup_vm.assert_called_once_with("root-session")
    kill_all.assert_called_once_with(task_id="root-session")
    assert terminal_tool._workspace_lease_authorities == {}
    assert cleanup_browser.call_count == 2


def test_close_after_session_rotation_kills_only_authority_root(
    monkeypatch,
):
    agent = _minimal_agent(session_id="compression-child")
    _claim(agent, "session-a", "compression-child")
    _prepare_for_close(agent)
    cleanup_vm = MagicMock()
    kill_all = MagicMock()
    monkeypatch.setattr(terminal_tool, "cleanup_vm", cleanup_vm)
    monkeypatch.setattr(
        "tools.process_registry.process_registry.kill_all",
        kill_all,
    )
    monkeypatch.setattr("run_agent.cleanup_browser", MagicMock())

    agent.close()

    kill_all.assert_called_once_with(task_id="root-session")
    cleanup_vm.assert_called_once_with("root-session")


def test_repeated_close_cannot_touch_reclaimed_runtime_identity(
    monkeypatch,
):
    first = _minimal_agent(owner_id="owner-a")
    _claim(first, "session-a")
    _prepare_for_close(first)
    cleanup_vm = MagicMock()
    kill_all = MagicMock()
    cleanup_browser = MagicMock()
    monkeypatch.setattr(terminal_tool, "cleanup_vm", cleanup_vm)
    monkeypatch.setattr(
        "tools.process_registry.process_registry.kill_all",
        kill_all,
    )
    monkeypatch.setattr("run_agent.cleanup_browser", cleanup_browser)

    first.close()
    replacement = _minimal_agent(owner_id="owner-b")
    _claim(replacement, "session-a")
    first.close()

    kill_all.assert_called_once_with(task_id="root-session")
    cleanup_vm.assert_called_once_with("root-session")
    cleanup_browser.assert_called_once_with("session-a")
    assert terminal_tool._workspace_lease_authority_owners[
        "session-a"
    ] == {"owner-b"}


def test_shared_session_helper_close_never_kills_parent_resources(
    monkeypatch,
):
    parent = _minimal_agent(owner_id="parent-owner")
    helper = _minimal_agent(owner_id="helper-owner")
    helper._owns_runtime_resources = False
    for agent in (parent, helper):
        _claim(agent, "session-a")
    _prepare_for_close(helper)

    cleanup_vm = MagicMock()
    kill_all = MagicMock()
    cleanup_browser = MagicMock()
    monkeypatch.setattr(terminal_tool, "cleanup_vm", cleanup_vm)
    monkeypatch.setattr(
        "tools.process_registry.process_registry.kill_all",
        kill_all,
    )
    monkeypatch.setattr("run_agent.cleanup_browser", cleanup_browser)

    helper.close()

    cleanup_vm.assert_not_called()
    kill_all.assert_not_called()
    cleanup_browser.assert_not_called()
    assert terminal_tool._workspace_lease_authority_owners[
        "session-a"
    ] == {"parent-owner"}


def test_non_owning_helper_reaps_authority_when_it_releases_last_claim(
    monkeypatch,
):
    parent = _minimal_agent(owner_id="parent-owner")
    helper = _minimal_agent(owner_id="helper-owner")
    helper._owns_runtime_resources = False
    for agent in (parent, helper):
        _claim(agent, "session-a")
        _prepare_for_close(agent)
    cleanup_vm = MagicMock()
    kill_all = MagicMock()
    cleanup_browser = MagicMock()
    monkeypatch.setattr(terminal_tool, "cleanup_vm", cleanup_vm)
    monkeypatch.setattr(
        "tools.process_registry.process_registry.kill_all",
        kill_all,
    )
    monkeypatch.setattr("run_agent.cleanup_browser", cleanup_browser)

    parent.close()
    cleanup_vm.assert_not_called()
    kill_all.assert_not_called()

    helper.close()
    cleanup_vm.assert_called_once_with("root-session")
    kill_all.assert_called_once_with(task_id="root-session")
    cleanup_browser.assert_called_once_with("session-a")


def test_close_cleans_browser_for_child_turn_task_id(monkeypatch):
    child = _minimal_agent(
        owner_id="child-owner",
        session_id="child-session",
    )
    child._current_task_id = "subagent-task"
    child._track_runtime_resource_task_ids(("subagent-task",))
    _claim(child, "child-session", "subagent-task")
    _prepare_for_close(child)
    monkeypatch.setattr(terminal_tool, "cleanup_vm", MagicMock())
    monkeypatch.setattr(
        "tools.process_registry.process_registry.kill_all",
        MagicMock(),
    )
    cleanup_browser = MagicMock()
    monkeypatch.setattr("run_agent.cleanup_browser", cleanup_browser)

    child.close()

    assert cleanup_browser.call_args_list == [
        ((runtime_id,),)
        for runtime_id in (
            "child-session",
            "subagent-task",
        )
    ]
