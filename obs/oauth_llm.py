"""OAuth-bridged LangChain ChatOpenAI client for Hermes.

Diego's contract (2026-04-24): every LLM call across the Hermes platform must
go through the openai-codex OAuth path with model gpt-5.5 — no
OPENAI_API_KEY-based side paths, no OpenRouter, no fallbacks.

This module gives my LangGraph code (graphs/jobflow.py + graphs/critic.py +
any future graph) a single entry point to get a working ChatOpenAI client
that talks to chatgpt.com/backend-api/codex via OAuth.

How it works:
  1. resolve_codex_runtime_credentials() (in hermes_cli.auth) returns a
     fresh access_token, auto-refreshing if it's near expiry.
  2. We hand that token to langchain_openai.ChatOpenAI as api_key and point
     base_url at the Codex endpoint.
  3. use_responses_api=True is required because the Codex endpoint speaks the
     Responses API, not Chat Completions. (langchain-openai >= 0.2 supports
     this flag.)

Quirks of the Codex endpoint (from agent/auxiliary_client.py:300):
  * No `temperature` (omit; raises 400 otherwise)
  * No `max_output_tokens` (omit)
  * Tools are converted to a flat {type:"function", name, description,
     parameters} shape inside resp_kwargs["tools"]

Cache-on-token-mtime so we refresh the client when auth.json rotates.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Ensure agent-src is on path so we can import hermes_cli.auth
_AGENT_SRC = Path.home() / ".hermes" / "agent-src"
if str(_AGENT_SRC) not in sys.path:
    sys.path.insert(0, str(_AGENT_SRC))

# Guard the openai SDK against the ChatGPT Codex backend's response.output=None
# completion snapshot, which otherwise crashes parse_response inside the
# responses.stream() accumulator used by codex_structured_invoke() below.
# Idempotent; applied at import. See agent/openai_codex_compat.py.
try:
    from agent.openai_codex_compat import apply_codex_output_none_guard
    apply_codex_output_none_guard()
except Exception:  # pragma: no cover - guard must never break host import
    logger.debug("codex output=None guard not applied in obs.oauth_llm", exc_info=True)

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

# Default model — Diego mandate 2026-04-24: ONLY gpt-5.5 via OAuth.
# Per-graph override via env (HERMES_JOBFLOW_MODEL / HERMES_CRITIC_MODEL).
DEFAULT_CODEX_MODEL = "gpt-5.5"

_LOCK = threading.Lock()
_CACHED_TOKEN: Optional[str] = None
_CACHED_AT: float = 0.0
_TOKEN_TTL_S: float = 600.0  # re-resolve every 10 minutes; SDK refresh is auto


def _get_oauth_token() -> str:
    """Return a fresh OAuth access_token, mirroring auxiliary_client's resolution chain.

    Priority (matches agent/auxiliary_client.py:_read_codex_access_token):
      1. ~/.codex/auth.json (Codex CLI shared file) — fresh whenever Diego
         re-runs `hermes auth add openai-codex --type oauth`. This is where
         the fresh credential lands; the Hermes auth store top-level may lag.
      2. credential_pool active entry in get_hermes_home()/auth.json — the
         profile-scoped store (e.g. ~/.hermes/profiles/main) that `hermes auth
         add` writes and the cron path (agent/credential_pool.py) reads. NOT
         hardcoded root; see Step 2 comment for the 2026-05-28 split-brain.
      3. resolve_codex_runtime_credentials() (top-level providers section,
         with auto-refresh attempt; falls through if refresh_token is invalid)
      4. _read_codex_tokens() raw read of top-level (last-resort, may be stale)
    """
    global _CACHED_TOKEN, _CACHED_AT
    with _LOCK:
        now = time.time()
        if _CACHED_TOKEN and (now - _CACHED_AT) < _TOKEN_TTL_S:
            return _CACHED_TOKEN

        # Step 1: ~/.codex/auth.json (Codex CLI's own store; freshest after `hermes auth add`)
        codex_home_env = os.environ.get("CODEX_HOME", "").strip()
        codex_path = Path(codex_home_env).expanduser() / "auth.json" if codex_home_env else (
            Path.home() / ".codex" / "auth.json"
        )
        if codex_path.is_file():
            try:
                import json as _json
                payload = _json.loads(codex_path.read_text(encoding="utf-8"))
                tokens = payload.get("tokens") or {}
                tok = tokens.get("access_token", "")
                # Reject expired
                if tok and not _jwt_is_expired(tok):
                    _CACHED_TOKEN = tok
                    _CACHED_AT = now
                    return tok
            except Exception as e:
                logger.debug("oauth_llm: could not read %s: %s", codex_path, e)

        # Step 2: credential_pool active entry in the profile-scoped Hermes
        # auth store. Resolve via get_hermes_home() (HERMES_HOME-aware — e.g.
        # ~/.hermes/profiles/main when active_profile=main) so we read the SAME
        # file `hermes auth add` writes and that agent/credential_pool.py +
        # agent/auxiliary_client.py read. Hardcoding Path.home()/.hermes here
        # was the 2026-05-28 split-brain: the gateway runs with
        # HERMES_HOME=profiles/main, so the root store was stale and this path
        # fell through to an expired token -> 401 token_expired.
        try:
            from hermes_constants import get_hermes_home  # type: ignore

            hermes_auth_path = get_hermes_home() / "auth.json"
            if hermes_auth_path.is_file():
                import json as _json
                data = _json.loads(hermes_auth_path.read_text(encoding="utf-8"))
                pool = (data.get("credential_pool") or {}).get("openai-codex") or []
                # Pick the freshest non-expired entry by last_refresh desc
                pool_sorted = sorted(
                    [e for e in pool if isinstance(e, dict)],
                    key=lambda e: e.get("last_refresh") or "",
                    reverse=True,
                )
                for entry in pool_sorted:
                    tok = entry.get("access_token") or entry.get("runtime_api_key", "")
                    if tok and not _jwt_is_expired(tok):
                        _CACHED_TOKEN = tok
                        _CACHED_AT = now
                        return tok
        except Exception as e:
            logger.debug("oauth_llm: pool read failed: %s", e)

        # Step 3: resolve_codex_runtime_credentials (top-level + auto-refresh attempt)
        try:
            from hermes_cli.auth import resolve_codex_runtime_credentials  # type: ignore

            creds = resolve_codex_runtime_credentials(
                force_refresh=False,
                refresh_if_expiring=True,
            )
            tok = (creds.get("tokens") or {}).get("access_token") or creds.get("access_token")
            if tok and not _jwt_is_expired(tok):
                _CACHED_TOKEN = tok
                _CACHED_AT = now
                return tok
        except Exception as exc:
            logger.debug("oauth_llm: resolve_codex_runtime_credentials failed: %s", exc)

        # Step 4: last-resort raw read (will warn if stale)
        try:
            from hermes_cli.auth import _read_codex_tokens  # type: ignore

            data = _read_codex_tokens()
            tok = (data.get("tokens") or {}).get("access_token", "")
            if tok:
                if _jwt_is_expired(tok):
                    logger.warning(
                        "oauth_llm: ALL fresh sources exhausted; falling back to "
                        "expired top-level token. Run `hermes auth add openai-codex "
                        "--type oauth` to refresh."
                    )
                _CACHED_TOKEN = tok
                _CACHED_AT = now
                return tok
        except Exception:
            pass

        raise RuntimeError(
            "Could not obtain a non-expired Codex OAuth access_token from any source. "
            "Run `hermes auth add openai-codex --type oauth` to re-authenticate."
        )


def _jwt_is_expired(token: str, skew_seconds: int = 60) -> bool:
    """True if the JWT exp claim has passed, OR the token is malformed.

    Malformed tokens are treated as expired so callers fall through to the
    credential_pool fallback (Step 2 of `_get_oauth_token`). Letting them
    through caused the 2026-04-25 shadow-coverage outage when a failed token
    refresh left `~/.codex/auth.json` with the placeholder string `access-new`.
    """
    if not token or not isinstance(token, str):
        return True
    try:
        import base64 as _b64
        import json as _json

        parts = token.split(".")
        if len(parts) != 3:
            return True
        body = parts[1]
        body += "=" * (-len(body) % 4)
        payload = _json.loads(_b64.urlsafe_b64decode(body))
        exp_raw = payload.get("exp") if isinstance(payload, dict) else None
        if not isinstance(exp_raw, (int, float)):
            return True
        exp = int(exp_raw)
        if exp <= 0:
            return True
        return time.time() + skew_seconds >= exp
    except Exception:
        return True


def get_codex_chat_model(
    model: Optional[str] = None,
    *,
    temperature: Optional[float] = None,
    max_retries: int = 2,
    **kwargs: Any,
):
    """Return a ChatOpenAI instance wired to the Codex OAuth endpoint.

    Parameters:
        model: model name (default DEFAULT_CODEX_MODEL = "gpt-5.5")
        temperature: SILENTLY DROPPED — Codex endpoint rejects it (400).
            Kept in the signature for drop-in compatibility with langchain_openai
            callers; logged at DEBUG when discarded.
        max_retries: passed through to ChatOpenAI.
        **kwargs: forwarded to ChatOpenAI (e.g. timeout). Watch for codex-incompatible
            params like max_output_tokens.

    Usage:
        from obs.oauth_llm import get_codex_chat_model
        llm = get_codex_chat_model("gpt-5.5")
        structured = llm.with_structured_output(MySchema, method="json_mode")
        result = structured.invoke([SystemMessage(...), HumanMessage(...)])

    NOTE on structured output: Codex Responses API doesn't support OpenAI's
    `function_calling` method directly. Use `method="json_mode"` instead and
    have the system prompt explicitly request a JSON response matching the
    schema. langchain handles the parsing.
    """
    from langchain_openai import ChatOpenAI

    model_name = model or os.environ.get("HERMES_OAUTH_MODEL") or DEFAULT_CODEX_MODEL
    token = _get_oauth_token()

    if temperature is not None:
        logger.debug(
            "oauth_llm.get_codex_chat_model: dropping temperature=%s (codex rejects)",
            temperature,
        )

    return ChatOpenAI(
        model=model_name,
        api_key=token,
        base_url=CODEX_BASE_URL,
        use_responses_api=True,
        max_retries=max_retries,
        **kwargs,
    )


def codex_structured_invoke(
    schema: Any,
    *,
    instructions: str,
    user: str,
    model: Optional[str] = None,
    max_retries: int = 2,
):
    """Direct call to Codex Responses API with structured-JSON-output prompting.

    We don't use langchain's `with_structured_output(method=...)` because:
      * "function_calling": Codex Responses API doesn't speak Chat-Completions
        function calling.
      * "json_mode": langchain's responses-API mode sends a `messages` list, but
        chatgpt.com/backend-api/codex requires top-level `instructions` (string)
        + `input` (list of {role, content}). The 400 reads `Instructions are required`.

    This helper sends the correct shape directly via the `openai` SDK, then parses
    the streamed text into the supplied Pydantic schema. Mirrors the body of
    `agent/auxiliary_client.py::CodexAuxiliaryClient` but trimmed to the
    structured-JSON case.

    Quirks honored:
      * NO temperature, NO max_output_tokens (Codex 400s).
      * `store: False` (no persistence on chatgpt.com side).
      * Output is collected from streamed deltas (the non-streaming
        `output_text` is sometimes empty even when the stream had content).

    Returns: an instance of `schema` (e.g. MatcherScore) parsed from JSON.
    Raises: RuntimeError on parse failure with the raw text in the message.
    """
    import json as _json

    from openai import OpenAI

    model_name = model or os.environ.get("HERMES_OAUTH_MODEL") or DEFAULT_CODEX_MODEL

    # Generate JSON Schema from the Pydantic model for prompt grounding.
    try:
        schema_dict = schema.model_json_schema()
    except Exception:
        schema_dict = {"type": "object"}

    full_instructions = (
        instructions.rstrip()
        + "\n\nReturn ONLY a JSON object that validates against this JSON Schema. "
        + "Do not wrap it in code fences or any other text. Schema:\n"
        + _json.dumps(schema_dict, indent=2)
    )

    last_err: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            token = _get_oauth_token()
            client = OpenAI(api_key=token, base_url=CODEX_BASE_URL)
            text_parts: list[str] = []
            with client.responses.stream(
                model=model_name,
                instructions=full_instructions,
                input=[{"role": "user", "content": user}],
                store=False,
            ) as stream:
                for event in stream:
                    et = getattr(event, "type", "")
                    if et == "response.output_text.delta":
                        delta = getattr(event, "delta", "")
                        if delta:
                            text_parts.append(delta)
                    elif et == "response.error":
                        raise RuntimeError(
                            f"Codex stream error: {getattr(event, 'error', None)}"
                        )
                final_response = stream.get_final_response()
            text = "".join(text_parts).strip()
            if not text and final_response is not None:
                text = (getattr(final_response, "output_text", "") or "").strip()
            if not text:
                raise RuntimeError("Codex returned empty response text")
            # Some models still wrap in fences; tolerate.
            if text.startswith("```"):
                text = text.strip("`")
                if text.lower().startswith("json"):
                    text = text[4:]
                text = text.strip()
            return schema.model_validate_json(text)
        except Exception as e:
            last_err = e
            if attempt >= max_retries:
                break
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"codex_structured_invoke failed after retries: {last_err}")


__all__ = [
    "get_codex_chat_model",
    "codex_structured_invoke",
    "DEFAULT_CODEX_MODEL",
    "CODEX_BASE_URL",
]
