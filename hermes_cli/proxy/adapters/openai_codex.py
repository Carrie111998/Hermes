"""OpenAI Codex adapter for the Hermes local OAuth proxy.

The adapter resolves Hermes-managed Codex OAuth state only inside the Hermes
process. Callers receive an ordinary OpenAI-compatible proxy response and
never read, persist, or receive the upstream OAuth credential.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, FrozenSet, Optional

from agent.credential_pool import load_pool
from hermes_cli.auth import (
    get_provider_auth_state,
    resolve_codex_runtime_credentials,
)
from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential

logger = logging.getLogger(__name__)

# The local broker intentionally forwards only the Responses endpoint. This is
# the narrow contract used by the Codex adapter and avoids becoming a generic
# bearer relay for unrelated upstream paths.
_ALLOWED_PATHS: FrozenSet[str] = frozenset({"/responses"})
_ALLOWED_METHODS: FrozenSet[str] = frozenset({"POST"})


class OpenAICodexAdapter(UpstreamAdapter):
    """Proxy upstream for Hermes-managed OpenAI Codex OAuth."""

    auth_hint = "hermes auth add openai-codex"

    def __init__(self) -> None:
        # The auth resolver serializes cross-process refresh/persistence. This
        # lock prevents concurrent proxy requests in this process from making
        # duplicate resolver calls around the same credential rotation.
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "openai-codex"

    @property
    def display_name(self) -> str:
        return "OpenAI Codex OAuth"

    @property
    def allowed_paths(self) -> FrozenSet[str]:
        return _ALLOWED_PATHS

    @property
    def allowed_methods(self) -> FrozenSet[str]:
        return _ALLOWED_METHODS

    def is_authenticated(self) -> bool:
        """Check local Hermes state without resolving or refreshing a token."""
        try:
            pool = load_pool("openai-codex")
            if pool is not None and pool.has_credentials():
                return True
        except Exception:
            pass
        try:
            state = get_provider_auth_state("openai-codex")
        except Exception:
            return False
        if not isinstance(state, dict):
            return False
        tokens = state.get("tokens")
        if not isinstance(tokens, dict):
            return False
        return self._has_nonempty_string(tokens.get("access_token")) and self._has_nonempty_string(
            tokens.get("refresh_token")
        )

    def get_credential(self) -> UpstreamCredential:
        return self._get_credential()

    def get_retry_credential(
        self,
        *,
        failed_credential: UpstreamCredential,
        status_code: int,
    ) -> Optional[UpstreamCredential]:
        if status_code != 401:
            return None
        refreshed = self._get_credential(force_refresh=True)
        if refreshed.bearer == failed_credential.bearer:
            return None
        logger.info("proxy: OpenAI Codex upstream rejected bearer; retried after Hermes refresh")
        return refreshed

    def _get_credential(self, *, force_refresh: bool = False) -> UpstreamCredential:
        with self._lock:
            try:
                resolved = resolve_codex_runtime_credentials(force_refresh=force_refresh)
            except Exception as exc:
                # Upstream/provider failures can carry arbitrary response text;
                # neither the proxy client nor its logs should expose it.
                logger.info(
                    "proxy: OpenAI Codex credential resolution failed (%s)",
                    type(exc).__name__,
                )
                raise RuntimeError(
                    "OpenAI Codex authentication is unavailable. "
                    "Run `hermes auth add openai-codex` to sign in."
                ) from None

            bearer = resolved.get("api_key") if isinstance(resolved, dict) else None
            base_url = resolved.get("base_url") if isinstance(resolved, dict) else None
            if not self._has_nonempty_string(bearer) or not self._has_nonempty_string(base_url):
                raise RuntimeError(
                    "OpenAI Codex authentication is unavailable. "
                    "Run `hermes auth add openai-codex` to sign in."
                )
            assert isinstance(bearer, str) and isinstance(base_url, str)
            return UpstreamCredential(
                bearer=bearer.strip(),
                base_url=base_url.strip().rstrip("/"),
            )

    @staticmethod
    def _has_nonempty_string(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())


__all__ = ["OpenAICodexAdapter"]
