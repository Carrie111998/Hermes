import importlib.util
import os
import sys

PLUGIN_INIT = os.path.join(os.path.dirname(__file__), "..", "__init__.py")
_spec = importlib.util.spec_from_file_location("subagent_handles_plugin", PLUGIN_INIT)
plugin = importlib.util.module_from_spec(_spec)
sys.modules["subagent_handles_plugin"] = plugin
_spec.loader.exec_module(plugin)

from subagent_handles import sender
from subagent_handles.sender import SCHEMA, handle_cancel_subagent, handle_subagent_send
from subagent_handles.registry import registry as shared_registry


def _install_registry():
    """Reset the shared singleton to a clean state for test isolation.

    Uses the SAME module-level singleton that the hook handlers and the
    sender tool both reference — production wiring, not a throwaway
    instance. This exercises the real hook→tool data path.
    """
    for h in list(shared_registry):
        shared_registry.remove(h.subagent_id)
    return shared_registry


def test_subagent_send_missing_handle():
    _install_registry()
    assert handle_subagent_send({"subagent_id": "missing", "text": "hi"}) == {
        "ok": False,
        "error": "subagent_id='missing' is not running or not found",
        "subagent_id": "missing",
    }


def test_subagent_send_running_ok():
    registry = _install_registry()
    registry.register(
        type("H", (), {"subagent_id": "a1", "session_id": "s1", "goal": "g1", "state": "running", "parent_subagent_id": None, "role": ""})()
    )
    out = handle_subagent_send({"subagent_id": "a1", "text": "hello"})
    assert out["ok"] is True
    assert out["queued"] is True
    assert out["subagent_send"]["state"] == "running"


def test_subagent_send_done_rejected():
    registry = _install_registry()
    registry.register(
        type("H", (), {"subagent_id": "a1", "session_id": "s1", "goal": "g1", "state": "done", "parent_subagent_id": None, "role": ""})()
    )
    out = handle_subagent_send({"subagent_id": "a1", "text": "hello"})
    assert out["ok"] is False


def test_subagent_send_missing_text():
    _install_registry()
    assert handle_subagent_send({"subagent_id": "a1", "text": ""}) == {
        "ok": False,
        "error": "text is required",
    }


def test_cancel_subagent_missing_handle():
    _install_registry()
    out = handle_cancel_subagent({"subagent_id": "missing"})
    assert out["ok"] is False


def test_cancel_subagent_running():
    registry = _install_registry()
    registry.register(
        type("H", (), {"subagent_id": "a1", "session_id": "s1", "goal": "g1", "state": "running", "parent_subagent_id": None, "role": ""})()
    )
    out = handle_cancel_subagent({"subagent_id": "a1"})
    assert out == {"ok": True, "subagent_id": "a1", "state": "cancelled", "session_id": "s1"}


def test_cancel_already_done():
    registry = _install_registry()
    registry.register(
        type("H", (), {"subagent_id": "a1", "session_id": "s1", "goal": "g1", "state": "done", "parent_subagent_id": None, "role": ""})()
    )
    out = handle_cancel_subagent({"subagent_id": "a1"})
    assert out["ok"] is False


def test_schema_keys():
    assert set(SCHEMA.keys()) == {"subagent_send", "cancel_subagent"}
    assert SCHEMA["subagent_send"]["name"] == "subagent_send"
    assert SCHEMA["cancel_subagent"]["name"] == "cancel_subagent"


# --- Integration: hooks and tools share the SAME registry instance ---
# Regression test for the separate-registry bug: sender.py used to create
# its own SubagentRegistry(), so a handle registered by the subagent_start
# hook could never be resolved by subagent_send/cancel_subagent. Both now
# import the module-level singleton from subagent_handles.registry.

def test_hook_registered_handle_is_resolvable_by_sender():
    from subagent_handles.registry import registry as shared
    from subagent_handles_plugin import _on_subagent_start, _on_subagent_stop

    # Clean slate
    for h in list(shared):
        shared.remove(h.subagent_id)

    # Simulate a real subagent_start hook firing with the delegate_tool.py kwargs
    _on_subagent_start(
        child_subagent_id="sa-integration-1",
        child_session_id="sess-integration-1",
        child_goal="integration goal",
        child_role="coder",
        parent_subagent_id="parent-1",
    )
    assert "sa-integration-1" in shared

    # The sender tool must be able to resolve and steer that same handle
    out = handle_subagent_send({"subagent_id": "sa-integration-1", "text": "steer"})
    assert out["ok"] is True
    assert out["subagent_send"]["state"] == "running"

    # Cancel must find it too
    out2 = handle_cancel_subagent({"subagent_id": "sa-integration-1"})
    assert out2["ok"] is True
    assert out2["state"] == "cancelled"

    # And the stop hook must see the same object (state transitions persist)
    _on_subagent_stop(child_session_id="sess-integration-1")
    assert shared.resolve("sa-integration-1").state == "done"

    # Cleanup
    shared.remove("sa-integration-1")


def test_singleton_identity():
    """The hook module and the sender module must reference the same object."""
    from subagent_handles.registry import registry as shared_registry
    import subagent_handles_plugin
    from subagent_handles import sender

    assert subagent_handles_plugin.registry is shared_registry
    assert sender.registry is shared_registry
    assert sender.registry is subagent_handles_plugin.registry
