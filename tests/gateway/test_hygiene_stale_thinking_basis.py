"""Contract: gateway hygiene rough estimates share the stale-thinking wire truth.

#84371 introduced ``stale_thinking_reaches_wire`` as the single wire-truth
predicate shared by the compaction trigger estimator and the tail-budget
walks, and threaded ``charge_stale_thinking`` through the preflight and
pre-API paths. The gateway hygiene path's rough estimates predate that
contract and charged persisted ``reasoning`` / ``reasoning_content`` on every
assistant turn regardless of route. On routes that strip stale thinking at
send time (every non-echo chat-completions endpoint), a reasoning-heavy
session with no recorded real usage inflates the hygiene estimate
several-fold and can fire phantom 85%-threshold compressions.

Two layers pin the contract:

1. Harness tests (``TestHygieneEstimateBasisWired``) drive the real
   ``GatewayRunner._handle_message`` hygiene block — patterned on
   ``test_session_hygiene.py::test_session_hygiene_preserves_transcript_when_no_rotation``
   — with a fake reasoning-heavy transcript on a non-echo route, spying on the
   estimator's ``charge_stale_thinking`` kwarg. Reverting the gateway hunk
   (full charge everywhere) makes these FAIL: the phantom estimate trips the
   hygiene trigger, the transcript gets compressed, and the recorded kwarg
   defaults to True.

2. Estimator-level relation tests (``TestStaleThinkingEstimateRelations``)
   pin the route classification and the like-for-like comparison invariants
   the hygiene block relies on.
"""

import importlib
import sys
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.model_metadata import estimate_messages_tokens_rough
from agent.message_sanitization import stale_thinking_reaches_wire
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionEntry, SessionSource


def _reasoning_heavy_history(n_turns: int = 6, blob: int = 15_000) -> list:
    """History shaped like a high-effort reasoning session: every assistant
    turn carries both the ``reasoning`` and ``reasoning_content`` copies of a
    thinking blob (the double-store observed in live sessions)."""
    history = []
    for i in range(n_turns):
        history.append({"role": "user", "content": f"question {i} " * 20})
        history.append(
            {
                "role": "assistant",
                "content": f"answer {i}",
                "reasoning": "R" * blob,
                "reasoning_content": "R" * blob,
            }
        )
    return history


def _phantom_history(context_length: int = 200_000) -> list:
    """Reasoning-heavy transcript sized so the FULL-CHARGE estimate trips the
    85% hygiene threshold while the LEAN (wire-truth) estimate stays well
    under it — the phantom-compression scenario. 60 turns × 2 × 15,000 blob
    chars of reasoning text on top of a tiny content surface."""
    return _reasoning_heavy_history(n_turns=60, blob=15_000)


class _NoopCompressAgent:
    """Fake AIAgent whose _compress_context refuses to rotate or compact —
    hygiene then runs the agent normally. We only need the estimate path to
    run; compression behavior is out of scope for this contract."""

    last_instance = None

    def __init__(self, **kwargs):
        self.model = kwargs.get("model")
        self.session_id = kwargs.get("session_id", "fake-session")
        self._print_fn = None
        self.shutdown_memory_provider = MagicMock()
        self.close = MagicMock()
        type(self).last_instance = self

    def _compress_context(self, messages, *_args, **_kwargs):
        return (messages, None)


class _CaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="fake-token"), Platform.TELEGRAM)
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append({"chat_id": chat_id, "content": content})
        return SendResult(success=True, message_id="hygiene-1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def _build_runner(monkeypatch, tmp_path, session_store):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _NoopCompressAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: _CaptureAdapter()}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = session_store
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    # Non-echo route facts, resolved before the estimate sites: a custom
    # Ollama-style endpoint. Context large enough that the LEAN estimate of
    # the reasoning-heavy transcript stays under the 85% hygiene threshold,
    # while the FULL-CHARGE estimate (pre-fix behavior) blows past it.
    # NOTE: takes ``self`` — it is set as a GatewayRunner class attribute, so
    # the instance is passed as the first positional argument.
    def _fake_runtime(self, source=None, session_key=None, user_config=None, **_kw):
        return (
            "glm-5.3:cloud",
            {
                "provider": "custom:ollama",
                "base_url": "http://127.0.0.1:11434/v1",
                "api_key": "fake",
                "api_mode": "chat_completions",
            },
        )

    monkeypatch.setattr(
        gateway_run.GatewayRunner, "_resolve_session_agent_runtime", _fake_runtime
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 200_000,
    )
    return gateway_run, runner


def _make_store(history):
    store = MagicMock()
    store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:private:12345",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="private",
    )
    # No recorded real usage → hygiene must use the rough estimate path.
    store.load_transcript.return_value = history
    store.has_any_sessions.return_value = True
    store.rewrite_transcript = MagicMock()
    store.append_to_transcript = MagicMock()
    return store


def _event():
    return MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM, chat_id="12345", chat_type="private", user_id="12345"
        ),
        message_id="1",
    )


class TestHygieneEstimateBasisWired:
    """E2E: the hygiene block must consult the route's wire truth and pass it
    to every rough estimate. These FAIL if the gateway hunk is reverted."""

    @pytest.mark.asyncio
    async def test_non_echo_route_hygiene_uses_lean_basis(self, monkeypatch, tmp_path):
        """A reasoning-heavy transcript on a non-echo route, with no recorded
        real usage, must NOT trip the hygiene trigger: the stale-thinking
        blobs are excluded from the estimate. Pre-fix (full charge), the same
        transcript is several times larger than its wire truth and fires a
        phantom compression."""
        history = _phantom_history()
        lean = estimate_messages_tokens_rough(history, charge_stale_thinking=False)
        full = estimate_messages_tokens_rough(history)
        # Scenario sanity: full charge trips the 85% threshold of 200K,
        # lean does not. (Relation, not a frozen literal.)
        assert full > int(200_000 * 0.85) > lean

        gateway_run, runner = _build_runner(monkeypatch, tmp_path, _make_store(history))

        # Spy on the estimator's basis kwarg inside the hygiene block. The
        # hygiene block imports the estimator from agent.model_metadata at
        # call time (local import), so patch the SOURCE module — the block's
        # late import then resolves to the spy.
        import agent.model_metadata as _mm

        seen_bases = []
        real_estimator = estimate_messages_tokens_rough

        def _spy(messages, **kwargs):
            seen_bases.append(kwargs.get("charge_stale_thinking"))
            return real_estimator(messages, **kwargs)

        monkeypatch.setattr(_mm, "estimate_messages_tokens_rough", _spy)

        result = await runner._handle_message(_event())
        assert result == "ok"
        # The hygiene pass ran and consulted the estimator with the route's
        # wire truth: non-echo → lean basis.
        assert seen_bases, "hygiene must have taken the rough-estimate path"
        assert all(b is False for b in seen_bases), (
            f"non-echo route must exclude stale thinking from hygiene "
            f"estimates, saw bases: {seen_bases}"
        )
        # No phantom compression: transcript untouched.
        runner.session_store.rewrite_transcript.assert_not_called()

    @pytest.mark.asyncio
    async def test_echo_route_hygiene_keeps_full_charge(self, monkeypatch, tmp_path):
        """An echo-back route (DeepSeek thinking) genuinely replays
        reasoning_content on the wire; hygiene must keep charging it, or
        hygiene would silently under-count and delay compression until a
        real provider error."""
        history = _reasoning_heavy_history()
        gateway_run, runner = _build_runner(monkeypatch, tmp_path, _make_store(history))

        def _fake_runtime_echo(self, source=None, session_key=None, user_config=None, **_kw):
            return (
                "deepseek-v4-flash",
                {
                    "provider": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "fake",
                    "api_mode": "chat_completions",
                },
            )

        monkeypatch.setattr(
            gateway_run.GatewayRunner, "_resolve_session_agent_runtime", _fake_runtime_echo
        )

        import agent.model_metadata as _mm

        seen_bases = []
        real_estimator = estimate_messages_tokens_rough

        def _spy(messages, **kwargs):
            seen_bases.append(kwargs.get("charge_stale_thinking"))
            return real_estimator(messages, **kwargs)

        monkeypatch.setattr(_mm, "estimate_messages_tokens_rough", _spy)

        result = await runner._handle_message(_event())
        assert result == "ok"
        assert seen_bases and all(b is True for b in seen_bases), (
            f"echo route must keep the full charge in hygiene estimates, "
            f"saw bases: {seen_bases}"
        )


class TestStaleThinkingEstimateRelations:
    """Estimator-level invariants the hygiene block relies on."""

    def test_non_echo_route_classification(self):
        assert stale_thinking_reaches_wire(
            "chat_completions",
            "custom:ollama",
            "glm-5.3:cloud",
            "http://127.0.0.1:11434/v1",
        ) is False

    def test_echo_route_classification(self):
        assert stale_thinking_reaches_wire(
            "chat_completions",
            "deepseek",
            "deepseek-v4-flash",
            "https://api.deepseek.com/v1",
        ) is True

    def test_codex_responses_never_replay_thinking_text(self):
        assert stale_thinking_reaches_wire(
            "codex_responses", "openai-codex", "gpt-5.6", "https://chatgpt.com/backend-api/codex"
        ) is False

    def test_comparisons_stay_like_for_like(self):
        """Every hygiene comparison (anti-growth guard, post-compression
        counts) must use the SAME basis on both sides; a compression that
        only removed reasoning blobs must still register as progress."""
        history = _reasoning_heavy_history()
        compressed = history[:-2]
        for basis in (True, False):
            before = estimate_messages_tokens_rough(history, charge_stale_thinking=basis)
            after = estimate_messages_tokens_rough(compressed, charge_stale_thinking=basis)
            assert after < before, "dropping turns must shrink the estimate"
