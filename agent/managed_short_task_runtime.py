"""Minimal inert runtime objects for verified managed short-task workers."""

from __future__ import annotations

import base64
import json
import os
from urllib.parse import urlparse
from typing import Any


class NullCheckpointManager:
    """Checkpoint-compatible no-op without importing Git/checkpoint code."""

    enabled = False

    def new_turn(self) -> None:
        return None

    def ensure_checkpoint(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def get_working_dir_for_path(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class ManagedShortTaskContext:
    """Static token accounting only; never summarizes, probes, or calls a model."""

    name = "managed-short-task-static"
    context_length = 128_000
    threshold_percent = 1.0
    threshold_tokens = 128_000
    threshold_tokens_cap = None
    protect_first_n = 0
    protect_last_n = 0
    summary_target_ratio = 0.0
    compression_count = 0
    last_prompt_tokens = 0
    last_completion_tokens = 0
    last_compression_rough_tokens = 0
    awaiting_real_usage_after_compression = False
    _context_probed = False
    _context_probe_persistable = False

    def update_from_response(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        completion = usage.get(
            "completion_tokens", usage.get("output_tokens", 0)
        )
        if isinstance(prompt, int) and not isinstance(prompt, bool):
            self.last_prompt_tokens = max(0, prompt)
        if isinstance(completion, int) and not isinstance(completion, bool):
            self.last_completion_tokens = max(0, completion)

    def should_compress(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    def update_model(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return []


def managed_main_model_headers(
    base_url: str,
    api_key: str,
) -> dict[str, str] | None:
    """Provider headers needed by the main request, without auxiliary code."""
    host = (urlparse(str(base_url or "")).hostname or "").lower()
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return {
            "HTTP-Referer": "https://hermes-agent.nousresearch.com",
            "X-Title": "Hermes Agent",
        }
    if host == "integrate.api.nvidia.com":
        return {"X-BILLING-INVOKE-ORIGIN": "HermesAgent"}
    if host == "chatgpt.com" or host.endswith(".chatgpt.com"):
        headers = {
            "User-Agent": "codex_cli_rs/0.0.0 (Hermes Agent)",
            "originator": "codex_cli_rs",
        }
        try:
            parts = str(api_key or "").split(".")
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            account_id = claims.get("https://api.openai.com/auth", {}).get(
                "chatgpt_account_id"
            )
            if isinstance(account_id, str) and account_id:
                headers["ChatGPT-Account-ID"] = account_id
        except Exception:
            pass
        return headers
    return None


def validate_managed_main_client_urls(base_url: str) -> None:
    """Validate the main endpoint/proxy inputs without auxiliary imports."""
    for key in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
    ):
        value = str(os.environ.get(key) or "").strip()
        if not value:
            continue
        if value.lower().startswith("socks://"):
            value = f"socks5://{value[len('socks://') :]}"
            os.environ[key] = value
        try:
            parsed = urlparse(value)
            if parsed.scheme:
                _ = parsed.port
        except ValueError as exc:
            raise RuntimeError(
                f"Malformed proxy environment variable {key}={value!r}."
            ) from exc

    candidate = str(base_url or "").strip()
    if not candidate or candidate.startswith("acp://"):
        return
    try:
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"}:
            _ = parsed.port
    except ValueError as exc:
        raise RuntimeError(
            f"Malformed custom endpoint URL: {candidate!r}."
        ) from exc
