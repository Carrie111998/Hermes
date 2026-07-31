import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key
from plugins.memory.holographic import HolographicMemoryProvider, _render_facts_table


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_source():
    from gateway.config import Platform
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_make_source(),
        message_id="m1",
        internal=True,
    )


def _make_runner():
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner._session_key_for_source = lambda source: "agent:main:test:dm:1"
    return runner


@pytest.fixture
def mock_holographic_provider():
    with patch("plugins.memory.load_memory_provider") as mock_load, \
         patch("plugins.memory._get_active_memory_provider", return_value="holographic"):
        mock_provider = MagicMock()
        mock_provider.handle_tool_call.return_value = "OK"
        mock_load.return_value = mock_provider
        yield mock_provider


# ── TestFactStoreListTable ───────────────────────────────────────────────

class TestFactStoreListTable:
    """Test the fact_store list action with table output format."""

    @pytest.fixture
    def provider(self, tmp_path):
        """Create a test provider with an in-memory database."""
        import os

        config = {
            "db_path": str(tmp_path / "test_memory.db"),
            "char_limit": 2200,
        }
        p = HolographicMemoryProvider(config=config)
        p.initialize("test-session")
        yield p
        # Cleanup
        if os.path.exists(config["db_path"]):
            os.unlink(config["db_path"])

    def test_list_json_format(self, provider):
        """Test default JSON format returns structured data."""
        provider._handle_fact_store(
            {"action": "add", "content": "test fact 1", "category": "general"}
        )
        provider._handle_fact_store(
            {"action": "add", "content": "test fact 2", "category": "tool"}
        )

        result = provider._handle_fact_store(
            {"action": "list", "output_format": "json", "limit": 10}
        )
        data = json.loads(result)

        assert "facts" in data
        assert "count" in data
        assert data["count"] == 2
        assert len(data["facts"]) == 2

    def test_list_table_format(self, provider):
        """Test table format returns Rich-rendered table."""
        provider._handle_fact_store(
            {"action": "add", "content": "test fact 1", "category": "general"}
        )
        provider._handle_fact_store(
            {"action": "add", "content": "test fact 2", "category": "tool", "tags": "tag1,tag2"}
        )

        result = provider._handle_fact_store(
            {"action": "list", "output_format": "table", "limit": 10}
        )

        # Table should contain headers and data
        assert "ID" in result
        assert "Trust" in result
        assert "Category" in result
        assert "Tags" in result
        assert "Created" in result
        assert "Content" in result
        assert "test fact 1" in result
        assert "test fact 2" in result
        assert "tag1" in result

    def test_list_empty(self, provider):
        """Test list on empty store renders empty table."""
        result = provider._handle_fact_store(
            {"action": "list", "output_format": "table", "limit": 10}
        )
        # Empty store returns a simple message, not a table
        assert "No facts" in result

    def test_list_with_category_filter(self, provider):
        """Test list with category filter."""
        provider._handle_fact_store(
            {"action": "add", "content": "general fact", "category": "general"}
        )
        provider._handle_fact_store(
            {"action": "add", "content": "tool fact", "category": "tool"}
        )

        result = provider._handle_fact_store(
            {"action": "list", "output_format": "table", "category": "tool", "limit": 10}
        )
        assert "tool fact" in result
        assert "general fact" not in result

    def test_list_with_min_trust_filter(self, provider):
        """Test list with min_trust filter."""
        provider._handle_fact_store(
            {"action": "add", "content": "high trust", "category": "general"}
        )
        provider._handle_fact_store(
            {"action": "add", "content": "low trust", "category": "general"}
        )

        import sqlite3
        conn = sqlite3.connect(provider._store.db_path)
        cur = conn.cursor()
        cur.execute("UPDATE facts SET trust_score = 0.9 WHERE fact_id = 1")
        cur.execute("UPDATE facts SET trust_score = 0.3 WHERE fact_id = 2")
        conn.commit()
        conn.close()

        result = provider._handle_fact_store(
            {"action": "list", "output_format": "table", "min_trust": 0.5, "limit": 10}
        )
        assert "high trust" in result
        assert "low trust" not in result

    def test_table_trust_color_coding(self, provider):
        """Test trust score color coding in table."""
        provider._handle_fact_store(
            {"action": "add", "content": "high", "category": "general"}
        )
        provider._handle_fact_store(
            {"action": "add", "content": "med", "category": "general"}
        )
        provider._handle_fact_store(
            {"action": "add", "content": "low", "category": "general"}
        )

        import sqlite3
        conn = sqlite3.connect(provider._store.db_path)
        cur = conn.cursor()
        cur.execute("UPDATE facts SET trust_score = 0.8 WHERE fact_id = 1")
        cur.execute("UPDATE facts SET trust_score = 0.5 WHERE fact_id = 2")
        cur.execute("UPDATE facts SET trust_score = 0.2 WHERE fact_id = 3")
        conn.commit()
        conn.close()

        result = provider._handle_fact_store(
            {"action": "list", "output_format": "table", "limit": 10}
        )

        # Content check: trust scores rendered (color codes absent when non-TTY)
        assert "0.80" in result or "0.8" in result
        assert "0.50" in result or "0.5" in result
        assert "0.20" in result or "0.2" in result


# ── TestRenderFactsTable ─────────────────────────────────────────────────

class TestRenderFactsTable:
    """Test the _render_facts_table helper directly."""

    def test_render_empty_list(self):
        """Test rendering empty list."""
        result = _render_facts_table([])
        assert "No facts" in result

    def test_render_single_fact(self):
        """Test rendering single fact."""
        facts = [
            {
                "fact_id": 1,
                "trust_score": 0.75,
                "category": "general",
                "tags": "tag1,tag2",
                "created_at": "2026-01-15 10:30:00",
                "content": "Test content here",
            }
        ]
        result = _render_facts_table(facts)
        assert "1" in result
        assert "0.75" in result
        assert "general" in result
        assert "tag1" in result
        assert "Test content" in result

    def test_render_long_content_truncated(self):
        """Test long content is truncated in table."""
        long_content = "x" * 150
        facts = [
            {
                "fact_id": 1,
                "trust_score": 0.5,
                "category": "general",
                "tags": "",
                "created_at": "2026-01-15 10:30:00",
                "content": long_content,
            }
        ]
        result = _render_facts_table(facts)
        # The rendered content column value should not contain the full 150-char string
        lines = result.split("\n")
        content_line = [l for l in lines if "x" * 80 in l]
        # Content is truncated — the full 150-char string should not appear
        assert long_content not in result


# ── TestHolographicTreeScript ────────────────────────────────────────────

class TestHolographicTreeScript:
    """Test the holographic_tree.py script functionality."""

    def _import(self, name):
        """Import from the holographic_tree script."""
        import importlib.util
        from pathlib import Path
        repo_root = Path(__file__).parent.parent.parent
        script_path = repo_root / "plugins" / "memory" / "holographic" / "scripts" / "holographic_tree.py"
        spec = importlib.util.spec_from_file_location(
            "holographic_tree",
            str(script_path),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, name)

    def test_build_tree_structure(self):
        """Test tree data building from facts."""
        build_tree = self._import("build_tree")

        facts = [
            {
                "fact_id": 1,
                "category": "general",
                "tags": "entity:tax",
                "content": "tax fact",
                "created_at": "2026-07-30 10:00:00",
                "trust_score": 0.5,
            },
            {
                "fact_id": 2,
                "category": "general",
                "tags": "entity:tax",
                "content": "tax fact 2",
                "created_at": "2026-07-29 10:00:00",
                "trust_score": 0.6,
            },
            {
                "fact_id": 3,
                "category": "project",
                "tags": "entity:ptg",
                "content": "ptg fact",
                "created_at": "2026-07-30 09:00:00",
                "trust_score": 0.55,
            },
        ]

        tree = build_tree(facts)

        assert "general" in tree
        assert "project" in tree
        assert "tax" in tree["general"]
        assert "ptg" in tree["project"]
        assert len(tree["general"]["tax"]) == 2
        assert len(tree["project"]["ptg"]) == 1

    def test_get_time_bucket(self):
        """Test time bucket categorization."""
        get_time_bucket = self._import("get_time_bucket")

        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d %H:%M:%S")
        week_ago_str = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        month_ago_str = (now - timedelta(days=15)).strftime("%Y-%m-%d %H:%M:%S")
        old_str = (now - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")

        assert get_time_bucket(today_str, now=now) == "Today"
        assert get_time_bucket(week_ago_str, now=now) == "This Week"
        assert get_time_bucket(month_ago_str, now=now) == "This Month"
        assert get_time_bucket(old_str, now=now) == "Older"


# ── TestSlashCommandWiring ──────────────────────────────────────────────

class TestMemSlashCommandWiring:
    """Test that /holographic-memory command is registered and wired correctly."""

    def test_mem_command_registered(self):
        """Test /holographic-memory is in COMMAND_REGISTRY."""
        from hermes_cli.commands import COMMAND_REGISTRY

        mem_cmd = next((c for c in COMMAND_REGISTRY if c.name == "holographic-memory"), None)
        assert mem_cmd is not None
        assert mem_cmd.description == (
            "Inspect holographic memory (tree / list / probe / search)"
        )
        assert mem_cmd.subcommands == ("tree", "list", "probe", "search")

    def test_handle_mem_command_exists(self):
        """Test _handle_mem_command is defined in the mixin."""
        from gateway.slash_commands import GatewaySlashCommandsMixin

        assert hasattr(GatewaySlashCommandsMixin, "_handle_mem_command")
        assert callable(getattr(GatewaySlashCommandsMixin, "_handle_mem_command"))

    @pytest.mark.asyncio
    async def test_mem_list_dispatch(self, mock_holographic_provider):
        """Test /holographic-memory list dispatches to fact_store list with correct args."""
        mock_holographic_provider.handle_tool_call.return_value = "TABLE_OUTPUT"

        runner = _make_runner()
        event = _make_event("/holographic-memory list --limit 5 --format table")

        from gateway.slash_commands import GatewaySlashCommandsMixin
        runner._handle_mem_command = GatewaySlashCommandsMixin._handle_mem_command.__get__(runner)

        res = await runner._handle_mem_command(event)

        assert res == "TABLE_OUTPUT"
        mock_holographic_provider.handle_tool_call.assert_called_once_with("fact_store", {
            "action": "list",
            "output_format": "table",
            "limit": 5,
        })

    @pytest.mark.asyncio
    async def test_mem_probe_dispatch(self, mock_holographic_provider):
        """Test /holographic-memory probe dispatches to fact_store probe."""
        mock_holographic_provider.handle_tool_call.return_value = '{"facts": []}'

        runner = _make_runner()
        event = _make_event("/holographic-memory probe tax")

        from gateway.slash_commands import GatewaySlashCommandsMixin
        runner._handle_mem_command = GatewaySlashCommandsMixin._handle_mem_command.__get__(runner)

        await runner._handle_mem_command(event)

        mock_holographic_provider.handle_tool_call.assert_called_once_with("fact_store", {
            "action": "probe",
            "entity": "tax",
        })

    @pytest.mark.asyncio
    async def test_mem_search_dispatch(self, mock_holographic_provider):
        """Test /holographic-memory search dispatches to fact_store search."""
        mock_holographic_provider.handle_tool_call.return_value = '{"facts": []}'

        runner = _make_runner()
        event = _make_event("/holographic-memory search penalty")

        from gateway.slash_commands import GatewaySlashCommandsMixin
        runner._handle_mem_command = GatewaySlashCommandsMixin._handle_mem_command.__get__(runner)

        await runner._handle_mem_command(event)

        mock_holographic_provider.handle_tool_call.assert_called_once_with("fact_store", {
            "action": "search",
            "query": "penalty",
        })

    @pytest.mark.asyncio
    async def test_mem_tree_runs_subprocess(self, mock_holographic_provider):
        """Test /holographic-memory tree launches holographic_tree.py via subprocess."""
        runner = _make_runner()
        event = _make_event("/holographic-memory tree")

        from gateway.slash_commands import GatewaySlashCommandsMixin
        runner._handle_mem_command = GatewaySlashCommandsMixin._handle_mem_command.__get__(runner)

        mock_proc = MagicMock()
        mock_proc.stdout = "TREE_OUTPUT"
        mock_proc.stderr = ""

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            with patch("plugins.memory.holographic._load_plugin_config", return_value={}):
                res = await runner._handle_mem_command(event)

        assert res == "TREE_OUTPUT"
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert cmd_args[-1].endswith("holographic_tree.py")