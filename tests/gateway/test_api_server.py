"""
Tests for the OpenAI-compatible API server gateway adapter.

Tests cover:
- Chat Completions endpoint (request parsing, response format)
- Responses API endpoint (request parsing, response format)
- previous_response_id chaining (store/retrieve)
- Auth (valid key, invalid key, no key configured)
- /v1/models endpoint
- /health endpoint
- System prompt extraction
- Error handling (invalid JSON, missing fields)
"""

import asyncio
import json
import os
import stat
import sys
import time
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    ResponseStore,
    _IdempotencyCache,
    _derive_chat_session_id,
    _hermes_version,
    _redact_api_error_text,
    _request_agent_overrides,
    check_api_server_requirements,
    cors_middleware,
    security_headers_middleware,
)


# ---------------------------------------------------------------------------
# check_api_server_requirements
# ---------------------------------------------------------------------------


class TestCheckRequirements:

    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", False)
    def test_returns_false_without_aiohttp(self):
        assert check_api_server_requirements() is False


# ---------------------------------------------------------------------------
# _redact_api_error_text â€” guards every outward error site (envelopes, SSE
# error events, cron-endpoint 500 bodies) that routes raw exception text to
# authenticated HTTP clients. #37733
# ---------------------------------------------------------------------------


class TestRedactApiErrorText:
    def test_masks_secret_value_but_preserves_structure(self):
        secret = "sk-api-server-leak-1234567890"
        out = _redact_api_error_text(Exception(f"auth failed OPENAI_API_KEY={secret}"))
        assert secret not in out
        assert "OPENAI_API_KEY=" in out

    def test_redacts_regardless_of_global_redaction_setting(self):
        # force=True must mask even when global redaction is disabled.
        secret = "sk-forced-redaction-0987654321"
        with patch("agent.redact._REDACT_ENABLED", False):
            out = _redact_api_error_text(Exception(f"boom AWS_SECRET_ACCESS_KEY={secret}"))
        assert secret not in out

    def test_limit_truncates_after_redaction(self):
        assert len(_redact_api_error_text("x" * 500, limit=50)) == 50


# ---------------------------------------------------------------------------
# ResponseStore
# ---------------------------------------------------------------------------


class TestResponseStore:
    def test_put_and_get(self):
        store = ResponseStore(max_size=10)
        store.put("resp_1", {"output": "hello"})
        assert store.get("resp_1") == {"output": "hello"}

    def test_get_missing_returns_none(self):
        store = ResponseStore(max_size=10)
        assert store.get("resp_missing") is None

    def test_lru_eviction(self):
        store = ResponseStore(max_size=3)
        store.put("resp_1", {"output": "one"})
        store.put("resp_2", {"output": "two"})
        store.put("resp_3", {"output": "three"})
        # Adding a 4th should evict resp_1
        store.put("resp_4", {"output": "four"})
        assert store.get("resp_1") is None
        assert store.get("resp_2") is not None
        assert len(store) == 3


    def test_delete_clears_conversation_mapping(self):
        """Deleting a response also removes conversation mappings that reference it."""
        store = ResponseStore(max_size=10)
        store.put("resp_1", {"output": "hello"})
        store.set_conversation("chat-a", "resp_1")
        assert store.get_conversation("chat-a") == "resp_1"
        store.delete("resp_1")
        assert store.get_conversation("chat-a") is None


# ---------------------------------------------------------------------------
# _IdempotencyCache
# ---------------------------------------------------------------------------


class TestIdempotencyCache:
    @pytest.mark.asyncio
    async def test_concurrent_same_key_and_fingerprint_runs_once(self):
        cache = _IdempotencyCache()
        gate = asyncio.Event()
        started = asyncio.Event()
        calls = 0

        async def compute():
            nonlocal calls
            calls += 1
            started.set()
            await gate.wait()
            return ("response", {"total_tokens": 1})

        first = asyncio.create_task(cache.get_or_set("idem-key", "fp-1", compute))
        second = asyncio.create_task(cache.get_or_set("idem-key", "fp-1", compute))

        await started.wait()
        assert calls == 1

        gate.set()
        first_result, second_result = await asyncio.gather(first, second)

        assert first_result == second_result == ("response", {"total_tokens": 1})


# ---------------------------------------------------------------------------
# Adapter initialization
# ---------------------------------------------------------------------------


class TestAdapterInit:
    def test_default_config(self):
        config = PlatformConfig(enabled=True)
        adapter = APIServerAdapter(config)
        assert adapter._host == "127.0.0.1"
        assert adapter._port == 8642
        assert adapter._api_key == ""
        assert adapter.platform == Platform.API_SERVER

    def test_custom_config_from_extra(self):
        config = PlatformConfig(
            enabled=True,
            extra={
                "host": "0.0.0.0",
                "port": 9999,
                "key": "sk-test",
                "cors_origins": ["http://localhost:3000"],
            },
        )
        adapter = APIServerAdapter(config)
        assert adapter._host == "0.0.0.0"
        assert adapter._port == 9999
        assert adapter._api_key == "sk-test"
        assert adapter._cors_origins == ("http://localhost:3000",)


    def test_create_agent_forwards_runtime_config(self, monkeypatch):
        captured = {}

        class FakeAgent:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
        monkeypatch.setattr(
            "gateway.run._resolve_runtime_agent_kwargs",
            lambda: {
                "provider": "openai-codex",
                "base_url": "https://example.test/v1",
                "api_mode": "codex_responses",
            },
        )
        monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "gpt-5.5")
        monkeypatch.setattr(
            "gateway.run._load_gateway_config",
            lambda: {
                "agent": {"reasoning_effort": "xhigh"},
                "checkpoints": {
                    "enabled": True,
                    "max_snapshots": 7,
                    "max_total_size_mb": 321,
                    "max_file_size_mb": 4,
                },
            },
        )
        monkeypatch.setattr(
            "gateway.run.GatewayRunner._load_reasoning_config",
            staticmethod(lambda model="": {"enabled": True, "effort": "xhigh"}),
        )
        monkeypatch.setattr("gateway.run.GatewayRunner._load_fallback_model", staticmethod(lambda: None))
        monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *_: set())

        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)

        agent = adapter._create_agent(session_id="api-session")

        assert isinstance(agent, FakeAgent)
        assert captured["reasoning_config"] == {"enabled": True, "effort": "xhigh"}
        assert captured["checkpoints_enabled"] is True
        assert captured["checkpoint_max_snapshots"] == 7
        assert captured["checkpoint_max_total_size_mb"] == 321
        assert captured["checkpoint_max_file_size_mb"] == 4


class TestAdapterDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_closes_cached_session_dbs(self):
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        first_db = MagicMock()
        second_db = MagicMock()
        adapter._session_dbs = {
            "profile-a": first_db,
            "profile-b": second_db,
            "profile-alias": first_db,
        }

        await adapter.disconnect()

        first_db.close.assert_called_once_with()
        second_db.close.assert_called_once_with()
        assert adapter._session_dbs == {}


# ---------------------------------------------------------------------------
# Auth checking
# ---------------------------------------------------------------------------


class TestAuth:
    def test_no_key_configured_allows_all(self):
        config = PlatformConfig(enabled=True)
        adapter = APIServerAdapter(config)
        mock_request = MagicMock()
        mock_request.headers = {}
        assert adapter._check_auth(mock_request) is None


    def test_non_ascii_bearer_token_returns_401_not_500(self):
        """A non-ASCII byte in the bearer token must be rejected with 401, not
        crash the handler: hmac.compare_digest raises TypeError on a str with
        non-ASCII characters, and the token is raw client input."""
        config = PlatformConfig(enabled=True, extra={"key": "sk-test123"})
        adapter = APIServerAdapter(config)
        mock_request = MagicMock()
        mock_request.headers = {"Authorization": "Bearer skÃ©-not-the-key"}
        result = adapter._check_auth(mock_request)  # must not raise
        assert result is not None
        assert result.status == 401


# ---------------------------------------------------------------------------
# Concurrency cap (gateway.api_server.max_concurrent_runs) â€” #7483
# ---------------------------------------------------------------------------


class TestConcurrencyCap:

    def test_resolve_reads_config_value(self):
        cfg = {"gateway": {"api_server": {"max_concurrent_runs": 3}}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            assert APIServerAdapter._resolve_max_concurrent_runs() == 3


    def test_under_cap_returns_none(self):
        adapter = _make_adapter()
        adapter._max_concurrent_runs = 5
        adapter._inflight_agent_runs = 2
        assert adapter._concurrency_limited_response() is None

    def test_at_cap_returns_429_with_retry_after(self):
        adapter = _make_adapter()
        adapter._max_concurrent_runs = 3
        adapter._inflight_agent_runs = 3
        resp = adapter._concurrency_limited_response()
        assert resp is not None
        assert resp.status == 429
        assert resp.headers.get("Retry-After")


# ---------------------------------------------------------------------------
# Helpers for HTTP tests
# ---------------------------------------------------------------------------


def _make_adapter(api_key: str = "", cors_origins=None) -> APIServerAdapter:
    """Create an adapter with optional API key."""
    extra = {}
    if api_key:
        extra["key"] = api_key
    if cors_origins is not None:
        extra["cors_origins"] = cors_origins
    config = PlatformConfig(enabled=True, extra=extra)
    return APIServerAdapter(config)


def _create_app(adapter: APIServerAdapter) -> web.Application:
    """Create the aiohttp app from the adapter (without starting the full server)."""
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_get("/health/detailed", adapter._handle_health_detailed)
    app.router.add_get("/v1/health", adapter._handle_health)
    app.router.add_get("/v1/models", adapter._handle_models)
    app.router.add_get("/api/model/options", adapter._handle_model_options)
    app.router.add_get("/v1/capabilities", adapter._handle_capabilities)
    app.router.add_get("/v1/skills", adapter._handle_skills)
    app.router.add_get("/v1/toolsets", adapter._handle_toolsets)
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post("/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream)
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    app.router.add_get("/v1/responses/{response_id}", adapter._handle_get_response)
    app.router.add_delete("/v1/responses/{response_id}", adapter._handle_delete_response)
    app.router.add_post(
        "/api/platforms/{platform}/events",
        adapter._handle_platform_event_callback,
    )
    return app


class _FakeGoogleChatAdapter:
    def __init__(self, *, verify_ok: bool = True, verify_code: str = ""):
        self.verify_ok = verify_ok
        self.verify_code = verify_code
        self.dispatched = []

    def verify_http_event_request(self, auth_header: str):
        self.auth_header = auth_header
        return self.verify_ok, self.verify_code

    async def dispatch_http_event(self, payload):
        self.dispatched.append(payload)
        return {"ok": True}


@pytest.fixture
def adapter():
    return _make_adapter()


@pytest.fixture
def auth_adapter():
    return _make_adapter(api_key="sk-secret")


# ---------------------------------------------------------------------------
# Adapter internals
# ---------------------------------------------------------------------------


class TestAgentExecution:
    @pytest.mark.asyncio
    async def test_run_agent_uses_session_id_as_task_id(self, adapter):
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent.session_prompt_tokens = 1
        mock_agent.session_completion_tokens = 2
        mock_agent.session_total_tokens = 3

        model_options = {"reasoning": {"enabled": False}, "fast": False}
        with patch.object(adapter, "_create_agent", return_value=mock_agent) as mock_create_agent:
            result, usage = await adapter._run_agent(
                user_message="hello",
                conversation_history=[],
                session_id="session-123",
                requested_model="MiniMax-M3",
                requested_provider="minimax",
                model_options=model_options,
            )

        # _run_agent annotates result with the effective agent.session_id
        # when it's a real string, so the response-header writer can track
        # compression-triggered session rotations (#16938). The mock agent
        # here doesn't set an explicit session_id string so the guard skips
        # the annotation â€” header will fall back to the provided session_id.
        assert result["final_response"] == "ok"
        assert usage == {"input_tokens": 1, "output_tokens": 2, "toßMyîÚ$z{-®éÜj××6V7&WB"æ÷B–â§6öâæGV×2†FF  ¦6Æ72FW7DÖöFVÅ&÷WFW4†æFÆW'3 ¢—FW7BæÖ&²æ7–æ6–ğ¢7–æ2FVbFW7Eö6†Eö6ö×ÆWF–öç5÷76W5÷&÷WFU÷Fõ÷'VåövVçB‡6VÆb“ ¢&÷WFW2Ò²&Ö–æ–Ö‚ÖÓ"#¢²&ÖöFVÂ#¢&Ö–æ–Ö‚öÖ–æ–Ö‚ÖÓ"Â'&÷f–FW"#¢&÷Vç&÷WFW"'×Ğ¢FFW"ÒöÖ¶U÷&÷WF–æuöFFW"‡&÷WFW2¢Òö7&VFUö†FFW"¢7–æ2v—F‚FW7D6Æ–VçB…FW7E6W'fW"†’’26Æ“ ¢v—F‚F6‚æö&¦V7B†FFW"Â%÷'VåövVçB"ÂæWuö6ÆÆ&ÆSÔ7–æ4Öö6²’2Öö6µ÷'Vã ¢Öö6µ÷'Vâç&WGW&å÷fÇVRÒ€¢²&f–æÅ÷&W7öç6R#¢&†’"Â&ÖW76vW2#¢µÒÂ&•ö6ÆÇ2#¢ÒÀ¢²&–çWE÷Fö¶Vç2#¢RÂ&÷WGWE÷Fö¶Vç2#¢RÂ'F÷FÅ÷Fö¶Vç2#¢ÒÀ¢¢&W7Òv—B6Æ’ç÷7B‚"÷cö6†Bö6ö×ÆWF–öç2"Â§6öã×°¢&ÖöFVÂ#¢&Ö–æ–Ö‚ÖÓ""À¢&ÖW76vW2#¢·²'&öÆR#¢'W6W""Â&6öçFVçB#¢&†VÆÆò'ÕÒÀ¢Ò¢76W'B&W7ç7FGW2ÓÒ# ¢·v&w2ÒÖö6µ÷'Vâæ6ÆÅö&w2æ·v&w0¢76W'B·v&w2ævWB‚'&÷WFR"’ÓÒ°¢&ÖöFVÂ#¢&Ö–æ–Ö‚öÖ–æ–Ö‚ÖÓ"Â'&÷f–FW"#¢&÷Vç&÷WFW""À¢Ğ  ¦6Æ72FW7DÖöFVÅ&÷WFW4vVçD7&VF–öã  ¢FVbFW7E÷&÷WFU÷&÷f–FW%÷&W6öÇfW5÷&÷f–FW%ö7&VFVçF–Ç2‡6VÆbÂÖöæ¶W—F6‚“ ¢6GW&VBÒ·Ğ ¢6Æ72f¶TvVçC ¢FVbõö–æ—Eõò‡6VÆbÂ¢¦·v&w2“ ¢6GW&VBçWFFR†·v&w2 ¢÷F6…ö7&VFUövVçE÷'VçF–ÖR†Ööæ¶W—F6‚Â6GW&VBÂf¶TvVçB¢Ööæ¶W—F6‚ç6WFGG"€¢&vFWv’ç'Vâå÷&W6öÇfU÷'VçF–ÖUövVçEö·v&w5öf÷%÷&÷f–FW""À¢ÆÖ&F&÷f–FW#¢°¢'&÷f–FW"#¢&÷f–FW"À¢&•ö¶W’#¢b'6²×·&÷f–FW'Ò"À¢&&6U÷W&Â#¢b&‡GG3¢ò÷·&÷f–FW'ÒæW†×ÆR÷c"À¢&•öÖöFR#¢&6†Eö6ö×ÆWF–öç2"À¢ÒÀ¢¢FFW"ÒöÖ¶U÷&÷WF–æuöFFW"€¢²&Æ–2#¢²&ÖöFVÂ#¢&÷F†W"öÖöFVÂ"Â'&÷f–FW"#¢&÷F†W'&÷b'×Ğ¢¢Ööæ¶W—F6‚ç6WFGG"†FFW"Â%öVç7W&U÷6W76–öåöF""ÂÆÖ&F¢æöæR¢Ööæ¶W—F6‚ç6WFGG"†FFW"Â%÷6W76–öåöÖöFVÅö÷fW'&–FUöf÷""ÂÆÖ&F¥ó¢æöæR ¢FFW"åö7&VFUövVçB‡6W76–öåö–CÒ'3"Â&÷WFSÖFFW"å÷&W6öÇfU÷&÷WFR‚&Æ–2"’ ¢76W'B6GW&VE²&ÖöFVÂ%ÒÓÒ&÷F†W"öÖöFVÂ ¢76W'B6GW&VE²'&÷f–FW"%ÒÓÒ&÷F†W'&÷b ¢76W'B6GW&VE²&•ö¶W’%ÒÓÒ'6²Ö÷F†W'&÷b   ¢FVbFW7E÷6W76–öåöÖöFVÅö÷fW'&–FUö&VG5÷&÷WFR‡6VÆbÂÖöæ¶W—F6‚“ ¢""$W6W"Ö—77VVBöÖöFVÂöâF†R6W76–öâ×W7Bv–â÷fW"7FF–2&÷WFR6öæf–râ"" ¢6GW&VBÒ·Ğ ¢6Æ72f¶TvVçC ¢FVbõö–æ—Eõò‡6VÆbÂ¢¦·v&w2“ ¢6GW&VBçWFFR†·v&w2 ¢÷F6…ö7&VFUövVçE÷'VçF–ÖR†Ööæ¶W—F6‚Â6GW&VBÂf¶TvVçB¢FFW"ÒöÖ¶U÷&÷WF–æuöFFW"‡²&Æ–2#¢²&ÖöFVÂ#¢'&÷WFRöÖöFVÂ"Â&•ö¶W’#¢'6²×&÷WFR'×Ò¢Ööæ¶W—F6‚ç6WFGG"†FFW"Â%öVç7W&U÷6W76–öåöF""ÂÆÖ&F¢æöæR¢Ööæ¶W—F6‚ç6WFGG"€¢FFW"À¢%÷6W76–öåöÖöFVÅö÷fW'&–FUöf÷""À¢ÆÖ&F¶W“¢°¢&ÖöFVÂ#¢'6W76–öâö÷fW'&–FRÖÖöFVÂ"À¢'&÷f–FW"#¢'6W76–öç&÷b"À¢&•ö¶W’#¢'6²×6W76–öâ"À¢&&6U÷W&Â#¢&‡GG3¢ò÷6W76–öâæW†×ÆR÷c"À¢&•öÖöFR#¢'&W7öç6W2"À¢&7&VFVçF–Å÷ööÂ#¢'ööÂ×6W76–öâ"À¢ÒÀ¢ ¢FFW"åö7&VFUövVçB‡6W76–öåö–CÒ'3"Â&÷WFSÖFFW"å÷&W6öÇfU÷&÷WFR‚&Æ–2"’ ¢76W'B6GW&VE²&ÖöFVÂ%ÒÓÒ'6W76–öâö÷fW'&–FRÖÖöFVÂ ¢76W'B6GW&VE²'&÷f–FW"%ÒÓÒ'6W76–öç&÷b ¢76W'B6GW&VE²&•ö¶W’%ÒÓÒ'6²×6W76–öâ   ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ¢2WfVçBÖÆö÷öffÆöF–ærf÷"7–æ6‡&öæ÷W26W76–öäD"6ÆÇ2…¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ  ¦6Æ72FW7E6W76–öäF$öfdWfVçDÆö÷ ¢""%&Vw&W76–öã¢7–æ6‡&öæ÷W26W76–öäD"6ÆÇ2–âF†R÷Vä’Ö6ö×F–&ÆR¢6W'fW"×W7B'VâôdbF†R–ö‡GGWfVçBÆö÷â&Æö6¶–ær5Æ—FR&VB÷w&—FRöà¢F†RÆö÷g&VW¦W2WfW'’–âÖfÆ–v‡B&WVW7BVæFW"ÆöB‡6ÖR6Æ72öb'Vr0¢vFWv’'V–ÆEö6†ææVÅöF—&V7F÷'’Â3cs“Bò3cƒ’Â6òV6‚6ÆÂ—2w&V@¢–â7–æ6–òçFõ÷F‡&VBà¢""  ¢—FW7BæÖ&²æ7–æ6–ğ¢7–æ2FVbFW7EövWEöW†—7F–æu÷6W76–öåö÷%óCEööffÆöG2‡6VÆbÂWF…öFFW"“ ¢–×÷'BF‡&VF–æp ¢6GW&VBÒ·Ğ ¢6Æ72f¶TD# ¢FVbvWE÷6W76–öâ‡6VÆbÂ6W76–öåö–B“ ¢6GW&VE²'F‡&VB%ÒÒF‡&VF–æræ7W'&VçE÷F‡&VB‚¢&WGW&â²&–B#¢6W76–öåö–BÂ'6÷W&6R#¢&•÷6W'fW"'Ğ ¢WF…öFFW"å÷6W76–öåöF"Òf¶TD"‚¢6W76–öâÂW'"Òv—BWF…öFFW"åövWEöW†—7F–æu÷6W76–öåö÷%óCB‚'6W72×‚"¢76W'BW'"—2æöæP¢76W'B6W76–öå²&–B%ÒÓÒ'6W72×‚ ¢2F†R&Æö6¶–ærD"6ÆÂ×W7BäõBW†V7WFRöâF†RWfVçBÖÆö÷F‡&VBà¢76W'B6GW&VE²'F‡&VB%Ò—2æ÷BæöæP¢76W'B6GW&VE²'F‡&VB%ÒÒF‡&VF–æræ7W'&VçE÷F‡&VB‚  ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ¢2ö•ö¶W•÷76W5÷7F'GWöwV&B(	Bf–ÂÖ6Æ÷6VBöââVçfW&–f–&ÆR¶W¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ ¦6Æ72FW7D”¶W•7F'GWwV&Df–Ç46Æ÷6VC ¢""%F†RwV&B—2F†RöæÇ’F†–ær&WGvVVâwVW76&ÆR¶W’æBâVæGö–çBF†P¢6öFR—G6VÆbFW67&–&W22FW&Ö–æÂÖ6&ÆRvVçBv÷&¶v†W&R&wVW76&ÆP¢¶W’—2&VÖ÷FR6öFRW†V7WF–öâ"à ¢6ò'F†R7G&VæwF‚6†V6²6÷VÆBæ÷B&R'Vâ"×W7BæWfW"&W6öÇfRFò'7F'@¢ç—v’"(	BF†R6ÖR÷7GW&RFööÇ2ö7&VFVçF–Åöf–ÆW2ç–F¶W2v†Vâ—G0¢FVç’ÖÆ—7B6ææ÷B&R6öç7VÇFVBà¢""  ¢6Æ72õ7GV# ¢æÖRÒ&•÷6W'fW" ¢ö†÷7BÒ#ããã  ¢FVbõö–æ—Eõò‡6VÆbÂ¶W’“ ¢6VÆbåö•ö¶W’Ò¶W ¢7FF–6ÖWF†ö@¢FVböwV&B†¶W’“ ¢&WGW&â•6W'fW$FFW"åö•ö¶W•÷76W5÷7F'GWöwV&B€¢FW7D”¶W•7F'GWwV&Df–Ç46Æ÷6VBåõ7GV"†¶W’¢ ¢7FF–6ÖWF†ö@¢FVbö&Æö6¶–æuöWF…ö–×÷'B‚“ ¢&VÅö–×÷'BÒõö–×÷'Eõğ ¢FVbö&Æö6¶VB†æÖRÂ¦&w2Â¢¦·v&w2“ ¢–bæÖRÓÒ&†W&ÖW5ö6Æ’æWF‚# ¢&—6R–×÷'DW'&÷"‚'6–×VÆFVC¢†W&ÖW5ö6Æ’æWF‚Væf–Æ&ÆR"¢&WGW&â&VÅö–×÷'B†æÖRÂ¦&w2Â¢¦·v&w2 ¢&WGW&âF6‚‚&'V–ÇF–ç2åõö–×÷'Eõò"Âö&Æö6¶VB ¢FVbFW7E÷vVµö¶W•÷&VgW6VE÷v†Våö6†V6µö—5÷Væf–Æ&ÆR‡6VÆb“ ¢""%F†R'Vs¢âVæ–×÷'F&ÆRWF‚ÖöGVÆR6–ÆVçFÇ’G&÷VBF†R6†V6²æ@¢F†R6W'fW"7F'FVBöâBÖ6†&7FW"¶W’â"" ¢v—F‚6VÆbåö&Æö6¶–æuöWF…ö–×÷'B‚“ ¢76W'B6VÆbåöwV&B‚'FW7B"’—2fÇ6P ¢FVbFW7E÷7G&öæuö¶W•öÇ6õ÷&VgW6VE÷v†Våö6†V6µö—5÷Væf–Æ&ÆR‡6VÆb“ ¢""$f–ÂÖ6Æ÷6VC¢vR6ææ÷BfW&–g’F†R¶W’Â6òvRFòæ÷BW‡÷6RF†P¢VæGö–çB(	BF†RÆörFVÆÇ2F†R÷W&F÷"Fò&W—"F†R–ç7FÆÂâ"" ¢v—F‚6VÆbåö&Æö6¶–æuöWF…ö–×÷'B‚“ ¢76W'B6VÆbåöwV&B‚&"¢C’—2fÇ6P  ¦6Æ72FW7D¶W•&V¦V7F–öå6WG4æöå&WG'–&ÆTfFÄW'&÷# ¢""$V6‚7F'GWÖwV&B&V¦V7F–öâ×W7B6WBæöâ×&WG'–&ÆRfFÂW'&÷"6ğ¢F†R&V6öææV7BvF6†W"G&÷2F†RÆFf÷&Òg&öÒF†R&WG'’VWVR–ç7FVBö`¢Æö÷–ær–æFVf–æ—FVÇ’à ¢&Wf–÷W6Ç’6öææV7B‚’&WGW&æVB&&RfÇ6VÂv†–6‚vFWv’ç'VâG&VFV@¢2&WG'–&ÆR(	B&R×VWVV–ærWfW'’&6¶öfb–çFW'fÂf÷&WfW"æ@¢&RÖ–ç7FçF–F–ærF†RFFW"‡v—F‚—G2&W7öç6U7F÷&R7Æ—FR6öææV7F–öâ¢V6‚&WG'’‚33ƒƒ3¢ãSÆV¶VB6öææV7F–öç2ò"fG2÷fW""ãRF—2À¢VæF–ær–âTÔd”ÄRf÷"F†Rv†öÆRvFWv’’âÖ—'&÷'2F†R÷'BÖ6öæfÆ–7@¢&V6VFVçB‡FW7E÷÷'Eö6öæfÆ–7E÷6WG5öæöå÷&WG'–&ÆUöfFÅöW'&÷"Â3cSccR’à¢""  ¢7FF–6ÖWF†ö@¢FVböÖ¶UöFFW"†¶W’ÂÖöæ¶W—F6‚“ ¢Ööæ¶W—F6‚æFVÆVçb‚$•õ4U%dU%ô´U’"Â&—6–æsÔfÇ6R¢&WGW&â•6W'fW$FFW"€¢ÆFf÷&Ô6öæf–r€¢Væ&ÆVCÕG'VRÀ¢W‡G&×²&†÷7B#¢##rããã"Â'÷'B#¢Â&¶W’#¢¶W—ÒÀ¢¢ ¢7FF–6ÖWF†ö@¢7–æ2FVbö76W'Eö¶W•÷&V¦V7F–öåö—5öfFÂ†FFW"“ ¢G'“ ¢76W'Bv—BFFW"æ6öææV7B‚’—2fÇ6P¢76W'BFFW"æ†5öfFÅöW'&÷"—2G'VP¢76W'BFFW"æfFÅöW'&÷%÷&WG'–&ÆR—2fÇ6P¢76W'BFFW"æfFÅöW'&÷%ö6öFRÓÒ&•÷6W'fW%ö¶W•ö–çfÆ–B ¢76W'B$•õ4U%dU%ô´U’"–â†FFW"æfFÅöW'&÷%öÖW76vR÷"""¢f–æÆÇ“ ¢v—BFFW"æF—66öææV7B‚ ¢—FW7BæÖ&²æ7–æ6–ğ¢7–æ2FVbFW7EöÖ—76–æuö¶W•÷6WG5öæöå÷&WG'–&ÆUöfFÅöW'&÷"‡6VÆbÂÖöæ¶W—F6‚“ ¢FFW"Ò6VÆbåöÖ¶UöFFW"‚""ÂÖöæ¶W—F6‚¢v—B6VÆbåö76W'Eö¶W•÷&V¦V7F–öåö—5öfFÂ†FFW"  ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ¢2&&RÖÖöFVÂ÷BÖ–âvFR†F—&V7EöÖöFVÅ÷&WVW7G2’f÷"÷&WVW7EövVçEö÷fW'&–FW0¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ  ¦6Æ72FW7DF—&V7DÖöFVÅ&WVW7G4vFS ¢""$&&RÖöFVÆ†æò&÷f–FW&’—2÷BÖ–âöâ÷Vä’Ö6ö×F–&ÆP¢VæGö–çG26òvVæW&–26Æ–VçG2†&F6öF–ær&wBÓFò"¶VWfÆÆ–ær&6²Fğ¢F†RvFWv’FVfVÇB†–FV7&VF—C¢"3##ƒ#R'’×77FWVW"’â""  ¢FVbFW7Eö&&UöÖöFVÅöG&÷VE÷v†VåöF—6ÆÆ÷vVB‡6VÆb“ ¢÷fW'&–FW2Ò÷&WVW7EövVçEö÷fW'&–FW2€¢²&ÖöFVÂ#¢&÷Væ’öwBÓR'ÒÂÆÆ÷uö&&UöÖöFVÃÔfÇ6P¢¢76W'B'&WVW7FVEöÖöFVÂ"æ÷B–â÷fW'&–FW0  ¢FVbFW7EöFFW%öfÆuö÷Eö–â‡6VÆb“ ¢FFW"Ò•6W'fW$FFW"€¢ÆFf÷&Ô6öæf–r†Væ&ÆVCÕG'VRÂW‡G&×²&F—&V7EöÖöFVÅ÷&WVW7G2#¢G'VWÒ¢¢76W'BFFW"åöF—&V7EöÖöFVÅ÷&WVW7G2—2G'VP  ¢—FW7BæÖ&²æ7–æ6–ğ¢7–æ2FVbFW7Eö6†Eö6ö×ÆWF–öç5ö&&UöÖöFVÅö†öæ÷&VE÷v†VåöVæ&ÆVB‡6VÆb“ ¢FFW"Ò•6W'fW$FFW"€¢ÆFf÷&Ô6öæf–r†Væ&ÆVCÕG'VRÂW‡G&×²&F—&V7EöÖöFVÅ÷&WVW7G2#¢G'VWÒ¢¢Òö7&VFUö†FFW"¢7–æ2v—F‚FW7D6Æ–VçB…FW7E6W'fW"†’’26Æ“ ¢v—F‚F6‚æö&¦V7B†FFW"Â%÷'VåövVçB"ÂæWuö6ÆÆ&ÆSÔ7–æ4Öö6²’2Öö6µ÷'Vã ¢Öö6µ÷'Vâç&WGW&å÷fÇVRÒ€¢²&f–æÅ÷&W7öç6R#¢&ö²"Â&ÖW76vW2#¢µÒÂ&•ö6ÆÇ2#¢ÒÀ¢²&–çWE÷Fö¶Vç2#¢Â&÷WGWE÷Fö¶Vç2#¢Â'F÷FÅ÷Fö¶Vç2#¢'ÒÀ¢¢&W7Òv—B6Æ’ç÷7B€¢"÷cö6†Bö6ö×ÆWF–öç2"À¢§6öã×°¢&ÖöFVÂ#¢&÷Væ’öwBÓR"À¢&ÖW76vW2#¢·²'&öÆR#¢'W6W""Â&6öçFVçB#¢&†’'ÕÒÀ¢ÒÀ¢¢76W'B&W7ç7FGW2ÓÒ# ¢76W'BÖö6µ÷'Vâæ6ÆÅö&w2æ·v&w2ævWB‚'&WVW7FVEöÖöFVÂ"’ÓÒ&÷Væ’öwBÓR   ¦6Æ72FW7E&÷WFUv—F†÷WDÖöFVÄ¶VW4FVfVÇC ¢""$ÖöFVÅ÷&÷WFW2Æ–2v†÷6R&÷WFR†2æòÖöFVÆ¶W’×W7B¶VWF†P¢vÆö&ÂFVfVÇBÖöFVÂ(	BF†RÆ–27G&–ær—G6VÆb—2æWfW"ÖöFVÂæÖRâ""  ¢FVbFW7EöÆ–5öæWfW%öÆV·5ö5öÖöFVÂ‡6VÆbÂÖöæ¶W—F6‚“ ¢6GW&VBÒ·Ğ ¢6Æ72f¶TvVçC ¢FVbõö–æ—Eõò‡6VÆbÂ¢¦·v&w2“ ¢6GW&VBçWFFR†·v&w2 ¢÷F6…ö7&VFUövVçE÷'VçF–ÖR†Ööæ¶W—F6‚Â6GW&VBÂf¶TvVçB¢FFW"ÒöÖ¶U÷&÷WF–æuöFFW"€¢²&Æ–2#¢²&ÖöFVÂ#¢""Â&•ö¶W’#¢'6²×&÷WFR'×Ğ¢¢2÷'6UöÖöFVÅ÷&÷WFW2G&÷2&÷WFW2v—F†÷WBÖöFVÃ²6–×VÆFR¢27&VFVçF–Ç2ÖöæÇ’&÷WFR7W'f—f–ærf–F—&V7BF–7B†FVfVç6—fRF‚’à¢FFW"åöÖöFVÅ÷&÷WFW2Ò²&Æ–2#¢²&•ö¶W’#¢'6²×&÷WFR'×Ğ¢Ööæ¶W—F6‚ç6WFGG"†FFW"Â%öVç7W&U÷6W76–öåöF""ÂÆÖ&F¢æöæR¢Ööæ¶W—F6‚ç6WFGG"†FFW"Â%÷6W76–öåöÖöFVÅö÷fW'&–FUöf÷""ÂÆÖ&F¥ó¢æöæR ¢FFW"åö7&VFUövVçB€¢6W76–öåö–CÒ'3"À¢&÷WFSÖFFW"å÷&W6öÇfU÷&÷WFR‚&Æ–2"’À¢&WVW7FVEöÖöFVÃÒ&Æ–2"À¢ ¢76W'B6GW&VE²&ÖöFVÂ%ÒÓÒ&vÆö&ÂöÖöFVÂ ¢76W'B6GW&VE²&•ö¶W’%ÒÓÒ'6²×&÷WFR   ¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ¢2V×G’ÖÖöFVÂ&V6÷fW'’²&÷f–FW"ÖWF‚W'&÷"G—–ær–âö7&VFUövVç@¢2‡6ÇfvVBg&öÒ"3Ss“Cr'’gfår¢2ÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒÒĞ  ¦6Æ72FW7D7&VFTvVçDÖöFVÅ&V6÷fW'“ ¢FVbFW7Eö7&VFUövVçEöFVfVÇG5÷Fõ÷&÷f–FW%ö6FÆöuöÖöFVÅ÷v†VåöV×G’‡6VÆbÂÖöæ¶W—F6‚“ ¢""&•÷6W'fW"ç’†BæòWV—fÆVçBöb'Vâç’w2&÷f–FW"Ö6FÆöp¢FVfVÇBv†VâÖöFVÂ&W6öÇfW2V×G’'WB&÷f–FW"F–B&W6öÇfR†Rærà¢†W&ÖW2WF‚FB÷Væ’Ö6öFW†v—F†÷WB†W&ÖW2ÖöFVÆ’(	@¢”vVçB†ÖöFVÃÒ""’C2WfW'’6ÆÂâ"" ¢6GW&VBÒ·Ğ ¢6Æ72f¶TvVçC ¢FVbõö–æ—Eõò‡6VÆbÂ¢¦·v&w2“ ¢6GW&VBçWFFR†·v&w2 ¢÷F6…ö7&VFUövVçE÷'VçF–ÖR†Ööæ¶W—F6‚Â6GW&VBÂf¶TvVçB¢Ööæ¶W—F6‚ç6WFGG"€¢&vFWv’ç'Vâå÷&W6öÇfU÷'VçF–ÖUövVçEö·v&w2"À¢ÆÖ&F¢²'&÷f–FW"#¢&÷Væ’Ö6öFW‚"Â&&6U÷W&Â#¢&‡GG3¢òöW†×ÆRçFW7B÷c"À¢&•öÖöFR#¢&6öFW…÷&W7öç6W2'ÒÀ¢¢Ööæ¶W—F6‚ç6WFGG"‚&vFWv’ç'Vâå÷&W6öÇfUövFWv•öÖöFVÂ"ÂÆÖ&F¢""¢Ööæ¶W—F6‚ç6WFGG"€¢&†W&ÖW5ö6Æ’æÖöFVÇ2ævWEöFVfVÇEöÖöFVÅöf÷%÷&÷f–FW""À¢ÆÖ&F&÷f–FW#¢&wBÓRãRÖ6öFW‚"–b&÷f–FW"ÓÒ&÷Væ’Ö6öFW‚"VÇ6RæöæRÀ¢ ¢FFW"Ò•6W'fW$FFW"…ÆFf÷&Ô6öæf–r†Væ&ÆVCÕG'VR’¢Ööæ¶W—F6‚ç6WFGG"†FFW"Â%öVç7W&U÷6W76–öåöF""ÂÆÖ&F¢æöæR ¢vVçBÒFFW"åö7&VFUövVçB‡6W76–öåö–CÒ&’×6W76–öâ" ¢76W'B—6–ç7Fæ6R†vVçBÂf¶TvVçB¢76W'B6GW&VE²&ÖöFVÂ%ÒÓÒ&wBÓRãRÖ6öFW‚  ¢FVbFW7Eö7&VFUövVçE÷&V6÷fW'5öÆ7Eö¶æ÷våövööEöÖöFVÅ÷v†VåöV×G’‡6VÆbÂÖöæ¶W—F6‚“ ¢""$Æ7BÖ¶æ÷vâÖvööB&V6÷fW'’‚33S3B“¢G&ç6–VçB6öæf–rÖ66†RÖ—70¢&öGV6–ærâV×G’ÖöFVÂv÷VÆB'V–ÆB”vVçB†ÖöFVÃÒ""’æBf–ÂWfW'¢6ÆÂVçF–ÂÖçVÂ&WG'’Â–ç7FVBöb&WW6–ærF†RÖöFVÂF†B§W7@¢v÷&¶VBâ"" ¢6GW&VBÒµĞ ¢6Æ72f¶TvVçC ¢FVbõö–æ—Eõò‡6VÆbÂ¢¦·v&w2“ ¢6GW&VBæVæB†F–7B†·v&w2’ ¢÷F6…ö7&VFUövVçE÷'VçF–ÖR†Ööæ¶W—F6‚Â·ÒÂf¶TvVçB¢Ööæ¶W—F6‚ç6WFGG"‚''VåövVçBä”vVçB"Âf¶TvVçB ¢FFW"Ò•6W'fW$FFW"…ÆFf÷&Ô6öæf–r†Væ&ÆVCÕG'VR’¢Ööæ¶W—F6‚ç6WFGG"†FFW"Â%öVç7W&U÷6W76–öåöF""ÂÆÖ&F¢æöæR ¢2GW&â¢ÖöFVÂ&W6öÇfW2f–æR(	B÷VÆFW2F†RÆ7BÖ¶æ÷vâÖvööB66†P¢2†¶W–VBöâvFWv•÷6W76–öåö¶W’’à¢Ööæ¶W—F6‚ç6WFGG"‚&vFWv’ç'Vâå÷&W6öÇfUövFWv•öÖöFVÂ"ÂÆÖ&F¢&Ö–æ–Ö‚öÖ–æ–Ö‚ÖÓ2"¢FFW"åö7&VFUövVçB‡6W76–öåö–CÒ&’×6W76–öâ"ÂvFWv•÷6W76–öåö¶W“Ò'7F&ÆRÖ6†âÓ"¢76W'B6GW&VE³Õ²&ÖöFVÂ%ÒÓÒ&Ö–æ–Ö‚öÖ–æ–Ö‚ÖÓ2 ¢76W'BFFW"åöÆ7E÷&W6öÇfVEöÖöFVÅ²'7F&ÆRÖ6†âÓ%ÒÓÒ&Ö–æ–Ö‚öÖ–æ–Ö‚ÖÓ2  ¢2GW&â#¢G&ç6–VçBV×G’&W6öÇWF–öâÂæò&÷f–FW"6FÆörFVfVÇB(	@¢2×W7B&V6÷fW"F†RÖöFVÂg&öÒGW&âÂæ÷B'V–ÆBÖöFVÃÒ""à¢Ööæ¶W—F6‚ç6WFGG"‚&vFWv’ç'Vâå÷&W6öÇfUövFWv•öÖöFVÂ"ÂÆÖ&F¢""¢Ööæ¶W—F6‚ç6WFGG"€¢&vFWv’ç'Vâå÷&W6öÇfU÷'VçF–ÖUövVçEö·v&w2"À¢ÆÖ&F¢²'&÷f–FW"#¢æöæRÂ&&6U÷W&Â#¢æöæRÂ&•öÖöFR#¢æöæWÒÀ¢¢FFW"åö7&VFUövVçB‡6W76–öåö–CÒ&æ÷F†W"×6W76–öâ"ÂvFWv•÷6W76–öåö¶W“Ò'7F&ÆRÖ6†âÓ"¢76W'B6GW&VE³Õ²&ÖöFVÂ%ÒÓÒ&Ö–æ–Ö‚öÖ–æ–Ö‚ÖÓ2   