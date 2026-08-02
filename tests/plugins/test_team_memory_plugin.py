"""Focused tests for the Stage 1 team-memory plugin.

The tests intentionally exercise the real plugin discovery and registry path,
not only direct calls into the storage module.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from hermes_cli.plugins import PluginManager
from plugins.team_memory import storage


def _write_config(home: Path, *, enabled: bool, plugin_enabled: bool = True) -> Path:
    config = {
        "plugins": {"enabled": ["team-memory"] if plugin_enabled else []},
        "team_memory": {
            "enabled": enabled,
            "workspace_id": "xinxiang",
            "database_path": str(home / "shared" / "xinxiang.db"),
            "agent_variant": "test",
        },
    }
    path = home / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def test_storage_scope_cjk_and_external_fts_update(tmp_path):
    db_path = tmp_path / "shared.db"
    storage.init_database(db_path, workspace_id="xinxiang")
    memory_id = storage.add_memory(
        "api_contract",
        "错误处理 API",
        "前端调用后端 API 时统一返回 request_id 和错误码。",
        "reviewer",
        ["前端", "后端"],
        workspace_id="xinxiang",
        db_path=db_path,
    )

    assert storage.search_memory(
        "错误处理", workspace_id="xinxiang", db_path=db_path
    )[0]["id"] == memory_id
    assert storage.search_memory(
        "API request_id", workspace_id="xinxiang", db_path=db_path
    )[0]["id"] == memory_id
    assert storage.search_memory(
        "错误处理", workspace_id="other", db_path=db_path
    ) == []

    storage.add_memory(
        "api_contract",
        "错误处理 API",
        "已更新为 trace_id。",
        "reviewer",
        ["后端"],
        workspace_id="xinxiang",
        db_path=db_path,
        replace=True,
    )
    assert storage.search_memory(
        "request_id", workspace_id="xinxiang", db_path=db_path
    ) == []
    assert storage.search_memory(
        "trace_id", workspace_id="xinxiang", db_path=db_path
    )[0]["id"] == memory_id
    assert storage.delete_memory(
        memory_id, workspace_id="other", db_path=db_path
    ) is False
    assert storage.delete_memory(
        memory_id, workspace_id="xinxiang", db_path=db_path
    ) is True
    assert storage.search_memory(
        "trace_id", workspace_id="xinxiang", db_path=db_path
    ) == []


def test_expiry_uses_chronological_comparison_and_is_auditable(tmp_path):
    db_path = tmp_path / "shared.db"
    storage.init_database(db_path, workspace_id="xinxiang")
    storage.add_memory(
        "best_practice",
        "Expired guidance",
        "expired-guidance",
        "reviewer",
        workspace_id="xinxiang",
        db_path=db_path,
        valid_until=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        memory_key="expired-guidance",
    )
    storage.add_memory(
        "best_practice",
        "Future guidance",
        "future-guidance",
        "reviewer",
        workspace_id="xinxiang",
        db_path=db_path,
        valid_until="2099-01-01T00:00:00Z",
        memory_key="future-guidance",
    )

    assert storage.search_memory(
        "guidance", workspace_id="xinxiang", db_path=db_path
    )[0]["title"] == "Future guidance"
    assert [row["title"] for row in storage.list_all_memories(
        workspace_id="xinxiang", db_path=db_path
    )] == ["Future guidance"]
    assert {
        row["title"]
        for row in storage.list_all_memories(
            workspace_id="xinxiang", db_path=db_path, include_expired=True
        )
    } == {"Expired guidance", "Future guidance"}


def test_legacy_database_migrates_and_rebuilds_fts(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE shared_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            author TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("ALTER TABLE shared_memory RENAME TO shared_memory_unused")
    conn.execute(
        """
        CREATE TABLE shared_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            tags TEXT,
            metadata TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO shared_memory(category, title, content, tags, metadata, created_by, created_at, updated_at) "
        "VALUES ('api_contract', 'Legacy contract', 'legacy-endpoint', '[]', '{\"internal\": \"hidden\"}', 'seed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE shared_memory_fts USING fts5(
            title, content, tags, content='shared_memory', content_rowid='id'
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER shared_memory_ai AFTER INSERT ON shared_memory BEGIN
            INSERT INTO shared_memory_fts(rowid, title, content, tags)
            VALUES (new.id, new.title, new.content, new.tags);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER shared_memory_ad AFTER DELETE ON shared_memory BEGIN
            DELETE FROM shared_memory_fts WHERE rowid = old.id;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER shared_memory_au AFTER UPDATE ON shared_memory BEGIN
            DELETE FROM shared_memory_fts WHERE rowid = old.id;
            INSERT INTO shared_memory_fts(rowid, title, content, tags)
            VALUES (new.id, new.title, new.content, new.tags);
        END
        """
    )
    conn.execute("INSERT INTO shared_memory_fts(shared_memory_fts) VALUES ('rebuild')")
    conn.execute("DROP TABLE shared_memory_unused")
    conn.commit()
    conn.close()

    storage.init_database(db_path, workspace_id="xinxiang")
    rows = storage.search_memory(
        "legacy-endpoint", workspace_id="xinxiang", db_path=db_path
    )
    assert len(rows) == 1
    assert rows[0]["workspace_id"] == "xinxiang"
    assert rows[0]["memory_key"].startswith("legacy-")
    assert rows[0]["author"] == "seed"
    assert "metadata" not in rows[0]


def test_empty_metrics_database_is_readable(tmp_path):
    metrics_path = tmp_path / "metrics.db"
    metrics_path.touch()
    assert storage.get_query_metrics(
        workspace_id="xinxiang", metrics_path=metrics_path
    ) == []


def test_uninstall_custom_database_removes_its_metrics_sidecar(tmp_path):
    db_path = tmp_path / "custom.db"
    metrics_path = storage.get_metrics_path({}, db_path=db_path)
    storage.init_database(db_path, workspace_id="xinxiang")
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.touch()

    assert storage.uninstall_database(db_path) is True
    assert not db_path.exists()
    assert not metrics_path.exists()


def test_seed_insert_is_idempotent_and_content_is_bounded(tmp_path):
    db_path = tmp_path / "shared.db"
    storage.init_database(db_path, workspace_id="xinxiang")
    kwargs = {
        "workspace_id": "xinxiang",
        "db_path": db_path,
        "memory_key": "api-users-v1",
    }
    first = storage.add_memory(
        "api_contract", "Users", "x" * 1000, "seed", [], **kwargs
    )
    second = storage.add_memory(
        "api_contract", "Users", "different", "seed", [], **kwargs
    )
    assert second == first
    row = storage.search_memory(
        "Users", workspace_id="xinxiang", db_path=db_path, max_content_chars=256
    )[0]
    assert len(row["content"]) == 256
    assert row["content_truncated"] is True
    bounded_rows = storage.search_memory(
        "Users",
        workspace_id="xinxiang",
        db_path=db_path,
        max_content_chars=4_000,
        max_total_chars=1_024,
    )
    assert bounded_rows
    assert len(json.dumps(bounded_rows, ensure_ascii=False)) <= 1_024
    assert bounded_rows[0]["content_truncated"] is True


def test_string_false_feature_flag_is_disabled():
    from plugins.team_memory.tool import is_feature_enabled

    assert is_feature_enabled(
        {"team_memory": {"enabled": "false"}, "features": {"team_memory": True}}
    ) is False
    assert is_feature_enabled({"features": {"team_memory": "off"}}) is False


def test_plugin_discovery_registers_cli_and_gated_tool(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_config(home, enabled=True)

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["team-memory"]
    assert loaded.enabled is True
    assert loaded.error is None
    assert "team_memory_search" in loaded.tools_registered
    assert "team-memory" in manager._cli_commands

    from tools.registry import invalidate_check_fn_cache, registry

    storage.init_database(workspace_id="xinxiang")
    storage.add_memory(
        "best_practice",
        "错误处理规范",
        "统一使用 request_id。",
        "reviewer",
        ["错误处理"],
        workspace_id="xinxiang",
    )
    definitions = registry.get_definitions({"team_memory_search"})
    assert [item["function"]["name"] for item in definitions] == ["team_memory_search"]
    result = json.loads(
        registry.dispatch(
            "team_memory_search",
            {"query": "错误处理"},
            session_id="s1",
            task_id="t1",
        )
    )
    assert result["success"] is True
    assert result["count"] == 1

    _write_config(home, enabled=False)
    invalidate_check_fn_cache()
    assert registry.get_definitions({"team_memory_search"}) == []

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    plugin_parser = subparsers.add_parser("team-memory")
    manager._cli_commands["team-memory"]["setup_fn"](plugin_parser)
    parsed = parser.parse_args(["team-memory", "init", "--workspace", "xinxiang"])
    assert parsed.team_memory_action == "init"
