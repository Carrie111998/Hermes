"""Focused tests for compression auto-handoff refactor."""

from __future__ import annotations

from pathlib import Path
import stat
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from agent.compression_handoff import (
    build_compression_handoff_packet,
    configure_auto_handoff_on_compression,
    maybe_trigger_compression_handoff,
)
from agent.conversation_compression import compress_context
from run_agent import AIAgent


class FakeCompressor:
    def __init__(self, returned: list[dict[str, str]], count: int = 2):
        self.returned = returned
        self.compression_count = max(0, count - 1)
        self.target_count = count
        self._last_compress_aborted = False
        self._last_summary_error = None
        self._last_aux_model_failure_model = None
        self._last_aux_model_failure_error = None
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.session_start_calls: list[dict[str, Any]] = []

    def compress(self, *args, **kwargs):
        self.compression_count = self.target_count
        return [m.copy() for m in self.returned]

    def on_session_start(self, session_id: str, **kwargs):
        self.session_start_calls.append({"session_id": session_id, **kwargs})


class FakeTodos:
    def format_for_injection(self):
        return "[Your active task list was preserved across context compression]\n- [>] work. Keep going"

    def read(self):
        return [{"id": "work", "content": "Keep going", "status": "in_progress"}]


class FakeSessionDB:
    def __init__(self):
        self.archived: list[tuple[str, list[dict[str, Any]]]] = []
        self.handoffs: list[tuple[str, str]] = []
        self.prompts: list[tuple[str, str]] = []

    def get_session_title(self, session_id):
        return "Compression handoff"

    def try_acquire_compression_lock(self, *args, **kwargs):
        return True

    def release_compression_lock(self, *args, **kwargs):
        return None

    def archive_and_compact(self, session_id, compressed):
        self.archived.append((session_id, compressed))

    def update_system_prompt(self, session_id, prompt):
        self.prompts.append((session_id, prompt))

    def request_handoff(self, session_id, platform):
        self.handoffs.append((session_id, platform))
        return True


class FakeMemory:
    def on_session_switch(self, *args, **kwargs):
        return None

    def on_pre_compress(self, *args, **kwargs):
        return None


def make_agent(tmp_path: Path, compressor: FakeCompressor) -> Any:
    agent = cast(Any, object.__new__(AIAgent))
    agent.context_compressor = compressor
    agent.session_id = "session-old"
    agent.model = "test-model"
    agent.provider = "openai-codex"
    agent.platform = "cli"
    agent.tools = []
    agent.log_prefix = ""
    agent.compression_in_place = True
    agent._compression_feasibility_checked = True
    agent._session_db = FakeSessionDB()
    agent._session_db_created = True
    agent._session_init_model_config = {"max_iterations": 90}
    agent._memory_manager = FakeMemory()
    agent._todo_store = FakeTodos()
    agent._cached_system_prompt = None
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._last_compaction_in_place = False
    agent._last_compression_lock_error_sid = None
    agent._last_compression_lock_warning_sid = None
    agent._compression_lock_ttl_seconds = 300
    agent._compression_lock_refresh_interval = None
    agent._auto_handoff_on_compression_enabled = False
    agent._auto_handoff_after_compressions = 2
    agent._auto_handoff_max_auto_handoffs = 1
    agent._auto_handoff_mode = "packet"
    agent._auto_handoff_platform = ""
    agent._auto_handoff_artifact_dir = "handoffs"
    agent._auto_handoff_count = 0
    agent._invalidate_system_prompt = lambda *a, **kw: None
    agent._build_system_prompt = lambda *a, **kw: "new-system-prompt"
    agent._emit_status = lambda message: None
    agent._emit_warning = lambda message: None
    agent.commit_memory_session = lambda *a, **kw: None
    return agent


def test_configure_auto_handoff_aliases_legacy_modes():
    agent = SimpleNamespace()
    configure_auto_handoff_on_compression(
        agent,
        {
            "auto_handoff_on_compression": {
                "enabled": "yes",
                "after_compressions": "3",
                "max_auto_handoffs": "2",
                "mode": "fresh_session",
                "platform": "discord",
                "handoff_artifact_dir": ".hermes/handoffs",
            }
        },
    )

    assert agent._auto_handoff_on_compression_enabled is True
    assert agent._auto_handoff_after_compressions == 3
    assert agent._auto_handoff_max_auto_handoffs == 2
    # Old fresh-session rotation is intentionally not replayed; current refactor
    # falls back to packet/platform handoff primitives.
    assert agent._auto_handoff_mode == "packet"
    assert agent._auto_handoff_platform == "discord"
    assert agent._auto_handoff_artifact_dir == ".hermes/handoffs"
    assert agent._auto_handoff_count == 0


def test_aiagent_initializes_auto_handoff_config_from_yaml():
    cfg = {
        "agent": {
            "auto_handoff_on_compression": {
                "enabled": True,
                "after_compressions": "3",
                "max_auto_handoffs": "2",
                "mode": "platform",
                "platform": "discord",
                "handoff_artifact_dir": "custom-handoffs",
            }
        }
    }
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("hermes_cli.config.load_config", return_value=cfg),
    ):
        agent = cast(
            Any,
            AIAgent(
                api_key="dummy",
                base_url="https://example.test/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            ),
        )

    assert agent._auto_handoff_on_compression_enabled is True
    assert agent._auto_handoff_after_compressions == 3
    assert agent._auto_handoff_max_auto_handoffs == 2
    assert agent._auto_handoff_mode == "platform"
    assert agent._auto_handoff_platform == "discord"
    assert agent._auto_handoff_artifact_dir == "custom-handoffs"


def test_maybe_trigger_writes_packet_and_queues_current_session_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    compressor = FakeCompressor([{"role": "user", "content": "compressed summary"}], count=2)
    compressor.compression_count = 2
    agent = make_agent(tmp_path, compressor)
    agent._auto_handoff_on_compression_enabled = True
    agent._auto_handoff_mode = "platform"
    agent._auto_handoff_platform = "discord"

    path = maybe_trigger_compression_handoff(
        agent,
        [{"role": "user", "content": "compressed summary"}],
        approx_tokens=123_456,
    )

    assert path is not None and path.exists()
    packet = path.read_text(encoding="utf-8")
    assert "# Hermes compression handoff packet" in packet
    assert "compressed summary" in packet
    assert "Keep going" in packet
    assert agent._session_db.handoffs == [("session-old", "discord")]
    assert agent._auto_handoff_count == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_maybe_trigger_is_bounded_by_max_auto_handoffs(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    compressor = FakeCompressor([{"role": "user", "content": "compressed summary"}], count=3)
    agent = make_agent(tmp_path, compressor)
    agent._auto_handoff_on_compression_enabled = True
    agent._auto_handoff_count = 1
    agent._auto_handoff_max_auto_handoffs = 1

    path = maybe_trigger_compression_handoff(agent, [{"role": "user", "content": "summary"}])

    assert path is None
    assert not (tmp_path / "handoffs").exists()
    assert agent._session_db.handoffs == []


def test_packet_redacts_secret_like_content(tmp_path):
    compressor = FakeCompressor([{"role": "user", "content": "unused"}], count=2)
    agent = make_agent(tmp_path, compressor)

    packet = build_compression_handoff_packet(
        agent,
        [{"role": "user", "content": "Use OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456"}],
    )

    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in packet
    assert "OPENAI_API_KEY=" in packet


def test_compress_context_triggers_packet_without_session_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    returned = [
        {"role": "user", "content": "compressed summary"},
        {"role": "assistant", "content": "tail"},
    ]
    compressor = FakeCompressor(returned, count=2)
    agent = make_agent(tmp_path, compressor)
    agent._auto_handoff_on_compression_enabled = True
    agent._auto_handoff_mode = "packet"

    messages, prompt = compress_context(
        agent,
        [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
            {"role": "assistant", "content": "four"},
        ],
        "",
        approx_tokens=250_000,
    )

    assert prompt == "new-system-prompt"
    assert agent.session_id == "session-old"
    assert agent._last_compaction_in_place is True
    assert agent._session_db.archived[-1][0] == "session-old"
    assert messages[:2] == returned
    packet_paths = list((tmp_path / "handoffs").glob("*.md"))
    assert len(packet_paths) == 1
    assert "compressed summary" in packet_paths[0].read_text(encoding="utf-8")
