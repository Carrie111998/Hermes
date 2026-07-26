from __future__ import annotations

import importlib.util
import sqlite3
import uuid
from pathlib import Path

import pytest

from hermes_cli.routing import facade


PLUGIN = (
    Path.home()
    / ".hermes"
    / "profiles"
    / "atlas"
    / "plugins"
    / "task-model-router"
    / "__init__.py"
)


@pytest.fixture
def plugin_env(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv(
        "HERMES_DOCTRINE_V1_PATH",
        str(tmp_path / "doctrine_v1.json"),
    )
    facade._READERS.clear()
    return db_path, tmp_path


def _load_plugin():
    name = f"atlas_task_model_router_cs05_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _disable_worker(module, tmp_path):
    module.PRIVATE_QUERY_LAUNCHER = tmp_path / "not-installed"


def _rows(db_path):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM routing_decisions ORDER BY id"
            )
        ]
    finally:
        connection.close()


def test_plugin_still_raises_when_use_doctrine_false_and_provider_missing(
    plugin_env,
):
    _db_path, tmp_path = plugin_env
    module = _load_plugin()
    _disable_worker(module, tmp_path)
    result = module._handle_task_model_route(
        {
            "route": "single",
            "prompt": "work",
            "use_doctrine_reader": False,
        }
    )
    assert "provider and model are required" in result


def test_plugin_uses_doctrine_when_use_doctrine_true_and_provider_missing(
    plugin_env,
):
    db_path, tmp_path = plugin_env
    module = _load_plugin()
    _disable_worker(module, tmp_path)
    module._handle_task_model_route(
        {
            "route": "single",
            "prompt": "work",
            "lane": "green_captains",
            "rung": "execute",
            "complexity": "standard",
            "use_doctrine_reader": True,
        }
    )
    row = _rows(db_path)[0]
    assert (row["chosen_provider"], row["chosen_model"]) == (
        "openai-codex",
        "gpt-5-6-sol",
    )
    assert row["used_doctrine_reader"] == 1
    assert row["overridden_by_caller"] == 0


def test_plugin_prefers_caller_when_both_supplied_with_use_doctrine_true(
    plugin_env,
):
    db_path, tmp_path = plugin_env
    module = _load_plugin()
    _disable_worker(module, tmp_path)
    module._handle_task_model_route(
        {
            "route": "single",
            "prompt": "work",
            "provider": "caller",
            "model": "caller-model",
            "use_doctrine_reader": True,
        }
    )
    row = _rows(db_path)[0]
    assert (row["chosen_provider"], row["chosen_model"]) == (
        "caller",
        "caller-model",
    )
    assert row["overridden_by_caller"] == 1
    assert row["doctrine_suggested_model"] == "gpt-5-6-sol"


def test_plugin_writes_routing_decisions_row_on_every_invocation(plugin_env):
    db_path, tmp_path = plugin_env
    module = _load_plugin()
    _disable_worker(module, tmp_path)
    module._handle_task_model_route(
        {
            "route": "single",
            "prompt": "one",
            "provider": "p",
            "model": "m",
        }
    )
    module._handle_task_model_route(
        {
            "route": "single",
            "prompt": "two",
            "use_doctrine_reader": True,
        }
    )
    module._handle_task_model_route(
        {"route": "openrouter-auto", "prompt": "three"}
    )
    assert len(_rows(db_path)) == 3


def test_plugin_preserves_openrouter_auto_and_fusion_routes_without_doctrine(
    plugin_env,
):
    db_path, tmp_path = plugin_env
    module = _load_plugin()
    _disable_worker(module, tmp_path)
    module._handle_task_model_route(
        {"route": "openrouter-auto", "prompt": "auto"}
    )
    module._handle_task_model_route(
        {"route": "openrouter-fusion", "prompt": "fusion"}
    )
    rows = _rows(db_path)
    assert [
        (row["chosen_provider"], row["chosen_model"])
        for row in rows
    ] == [
        ("openrouter", "openrouter/auto"),
        ("openrouter", "openrouter/fusion"),
    ]
    assert {row["used_doctrine_reader"] for row in rows} == {0}


def test_existing_route_selection_does_not_hardcode_single_model_STILL_PASSES(
    plugin_env,
):
    module = _load_plugin()
    assert module._route_selection(
        {
            "route": "single",
            "provider": "openrouter",
            "model": "vendor/current-model",
        }
    ) == ("single", "openrouter", "vendor/current-model")
