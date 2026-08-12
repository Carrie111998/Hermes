"""Behavior tests for the built-in memory → external provider bridge.

The bridge lives behind the MemoryManager interface
(``MemoryManager.notify_memory_tool_write``): the agent loop hands over the raw
built-in memory tool result + args, and the manager decides whether/what to
mirror to external providers. These tests drive that method with a fake
external provider and assert which ``on_memory_write`` calls land.
"""

import json

import pytest

from agent.background_review import build_memory_write_metadata
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class _RecordingProvider(MemoryProvider):
    """Minimal external provider that records on_memory_write calls."""

    def __init__(self) -> None:
        self.calls = []

    @property
    def name(self) -> str:
        return "recording"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def get_tool_schemas(self):
        return []

    def shutdown(self) -> None:
        pass

    def on_memory_write(self, action, target, content, metadata=None):
        self.calls.append({
            "action": action,
            "target": target,
            "content": content,
            "metadata": dict(metadata or {}),
        })


def _manager_with_provider():
    mgr = MemoryManager()
    provider = _RecordingProvider()
    mgr.add_provider(provider)
    return mgr, provider


def test_notifies_remove_with_old_text_after_success():
    mgr, provider = _manager_with_provider()
    mgr.notify_memory_tool_write(
        json.dumps({"success": True}),
        {"action": "remove", "target": "memory", "old_text": "stale preference entry"},
    )
    assert provider.calls == [
        {
            "action": "remove",
            "target": "memory",
            "content": "",
            "metadata": {"old_text": "stale preference entry"},
        }
    ]






@pytest.mark.parametrize("tool_result", [None, [], object(), "not-json"])
def test_skips_unrecognized_tool_result_shape(tool_result):
    mgr, provider = _manager_with_provider()
    mgr.notify_memory_tool_write(
        tool_result,
        {"action": "add", "target": "memory", "content": "new fact"},
    )
    assert provider.calls == []






def test_build_metadata_callback_is_merged_per_op():
    mgr, provider = _manager_with_provider()
    mgr.notify_memory_tool_write(
        json.dumps({"success": True}),
        {"action": "add", "target": "memory", "content": "fact"},
        build_metadata=lambda: {"session_id": "s1", "tool_name": "memory"},
    )
    assert provider.calls == [
        {
            "action": "add",
            "target": "memory",
            "content": "fact",
            "metadata": {"session_id": "s1", "tool_name": "memory"},
        }
    ]


def test_build_metadata_carries_host_owned_explicit_user_intent():
    class Agent:
        _memory_write_origin = "assistant_tool"
        _memory_write_context = "foreground"
        _memory_user_intent = "explicit_remember"
        _memory_user_turn_synthetic = False
        session_id = "s1"
        _parent_session_id = ""
        platform = "desktop"

    metadata = build_memory_write_metadata(Agent(), task_id="t1", tool_call_id="tc1")

    assert metadata["write_origin"] == "assistant_tool"
    assert metadata["user_memory_intent"] == "explicit_remember"
    assert metadata["host_confirmed_user_memory"] is True


def test_build_metadata_fails_closed_for_background_review():
    class Agent:
        _memory_write_origin = "background_review"
        _memory_write_context = "background_review"
        _memory_user_intent = "explicit_remember"
        _memory_user_turn_synthetic = False
        session_id = "s1"
        _parent_session_id = ""
        platform = "desktop"

    metadata = build_memory_write_metadata(Agent())

    assert metadata["write_origin"] == "background_review"
    assert metadata["host_confirmed_user_memory"] is False


def test_actual_memory_manager_bridge_preserves_host_provenance_for_memory_targets(tmp_path):
    from plugins.memory.obsidian_duo import ObsidianDuoMemoryProvider
    from plugins.memory.obsidian_duo.config import ObsidianDuoConfig

    home = tmp_path / "home"
    ObsidianDuoConfig(vault_path=str(tmp_path / "vault"), inference_mode="disabled").save(home)
    provider = ObsidianDuoMemoryProvider()
    provider.initialize("session-1", hermes_home=str(home))
    manager = MemoryManager()
    manager.add_provider(provider)

    trusted_metadata = {
        "session_id": "s1",
        "task_id": "t1",
        "write_origin": "assistant_tool",
        "execution_context": "foreground",
        "user_memory_intent": "explicit_remember",
        "host_confirmed_user_memory": True,
    }
    manager.notify_memory_tool_write(
        json.dumps({"success": True}),
        {"action": "add", "target": "memory", "content": "host fact"},
        build_metadata=lambda: trusted_metadata,
    )
    manager.notify_memory_tool_write(
        json.dumps({"success": True}),
        {"action": "add", "target": "user", "content": "host preference"},
        build_metadata=lambda: trusted_metadata,
    )

    rows = provider._broker._broker.store.connection().execute(
        "SELECT content, memory_type, authority, verification, source_session_id, task_id FROM memories ORDER BY rowid"
    ).fetchall()
    assert [(row[0], row[1], row[2], row[3]) for row in rows] == [
        ("host fact", "fact", "user", "user_confirmed"),
        ("host preference", "preference", "user", "user_confirmed"),
    ]
    assert all(row[4] == "s1" and row[5] == "t1" for row in rows)
    assert len(list((tmp_path / "vault" / "Hermes Memory").rglob("*.md"))) == 2
    provider.shutdown()


def test_model_memory_args_cannot_supply_host_provenance(tmp_path):
    from plugins.memory.obsidian_duo import ObsidianDuoMemoryProvider
    from plugins.memory.obsidian_duo.config import ObsidianDuoConfig

    home = tmp_path / "home"
    ObsidianDuoConfig(vault_path=str(tmp_path / "vault"), inference_mode="disabled").save(home)
    provider = ObsidianDuoMemoryProvider()
    provider.initialize("session-1", hermes_home=str(home))
    manager = MemoryManager()
    manager.add_provider(provider)

    manager.notify_memory_tool_write(
        json.dumps({"success": True}),
        {
            "action": "add",
            "target": "memory",
            "content": "model spoof attempt",
            "write_origin": "user",
            "user_memory_intent": "explicit_remember",
            "host_confirmed_user_memory": True,
        },
    )

    connection = provider._broker._broker.store.connection()
    assert connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    candidate = connection.execute("SELECT payload FROM candidates").fetchone()
    assert candidate is not None
    assert '"authority": "agent"' in candidate[0]
    assert '"verification": "unverified"' in candidate[0]
    provider.shutdown()
