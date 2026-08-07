"""Tests for memory target=posture (POSTURE.md rotating bets store)."""

from __future__ import annotations

import json

import pytest

from hermes_cli.config import load_config, resolve_memory_target_flags
from hermes_cli.config_defaults import DEFAULT_CONFIG
from tools.memory_tool import MEMORY_BLOCK_HEADERS, MEMORY_SCHEMA, MemoryStore, memory_tool


@pytest.fixture
def mem_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


class TestPostureSchema:
    def test_target_includes_posture(self):
        enum = MEMORY_SCHEMA["parameters"]["properties"]["target"]["enum"]
        assert "posture" in enum
        assert "posture" in MEMORY_SCHEMA["description"].lower()
        assert "agents.md" in MEMORY_SCHEMA["description"].lower()

    def test_header_defined(self):
        assert "posture" in MEMORY_BLOCK_HEADERS

    def test_default_config_preserves_derived_enablement(self):
        memory = DEFAULT_CONFIG["memory"]
        assert "posture_enabled" not in memory
        assert memory["posture_char_limit"] == 1800

    def test_loaded_disabled_stores_leave_posture_unspecified(self, mem_home):
        (mem_home / "config.yaml").write_text(
            "memory:\n"
            "  memory_enabled: false\n"
            "  user_profile_enabled: false\n",
            encoding="utf-8",
        )

        memory = load_config()["memory"]

        assert "posture_enabled" not in memory
        assert resolve_memory_target_flags(memory) == (False, False, False)

    @pytest.mark.parametrize(
        "config, expected",
        [
            (
                {"memory_enabled": True, "user_profile_enabled": False},
                (True, False, True),
            ),
            (
                {
                    "memory_enabled": True,
                    "user_profile_enabled": True,
                    "posture_enabled": False,
                },
                (True, True, False),
            ),
            (
                {
                    "memory_enabled": False,
                    "user_profile_enabled": False,
                    "posture_enabled": True,
                },
                (False, False, True),
            ),
            (
                {
                    "memory_enabled": False,
                    "user_profile_enabled": False,
                    "posture_enabled": False,
                },
                (False, False, False),
            ),
        ],
    )
    def test_effective_enablement(self, config, expected):
        assert resolve_memory_target_flags(config) == expected


class TestPostureStore:
    def test_add_replace_remove_roundtrip(self, mem_home):
        store = MemoryStore(posture_char_limit=500)
        store.load_from_disk()
        r = store.add("posture", "Primary bet: upstream hermes-agent contributor program.")
        assert r["success"] is True
        path = mem_home / "memories" / "POSTURE.md"
        assert path.exists()
        assert "contributor program" in path.read_text(encoding="utf-8")

        r2 = store.replace(
            "posture",
            "Primary bet",
            "Primary bet: Desktop stability + remote phone parity.",
        )
        assert r2["success"] is True
        assert "Desktop stability" in path.read_text(encoding="utf-8")

        r3 = store.remove("posture", "Desktop stability")
        assert r3["success"] is True

    def test_over_budget_refuses(self, mem_home):
        store = MemoryStore(posture_char_limit=80)
        store.load_from_disk()
        r = store.add("posture", "x" * 200)
        assert r["success"] is False

    def test_system_prompt_snapshot_frozen(self, mem_home):
        store = MemoryStore()
        store.load_from_disk()
        store.add("posture", "Routing: Rocky control plane.")
        # Mid-session add does not change frozen snapshot until reload
        frozen = store.format_for_system_prompt("posture")
        assert frozen is None or "Routing" not in (frozen or "")
        store.load_from_disk()
        block = store.format_for_system_prompt("posture")
        assert block is not None
        assert "CURRENT POSTURE" in block
        assert "Routing: Rocky" in block


class TestPostureToolDispatch:
    def test_tool_json(self, mem_home):
        store = MemoryStore()
        store.load_from_disk()
        raw = memory_tool(
            action="add",
            target="posture",
            content="Parked: agents.brill as primary bet.",
            store=store,
        )
        data = json.loads(raw)
        assert data["success"] is True
        assert (mem_home / "memories" / "POSTURE.md").exists()

    def test_invalid_still_rejected(self, mem_home):
        store = MemoryStore()
        raw = memory_tool(action="add", target="vault", content="nope", store=store)
        data = json.loads(raw)
        assert data["success"] is False
