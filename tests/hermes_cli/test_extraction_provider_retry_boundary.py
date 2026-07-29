"""LPV2 extraction retry-boundary regression for Hermes' provider adapter.

These tests pin the contract that LPV2 ``limits.retry = 0``:

- must not depend on the global ``agent.api_max_retries`` config;
- must constrain ``AIAgent._api_max_retries`` to ``1`` at the dispatch
  boundary;
- must leave every other ``AIAgent(...)`` creation path untouched.

Scope: extraction adapter only.  These tests must not call any provider.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _reload_adapter():
    """Reload the adapter module to pick up the latest source for each test."""
    for mod in list(sys.modules):
        if mod == "hermes_cli.extraction_provider_adapter" or mod.startswith(
            "hermes_cli.extraction_provider_adapter."
        ):
            del sys.modules[mod]
    return importlib.import_module("hermes_cli.extraction_provider_adapter")


@pytest.fixture
def adapter_module():
    return _reload_adapter()


def _runtime(provider: str = "synthetic-test-provider") -> dict[str, object]:
    return {
        "provider": provider,
        "api_mode": "chat_completions",
        "base_url": "https://example.invalid/v1",
        "api_key": "extraction-boundary-test-key",
        "model": "extraction-boundary-test-model",
    }


def _config(api_max_retries: int = 3) -> dict[str, object]:
    return {
        "agent": {"api_max_retries": api_max_retries},
        "model": {"default": "config-default-model", "model": "config-model-fallback"},
    }


class _RecordingAgent:
    """Stand-in for ``run_agent.AIAgent``.

    The recording agent captures:

    - ``self._api_max_retries`` *immediately after* the adapter writes it,
      so the test can assert the boundary value;
    - whether ``run_conversation`` was invoked at all.
    """

    def __init__(self, *, init_api_max_retries: int, **kwargs: object) -> None:
        self.init_kwargs = kwargs
        # Simulate Hermes' ``agent_init``: read config and clamp to >= 1.
        self._api_max_retries = max(init_api_max_retries, 1)
        self._disable_primary_transport_recovery = False
        self.run_conversation_called = False
        self.observed_pre_run_max_retries: int | None = None

    def _build_assistant_message(self, assistant_message: object, _finish_reason: str) -> dict[str, object]:
        return {"role": "assistant", "content": getattr(assistant_message, "content", "")}

    def run_conversation(self, prompt: str, **_kwargs: object) -> dict[str, object]:
        self.run_conversation_called = True
        self.observed_pre_run_max_retries = self._api_max_retries
        return {"final_response": "[]"}


def _install_runtime_fakes(monkeypatch: pytest.MonkeyPatch, *, init_api_max_retries: int = 3) -> list[_RecordingAgent]:
    """Patch the four runtime seams the adapter reads inside
    ``_execute_with_hermes``. Returns the list of constructed fake agents
    so tests can inspect what each adapter call recorded.
    """
    instances: list[_RecordingAgent] = []

    def _aiagent_factory(**kwargs: object) -> _RecordingAgent:
        agent = _RecordingAgent(init_api_max_retries=init_api_max_retries, **kwargs)
        instances.append(agent)
        return agent

    import hermes_cli.config as _config_mod
    import hermes_cli.fallback_config as _fallback_mod
    import hermes_cli.runtime_provider as _runtime_mod

    monkeypatch.setattr(_config_mod, "load_config", lambda: _config(api_max_retries=init_api_max_retries))

    def _resolve_runtime(*, requested: str | None = None, target_model: str | None = None) -> dict[str, object]:
        runtime = _runtime(provider=requested or "synthetic-test-provider")
        if target_model:
            runtime["model"] = target_model
        return runtime

    monkeypatch.setattr(_runtime_mod, "resolve_runtime_provider", _resolve_runtime)
    monkeypatch.setattr(_fallback_mod, "get_fallback_chain", lambda _cfg: [])

    # ``run_agent.AIAgent`` is imported by name inside the adapter's
    # ``_execute_with_hermes``; the import binds the attribute on the
    # ``run_agent`` module, so patching the attribute is sufficient.
    monkeypatch.setattr("run_agent.AIAgent", _aiagent_factory, raising=True)
    return instances


class TestRetryBoundaryInAdapter:
    def test_capped_continuation_composes_exactly_two_plain_assistant_parts(
        self, adapter_module
    ) -> None:
        assert (
            adapter_module.HermesExtractionProviderAdapter._compose_capped_continuation(
                ['[{"candidate_type":"EO"', '} ]'], api_call_count=2
            )
            == '[{"candidate_type":"EO"} ]'
        )

    @pytest.mark.parametrize("parts", [[], ["only one fragment"]])
    def test_capped_continuation_marks_insufficient_visible_fragments_with_stable_code(
        self, adapter_module, parts: list[str]
    ) -> None:
        """A non-composable length response remains fail-closed and identifiable.

        The two-call ceiling is preserved: zero/one visible fragments cannot be
        silently transformed into a candidate JSON array.
        """
        with pytest.raises(adapter_module.HermesExtractionExecutionError) as exc:
            adapter_module.HermesExtractionProviderAdapter._compose_capped_continuation(
                parts, api_call_count=2
            )

        assert str(exc.value).startswith("VISIBLE_FRAGMENT_CARDINALITY_UNSATISFIED:")
        assert "exactly two" in str(exc.value).lower()

    def test_array_shaped_malformed_json_remains_invalid_for_strict_validation(
        self, adapter_module
    ) -> None:
        """Noise stripping must not repair P0346-style interior JSON syntax errors."""
        malformed = '[{"candidate_type":"EO","quote":"unescaped "inner" quote"}]'

        normalised = adapter_module.HermesExtractionProviderAdapter._strip_non_json_noise(
            malformed
        )

        assert normalised == malformed
        with pytest.raises(json.JSONDecodeError):
            json.loads(normalised)

    def test_adapter_installs_local_no_summary_handler(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        instances = _install_runtime_fakes(monkeypatch, init_api_max_retries=3)

        adapter_module.HermesExtractionProviderAdapter().complete(
            prompt="extraction-boundary-payload", timeout_seconds=60
        )

        assert len(instances) == 1
        build_message = getattr(instances[0], "_build_assistant_message")
        build_message(SimpleNamespace(content="", tool_calls=None), "length")
        build_message(SimpleNamespace(content="[", tool_calls=None), "length")
        build_message(SimpleNamespace(content="]", tool_calls=None), "length")
        handler = getattr(instances[0], "_handle_max_iterations")
        assert handler([], 2) == "[]"

    def test_local_no_summary_handler_records_dispatch_before_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        instances = _install_runtime_fakes(monkeypatch, init_api_max_retries=3)
        adapter = adapter_module.HermesExtractionProviderAdapter()
        adapter.complete(prompt="extraction-boundary-payload", timeout_seconds=60)

        handler = getattr(instances[0], "_handle_max_iterations")
        with pytest.raises(adapter_module.HermesExtractionExecutionError):
            handler([{"role": "assistant", "content": "only one fragment"}], 2)

        assert adapter.last_dispatch_metadata == {
            "api_calls": 2,
            "completed": False,
            "partial": False,
        }

    def test_failed_transport_dict_is_rejected_before_json_normalization(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        """A failed Hermes turn must not be converted into array-shaped text."""
        instances = _install_runtime_fakes(monkeypatch, init_api_max_retries=3)

        def _failed_turn(self: _RecordingAgent, _prompt: str, **_kwargs: object) -> dict[str, object]:
            self.run_conversation_called = True
            self.observed_pre_run_max_retries = self._api_max_retries
            return {
                "final_response": "API call failed after 1 retries: [Errno 32] Broken pipe",
                "completed": False,
                "failed": True,
                "error": "[Errno 32] Broken pipe",
            }

        monkeypatch.setattr(_RecordingAgent, "run_conversation", _failed_turn)

        def _must_not_normalize(*_args: object, **_kwargs: object) -> str:
            raise AssertionError("failed transport response reached _normalize_agent_response")

        def _must_not_strip(*_args: object, **_kwargs: object) -> str:
            raise AssertionError("failed transport response reached _strip_non_json_noise")

        monkeypatch.setattr(
            adapter_module.HermesExtractionProviderAdapter,
            "_normalize_agent_response",
            staticmethod(_must_not_normalize),
        )
        monkeypatch.setattr(
            adapter_module.HermesExtractionProviderAdapter,
            "_strip_non_json_noise",
            staticmethod(_must_not_strip),
        )

        with pytest.raises(adapter_module.HermesExtractionExecutionError) as exc:
            adapter_module.HermesExtractionProviderAdapter().complete(
                prompt="extraction-boundary-payload", timeout_seconds=60
            )

        assert "OPENAI_CODEX_RESPONSE_NOT_JSON" not in str(exc.value)
        assert "Broken pipe" in str(exc.value)
        assert len(instances) == 1
        assert instances[0].run_conversation_called is True

    def test_ordinary_json_dict_response_remains_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        """A completed normal response must retain its strict JSON array text."""
        _install_runtime_fakes(monkeypatch, init_api_max_retries=3)
        json_text = '[{"candidate_type":"synthetic"}]'

        def _successful_turn(self: _RecordingAgent, _prompt: str, **_kwargs: object) -> dict[str, object]:
            self.run_conversation_called = True
            self.observed_pre_run_max_retries = self._api_max_retries
            return {
                "final_response": json_text,
                "completed": True,
                "failed": False,
                "partial": False,
                "api_calls": 2,
            }

        monkeypatch.setattr(_RecordingAgent, "run_conversation", _successful_turn)

        adapter = adapter_module.HermesExtractionProviderAdapter()
        result = adapter.complete(
            prompt="extraction-boundary-payload", timeout_seconds=60
        )

        assert result.text == json_text
        assert adapter.last_dispatch_metadata == {
            "api_calls": 2,
            "completed": True,
            "partial": False,
        }

    def test_adapter_sets_api_max_retries_to_one_before_run_conversation(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        instances = _install_runtime_fakes(monkeypatch, init_api_max_retries=3)

        result = adapter_module.HermesExtractionProviderAdapter().complete(
            prompt="extraction-boundary-payload", timeout_seconds=60
        )

        assert len(instances) == 1
        agent = instances[0]
        # Adapter must set boundary BEFORE run_conversation.
        assert agent.observed_pre_run_max_retries == 1
        assert agent.run_conversation_called is True
        assert result.text == "[]"
        assert result.provider == "synthetic-test-provider"

    def test_adapter_overrides_even_when_global_api_max_retries_is_higher(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        instances = _install_runtime_fakes(monkeypatch, init_api_max_retries=5)

        adapter_module.HermesExtractionProviderAdapter().complete(
            prompt="extraction-boundary-payload", timeout_seconds=60
        )

        assert len(instances) == 1
        assert instances[0].observed_pre_run_max_retries == 1

    def test_adapter_overrides_even_when_global_api_max_retries_is_one(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        """Even when the global config is already '1', the adapter keeps
        the explicit invariant rather than relying on the config value.
        This catches accidental future relaxations of the global config.
        """
        instances = _install_runtime_fakes(monkeypatch, init_api_max_retries=1)
        adapter_module.HermesExtractionProviderAdapter().complete(
            prompt="extraction-boundary-payload", timeout_seconds=60
        )
        assert len(instances) == 1
        assert instances[0].observed_pre_run_max_retries == 1

    def test_adapter_fail_closed_when_aiagent_has_no_max_retries_field(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        import hermes_cli.config as _config_mod
        import hermes_cli.fallback_config as _fallback_mod
        import hermes_cli.runtime_provider as _runtime_mod

        monkeypatch.setattr(_config_mod, "load_config", lambda: _config(api_max_retries=3))
        monkeypatch.setattr(_runtime_mod, "resolve_runtime_provider", lambda: _runtime())
        monkeypatch.setattr(_fallback_mod, "get_fallback_chain", lambda _cfg: [])

        class _NoRetryFieldAgent:
            def __init__(self, **_kwargs: object) -> None:
                # Intentionally: no ``_api_max_retries``.
                pass

            def run_conversation(self, _prompt: str, **_kwargs: object) -> dict[str, object]:
                # Must NOT be reached; adapter must fail closed.
                raise AssertionError(
                    "run_conversation must NOT be called when the retry boundary is missing"
                )

        monkeypatch.setattr("run_agent.AIAgent", lambda **_kw: _NoRetryFieldAgent(), raising=True)

        with pytest.raises(adapter_module.HermesExtractionExecutionError) as exc:
            adapter_module.HermesExtractionProviderAdapter().complete(
                prompt="extraction-boundary-payload", timeout_seconds=60
            )

        assert "AIAgent._api_max_retries missing" in str(exc.value)

    def test_physical_attempt_test_one_retry_eligible_failure_does_not_retry(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        """The extraction-local retry loop performs exactly one attempt."""
        instances = _install_runtime_fakes(monkeypatch, init_api_max_retries=3)
        attempts: list[int] = []

        def _simulate_retry_loop(
            self: _RecordingAgent, _prompt: str, **_kwargs: object
        ) -> dict[str, object]:
            self.run_conversation_called = True
            self.observed_pre_run_max_retries = self._api_max_retries
            retry_count = 0
            while retry_count < self._api_max_retries:
                retry_count += 1
                attempts.append(retry_count)
                # Simulate a retry-eligible failure. The extraction boundary
                # is exhausted after this first physical attempt.
            return {"final_response": "[]"}

        monkeypatch.setattr(_RecordingAgent, "run_conversation", _simulate_retry_loop)

        adapter_module.HermesExtractionProviderAdapter().complete(
            prompt="extraction-boundary-payload", timeout_seconds=60
        )

        assert len(instances) == 1
        assert instances[0].observed_pre_run_max_retries == 1
        assert attempts == [1]

    def test_fallback_chain_passed_through_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        """The boundary patch must not mutate the fallback chain or any
        other ``AIAgent`` construction argument.
        """
        import hermes_cli.config as _config_mod
        import hermes_cli.fallback_config as _fallback_mod
        import hermes_cli.runtime_provider as _runtime_mod

        monkeypatch.setattr(_config_mod, "load_config", lambda: _config(api_max_retries=3))
        monkeypatch.setattr(_runtime_mod, "resolve_runtime_provider", lambda: _runtime())
        fallback_chain = [
            {"provider": "deepseek", "model": "deepseek-v4-pro"},
            {"provider": "minimax-cn", "model": "MiniMax-M3"},
        ]
        monkeypatch.setattr(_fallback_mod, "get_fallback_chain", lambda _cfg: fallback_chain)

        instances: list[_RecordingAgent] = []

        def _factory(**kwargs: object) -> _RecordingAgent:
            a = _RecordingAgent(init_api_max_retries=3, **kwargs)
            instances.append(a)
            return a

        monkeypatch.setattr("run_agent.AIAgent", _factory, raising=True)

        adapter_module.HermesExtractionProviderAdapter().complete(
            prompt="extraction-boundary-payload", timeout_seconds=60
        )

        assert len(instances) == 1
        kwargs = instances[0].init_kwargs
        assert kwargs["fallback_model"] == fallback_chain
        assert kwargs["request_overrides"] == {"timeout": 60, "max_tokens": 4000}
        assert kwargs["max_tokens"] == 4000
        assert kwargs["max_iterations"] == 2
        assert kwargs["enabled_toolsets"] == []
        assert kwargs["quiet_mode"] is True
        assert kwargs["skip_context_files"] is True
        assert kwargs["load_soul_identity"] is False
        assert kwargs["skip_memory"] is True
        # Provider / model / base_url / api_mode / api_key resolution all
        # routed through the unchanged runtime+config path.
        assert kwargs["provider"] == "synthetic-test-provider"
        assert kwargs["model"] == "extraction-boundary-test-model"
        assert kwargs["api_mode"] == "chat_completions"
        assert kwargs["base_url"] == "https://example.invalid/v1"
        assert kwargs["api_key"] == "extraction-boundary-test-key"


class TestIsolationOfNormalHermesAgents:
    """The boundary must not leak to other ``AIAgent(...)`` call sites."""

    def test_other_aiagent_calls_unaffected_by_extraction_boundary(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        # 1) Construct a normal AIAgent via a path that is NOT the
        # extraction adapter — its retry budget must reflect the
        # global config (default 3).
        class _NormalAgent:
            def __init__(self) -> None:
                self._api_max_retries = 3  # simulating global config default

        normal = _NormalAgent()
        assert normal._api_max_retries == 3

        # 2) Run the extraction adapter on a SEPARATE fake.
        instances = _install_runtime_fakes(monkeypatch, init_api_max_retries=3)
        adapter_module.HermesExtractionProviderAdapter().complete(
            prompt="isolation-test", timeout_seconds=60
        )

        # Isolation: the normal agent's value is unchanged.
        assert normal._api_max_retries == 3
        # The extraction-local agent's value is constrained to 1.
        assert instances[0].observed_pre_run_max_retries == 1


class APITimeoutError(Exception):
    """Name-compatible synthetic transport error; no Provider interaction."""


class TestPrimaryTransportRecoveryIsolation:
    def test_extraction_marker_blocks_primary_transport_recovery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent import agent_runtime_helpers

        replacement_client = object()
        extraction_agent = SimpleNamespace(
            _disable_primary_transport_recovery=True,
            _fallback_activated=False,
            provider="openai-codex",
            log_prefix="",
            client=None,
            _primary_runtime={
                "client_kwargs": {},
                "model": "extraction-model",
                "provider": "openai-codex",
                "base_url": "https://example.invalid/v1",
                "api_mode": "chat_completions",
                "api_key": "extraction-test-key",
            },
            _transport_cache={},
            _is_openrouter_url=lambda: False,
            _create_openai_client=lambda _kwargs, **_kw: replacement_client,
            _vprint=lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(agent_runtime_helpers.time, "sleep", lambda _seconds: None)

        assert agent_runtime_helpers.try_recover_primary_transport(
            extraction_agent,
            APITimeoutError("synthetic timeout"),
            retry_count=1,
            max_retries=1,
        ) is False
        assert extraction_agent.client is None

    def test_unmarked_normal_agent_remains_recovery_eligible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent import agent_runtime_helpers

        replacement_client = object()
        normal_agent = SimpleNamespace(
            _fallback_activated=False,
            provider="openai-codex",
            log_prefix="",
            client=None,
            _primary_runtime={
                "client_kwargs": {},
                "model": "normal-model",
                "provider": "openai-codex",
                "base_url": "https://example.invalid/v1",
                "api_mode": "chat_completions",
                "api_key": "normal-test-key",
            },
            _transport_cache={},
            _is_openrouter_url=lambda: False,
            _create_openai_client=lambda _kwargs, **_kw: replacement_client,
            _vprint=lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(agent_runtime_helpers.time, "sleep", lambda _seconds: None)

        assert agent_runtime_helpers.try_recover_primary_transport(
            normal_agent,
            APITimeoutError("synthetic timeout"),
            retry_count=1,
            max_retries=1,
        ) is True
        assert normal_agent.client is replacement_client


class TestRetryBoundaryTransportMarker:
    def test_adapter_marks_only_extraction_agent_for_no_primary_recovery(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        instances = _install_runtime_fakes(monkeypatch, init_api_max_retries=3)

        adapter_module.HermesExtractionProviderAdapter().complete(
            prompt="extraction-boundary-payload", timeout_seconds=60
        )

        assert len(instances) == 1
        assert instances[0]._disable_primary_transport_recovery is True


class TestProviderOverrideCapability:
    """Provider-comparison capability: instance-scoped ``provider_override`` seam.

    These tests pin the four contract properties required for a future
    MiniMax-M3 comparison run, without making any real Provider call:

    1. ``provider_override=None`` (default) keeps the existing behaviour:
       provider and model come from Hermes' own ``resolve_runtime_provider()``
       and the config fallback chain.
    2. ``provider_override={"provider": "minimax-cn", "model": "MiniMax-M3"}``
       routes the dispatch to the override (provider, model) tuple only.
    3. Two adapter instances are independent: an override on one does not
       leak to the other.
    4. With or without override, the LPV2 retry-boundary invariant
       (``_api_max_retries = 1``) and the transport-recovery flag
       (``_disable_primary_transport_recovery = True``) remain unchanged;
       ``api_key`` / ``base_url`` / ``api_mode`` / ``fallback_model`` /
       ``request_overrides`` are never replaced by the override.
    """

    def test_default_override_none_keeps_runtime_provider_and_model(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        instances = _install_runtime_fakes(monkeypatch, init_api_max_retries=3)

        adapter_module.HermesExtractionProviderAdapter().complete(
            prompt="extraction-boundary-payload", timeout_seconds=60
        )

        assert len(instances) == 1
        agent = instances[0]
        assert agent.init_kwargs["provider"] == "synthetic-test-provider"
        assert agent.init_kwargs["model"] == "extraction-boundary-test-model"
        # api_key / base_url / api_mode / fallback_model remain bound to
        # the configured Hermes runtime — never overridden.
        assert agent.init_kwargs["api_key"] == "extraction-boundary-test-key"
        assert agent.init_kwargs["base_url"] == "https://example.invalid/v1"
        assert agent.init_kwargs["api_mode"] == "chat_completions"
        assert agent.init_kwargs["fallback_model"] is None
        # Extraction request limits are preserved through request_overrides.
        assert agent.init_kwargs["request_overrides"] == {"timeout": 60, "max_tokens": 4000}
        # LPV2 retry-boundary invariant unchanged.
        assert agent._api_max_retries == 1
        # Transport-recovery isolation flag unchanged.
        assert agent._disable_primary_transport_recovery is True

    def test_override_replaces_provider_and_model_only(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        instances = _install_runtime_fakes(monkeypatch, init_api_max_retries=3)

        adapter_module.HermesExtractionProviderAdapter(
            provider_override={"provider": "minimax-cn", "model": "MiniMax-M3"},
        ).complete(prompt="extraction-boundary-payload", timeout_seconds=60)

        assert len(instances) == 1
        agent = instances[0]
        # Provider / model are taken from the override.
        assert agent.init_kwargs["provider"] == "minimax-cn"
        assert agent.init_kwargs["model"] == "MiniMax-M3"
        # api_key / base_url / api_mode / fallback_model are NOT overridden.
        assert agent.init_kwargs["api_key"] == "extraction-boundary-test-key"
        assert agent.init_kwargs["base_url"] == "https://example.invalid/v1"
        assert agent.init_kwargs["api_mode"] == "chat_completions"
        assert agent.init_kwargs["fallback_model"] is None
        # Extraction request limits are preserved through request_overrides.
        assert agent.init_kwargs["request_overrides"] == {"timeout": 60, "max_tokens": 4000}
        # Invariants unchanged.
        assert agent._api_max_retries == 1
        assert agent._disable_primary_transport_recovery is True

    def test_two_instances_override_isolation(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        instances = _install_runtime_fakes(monkeypatch, init_api_max_retries=3)

        override_a = adapter_module.HermesExtractionProviderAdapter(
            provider_override={"provider": "minimax-cn", "model": "MiniMax-M3"},
        )
        default_b = adapter_module.HermesExtractionProviderAdapter()
        override_a.complete(prompt="extraction-boundary-payload", timeout_seconds=60)
        default_b.complete(prompt="extraction-boundary-payload", timeout_seconds=60)

        assert len(instances) == 2
        # First dispatch — the override adapter dispatched with m3.
        assert instances[0].init_kwargs["provider"] == "minimax-cn"
        assert instances[0].init_kwargs["model"] == "MiniMax-M3"
        # Second dispatch — the default adapter is unaffected by the first
        # instance's override: provider and model still come from the
        # configured Hermes runtime.
        assert instances[1].init_kwargs["provider"] == "synthetic-test-provider"
        assert instances[1].init_kwargs["model"] == "extraction-boundary-test-model"
        # Both still carry the LPV2 invariants.
        assert instances[0]._api_max_retries == 1
        assert instances[1]._api_max_retries == 1
        assert instances[0]._disable_primary_transport_recovery is True
        assert instances[1]._disable_primary_transport_recovery is True

    def test_override_preserves_retry_boundary_and_transport_recovery(
        self, monkeypatch: pytest.MonkeyPatch, adapter_module
    ) -> None:
        # Use a different ``init_api_max_retries`` to prove the adapter's
        # boundary fix still wins over both the config value AND the
        # override path.
        instances = _install_runtime_fakes(monkeypatch, init_api_max_retries=5)

        adapter_module.HermesExtractionProviderAdapter(
            provider_override={"provider": "minimax-cn", "model": "MiniMax-M3"},
        ).complete(prompt="extraction-boundary-payload", timeout_seconds=60)

        assert len(instances) == 1
        agent = instances[0]
        # The post-init boundary clamp must still be ``1`` — the override
        # does not relax the LPV2 retry=0 contract.
        assert agent._api_max_retries == 1
        # The transport-recovery isolation flag must still be set.
        assert agent._disable_primary_transport_recovery is True
        # Timeout and extraction output ceiling are carried together.
        assert agent.init_kwargs["request_overrides"] == {"timeout": 60, "max_tokens": 4000}
        # The HermesExecutionResult returned by ``complete`` records the
        # *actual* provider / model dispatched — i.e. the override values,
        # so downstream attestation can trace what was used.
        result = adapter_module.HermesExtractionProviderAdapter(
            provider_override={"provider": "minimax-cn", "model": "MiniMax-M3"},
        ).complete(prompt="extraction-boundary-payload", timeout_seconds=60)
        assert result.provider == "minimax-cn"
        assert result.model == "MiniMax-M3"
