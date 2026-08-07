"""Session model-override methods for ``GatewayRunner``.

Extracted from ``gateway/run.py`` (god-file decomposition campaign, wave 1).
Holds the /model session-override cluster: rehydrate from the session
store, apply, snapshot, restore, and intentional-switch detection.

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
``_resolve_runtime_agent_kwargs_for_provider`` and
``_credential_pool_for_provider`` stay in ``gateway/run.py`` (shared with
staying methods) and are imported lazily inside the methods that use them.
"""


from __future__ import annotations

import logging

from typing import Any, Dict

logger = logging.getLogger("gateway.run")


class GatewaySessionModelMixin:

    def _rehydrate_session_model_override(self, session_key: str) -> None:
        """Lazily restore a persisted /model override after a gateway restart.

        ``_session_model_overrides`` is in-memory only, so before persistence
        a restart silently reverted every session to the global default model.
        The non-secret parts (model/provider/base_url) are written through to
        the session store when /model runs (and cleared on /new); here we read
        them back on first use and re-resolve credentials via the normal
        runtime provider resolution — api_key is never persisted to disk.

        No-op when an in-memory override already exists (live state wins) or
        when the store has nothing persisted (e.g. the user ran /new, which
        clears both the in-memory dict and the persisted field).
        """
        from gateway.run import _resolve_runtime_agent_kwargs_for_provider
        _rehydrate_state = self._peek_session_state(session_key)
        if (
            _rehydrate_state is not None
            and _rehydrate_state.conversation.model_override is not None
        ):
            return
        store = getattr(self, "session_store", None)
        if store is None:
            return
        try:
            persisted = store.get_model_override(session_key)
        except Exception:
            logger.debug(
                "Failed to read persisted session model override", exc_info=True
            )
            return
        if not persisted:
            return
        override: Dict[str, Any] = {
            "model": persisted.get("model"),
            "provider": persisted.get("provider"),
            "base_url": persisted.get("base_url"),
        }
        provider = persisted.get("provider")
        if provider:
            # Re-resolve credentials for the persisted provider. On failure
            # (e.g. credentials were removed since the switch) keep the
            # credential-less override — _resolve_session_agent_runtime falls
            # back to env-based resolution and applies model/provider on top.
            try:
                runtime = _resolve_runtime_agent_kwargs_for_provider(provider)
                override["api_key"] = runtime.get("api_key")
                override["api_mode"] = runtime.get("api_mode")
                override["credential_pool"] = runtime.get("credential_pool")
                if not override.get("base_url"):
                    override["base_url"] = runtime.get("base_url")
            except Exception:
                logger.debug(
                    "Credential re-resolution failed for persisted override "
                    "(provider=%s); using credential-less override",
                    provider, exc_info=True,
                )
        self._session_state(session_key).conversation.model_override = override
        logger.info(
            "Rehydrated persisted /model override for session=%s: model=%s provider=%s",
            session_key, override.get("model"), provider or "",
        )

    def _apply_session_model_override(
        self, session_key: str, model: str, runtime_kwargs: dict
    ) -> tuple:
        """Apply /model session overrides if present, returning (model, runtime_kwargs).

        The gateway /model command stores per-session overrides in
        ``_session_model_overrides``.  These must take precedence over
        config.yaml defaults so the switched model is actually used for
        subsequent messages.  Fields with ``None`` values are skipped so
        partial overrides don't clobber valid config defaults.
        """
        from gateway.run import _credential_pool_for_provider
        _apply_state = self._peek_session_state(session_key)
        override = _apply_state.conversation.model_override if _apply_state else None
        if not override:
            return model, runtime_kwargs
        model = override.get("model", model)
        for key in ("provider", "api_key", "base_url", "api_mode", "credential_pool"):
            val = override.get(key)
            if val is not None:
                runtime_kwargs[key] = val
        if (
            runtime_kwargs.get("api_key")
            and runtime_kwargs.get("credential_pool") is None
            and override.get("provider")
        ):
            runtime_kwargs["credential_pool"] = _credential_pool_for_provider(
                override.get("provider")
            )
        return model, runtime_kwargs

    def _snapshot_session_model_override(self, session_key: str) -> dict:
        """Capture a gateway session override before a one-turn switch."""
        _snap_state = self._peek_session_state(session_key)
        override = _snap_state.conversation.model_override if _snap_state else None
        return {
            "had_override": override is not None,
            "override": dict(override) if override is not None else None,
        }

    def _restore_session_model_override(self, session_key: str, snapshot: dict) -> None:
        """Restore the session override captured before a one-turn switch."""
        if not session_key:
            return
        if snapshot.get("had_override"):
            self._session_state(session_key).conversation.model_override = dict(
                snapshot.get("override") or {}
            )
        else:
            _rst_state = self._peek_session_state(session_key)
            if _rst_state is not None:
                _rst_state.conversation.model_override = None
        self._evict_cached_agent(session_key)

    def _is_intentional_model_switch(self, session_key: str, agent_model: str) -> bool:
        """Return True if *agent_model* matches an active /model session override."""
        _ims_state = self._peek_session_state(session_key)
        override = _ims_state.conversation.model_override if _ims_state else None
        return override is not None and override.get("model") == agent_model

