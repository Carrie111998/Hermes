"""Deterministic, no-model handlers for small gateway operations.

This module deliberately sits outside the agent/tool loop.  Requests that have
a stable local source of truth should not pay to rediscover that source with a
large tool schema and an ever-growing conversation transcript.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import re
import sqlite3
import time
from typing import Any, Literal

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

RouteKind = Literal["handled", "clarification", "deep_mode", "unmatched"]


@dataclass(frozen=True)
class OperationalRoute:
    """Result returned before session creation or agent dispatch."""

    kind: RouteKind
    text: str = ""
    route_name: str = ""


_REACTION_EVENT_RE = re.compile(r"^reaction:(?:added|removed):[a-z0-9_+\-]+$", re.I)
_TOKEN_WORD_RE = re.compile(r"\btokens?\b|token|令牌", re.I)
_TOKEN_USAGE_RE = re.compile(r"用量|消耗|统计|使用|usage|spend|cost|input|output", re.I)
_DEEP_RE = re.compile(r"(^\s*/deep(?:\s+|$))|深入排查|分析根因", re.I)
_CRON_JOB_RE = re.compile(r"[“‘\"'](?P<name>[^”’\"']+)[”’\"']\s*(?:这个)?(?:定时)?任务")
_CRON_TARGET_RE = re.compile(r"(?:改为|改成|调整为).*?(?:发送到|投递到)\s*(?P<target>[^，。！？!?]+)")


def is_non_conversation_event(event: Any) -> bool:
    """Return whether a normalized event must never become an agent turn."""
    metadata = getattr(event, "metadata", None) or {}
    event_name = str(metadata.get("event_name") or metadata.get("event_type") or "")
    if event_name.lower() in {
        "reaction:added",
        "reaction:removed",
        "message_read",
        "message_status",
    }:
        return True
    text = str(getattr(event, "text", "") or "").strip()
    return bool(_REACTION_EVENT_RE.fullmatch(text))


def route_operational_request(
    text: str,
    *,
    platform: str,
    received_at: float | datetime | None = None,
) -> OperationalRoute:
    """Route a narrow, repeatable operation without consulting an LLM."""
    raw = (text or "").strip()
    if not raw:
        return OperationalRoute("unmatched")

    if _DEEP_RE.search(raw):
        if raw.lower().startswith("/deep"):
            raw = raw[5:].strip()
        return OperationalRoute("deep_mode", raw, "deep_mode")

    if _looks_like_token_request(raw):
        window = _parse_window(raw, received_at=received_at)
        if window is None:
            return OperationalRoute(
                "clarification",
                "请说明统计范围，例如：今天、昨天、本周、本月或最近 7 天。",
                "token_usage_report",
            )
        return _render_token_report(platform=platform, window=window)

    cron_request = _parse_cron_retarget_request(raw)
    if cron_request is None:
        return OperationalRoute("unmatched")
    job_name, target_name = cron_request
    return _retarget_cron_delivery(job_name, target_name, platform)


def _looks_like_token_request(text: str) -> bool:
    normalized = text.lower()
    return bool(_TOKEN_WORD_RE.search(normalized) and _TOKEN_USAGE_RE.search(normalized))


def _parse_window(
    text: str, *, received_at: float | datetime | None
) -> tuple[float, float, str] | None:
    # Gateway MessageEvent.timestamp is a datetime, while direct callers and
    # tests commonly provide Unix seconds. Accept both so this route cannot
    # fail over to the general agent merely because of timestamp shape.
    if isinstance(received_at, datetime):
        now = received_at.astimezone()
    else:
        now = datetime.fromtimestamp(
            received_at if received_at is not None else time.time()
        ).astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    normalized = re.sub(r"\s+", "", text.lower())
    if any(word in normalized for word in ("今天", "今日", "当天", "today")):
        return today.timestamp(), now.timestamp(), "今天"
    if any(word in normalized for word in ("昨天", "昨日", "yesterday")):
        start = today - timedelta(days=1)
        return start.timestamp(), today.timestamp(), "昨天"
    if any(word in normalized for word in ("本周", "thisweek")):
        start = today - timedelta(days=today.weekday())
        return start.timestamp(), now.timestamp(), "本周"
    if any(word in normalized for word in ("本月", "thismonth")):
        start = today.replace(day=1)
        return start.timestamp(), now.timestamp(), "本月"
    match = re.search(r"(?:最近|近|last)\s*(\d+)\s*(?:天|days?)", normalized, re.I)
    if match:
        days = max(1, min(int(match.group(1)), 365))
        return (now - timedelta(days=days)).timestamp(), now.timestamp(), f"最近 {days} 天"
    return None


def _render_token_report(*, platform: str, window: tuple[float, float, str]) -> OperationalRoute:
    start, end, label = window
    db_path = get_hermes_home() / "state.db"
    if not db_path.exists():
        return OperationalRoute("handled", "暂未找到 Hermes 用量数据库。", "token_usage_report")

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        with conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS sessions,
                       COALESCE(SUM(message_count), 0) AS messages,
                       COALESCE(SUM(tool_call_count), 0) AS tools,
                       COALESCE(SUM(api_call_count), 0) AS api_calls,
                       COALESCE(SUM(input_tokens), 0) AS non_cached_input,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read,
                       COALESCE(SUM(output_tokens), 0) AS output
                FROM sessions
                WHERE source = ? AND started_at >= ? AND started_at < ?
                """,
                (platform, start, end),
            ).fetchone()
            models = conn.execute(
                """
                SELECT COALESCE(model, 'unknown') AS model,
                       COALESCE(SUM(input_tokens), 0) +
                       COALESCE(SUM(cache_read_tokens), 0) +
                       COALESCE(SUM(output_tokens), 0) AS total
                FROM sessions
                WHERE source = ? AND started_at >= ? AND started_at < ?
                GROUP BY COALESCE(model, 'unknown')
                ORDER BY total DESC, model ASC
                LIMIT 5
                """,
                (platform, start, end),
            ).fetchall()
            tools = conn.execute(
                """
                SELECT COALESCE(m.tool_name, 'unknown') AS name, COUNT(*) AS calls
                FROM messages AS m
                JOIN sessions AS s ON s.id = m.session_id
                WHERE s.source = ? AND s.started_at >= ? AND s.started_at < ?
                  AND m.role = 'tool'
                GROUP BY COALESCE(m.tool_name, 'unknown')
                ORDER BY calls DESC, name ASC
                LIMIT 8
                """,
                (platform, start, end),
            ).fetchall()
    except sqlite3.Error:
        logger.warning("gateway.operational_route token_usage_report failed", exc_info=True)
        return OperationalRoute("handled", "读取 Hermes 用量数据库失败，请稍后重试。", "token_usage_report")
    finally:
        try:
            conn.close()
        except UnboundLocalError:
            pass

    non_cached_input = int(row["non_cached_input"])
    cache_read = int(row["cache_read"])
    output = int(row["output"])
    raw_input = non_cached_input + cache_read
    total = raw_input + output
    lines = [
        f"以下是 {label} 的 Token 消耗统计（{platform}）：",
        "",
        "📋 概览",
        f"· 会话数：{int(row['sessions'])}",
        f"· 消息数：{int(row['messages'])}",
        f"· 工具调用：{int(row['tools'])}",
        f"· API 调用：{int(row['api_calls'])}",
        f"· 非缓存输入 Token：{non_cached_input:,}",
        f"· 缓存读取 Token：{cache_read:,}",
        f"· 原始输入 Token：{raw_input:,}",
        f"· 输出 Token：{output:,}",
        f"· 总 Token：{total:,}",
        "· 本报告自身开销：模型 0 Token，Agent 工具 0 次",
    ]
    if models:
        lines.extend(["", "🤖 模型分布"])
        lines.extend(f"· {item['model']}：{int(item['total']):,} Token" for item in models)
    if tools:
        lines.extend(["", "🔧 工具调用情况"])
        lines.extend(f"· {item['name']}：{int(item['calls'])} 次" for item in tools)
    return OperationalRoute("handled", "\n".join(lines), "token_usage_report")


def _parse_cron_retarget_request(text: str) -> tuple[str, str] | None:
    if not any(word in text for word in ("定时任务", "定时", "cron")):
        return None
    job_match = _CRON_JOB_RE.search(text)
    target_match = _CRON_TARGET_RE.search(text)
    if not job_match or not target_match:
        return None
    name = job_match.group("name").strip()
    target = target_match.group("target").strip().rstrip("。！？!?")
    return (name, target) if name and target else None


def _retarget_cron_delivery(job_name: str, target_name: str, platform: str) -> OperationalRoute:
    from cron.jobs import AmbiguousJobReference, resolve_job_ref, update_job
    from cron.scheduler import _resolve_delivery_target
    from gateway.channel_directory import resolve_channel_name

    try:
        job = resolve_job_ref(job_name)
    except AmbiguousJobReference:
        return OperationalRoute(
            "clarification",
            f"找到多个名为“{job_name}”的定时任务，请提供任务 ID。",
            "cron_retarget_delivery",
        )
    if job is None:
        return OperationalRoute(
            "clarification",
            f"未找到名为“{job_name}”的定时任务，请检查任务名称或提供任务 ID。",
            "cron_retarget_delivery",
        )

    chat_id = resolve_channel_name(platform, target_name)
    if not chat_id:
        return OperationalRoute(
            "clarification",
            f"无法唯一识别“{target_name}”这个 {platform} 目标，请提供准确群名或 chat ID。",
            "cron_retarget_delivery",
        )

    old_target = _resolve_delivery_target(job)
    updated = update_job(job["id"], {"deliver": f"{platform}:{chat_id}"})
    if updated is None:
        return OperationalRoute("handled", "更新定时任务投递目标失败，请稍后重试。", "cron_retarget_delivery")
    verified = _resolve_delivery_target(updated)
    if not verified or str(verified.get("platform", "")).lower() != platform.lower() or str(verified.get("chat_id")) != str(chat_id):
        logger.error("gateway.operational_route cron target verification failed for job %s", job["id"])
        return OperationalRoute("handled", "投递目标更新后验证失败；任务未报告为已完成。", "cron_retarget_delivery")

    old_label = (
        f"{old_target.get('platform')}:{old_target.get('chat_id')}"
        if old_target else "无/本地"
    )
    return OperationalRoute(
        "handled",
        "\n".join(
            [
                f"“{updated.get('name') or job_name}”定时任务的发送目标已修改成功。",
                "",
                "变更信息",
                f"· 原接收目标：{old_label}",
                f"· 新接收目标：{platform}:{chat_id}",
                "· 配置生效验证：通过",
                "· 本次操作自身开销：模型 0 Token，Agent 工具 0 次",
            ]
        ),
        "cron_retarget_delivery",
    )
