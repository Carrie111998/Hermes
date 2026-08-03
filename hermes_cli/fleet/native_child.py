"""Isolated native-provider fleet worker.

The parent owns process lifetime, CWD, timeout, and environment isolation.
This module owns exact runtime resolution and emits one strict JSON envelope.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


if __package__ in {None, ""}:
    # Direct execution by an absolute path keeps TaskSpec.cwd as the process
    # CWD without depending on an inherited PYTHONPATH.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


_REQUEST_FIELDS = frozenset({"provider_id", "model", "effort", "prompt"})
_OAUTH_PROVIDERS = frozenset({"openai-codex", "xai-oauth"})


class _BoundedSink(io.TextIOBase):
    """Absorb noisy imports/provider output without retaining secret text."""

    def __init__(self, limit: int = 4096) -> None:
        self.limit = limit
        self.count = 0

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        size = len(value)
        self.count = min(self.limit, self.count + size)
        return size


def _runtime_identity(
    runtime: Mapping[str, Any], expected_provider: str
) -> tuple[str, str]:
    """Derive subscription identity from the resolved runtime, never a token."""

    provider = runtime.get("provider")
    requested = runtime.get("requested_provider")
    api_mode = runtime.get("api_mode")
    pool = runtime.get("credential_pool")
    pool_provider = (
        pool.get("provider")
        if isinstance(pool, Mapping)
        else getattr(pool, "provider", None)
    )
    source = runtime.get("source")
    if (
        expected_provider not in _OAUTH_PROVIDERS
        or provider != expected_provider
        or requested != expected_provider
        or api_mode != "codex_responses"
        or pool_provider != expected_provider
        or not isinstance(source, str)
        or not source
    ):
        raise RuntimeError("resolved runtime is not an attributable OAuth route")
    return "oauth_subscription", f"{provider}:oauth_subscription"


def execute_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve and execute one exact native request inside this child."""

    if set(request) != _REQUEST_FIELDS:
        raise ValueError("invalid native child request envelope")
    provider = request.get("provider_id")
    model = request.get("model")
    effort = request.get("effort")
    prompt = request.get("prompt")
    if not all(
        isinstance(value, str) and value
        for value in (provider, model, effort)
    ):
        raise ValueError("provider, model, and effort must be non-empty strings")
    if not isinstance(prompt, str):
        raise ValueError("prompt must be a string")

    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_constants import parse_reasoning_effort
    from run_agent import AIAgent

    runtime = resolve_runtime_provider(requested=provider, target_model=model)
    auth_kind, auth_source = _runtime_identity(runtime, provider)
    agent = AIAgent(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=provider,
        api_mode=runtime.get("api_mode"),
        credential_pool=runtime.get("credential_pool"),
        model=model,
        reasoning_config=parse_reasoning_effort(effort),
        service_tier=None,
        fallback_model=None,
        quiet_mode=True,
        platform="subagent",
        skip_context_files=False,
        skip_memory=False,
    )
    try:
        result = agent.run_conversation(prompt)
        output = result.get("final_response")
        if not isinstance(output, str):
            raise RuntimeError("native child returned no final response")
        return {
            "ok": True,
            "provider_id": provider,
            "model_id": model,
            "effort": effort,
            "auth_kind": auth_kind,
            "auth_source": auth_source,
            "fallback_enabled": False,
            "fast_mode": False,
            "output": output,
        }
    finally:
        agent.close()


def main() -> int:
    real_stdout = sys.stdout
    sink = _BoundedSink()
    try:
        raw = sys.stdin.read()
        request = json.loads(raw)
        if not isinstance(request, Mapping):
            raise ValueError("native child request must be an object")
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            payload = execute_request(request)
    except Exception:
        real_stdout.write(
            json.dumps(
                {"ok": False, "error": "native_child_failed"},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 1
    real_stdout.write(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
