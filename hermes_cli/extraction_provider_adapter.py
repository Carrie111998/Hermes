"""Narrow provider-execution facade for contract-owned extraction workloads.

This module owns provider resolution and fallback execution.  Callers supply a
fully prepared text prompt and receive plain response text plus the resolved
runtime attribution.  It deliberately owns no extraction schema, prompt
construction, provider registry, or evidence persistence.

LPV2 extraction invariant
-------------------------
LPV2 contracts declare ``limits.retry = 0``.  The Hermes runtime exposes the
app-level retry budget as ``AIAgent._api_max_retries``; ``agent_init`` reads
the global ``agent.api_max_retries`` config (default ``3``) and clamps
non-positive values to ``1`` (single attempt, no retry).  This adapter maps
LPV2's ``retry = 0`` to ``_api_max_retries = 1`` *at the dispatch boundary*
so a global config value of ``3`` cannot escalate an LPV2 extraction into
multiple app-level Hermes API attempts.

Scope: extraction adapter only.  Other AIAgent creation paths and the global
config are intentionally untouched.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


_EXTRACTION_SYSTEM_INSTRUCTION = "Jianke V2.2 semantic executor. Follow the user payload exactly."

# LPV2 ``limits.retry = 0`` → exactly one Hermes app-level API attempt.
# ``agent_init`` clamps ``0`` → ``1`` (single attempt, no app-level retry), so
# this constant matches the Hermes-runtime interpretation of zero retries.
_EXTRACTION_APP_RETRY_BUDGET = 1

# P3 STRICT-328 Retry-Batch-001 / Master-authorized effective α (2026-07-25):
# pass an explicit output ceiling to the actual provider request rather than
# merely carrying a budget value inside the serialized Jianke payload.  This
# is scoped to extraction-only AIAgent instances created below; normal Hermes
# conversations and all non-extraction runtime paths remain unchanged.
_EXTRACTION_MAX_TOKENS = 4000

# A length-truncated extraction response is continued once by the same
# provider. Hermes counts this as a second conversation iteration, not an
# SDK/app retry. The adapter replaces the per-instance max-iteration summary
# handler with a strict two-part combiner, so two is the exact ceiling:
# initial request + one continuation; no fallback, summary, or third request
# are permitted here.
_EXTRACTION_MAX_ITERATIONS = 2

# This is a terminal execution state, not a provider response.  Keep the
# machine-readable prefix stable so an FCE future runner can preserve the
# fail-closed taxonomy without sending terminal prose to JSON validation.
_FCE_CONTINUATION_EXHAUSTED = "FCE_CONTINUATION_EXHAUSTED"

# Provider-neutral extraction output contract — applied to every Hermes resolved
# provider so the assistant's response conforms to Jianke's strict JSON array
# contract regardless of underlying provider dialect (minimax-cn anthropic_messages,
# openai-codex, deepseek chat_completions, gemini, etc.). This block is appended
# to the user prompt verbatim; the prompt shaping remains Jianke-owned and the
# contract remains the caller's responsibility.
_EXTRACTION_OUTPUT_INSTRUCTION = (
    "\n--- EXTRACTION OUTPUT CONTRACT (provider-neutral) ---\n"
    "You MUST respond with exactly one of the following two outputs and nothing else:\n"
    "  (a) a strict JSON array (e.g. `[{\"k\":\"v\"}]`), possibly empty: `[]`\n"
    "  (b) the literal token [] when there are no candidates.\n"
    "Forbidden in your response: Markdown, prose explanations, code fences, headings,\n"
    "trailing commentary, greetings, preamble, or any non-JSON content.\n"
    "The response MUST be parseable by Python json.loads with no preprocessing.\n"
    "If the input payload contains no eligible candidates, return [] and nothing else.\n"
    "Do not return null, do not return a JSON object, do not add any wrapper.\n"
    "----- END EXTRACTION OUTPUT CONTRACT -----\n"
)


class HermesExtractionExecutionError(RuntimeError):
    """Hermes could not execute one extraction completion."""


@dataclass(frozen=True)
class HermesExecutionResult:
    """Provider-neutral response returned to a contract-owning caller."""

    text: str
    provider: str
    model: str
    execution_id: str
    attempt_count: int


class HermesExtractionProviderAdapter:
    """Execute one prepared extraction prompt through Hermes' provider chain.

    ``executor`` exists only as a deterministic test seam.  Production calls
    use the configured Hermes primary runtime and configured fallback chain via
    :class:`run_agent.AIAgent`; this adapter itself has no retry or fallback
    loop.

    ``provider_override`` is an **instance-scoped** capability comparison seam.
    When ``None`` (the default) the adapter behaves exactly as before — provider
    and model come from Hermes' own ``resolve_runtime_provider()``.  When set,
    only the (provider, model) pair is taken from the override; the dispatch
    still goes through Hermes' existing agent so the extraction invariant
    (single attempt, no transport recovery, 60s timeout) is preserved
    unchanged.  Override scope is the **single adapter instance**: it does
    not mutate any module global, thread-local, or fork-shared state, and
    does not affect any other adapter instance.
    """

    def __init__(
        self,
        *,
        executor: Callable[..., HermesExecutionResult] | None = None,
        provider_override: dict[str, str] | None = None,
    ) -> None:
        # Validate override shape eagerly so a misconfigured caller fails at
        # construction time, not at the dispatch boundary.  We accept a dict
        # (not a tuple) to keep the call site self-documenting.
        normalised_override: tuple[str, str] | None = None
        if provider_override is not None:
            if not isinstance(provider_override, dict):
                raise HermesExtractionExecutionError(
                    "provider_override must be a dict with 'provider' and 'model' keys, "
                    "got %s" % type(provider_override).__name__
                )
            provider = str(provider_override.get("provider") or "").strip()
            model = str(provider_override.get("model") or "").strip()
            if not provider or not model:
                raise HermesExtractionExecutionError(
                    "provider_override requires non-empty 'provider' and 'model' "
                    "(got provider=%r, model=%r)" % (provider, model)
                )
            normalised_override = (provider, model)
        self._provider_override = normalised_override
        self._executor = executor or self._execute_with_hermes
        # Best-effort runtime facts for the adjacent retry evidence writer.
        # This remains in-memory only; callers must treat absent fields as
        # unknown rather than inferring a provider-call count.
        self.last_dispatch_metadata: dict[str, Any] | None = None

    def complete(self, *, prompt: str, timeout_seconds: int) -> HermesExecutionResult:
        if not isinstance(prompt, str) or not prompt.strip():
            raise HermesExtractionExecutionError("extraction prompt must be a non-empty string")
        if timeout_seconds != 60:
            raise HermesExtractionExecutionError("extraction timeout must be exactly 60 seconds")
        result = self._executor(prompt=prompt, timeout_seconds=timeout_seconds)
        if not isinstance(result, HermesExecutionResult):
            raise HermesExtractionExecutionError("Hermes extraction executor returned an invalid result")
        if not result.text.strip() or not result.provider.strip() or not result.model.strip() or not result.execution_id.strip():
            raise HermesExtractionExecutionError("Hermes extraction executor returned incomplete attribution")
        return result

    @staticmethod
    def _wrap_prompt_with_extraction_contract(prompt: str) -> str:
        """Append the provider-neutral extraction output contract to a frozen Jianke prompt.

        The original Jianke payload body (instruction / schema_version /
        instruction_hash / source_hash / run_id / parsed_sections /
        output_contract) is preserved verbatim — only an output-contract tail
        is appended so every Hermes resolved provider receives the same strict
        output expectation. Wrapping happens here, in the Hermes boundary
        adapter, so neither Jianke's frozen prompt nor the validation contract
        need to change.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise HermesExtractionExecutionError("extraction prompt must be a non-empty string")
        return prompt.rstrip("\n") + "\n" + _EXTRACTION_OUTPUT_INSTRUCTION

    @staticmethod
    def _compose_capped_continuation(parts: list[str], *, api_call_count: int) -> str:
        """Join the exact two visible extraction fragments or fail closed."""
        if len(parts) != 2 or any(not isinstance(part, str) or not part for part in parts):
            raise HermesExtractionExecutionError(
                "VISIBLE_FRAGMENT_CARDINALITY_UNSATISFIED: expected exactly two visible fragments "
                f"(api_call_count={api_call_count}, observed={len(parts)})"
            )
        return "".join(parts)

    @staticmethod
    def _normalize_agent_response(text: Any) -> str:
        """Normalize ``AIAgent.run_conversation(...)`` into a plain string.

        Some Hermes AIAgent paths return::

            {
              "final_response": "...",
              "last_reasoning": ...,
              "messages": [...],
            }

        while legacy / minimal agents return ``str``.  This helper extracts
        the contracted text content (``final_response``) for the dict case
        and keeps legacy str returns identity-preserving.  The downstream
        flow (:meth:`_strip_non_json_noise` then caller-side validation)
        remains the only contract authority — this helper only shapes the
        transport boundary.
        """
        # Legacy str path: identity preserving
        if isinstance(text, str):
            return text
        # Dict path used by AIAgent.run_conversation under anthropic_messages / minimax-cn
        if isinstance(text, dict):
            if not text:
                raise HermesExtractionExecutionError(
                    "Hermes returned an empty extraction response: AIAgent produced an empty dict"
                )
            if "final_response" not in text:
                raise HermesExtractionExecutionError(
                    "Hermes returned an extraction response without 'final_response' "
                    "(keys=%s); cannot normalize" % sorted(text.keys())
                )
            final = text.get("final_response")
            if not isinstance(final, str):
                raise HermesExtractionExecutionError(
                    "Hermes 'final_response' field must be a string, got %s"
                    % type(final).__name__
                )
            if not final.strip():
                raise HermesExtractionExecutionError(
                    "Hermes returned an empty extraction response (final_response is empty)"
                )
            return final
        # Any other container / unexpected type — fail closed with a clear signal.
        raise HermesExtractionExecutionError(
            "Hermes returned an unsupported response type: %s" % type(text).__name__
        )

    @staticmethod
    def _raise_for_failed_agent_response(response: Any) -> None:
        """Fail closed before a Hermes failure envelope reaches JSON handling."""
        if not isinstance(response, dict):
            return
        final_response = response.get("final_response")
        is_transport_failure = (
            response.get("completed") is False
            and isinstance(final_response, str)
            and any(
                marker in final_response.lower()
                for marker in (
                    "api call failed",
                    "broken pipe",
                    "connection",
                    "readerror",
                    "timeout",
                    "transport",
                )
            )
        )
        if response.get("failed") is True or "error" in response or is_transport_failure:
            detail = response.get("error") or final_response or "unknown Hermes runtime failure"
            raise HermesExtractionExecutionError("Hermes extraction transport failure: %s" % detail)

    @staticmethod
    def _strip_non_json_noise(text: str) -> str:
        """Best-effort extraction of a JSON array out of a provider response.

        MiniMax, Gemini, DeepSeek, and Codex frequently wrap the JSON
        array in code fences (` ```json ... ``` `), leading prose, trailing
        commentary, or both. This helper is intentionally permissive:
        it returns the substring that begins with ``[`` and ends with ``]``
        when one is present, otherwise it returns the original text so
        downstream validation can report a real contract failure.

        No silent re-mapping: the result is what we hand to caller. If the
        response contained no JSON array at all, the original text passes
        through unchanged and the contract check fails honestly.
        """
        if not isinstance(text, str):
            return ""
        stripped = text.strip()
        if not stripped:
            return stripped
        # Strip the most common Markdown code fences if present.
        if stripped.startswith("```"):
            newline = stripped.find("\n")
            if newline != -1:
                stripped = stripped[newline + 1:]
            fence_end = stripped.rfind("```")
            if fence_end != -1:
                stripped = stripped[:fence_end]
            stripped = stripped.strip()
        first_open = stripped.find("[")
        last_close = stripped.rfind("]")
        if first_open != -1 and last_close != -1 and last_close > first_open:
            candidate = stripped[first_open:last_close + 1]
            return candidate.strip()
        return text


    @staticmethod
    def _resolved_provider(runtime: dict[str, Any]) -> str:
        resolved = str(runtime.get("provider") or "").strip()
        if not resolved:
            raise HermesExtractionExecutionError(
                "Hermes resolved provider is missing (resolve_runtime_provider() returned an empty provider)"
            )
        return resolved

    @staticmethod
    def _resolved_model(runtime: dict[str, Any], config: dict[str, Any]) -> str:
        """Resolve the model using the strict runtime-first, config-second priority.

        Priority:
          1. ``runtime["model"]`` (already a resolved value from Hermes)
          2. ``config["model"]["default"]``
          3. ``config["model"]["model"]``

        ``provider``, ``api_mode``, ``base_url``, and ``api_key`` remain bound to
        ``resolve_runtime_provider()`` exclusively; only ``model`` falls back to the
        Hermes config defaults because ``resolve_runtime_provider()`` historically
        does not populate the ``model`` field for non-OpenAI providers (e.g.
        ``minimax-cn`` with ``api_mode=anthropic_messages``).
        """
        runtime_model = str(runtime.get("model") or "").strip()
        if runtime_model:
            return runtime_model
        model_config = config.get("model")
        if isinstance(model_config, dict):
            for key in ("default", "model"):
                candidate = str(model_config.get(key) or "").strip()
                if candidate:
                    return candidate
        elif isinstance(model_config, str):
            candidate = model_config.strip()
            if candidate:
                return candidate
        raise HermesExtractionExecutionError(
            "Hermes resolved model is missing: runtime model is empty and "
            "config['model.default']/'config['model.model']' are also unset; "
            "binding scope metadata must not override Hermes runtime resolution"
        )

    @staticmethod
    def _resolved_api_mode(runtime: dict[str, Any]) -> str:
        resolved = str(runtime.get("api_mode") or "").strip()
        if not resolved:
            raise HermesExtractionExecutionError("Hermes resolved api_mode is missing")
        return resolved

    @staticmethod
    def _resolved_base_url(runtime: dict[str, Any]) -> str:
        resolved = str(runtime.get("base_url") or "").strip()
        if not resolved:
            raise HermesExtractionExecutionError("Hermes resolved base_url is missing")
        return resolved

    @staticmethod
    def _resolved_api_key(runtime: dict[str, Any]) -> str:
        resolved = str(runtime.get("api_key") or "").strip()
        if not resolved:
            raise HermesExtractionExecutionError("Hermes resolved api_key is missing")
        return resolved

    def _execute_with_hermes(self, *, prompt: str, timeout_seconds: int) -> HermesExecutionResult:
        """Delegate one completion to Hermes' existing agent/fallback runtime.

        The caller-supplied ``prompt`` (a Jianke frozen payload) is wrapped
        with the provider-neutral extraction output contract by
        :meth:`_wrap_prompt_with_extraction_contract` immediately before
        dispatch; the assistant's raw response is normalised through
        :meth:`_strip_non_json_noise`. The contract check (parseable JSON
        array, possibly ``[]``) remains the caller's responsibility.

        When ``self._provider_override`` is set, resolve the complete runtime
        binding for that named provider (provider, model, API mode, endpoint,
        and credential) in this instance only.  Extraction overrides are
        explicitly no-fallback: cross-provider fallback would make the
        evidence claim one provider while the request ran on another.
        """
        try:
            from hermes_cli.config import load_config
            from hermes_cli.fallback_config import get_fallback_chain
            from hermes_cli.runtime_provider import resolve_runtime_provider
            from run_agent import AIAgent

            config = load_config()
            override_provider = None
            override_model = None
            if self._provider_override is not None:
                override_provider, override_model = self._provider_override
                runtime = resolve_runtime_provider(
                    requested=override_provider,
                    target_model=override_model,
                )
            else:
                runtime = resolve_runtime_provider()
            resolved_provider = self._resolved_provider(runtime)
            resolved_model = self._resolved_model(runtime, config)
            resolved_api_mode = self._resolved_api_mode(runtime)
            resolved_base_url = self._resolved_base_url(runtime)
            resolved_api_key = self._resolved_api_key(runtime)
            if override_provider is not None:
                if resolved_provider != override_provider:
                    raise HermesExtractionExecutionError(
                        "provider override binding drift: "
                        f"expected={override_provider!r}, actual={resolved_provider!r}"
                    )
                resolved_model = str(override_model)
                fallback_model = None
            else:
                fallback_model = get_fallback_chain(config) or None
            wrapped_prompt = self._wrap_prompt_with_extraction_contract(prompt)
            agent = AIAgent(
                provider=resolved_provider,
                model=resolved_model,
                base_url=resolved_base_url,
                api_key=resolved_api_key,
                api_mode=resolved_api_mode,
                fallback_model=fallback_model,
                max_iterations=_EXTRACTION_MAX_ITERATIONS,
                enabled_toolsets=[],
                quiet_mode=True,
                skip_context_files=True,
                load_soul_identity=False,
                skip_memory=True,
                ephemeral_system_prompt=_EXTRACTION_SYSTEM_INSTRUCTION,
                # AIAgent.max_tokens is the provider-neutral route used by
                # both Anthropic Messages (MiniMax) and chat-completions
                # (DeepSeek).  Keep the same value in request_overrides for
                # OpenAI-compatible transports, where overrides are merged
                # last into the wire request.
                max_tokens=_EXTRACTION_MAX_TOKENS,
                request_overrides={
                    "timeout": timeout_seconds,
                    **(
                        {}
                        if resolved_api_mode == "codex_responses"
                        else {"max_tokens": _EXTRACTION_MAX_TOKENS}
                    ),
                },
            )
            def _handle_max_iterations(messages: list[dict[str, Any]], api_call_count: int) -> str:
                parts = [
                    str(message.get("content") or "")
                    for message in messages
                    if isinstance(message, dict) and message.get("role") == "assistant"
                ]
                if not parts:
                    return "[]"
                try:
                    return self._compose_capped_continuation(parts, api_call_count=api_call_count)
                except HermesExtractionExecutionError:
                    self.last_dispatch_metadata = {
                        "api_calls": api_call_count,
                        "completed": False,
                        "partial": False,
                    }
                    raise

            agent._handle_max_iterations = _handle_max_iterations
            # LPV2 extraction invariant: ``limits.retry = 0`` → one app-level
            # Hermes API attempt regardless of the global ``agent.api_max_retries``
            # config value. The global config is read but not modified: only this
            # adapter-local agent instance is constrained for the duration of
            # this single dispatch. Provider routing and the fallback chain
            # resolved above are intentionally untouched.
            if not hasattr(agent, "_api_max_retries"):
                raise HermesExtractionExecutionError(
                    "Hermes runtime does not expose the extraction retry boundary "
                    "expected by LPV2 (AIAgent._api_max_retries missing)"
                )
            agent._api_max_retries = _EXTRACTION_APP_RETRY_BUDGET
            # LPV2 extraction must not turn one exhausted app-level attempt
            # into a rebuilt-client primary transport recovery attempt.
            # This marker is read only by ``try_recover_primary_transport``
            # and exists only on this adapter-created agent instance.
            agent._disable_primary_transport_recovery = True
            started = time.monotonic()
            raw_response = agent.run_conversation(wrapped_prompt)
            self.last_dispatch_metadata = {
                "api_calls": (
                    raw_response.get("api_calls")
                    if isinstance(raw_response, dict)
                    else None
                ),
                "completed": (
                    raw_response.get("completed")
                    if isinstance(raw_response, dict)
                    else None
                ),
                "partial": (
                    raw_response.get("partial")
                    if isinstance(raw_response, dict)
                    else None
                ),
            }
            self._raise_for_failed_agent_response(raw_response)
            text = self._normalize_agent_response(raw_response)
            _ = started  # execution timing remains Hermes-owned; no evidence schema change.
            normalised = self._strip_non_json_noise(text)
            return HermesExecutionResult(
                text=normalised,
                provider=resolved_provider,
                model=resolved_model,
                execution_id="hermes-exec-" + uuid.uuid4().hex,
                attempt_count=int(getattr(agent, "_fallback_index", 0)) + 1,
            )
        except HermesExtractionExecutionError:
            raise
        except Exception as exc:
            raise HermesExtractionExecutionError(str(exc)) from exc


__all__ = [
    "HermesExecutionResult",
    "HermesExtractionExecutionError",
    "HermesExtractionProviderAdapter",
]
