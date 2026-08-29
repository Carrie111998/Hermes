from hermes_state import AsyncSessionDB, SessionDB
"""Tests for gateway /status behavior and token persistence."""

from datetime import datetime, timezone
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source(platform: Platform = Platform.TELEGRAM) -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str, *, platform: Platform = Platform.TELEGRAM) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=_make_source(platform),
        message_id="m1",
    )


def _make_runner(session_entry: SessionEntry, *, platform: Platform = Platform.TELEGRAM):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={platform: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {platform: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = AsyncSessionDB(MagicMock())
    runner._session_db._db.get_session_title.return_value = None
    # Default: no DB row → /status reports 0 tokens.  Tests that exercise
    # the populated path override this.
    runner._session_db._db.get_session.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._agent_cache = {}
    runner._agent_cache_lock = MagicMock()
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner


@pytest.mark.asyncio
async def test_status_command_reads_token_totals_from_session_db():
    """Regression test for #17158: /status must source token totals from the
    SQLite SessionDB (where run_agent.py persists them) and sum all component
    counts, not from SessionEntry (which the agent never writes)."""
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,  # SessionEntry never gets written to — always 0.
    )
    runner = _make_runner(session_entry)
    runner._session_db._db.get_session.return_value = {
        "input_tokens": 1000,
        "output_tokens": 250,
        "cache_read_tokens": 500,
        "cache_write_tokens": 100,
        "reasoning_tokens": 50,
    }

    result = await runner._handle_message(_make_event("/status"))

    # 1000 + 250 + 500 + 100 + 50 = 1,900
    assert "**Lifetime tokens billed:** 1,900" in result


@pytest.mark.asyncio
async def test_status_command_reports_healthy_gateway_and_idle_agent(monkeypatch):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-idle",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.adapters[Platform.TELEGRAM].is_connected = True
    monkeypatch.setattr(
        "gateway.memory_status.collect_memory_status", lambda: {"pressure": "ok"}
    )
    monkeypatch.setattr(
        "gateway.disk_status.collect_disk_status", lambda: {"pressure": "ok"}
    )

    result = await runner._handle_message(_make_event("/status"))

    assert "🟢 **Hermes is operating normally**" in result
    assert "**Gateway:** Running" in result
    assert "**Agent:** Waiting for messages" in result
    assert "Agent Running" not in result


@pytest.mark.asyncio
async def test_status_command_russian_idle_wording_is_not_false_negative(monkeypatch):
    from agent import i18n

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-ru",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.adapters[Platform.TELEGRAM].is_connected = True
    monkeypatch.setattr(
        "gateway.memory_status.collect_memory_status", lambda: {"pressure": "ok"}
    )
    monkeypatch.setattr(
        "gateway.disk_status.collect_disk_status", lambda: {"pressure": "ok"}
    )
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        result = await runner._handle_message(_make_event("/status"))
    finally:
        i18n.reset_language_cache()

    assert "🟢 **Hermes работает штатно**" in result
    assert "**Агент:** ожидает сообщения" in result
    assert "Агент активен" not in result


@pytest.mark.asyncio
async def test_status_command_localizes_resource_and_retry_labels(monkeypatch):
    from agent import i18n

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-ru-details",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.adapters[Platform.TELEGRAM].is_connected = True
    runner.config.platforms[Platform.SLACK] = PlatformConfig(enabled=True, token="***")
    monkeypatch.setattr(
        "gateway.memory_status.collect_memory_status",
        lambda: {"pressure": "ok", "gateway_rss_mb": 512, "system_available_mb": 96},
    )
    monkeypatch.setattr(
        "gateway.disk_status.collect_disk_status",
        lambda: {"pressure": "ok", "free_mb": 200, "used_percent": 98.0},
    )
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "platforms": {
                "telegram": {"state": "connected"},
                "slack": {
                    "state": "retrying",
                    "retrying_since": "2026-08-29T10:00:00+00:00",
                },
            }
        },
    )
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        result = await runner._handle_message(_make_event("/status"))
    finally:
        i18n.reset_language_cache()

    assert "**Память:** норма · RSS 512 МБ · 96 МБ доступно" in result
    assert "**Диск:** норма · 200 МБ свободно · использовано 98.0%" in result
    assert "повторные попытки с: 2026-08-29T10:00:00+00:00" in result
    assert "**Memory:**" not in result
    assert "MB available" not in result
    assert "retrying since" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "pressure", "expected"),
    [
        ("ru", "ok", "**Память:** норма"),
        ("ru", "elevated", "**Память:** повышено"),
        ("ru", "critical", "**Память:** критично"),
        ("ru", "unknown", "**Память:** неизвестно"),
        ("de", "ok", "**Arbeitsspeicher:** normal"),
        ("de", "elevated", "**Arbeitsspeicher:** erhöht"),
        ("de", "critical", "**Arbeitsspeicher:** kritisch"),
        ("de", "unknown", "**Arbeitsspeicher:** unbekannt"),
        ("ja", "ok", "**メモリ:** 正常"),
        ("ja", "elevated", "**メモリ:** 上昇"),
        ("ja", "critical", "**メモリ:** 危険"),
        ("ja", "unknown", "**メモリ:** 不明"),
    ],
)
async def test_status_command_localizes_pressure_states(
    monkeypatch, language, pressure, expected
):
    from agent import i18n

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id=f"sess-pressure-{language}-{pressure}",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.adapters[Platform.TELEGRAM].is_connected = True
    monkeypatch.setattr(
        "gateway.memory_status.collect_memory_status", lambda: {"pressure": pressure}
    )
    monkeypatch.setattr(
        "gateway.disk_status.collect_disk_status", lambda: {"pressure": "ok"}
    )
    monkeypatch.setenv("HERMES_LANGUAGE", language)
    i18n.reset_language_cache()
    try:
        result = await runner._handle_message(_make_event("/status"))
    finally:
        i18n.reset_language_cache()

    assert expected in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "elapsed", "expected"),
    [
        ("ru", 90_000, "1 д 1 ч"),
        ("ru", 3_660, "1 ч 1 мин"),
        ("ru", 60, "1 мин"),
        ("ru", 1, "1 с"),
        ("de", 90_000, "1 T 1 Std."),
        ("de", 3_660, "1 Std. 1 Min."),
        ("de", 60, "1 Min."),
        ("de", 1, "1 Sek."),
        ("ja", 90_000, "1日 1時間"),
        ("ja", 3_660, "1時間 1分"),
        ("ja", 60, "1分"),
        ("ja", 1, "1秒"),
    ],
)
async def test_status_command_localizes_duration_units(
    monkeypatch, language, elapsed, expected
):
    from agent import i18n

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id=f"sess-duration-{language}-{elapsed}",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.adapters[Platform.TELEGRAM].is_connected = True
    runner._gateway_started_at = 1_000.0
    monkeypatch.setattr(time, "time", lambda: 1_000.0 + elapsed)
    monkeypatch.setattr(
        "gateway.memory_status.collect_memory_status", lambda: {"pressure": "ok"}
    )
    monkeypatch.setattr(
        "gateway.disk_status.collect_disk_status", lambda: {"pressure": "ok"}
    )
    monkeypatch.setenv("HERMES_LANGUAGE", language)
    i18n.reset_language_cache()
    try:
        result = await runner._handle_message(_make_event("/status"))
    finally:
        i18n.reset_language_cache()

    assert expected in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "expected"),
    [("ru", "(2 мин, 3)"), ("de", "(2 Min., 3)"), ("ja", "(2分, 3)")],
)
async def test_platform_attention_notification_localizes_minute_unit(
    monkeypatch, language, expected
):
    from agent import i18n
    import gateway.run as gateway_run

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id=f"sess-attention-{language}",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-1",
        name="Home",
    )
    monkeypatch.setattr(gateway_run.time, "monotonic", lambda: 1_000.0)
    monkeypatch.setenv("HERMES_LANGUAGE", language)
    i18n.reset_language_cache()
    try:
        message = runner._platform_attention_message(
            Platform.SLACK, {"queued_at": 880.0, "attempts": 3}
        )
    finally:
        i18n.reset_language_cache()

    assert expected in message


@pytest.mark.asyncio
async def test_status_command_reports_disconnected_platform_as_degraded():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-degraded",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    telegram = runner.adapters[Platform.TELEGRAM]
    telegram.is_connected = True
    slack = MagicMock()
    slack.is_connected = False
    runner.adapters[Platform.SLACK] = slack

    result = await runner._handle_message(_make_event("/status"))

    assert "🟡 **Hermes is operating with limitations**" in result
    assert "**Telegram:** Connected ✓" in result
    assert "**Slack:** Disconnected" in result
    assert "**Advice:** Reconnect Slack" in result


@pytest.mark.asyncio
async def test_status_command_includes_configured_platform_missing_from_adapters(monkeypatch):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-runtime",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.adapters[Platform.TELEGRAM].is_connected = True
    runner.config.platforms[Platform.SLACK] = PlatformConfig(enabled=True, token="***")
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "platforms": {
                "telegram": {"state": "connected"},
                "slack": {
                    "state": "retrying",
                    "error_code": "auth_failed",
                    "error_message": "token revoked",
                    "retrying_since": "2026-08-29T10:00:00+00:00",
                    "needs_attention": True,
                },
            },
        },
    )

    result = await runner._handle_message(_make_event("/status"))

    assert "🟡 **Hermes is operating with limitations**" in result
    assert "**Slack:** Disconnected" in result
    assert "auth＿failed: token revoked" in result
    assert "2026-08-29T10:00:00+00:00" in result


@pytest.mark.asyncio
async def test_status_command_includes_runtime_only_retrying_platform(monkeypatch):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-runtime-only",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.adapters[Platform.TELEGRAM].is_connected = True
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "platforms": {
                "telegram": {"state": "connected"},
                "slack": {"state": "retrying"},
                "future_plugin_platform": {"state": "retrying"},
            }
        },
    )

    result = await runner._handle_message(_make_event("/status"))

    assert "🟡 **Hermes is operating with limitations**" in result
    assert "**Slack:** Disconnected" in result
    assert "future_plugin_platform" not in result


@pytest.mark.asyncio
async def test_status_command_safely_renders_untrusted_runtime_errors(monkeypatch):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-safe-error",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.adapters[Platform.TELEGRAM].is_connected = True
    leaked_token = "sk-supersecret0123456789"
    credential_url = "https://alice:hunter2@example.com/oauth?access_token=opaque-secret"
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "platforms": {
                "telegram": {
                    "state": "retrying",
                    "error_code": f"auth_failed **bold** {leaked_token}",
                    "error_message": (
                        f"{credential_url} @everyone <@U123> [click](https://evil.invalid)"
                    ),
                    "retrying_since": f"{credential_url} @everyone",
                }
            }
        },
    )

    result = await runner._handle_message(_make_event("/status"))

    assert leaked_token not in result
    assert "alice:hunter2" not in result
    assert "opaque-secret" not in result
    assert "@everyone" not in result
    assert "<@U123>" not in result
    assert "**bold**" not in result
    assert "[click](https://evil.invalid)" not in result


@pytest.mark.asyncio
async def test_status_command_surfaces_memory_and_disk_pressure(monkeypatch):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-pressure",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.adapters[Platform.TELEGRAM].is_connected = True
    monkeypatch.setattr(
        "gateway.memory_status.collect_memory_status",
        lambda: {"pressure": "elevated", "gateway_rss_mb": 512, "system_available_mb": 96},
    )
    monkeypatch.setattr(
        "gateway.disk_status.collect_disk_status",
        lambda: {"pressure": "critical", "free_mb": 200, "used_percent": 98.0},
    )

    result = await runner._handle_message(_make_event("/status"))

    assert "🟡 **Hermes is operating with limitations**" in result
    assert "**Memory:** elevated" in result
    assert "RSS 512 MB" in result
    assert "**Disk:** critical" in result
    assert "200 MB free" in result


@pytest.mark.asyncio
async def test_status_command_does_not_claim_healthy_when_telemetry_unknown(
    monkeypatch,
):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-unknown-telemetry",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.adapters[Platform.TELEGRAM].is_connected = True
    monkeypatch.setattr(
        "gateway.memory_status.collect_memory_status",
        lambda: {"pressure": "unknown"},
    )
    monkeypatch.setattr(
        "gateway.disk_status.collect_disk_status",
        lambda: {"pressure": "unknown"},
    )

    result = await runner._handle_message(_make_event("/status"))

    assert "🟢 **Hermes is operating normally**" not in result
    assert "🟡 **Hermes is operating with limitations**" in result
    assert "**Memory:** unknown" in result
    assert "**Disk:** unknown" in result


@pytest.mark.asyncio
async def test_status_command_shows_timezone_uptime_and_active_task_duration(monkeypatch):
    now = datetime(2026, 8, 29, 10, 30)
    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-time",
        created_at=now,
        updated_at=now,
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.adapters[Platform.TELEGRAM].is_connected = True
    runner._gateway_started_at = 1_000.0
    runner._running_agents_ts = {session_key: 4_540.0}
    runner._running_agents[session_key] = SimpleNamespace(
        model="openai/gpt-test",
        provider="openai",
        context_compressor=SimpleNamespace(last_prompt_tokens=0, context_length=100_000),
        interrupt=MagicMock(),
        get_activity_summary=lambda: {"seconds_since_activity": 0},
    )
    monkeypatch.setattr(time, "time", lambda: 4_600.0)

    result = await runner._handle_message(_make_event("/status"))

    assert "2026-08-29 10:30 UTC" in result
    assert "**Gateway uptime:** 1h" in result
    assert "**Current task duration:** 1m" in result
    assert "**Agent:** Processing a request ⚡" in result


@pytest.mark.asyncio
async def test_status_command_converts_each_timestamp_across_dst_transition(
    monkeypatch,
):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-dst",
        created_at=datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc),
        updated_at=datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.adapters[Platform.TELEGRAM].is_connected = True
    monkeypatch.setattr(
        "hermes_time.get_timezone", lambda: ZoneInfo("America/New_York")
    )

    result = await runner._handle_message(_make_event("/status"))

    assert "**Created:** 2026-11-01 01:30 EDT" in result
    assert "**Last Activity:** 2026-11-01 01:30 EST" in result


@pytest.mark.asyncio
async def test_status_command_includes_live_agent_model_and_context():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )
    runner = _make_runner(session_entry)
    runner._session_db._db.get_session.return_value = {
        "input_tokens": 1000,
        "output_tokens": 250,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "model": "openai/gpt-test",
    }
    running_agent = SimpleNamespace(
        model="openai/gpt-test",
        provider="openai",
        context_compressor=SimpleNamespace(
            last_prompt_tokens=12_345,
            context_length=100_000,
        ),
        interrupt=MagicMock(),
    )
    runner._running_agents[build_session_key(_make_source())] = running_agent

    result = await runner._handle_message(_make_event("/status"))

    assert "**Model:** `openai/gpt-test` (openai)" in result
    assert "**Context:** 12,345 / 100,000 (12%)" in result
    assert "**Lifetime tokens billed:** 1,250" in result


@pytest.mark.asyncio
async def test_status_command_uses_dominant_persisted_model_route(tmp_path):
    """Persisted status must not combine a model and provider from different calls."""
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    db = SessionDB(db_path=tmp_path / "state.db")
    runner._session_db = AsyncSessionDB(db)
    try:
        db.create_session("sess-1", "telegram", model="z-ai/glm-5.2")
        db.update_token_counts(
            "sess-1",
            model="z-ai/glm-5.2",
            billing_provider="nvidia",
            billing_base_url="https://integrate.api.nvidia.com/v1/",
            input_tokens=480,
            api_call_count=48,
        )
        db.update_token_counts(
            "sess-1",
            model="upstage/solar-pro4:free",
            billing_provider="nous",
            billing_base_url="https://inference-api.nousresearch.com/v1/",
            input_tokens=60,
            api_call_count=6,
        )
        # Reproduce the inconsistent legacy summary observed in #87227.
        db.update_session_model("sess-1", "z-ai/glm-5.2")
        db.update_session_billing_route(
            "sess-1",
            provider="nous",
            base_url="https://inference-api.nousresearch.com/v1/",
        )

        result = await runner._handle_message(_make_event("/status"))

        assert "**Model:** `z-ai/glm-5.2` (nvidia)" in result
        assert "**Model:** `z-ai/glm-5.2` (nous)" not in result
    finally:
        db.close()


@pytest.mark.asyncio
async def test_agents_command_reports_active_agents_and_processes(monkeypatch):
    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )
    runner = _make_runner(session_entry)
    running_agent = SimpleNamespace(
        session_id="sess-running",
        model="openrouter/test-model",
        interrupt=MagicMock(),
        get_activity_summary=lambda: {"seconds_since_activity": 0},
    )
    runner._running_agents[session_key] = running_agent
    runner._running_agents_ts = {session_key: time.time() - 8}
    runner._background_tasks = set()

    class _FakeRegistry:
        def list_sessions(self):
            return [
                {
                    "session_id": "proc-1",
                    "status": "running",
                    "uptime_seconds": 17,
                    "command": "sleep 30",
                }
            ]

    monkeypatch.setattr("tools.process_registry.process_registry", _FakeRegistry())

    result = await runner._handle_message(_make_event("/agents"))

    assert "**Active agents:** 1" in result
    assert "**Running background processes:** 1" in result
    assert "proc-1" in result
    running_agent.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_tasks_alias_routes_to_agents_command(monkeypatch):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )
    runner = _make_runner(session_entry)
    runner._background_tasks = set()

    class _FakeRegistry:
        def list_sessions(self):
            return []

    monkeypatch.setattr("tools.process_registry.process_registry", _FakeRegistry())

    result = await runner._handle_message(_make_event("/tasks"))

    assert "Active Agents & Tasks" in result


@pytest.mark.asyncio
async def test_first_run_slack_home_channel_onboarding_uses_parent_command(monkeypatch):
    import gateway.run as gateway_run

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source(Platform.SLACK)),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.SLACK,
        chat_type="dm",
    )
    runner = _make_runner(session_entry, platform=Platform.SLACK)
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = False
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "model": "openai/test-model",
        }
    )

    monkeypatch.delenv("SLACK_HOME_CHANNEL", raising=False)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100000,
    )

    result = await runner._handle_message(_make_event("hello", platform=Platform.SLACK))

    assert result == "ok"
    runner.adapters[Platform.SLACK].send.assert_awaited_once()
    onboarding = runner.adapters[Platform.SLACK].send.await_args.args[1]
    assert "/hermes sethome" in onboarding
    assert "Type /sethome" not in onboarding


@pytest.mark.asyncio
async def test_handle_message_stale_result_keeps_newer_generation_callback(monkeypatch):
    import gateway.run as gateway_run

    class _Adapter:
        def __init__(self):
            self._post_delivery_callbacks = {}

        async def send(self, *args, **kwargs):
            return None

        def pop_post_delivery_callback(self, session_key, *, generation=None):
            entry = self._post_delivery_callbacks.get(session_key)
            if entry is None:
                return None
            if isinstance(entry, tuple):
                entry_generation, callback = entry
                if generation is not None and entry_generation != generation:
                    return None
                self._post_delivery_callbacks.pop(session_key, None)
                return callback
            if generation is not None:
                return None
            return self._post_delivery_callbacks.pop(session_key, None)

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.session_store.load_transcript.return_value = [{"role": "user", "content": "earlier"}]
    session_key = session_entry.session_key
    adapter = _Adapter()
    runner.adapters[Platform.TELEGRAM] = adapter

    async def _stale_result(**kwargs):
        # Simulate a newer run claiming the callback slot before the stale run unwinds.
        runner._session_run_generation[session_key] = 2
        adapter._post_delivery_callbacks[session_key] = (2, lambda: None)
        return {
            "final_response": "late reply",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 80,
            "input_tokens": 120,
            "output_tokens": 45,
            "model": "openai/test-model",
        }

    runner._run_agent = AsyncMock(side_effect=_stale_result)

    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100000,
    )

    result = await runner._handle_message(_make_event("hello"))

    assert result is None
    assert session_key in adapter._post_delivery_callbacks
    assert adapter._post_delivery_callbacks[session_key][0] == 2


@pytest.mark.asyncio
async def test_status_command_bypasses_active_session_guard():
    """When an agent is running, /status must be dispatched immediately via
    base.handle_message — not queued or treated as an interrupt (#5046)."""
    import asyncio
    from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
    from gateway.session import build_session_key
    from gateway.config import Platform, PlatformConfig

    source = _make_source()
    session_key = build_session_key(source)

    handler_called_with = []

    async def fake_handler(event):
        handler_called_with.append(event)
        return "📊 **Hermes Gateway Status**\n**Agent Running:** Yes ⚡"

    # Concrete subclass to avoid abstract method errors
    class _ConcreteAdapter(BasePlatformAdapter):
        platform = Platform.TELEGRAM

        async def connect(self, *, is_reconnect: bool = False): pass
        async def disconnect(self): pass
        async def send(self, chat_id, content, **kwargs): pass
        async def get_chat_info(self, chat_id): return {}

    platform_config = PlatformConfig(enabled=True, token="***")
    adapter = _ConcreteAdapter(platform_config, Platform.TELEGRAM)
    adapter.set_message_handler(fake_handler)

    sent = []

    async def fake_send_with_retry(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)

    adapter._send_with_retry = fake_send_with_retry

    # Simulate an active session
    interrupt_event = asyncio.Event()
    adapter._active_sessions[session_key] = interrupt_event

    event = MessageEvent(
        text="/status",
        source=source,
        message_id="m1",
        message_type=MessageType.COMMAND,
    )
    await adapter.handle_message(event)

    assert handler_called_with, "/status handler was never called (event was queued or dropped)"
    assert sent, "/status response was never sent"
    assert "Agent Running" in sent[0]
    assert not interrupt_event.is_set(), "/status incorrectly triggered an agent interrupt"
    assert session_key not in adapter._pending_messages, "/status was incorrectly queued"


@pytest.mark.asyncio
async def test_profile_command_reports_source_stamped_profile(monkeypatch, tmp_path):
    """On a multiplexed gateway, /profile reports the profile SERVING the
    source (source.profile — URL prefix / per-credential adapter / room map),
    not the multiplexer's active profile, which is always the default and
    made /profile answer "default" in every persona chat."""
    hermes_home = tmp_path / ".hermes"
    profile_home = hermes_home / "profiles" / "milo"
    profile_home.mkdir(parents=True)

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.config.multiplex_profiles = True
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    event = _make_event("/profile")
    event.source.profile = "milo"

    result = await runner._handle_profile_command(event)

    assert "**Profile:** `milo`" in result
    assert f"**Home:** `{profile_home}`" in result


# ── /context command tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_context_command_keeps_configured_window_without_resident_agent():
    """The no-agent fallback must not replace a custom-provider context pin."""
    model = "unsloth/Qwen3.8-27B-GGUF:Q8_0"
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-context-pin",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    session_entry.last_prompt_tokens = 66_570
    runner = _make_runner(session_entry)
    runner._session_db._db.get_session.return_value = {"model": model}

    config = {
        "model": {
            "default": model,
            "provider": "custom-local-qwen",
            "context_length": 262_144,
        },
        "custom_providers": [
            {
                "name": "custom-local-qwen",
                "base_url": "http://127.0.0.1:8080/v1",
                "models": {},
            }
        ],
    }
    runtime = {
        "provider": "custom-local-qwen",
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key": "",
    }

    with patch("gateway.run._load_gateway_config", return_value=config), patch(
        "gateway.run._resolve_runtime_agent_kwargs", return_value=runtime
    ), patch(
        "hermes_cli.config.get_compatible_custom_providers",
        return_value=config["custom_providers"],
    ), patch(
        "agent.model_metadata.get_model_context_length",
        side_effect=lambda *args, **kwargs: kwargs.get("config_context_length") or 131_072,
    ) as context_lookup:
        result = await runner._handle_context_command(_make_event("/context"))

    assert "Window: 262,144 tokens" in result
    assert "In use: 66,570 / 262,144 (25%)" in result
    assert "131,072" not in result
    assert context_lookup.call_count == 1
    assert context_lookup.call_args.kwargs["config_context_length"] == 262_144


def _stub_agent(**overrides) -> SimpleNamespace:
    """Build a stub agent with the attributes _handle_context_command reads."""
    props = dict(
        model="openai/gpt-test",
        context_compressor=SimpleNamespace(
            last_prompt_tokens=47_231,
            context_length=200_000,
            threshold_tokens=100_000,
            threshold_percent=0.5,
            compression_count=2,
            _last_compression_savings_pct=63.0,
        ),
        session_api_calls=47,
        session_input_tokens=410_000,
        session_output_tokens=38_000,
        session_reasoning_tokens=12_000,
        session_total_tokens=3_158_641,
        session_cache_read_tokens=2_900_000,
        session_cache_write_tokens=48_000,
    )
    props.update(overrides)
    return SimpleNamespace(**props)


@pytest.mark.asyncio
async def test_context_all_appends_expanded_listings():
    """/context all appends per-toolset and per-skill cost listings."""
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-6",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    agent = _stub_agent()
    runner._running_agents[session_entry.session_key] = agent

    fake_payload = {
        "categories": [
            {"id": "skills", "label": "Skills", "tokens": 2_000},
        ],
        "context_max": 200_000,
        "context_percent": 24,
        "context_used": 47_231,
        "estimated_total": 2_000,
        "model": "openai/gpt-test",
    }
    fake_details = {
        "skills": [
            {"name": "hermes-agent", "index_tokens": 30, "skill_md_tokens": 2_500},
        ],
        "toolsets": [
            {"toolset": "terminal", "tool_count": 4, "schema_tokens": 5_100},
        ],
    }
    from unittest.mock import patch as _patch
    with _patch(
        "agent.context_breakdown.compute_session_context_breakdown",
        return_value=fake_payload,
    ), _patch(
        "agent.context_breakdown.compute_context_details",
        return_value=fake_details,
    ):
        result = await runner._handle_context_command(_make_event("/context all"))

    assert "Toolsets by schema cost" in result
    assert "terminal" in result and "5,100 tokens" in result
    assert "Skills by cost" in result
    assert "hermes-agent" in result
    # Expanded view drops the hint
    assert "Use /context all" not in result
