"""Behavior tests for deterministic, no-model gateway operations."""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest

from gateway import operational_router
from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _write_usage_db(tmp_path) -> None:
    db = sqlite3.connect(tmp_path / "state.db")
    with db:
        db.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, source TEXT, model TEXT, started_at REAL,
                message_count INTEGER, tool_call_count INTEGER, api_call_count INTEGER,
                input_tokens INTEGER, cache_read_tokens INTEGER, output_tokens INTEGER
            )
            """
        )
        db.execute("CREATE TABLE messages (session_id TEXT, role TEXT, tool_name TEXT)")
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("included", "feishu", "model-a", time.time() - 60, 3, 2, 1, 100, 40, 10),
        )
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("future", "feishu", "model-b", time.time() + 60, 9, 9, 9, 900, 900, 900),
        )
        db.executemany(
            "INSERT INTO messages VALUES (?, ?, ?)",
            [("included", "tool", "search_files"), ("included", "tool", "search_files")],
        )
    db.close()


def test_reaction_sentinel_is_not_a_conversation_event():
    event = SimpleNamespace(text="reaction:added:THUMBSUP", metadata={})

    assert operational_router.is_non_conversation_event(event) is True


@pytest.mark.asyncio
async def test_gateway_drops_reaction_before_session_or_agent_work():
    # A bare runner has none of the session/agent fields. Reaching either
    # downstream path would fail, which makes this an executable assertion of
    # the required early return rather than a source-shape test.
    runner = object.__new__(GatewayRunner)
    event = MessageEvent(
        text="reaction:added:THUMBSUP",
        source=SessionSource(platform=Platform.FEISHU, chat_id="chat", user_id="user"),
    )

    assert await runner._handle_message(event) is None


def test_token_report_is_local_and_uses_request_time_as_its_cutoff(tmp_path, monkeypatch):
    _write_usage_db(tmp_path)
    monkeypatch.setattr(operational_router, "get_hermes_home", lambda: tmp_path)
    received_at = time.time()

    result = operational_router.route_operational_request(
        "请查一下今天的 token 消耗情况", platform="feishu", received_at=received_at
    )

    assert result.kind == "handled"
    assert result.route_name == "token_usage_report"
    assert "会话数：1" in result.text
    assert "非缓存输入 Token：100" in result.text
    assert "缓存读取 Token：40" in result.text
    assert "原始输入 Token：140" in result.text
    assert "总 Token：150" in result.text
    assert "search_files：2 次" in result.text
    assert "本报告自身开销：模型 0 Token，Agent 工具 0 次" in result.text
    assert "900" not in result.text


def test_vague_token_request_asks_for_window_without_agent_dispatch():
    result = operational_router.route_operational_request(
        "查 token 消耗", platform="feishu", received_at=time.time()
    )

    assert result.kind == "clarification"
    assert result.route_name == "token_usage_report"


def test_clear_cron_retarget_uses_cron_and_directory_interfaces(monkeypatch):
    job = {"id": "job-1", "name": "模型限频监控", "deliver": "origin"}
    updated = {"id": "job-1", "name": "模型限频监控", "deliver": "feishu:oc_target"}
    monkeypatch.setattr("cron.jobs.resolve_job_ref", lambda name: job if name == job["name"] else None)
    monkeypatch.setattr("cron.jobs.update_job", lambda job_id, changes: updated)
    monkeypatch.setattr("gateway.channel_directory.resolve_channel_name", lambda platform, name: "oc_target")
    monkeypatch.setattr(
        "cron.scheduler._resolve_delivery_target",
        lambda record: {"platform": "feishu", "chat_id": "oc_old"}
        if record is job else {"platform": "feishu", "chat_id": "oc_target"},
    )

    result = operational_router.route_operational_request(
        "请把 “模型限频监控”这个定时任务的结果，从目前发送到这个对话，改为发送到 Hermes IT 群",
        platform="feishu",
    )

    assert result.kind == "handled"
    assert result.route_name == "cron_retarget_delivery"
    assert "配置生效验证：通过" in result.text
    assert "模型 0 Token" in result.text


def test_deep_prefix_keeps_request_on_the_agent_path():
    result = operational_router.route_operational_request(
        "/deep 分析根因并修复 cron 投递失败", platform="feishu"
    )

    assert result.kind == "deep_mode"
    assert result.text == "分析根因并修复 cron 投递失败"


def test_file_search_cap_stops_broad_gateway_discovery_loops():
    config = ToolCallGuardrailConfig.from_mapping(
        {"loop_caps": {"max_file_searches": 2}}
    )
    controller = ToolCallGuardrailController(config)

    assert controller.before_call("search_files", {"query": "one"}).allows_execution
    assert controller.before_call("search_files", {"query": "two"}).allows_execution
    blocked = controller.before_call("search_files", {"query": "three"})

    assert blocked.should_halt
    assert blocked.code == "loop_file_search_cap"
