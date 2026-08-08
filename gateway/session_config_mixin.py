"""Session-config resolution methods for ``GatewayRunner``.

Extracted from ``gateway/run.py`` (god-file decomposition campaign, Wave 1,
shard s2, cluster c12). This mixin holds the session-config cluster: runtime
session agent kwargs resolution, per-channel model/system-prompt resolution,
reasoning-config and service-tier loaders, busy-input/text-mode knobs, restart
drain timeouts, provider routing, and the fallback-chain refresh path.

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
``self.*`` calls resolve unchanged via the MRO. The module-level ``logger`` is
``logging.getLogger("gateway.run")`` so log records keep the exact name
(``"gateway.run"``), matching the sibling mixins' convention. run.py module
helpers/constants that stay behind (``_load_gateway_runtime_config``,
``_get_channel_override``, ``_resolve_gateway_model``,
``_resolve_runtime_agent_kwargs``, ``_resolve_runtime_agent_kwargs_for_provider``,
``_credential_pool_for_provider``, ``_hermes_home``, ``GatewayRunner``) are
imported lazily inside the method that uses them — a deferred
``from gateway.run import ...`` resolves at call time (run.py fully loaded by
then), so this module never imports ``gateway.run`` at import time -> no import
cycle.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from gateway.config import Platform
from gateway.restart import (
    DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT,
    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
    parse_restart_after_turn_timeout,
    parse_restart_drain_timeout,
)
from gateway.session import SessionSource
from gateway.session_state import SERVICE_TIER_UNSET as _SERVICE_TIER_UNSET
from hermes_cli.config import cfg_get
from hermes_cli.fallback_config import get_fallback_chain
from utils import is_truthy_value

logger = logging.getLogger("gateway.run")


class SessionConfigMixin:

    def _resolve_session_agent_runtime(
        self,
        *,
        source: Optional[SessionSource] = None,
        session_key: Optional[str] = None,
        user_config: Optional[dict] = None,
    ) -> tuple[str, dict]:
        """Resolve model/runtime for a session.

        Priority (highest first): session ``/model`` → ``channel_overrides`` →
        global config/env (``_resolve_gateway_model(user_config)`` and default
        provider resolution).
        """
        from gateway.run import _credential_pool_for_provider
        from gateway.run import _get_channel_override
        from gateway.run import _resolve_gateway_model
        from gateway.run import _resolve_runtime_agent_kwargs
        from gateway.run import _resolve_runtime_agent_kwargs_for_provider
        resolved_session_key = session_key
        if not resolved_session_key and source is not None:
            try:
                resolved_session_key = self._session_key_for_source(source)
            except Exception:
                resolved_session_key = None

        model = _resolve_gateway_model(user_config)
        if resolved_session_key:
            self._rehydrate_session_model_override(resolved_session_key)
        _override_state = (
            self._peek_session_state(resolved_session_key)
            if resolved_session_key
            else None
        )
        override = (
            _override_state.conversation.model_override if _override_state else None
        )
        if override:
            override_model = override.get("model", model)
            override_runtime = {
                "provider": override.get("provider"),
                "api_key": override.get("api_key"),
                "base_url": override.get("base_url"),
                "api_mode": override.get("api_mode"),
                "max_tokens": override.get("max_tokens"),
                "credential_pool": override.get("credential_pool"),
            }
            if override_runtime.get("api_key"):
                if override_runtime.get("credential_pool") is None:
                    override_runtime["credential_pool"] = _credential_pool_for_provider(
                        override.get("provider")
                    )
                logger.debug(
                    "Session model override (fast): session=%s config_model=%s -> override_model=%s provider=%s",
                    resolved_session_key or "", model, override_model,
                    override_runtime.get("provider"),
                )
                return override_model, override_runtime
            # Override exists but has no api_key — fall through to env-based
            # resolution and apply model/provider from the override on top.
            logger.debug(
                "Session model override (no api_key, fallback): session=%s config_model=%s override_model=%s",
                resolved_session_key or "", model, override_model,
            )
        else:
            logger.debug(
                "No session model override: session=%s config_model=%s override_keys=%s",
                resolved_session_key or "", model,
                [
                    _key
                    for _key, _st in list(self._sessions_map().items())
                    if _st.conversation.model_override is not None
                ][:5] or "[]",
            )

        runtime_kwargs = _resolve_runtime_agent_kwargs()
        runtime_model = runtime_kwargs.pop("model", None)
        if runtime_model:
            logger.info(
                "Runtime provider supplied explicit model override: %s -> %s",
                model,
                runtime_model,
            )
            model = runtime_model

        cfg = getattr(self, "config", None)
        if cfg and source is not None:
            chat_id = str(source.chat_id) if source.chat_id else ""
            thread_id = (
                str(source.thread_id) if getattr(source, "thread_id", None) else None
            )
            parent_id = (
                str(source.parent_chat_id)
                if getattr(source, "parent_chat_id", None)
                else None
            )
            ch = _get_channel_override(
                cfg,
                source.platform,
                chat_id,
                thread_id=thread_id,
                parent_id=parent_id,
            )
            if ch:
                if ch.model:
                    model = ch.model
                if ch.provider:
                    runtime_kwargs = _resolve_runtime_agent_kwargs_for_provider(
                        ch.provider
                    )
                    ch_runtime_model = runtime_kwargs.pop("model", None)
                    # Only adopt the provider's bundled model when the override
                    # did not specify an explicit model.
                    if ch_runtime_model and not ch.model:
                        model = ch_runtime_model

        if override and resolved_session_key:
            model, runtime_kwargs = self._apply_session_model_override(
                resolved_session_key, model, runtime_kwargs
            )

        # When the config has no model.default but a provider was resolved
        # (e.g. user ran `hermes auth add openai-codex` without `hermes model`),
        # fall back to the provider's first catalog model so the API call
        # doesn't fail with "model must be a non-empty string".
        if not model and runtime_kwargs.get("provider"):
            try:
                from hermes_cli.models import get_default_model_for_provider
                model = get_default_model_for_provider(runtime_kwargs["provider"])
                if model:
                    logger.info(
                        "No model configured — defaulting to %s for provider %s",
                        model, runtime_kwargs["provider"],
                    )
            except Exception:
                pass

        # Final safety net (#35314): if resolution still produced an empty
        # model — e.g. a transient config-cache miss during a post-interrupt
        # recovery turn returned an empty user_config — reuse the last model we
        # successfully resolved for this session (or, failing that, the most
        # recent one resolved process-wide). Building an agent with model=""
        # makes every API call fail HTTP 400 "No models provided" and the
        # session goes silent until the user manually re-sends. ``getattr``
        # guards against bare test runners built via ``object.__new__``.
        if not model:
            _lr_state = (
                self._peek_session_state(resolved_session_key)
                if resolved_session_key
                else None
            )
            _lr_star = self._peek_session_state("*")
            _recovered = (
                (_lr_state.conversation.last_resolved_model if _lr_state else "")
                or (_lr_star.conversation.last_resolved_model if _lr_star else "")
            )
            if _recovered:
                logger.warning(
                    "Empty model resolved for session=%s — recovering "
                    "last-known-good model %s (config read likely returned "
                    "empty; see #35314)",
                    resolved_session_key or "", _recovered,
                )
                model = _recovered
        elif model:
            # Cache the good resolution for future recovery turns.
            if resolved_session_key:
                self._session_state(
                    resolved_session_key
                ).conversation.last_resolved_model = model
            self._session_state("*").conversation.last_resolved_model = model

        return model, runtime_kwargs

    def _resolve_turn_agent_config(self, user_message: str, model: str, runtime_kwargs: dict) -> dict:
        """Build the effective model/runtime config for a single turn.

        Always uses the session's primary model/provider.  If `/fast` is
        enabled and the model supports Priority Processing / Anthropic fast
        mode, attach `request_overrides` so the API call is marked
        accordingly.
        """
        from hermes_cli.models import resolve_fast_mode_overrides

        runtime = {
            "api_key": runtime_kwargs.get("api_key"),
            "base_url": runtime_kwargs.get("base_url"),
            "provider": runtime_kwargs.get("provider"),
            "requested_provider": runtime_kwargs.get("requested_provider"),
            "api_mode": runtime_kwargs.get("api_mode"),
            "command": runtime_kwargs.get("command"),
            "args": list(runtime_kwargs.get("args") or []),
            "credential_pool": runtime_kwargs.get("credential_pool"),
            "max_tokens": runtime_kwargs.get("max_tokens"),
        }
        route = {
            "model": model,
            "runtime": runtime,
            "signature": (
                model,
                runtime["provider"],
                runtime["requested_provider"],
                runtime["base_url"],
                runtime["api_mode"],
                runtime["command"],
                tuple(runtime["args"]),
            ),
        }

        service_tier = getattr(self, "_service_tier", None)
        if not service_tier:
            route["request_overrides"] = {}
            return route

        try:
            overrides = resolve_fast_mode_overrides(route["model"])
        except Exception:
            overrides = None
        route["request_overrides"] = overrides or {}
        return route

    def _sync_session_model_from_agent(self, session_id: str, agent: Any) -> None:
        """Persist the runtime model/provider actually used by a gateway turn.

        Provider fallback can switch ``agent.model``/``agent.provider`` after the
        session row was created. Keep the session DB metadata in sync so session
        lists, desktop/dashboard details, and follow-up session tooling report the
        backend that actually answered the latest turn.

        Called from the ``run_sync`` closure, which executes off the event loop
        in the executor thread — so the synchronous ``SessionDB`` (``_db``) is
        used directly rather than awaiting the AsyncSessionDB forwarder.
        """
        if not session_id or agent is None or self._session_db is None:
            return
        model = getattr(agent, "model", None)
        if not model:
            return
        runtime = {
            "provider": getattr(agent, "provider", None),
            "base_url": getattr(agent, "base_url", None),
            "api_mode": getattr(agent, "api_mode", None),
            "fallback_active": bool(getattr(agent, "_fallback_activated", False)),
        }
        runtime = {k: v for k, v in runtime.items() if v not in (None, "")}

        try:
            db = self._session_db._db
            row = db.get_session(session_id)
            if not row:
                return
            current_model = row.get("model")
            raw_config = row.get("model_config")
            try:
                config = json.loads(raw_config) if raw_config else {}
            except Exception:
                config = {}
            if not isinstance(config, dict):
                config = {}
            gateway_runtime = dict(config.get("gateway_runtime") or {})
            if current_model == model and all(
                gateway_runtime.get(k) == v for k, v in runtime.items()
            ):
                return
            config["gateway_runtime"] = runtime
            db.update_session_meta(session_id, json.dumps(config), model=model)
        except Exception:
            logger.debug("Failed to sync gateway session model metadata", exc_info=True)

    @staticmethod
    def _load_prefill_messages() -> List[Dict[str, Any]]:
        """Load ephemeral prefill messages from config or env var.
        
        Checks HERMES_PREFILL_MESSAGES_FILE env var first, then falls back to
        the top-level prefill_messages_file key in ~/.hermes/config.yaml.
        agent.prefill_messages_file is accepted as a legacy fallback.
        Relative paths are resolved from ~/.hermes/.
        """
        from gateway.run import _hermes_home
        from gateway.run import _load_gateway_runtime_config
        file_path = os.getenv("HERMES_PREFILL_MESSAGES_FILE", "")
        if not file_path:
            cfg = _load_gateway_runtime_config()
            file_path = str(cfg.get("prefill_messages_file", "") or "")
            if not file_path:
                file_path = str(cfg_get(cfg, "agent", "prefill_messages_file", default="") or "")
        if not file_path:
            return []
        path = Path(file_path).expanduser()
        if not path.is_absolute():
            path = _hermes_home / path
        if not path.exists():
            logger.warning("Prefill messages file not found: %s", path)
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                logger.warning("Prefill messages file must contain a JSON array: %s", path)
                return []
            return data
        except Exception as e:
            logger.warning("Failed to load prefill messages from %s: %s", path, e)
            return []

    @staticmethod
    def _load_ephemeral_system_prompt() -> str:
        """Load ephemeral system prompt from config or env var.
        
        Checks HERMES_EPHEMERAL_SYSTEM_PROMPT env var first, then falls back to
        agent.system_prompt in ~/.hermes/config.yaml.
        """
        from gateway.run import _load_gateway_runtime_config
        prompt = os.getenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", "")
        if prompt:
            return prompt
        cfg = _load_gateway_runtime_config()
        return str(cfg_get(cfg, "agent", "system_prompt", default="") or "").strip()

    def _resolve_model_for_channel(
        self,
        platform: Platform,
        chat_id: str,
        *,
        user_config: Optional[dict] = None,
        thread_id: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> str:
        """Resolve model for this channel: channel_overrides else global default.

        Delegates the precedence rule to
        :func:`hermes_cli.model_switch.resolve_effective_model` (session
        override > channel override > global default) — the single owner
        shared with the API server, so the two surfaces cannot diverge
        again (see 7dd00bb47d).  This call site has no session tier: session
        /model overrides are applied later by
        ``_apply_session_model_override`` on the resolved runtime.
        """
        from gateway.run import _get_channel_override
        from gateway.run import _resolve_gateway_model
        from hermes_cli.model_switch import resolve_effective_model

        override = None
        config = getattr(self, "config", None)
        if config:
            override = _get_channel_override(
                config,
                platform,
                chat_id,
                thread_id=thread_id,
                parent_id=parent_id,
            )
        return resolve_effective_model(
            None,  # session tier applied downstream (_apply_session_model_override)
            override,
            _resolve_gateway_model(user_config),
        )

    def _get_system_prompt_for_channel(
        self,
        platform: Platform,
        chat_id: str,
        *,
        thread_id: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> str:
        """Ephemeral system prompt for this channel/thread.

        Uses ``channel_overrides`` when set, else the global gateway prompt.
        Legacy ``channel_prompts`` are applied separately via ``event.channel_prompt``
        in ``run_sync`` (adapter ``resolve_channel_prompt``), so they are not
        duplicated here.
        """
        from gateway.run import _get_channel_override
        config = getattr(self, "config", None)
        if config:
            override = _get_channel_override(
                config,
                platform,
                chat_id,
                thread_id=thread_id,
                parent_id=parent_id,
            )
            if override and override.system_prompt:
                return (override.system_prompt or "").strip()
        return getattr(self, "_ephemeral_system_prompt", None) or ""

    @staticmethod
    def _load_reasoning_config(model: str = "") -> dict | None:
        """Load reasoning effort from config.yaml, respecting per-model overrides.

        Thin wrapper over the shared chokepoint
        :func:`hermes_constants.resolve_reasoning_config` (per-model override >
        global ``agent.reasoning_effort``; YAML boolean False = disabled).
        Closes #21256.

        Args:
            model: The effective model for the calling session. When empty,
                   the config's ``model.default`` is used.
        """
        from gateway.run import _load_gateway_runtime_config
        from hermes_constants import resolve_reasoning_config
        cfg = _load_gateway_runtime_config()
        return resolve_reasoning_config(cfg, model)

    @staticmethod
    def _parse_reasoning_command_args(raw_args: str) -> tuple[str, bool]:
        """Parse `/reasoning` args into `(value, persist_global)`.

        `/reasoning <level>` is session-scoped by default. `--global` may be
        supplied in any position to persist the change to config.yaml.
        """
        import shlex

        text = str(raw_args or "").strip().replace("—", "--")
        if not text:
            return "", False
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()

        persist_global = False
        value_tokens = []
        for token in tokens:
            if token == "--global":
                persist_global = True
            else:
                value_tokens.append(token)
        return " ".join(value_tokens).strip().lower(), persist_global

    def _resolve_session_reasoning_config(
        self,
        *,
        source: Optional[SessionSource] = None,
        session_key: Optional[str] = None,
        model: str = "",
    ) -> dict | None:
        """Resolve reasoning effort for a session, honoring session overrides.

        Priority: session-scoped ``/reasoning --session`` override >
        per-model override (``agent.reasoning_overrides``) > global
        ``agent.reasoning_effort``. ``model`` should be the session's
        *effective* model (session ``/model`` override included) so
        per-model overrides track what the session actually runs — when
        empty, the config's ``model.default`` is used.
        """
        resolved_session_key = session_key
        if not resolved_session_key and source is not None:
            try:
                resolved_session_key = self._session_key_for_source(source)
            except Exception:
                resolved_session_key = None

        if resolved_session_key:
            _r_state = self._peek_session_state(resolved_session_key)
            if _r_state is not None and _r_state.conversation.reasoning_override is not None:
                return _r_state.conversation.reasoning_override
        return self._load_reasoning_config(model)

    def _set_session_reasoning_override(
        self,
        session_key: str,
        reasoning_config: Optional[dict],
    ) -> None:
        """Set or clear the session-scoped reasoning override."""
        if not session_key:
            return
        # Per-session field write — the old lazy ``self._session_reasoning_overrides
        # = {}`` init replaced the WHOLE dict, racing concurrent sessions'
        # overrides; a SessionState field reset cannot cross sessions.
        self._session_state(session_key).conversation.reasoning_override = (
            None if reasoning_config is None else dict(reasoning_config)
        )

    def _resolve_session_service_tier(
        self,
        source=None,
        session_key: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve the effective service tier for a session.

        A session-scoped /fast override wins over the config default. The
        override dict stores "priority" or None (explicit normal), so key
        presence — not value truthiness — decides whether it applies.
        """
        resolved_session_key = session_key
        if not resolved_session_key and source is not None:
            try:
                resolved_session_key = self._session_key_for_source(source)
            except Exception:
                resolved_session_key = None

        if resolved_session_key:
            _t_state = self._peek_session_state(resolved_session_key)
            if (
                _t_state is not None
                and _t_state.conversation.service_tier_override
                is not _SERVICE_TIER_UNSET
            ):
                return _t_state.conversation.service_tier_override
        return self._load_service_tier()

    def _set_session_service_tier_override(
        self,
        session_key: str,
        service_tier,
        clear: bool = False,
    ) -> None:
        """Set or clear the session-scoped /fast override.

        ``service_tier`` is "priority" or None (explicit normal). Pass
        ``clear=True`` to remove the override entirely (fall back to config).
        """
        if not session_key:
            return
        # Presence-sensitive: "priority" or None (explicit normal) both count
        # as an override; the sentinel means "no override".  Old code
        # wholesale-replaced the dict on lazy init (cross-session race) —
        # per-session field writes eliminate that class of bug.
        self._session_state(session_key).conversation.service_tier_override = (
            _SERVICE_TIER_UNSET if clear else service_tier
        )

    @staticmethod
    def _load_service_tier() -> str | None:
        """Load Priority Processing setting from config.yaml.

        Reads agent.service_tier from config.yaml. Accepted values mirror the CLI:
        "fast"/"priority"/"on" => "priority", while "normal"/"off" disables it.
        Returns None when unset or unsupported.
        """
        from gateway.run import _load_gateway_runtime_config
        cfg = _load_gateway_runtime_config()
        raw = str(cfg_get(cfg, "agent", "service_tier", default="") or "").strip()

        value = raw.lower()
        if not value or value in {"normal", "default", "standard", "off", "none"}:
            return None
        if value in {"fast", "priority", "on"}:
            return "priority"
        logger.warning("Unknown service_tier '%s', ignoring", raw)
        return None

    @staticmethod
    def _load_show_reasoning() -> bool:
        """Load show_reasoning toggle from config.yaml display section."""
        from gateway.run import _load_gateway_runtime_config
        cfg = _load_gateway_runtime_config()
        return is_truthy_value(
            cfg_get(cfg, "display", "show_reasoning"),
            default=False,
        )

    @staticmethod
    def _load_busy_input_mode() -> str:
        """Load gateway drain-time busy-input behavior from config/env."""
        from gateway.run import _load_gateway_runtime_config
        mode = os.getenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "").strip().lower()
        if not mode:
            cfg = _load_gateway_runtime_config()
            mode = str(cfg_get(cfg, "display", "busy_input_mode", default="") or "").strip().lower()
        if mode == "queue":
            return "queue"
        if mode == "steer":
            return "steer"
        return "interrupt"

    @staticmethod
    def _load_busy_text_mode() -> str:
        """Resolve normal busy TEXT follow-up behavior.

        ``busy_input_mode`` is the single source of truth (default
        ``interrupt``). The legacy ``busy_text_mode`` knob is honored only
        when a user explicitly set it, so existing queue setups keep
        working; new installs follow ``busy_input_mode``. Returns one of
        ``interrupt`` | ``queue`` (``steer`` is handled upstream by
        ``busy_input_mode`` and maps to non-queue text handling here).
        """
        from gateway.run import _load_gateway_runtime_config
        from gateway.run import GatewayRunner
        # Legacy explicit override wins for backward compat.
        legacy = os.getenv("HERMES_GATEWAY_BUSY_TEXT_MODE", "").strip().lower()
        if not legacy:
            cfg = _load_gateway_runtime_config()
            legacy = str(cfg_get(cfg, "display", "busy_text_mode", default="") or "").strip().lower()
        if legacy == "interrupt":
            return "interrupt"
        if legacy == "queue":
            return "queue"
        # No explicit legacy knob → follow busy_input_mode.
        input_mode = GatewayRunner._load_busy_input_mode()
        return "queue" if input_mode == "queue" else "interrupt"

    @staticmethod
    def _load_restart_drain_timeout() -> float:
        """Load graceful gateway restart/stop drain timeout in seconds."""
        from gateway.run import _load_gateway_runtime_config
        raw = os.getenv("HERMES_RESTART_DRAIN_TIMEOUT", "").strip()
        if not raw:
            cfg = _load_gateway_runtime_config()
            raw = str(cfg_get(cfg, "agent", "restart_drain_timeout", default="") or "").strip()
        value = parse_restart_drain_timeout(raw)
        if raw and value == DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT:
            try:
                float(raw)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid restart_drain_timeout '%s', using default %.0fs",
                    raw,
                    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
                )
        return value

    @staticmethod
    def _load_restart_after_turn_timeout() -> float:
        """Load in-band restart wait-for-idle timeout in seconds (#77184)."""
        from gateway.run import _load_gateway_runtime_config
        env_raw = os.getenv("HERMES_RESTART_AFTER_TURN_TIMEOUT")
        if env_raw is not None and str(env_raw).strip() != "":
            raw: object = env_raw
        else:
            cfg = _load_gateway_runtime_config()
            raw = cfg_get(cfg, "agent", "restart_after_turn_timeout", default=None)
        value = parse_restart_after_turn_timeout(raw)
        # Warn only when the user supplied a non-empty value that failed to
        # parse (parser falls back to the default). ``0`` is valid.
        if raw is not None and str(raw).strip() != "":
            try:
                float(raw)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid restart_after_turn_timeout '%s', using default %.0fs",
                    raw,
                    DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT,
                )
        return value

    @staticmethod
    def _load_background_notifications_mode() -> str:
        """Load background process notification mode from config or env var.

        Modes:
          - ``all``    — push running-output updates *and* the final message (default)
          - ``result`` — only the final completion message (regardless of exit code)
          - ``error``  — only the final message when exit code is non-zero
          - ``off``    — no watcher messages at all
        """
        from gateway.run import _load_gateway_runtime_config
        mode = os.getenv("HERMES_BACKGROUND_NOTIFICATIONS", "")
        if not mode:
            cfg = _load_gateway_runtime_config()
            raw = cfg_get(cfg, "display", "background_process_notifications")
            if raw is False:
                mode = "off"
            elif raw not in {None, ""}:
                mode = str(raw)
        mode = (mode or "all").strip().lower()
        valid = {"all", "result", "error", "off"}
        if mode not in valid:
            logger.warning(
                "Unknown background_process_notifications '%s', defaulting to 'all'",
                mode,
            )
            return "all"
        return mode

    @staticmethod
    def _load_provider_routing() -> dict:
        """Load OpenRouter provider routing preferences from config.yaml."""
        from gateway.run import _load_gateway_runtime_config
        try:
            # Canonical gateway loader (fail-open): managed overlay + ${VAR}
            # expansion now apply to provider_routing too.
            cfg = _load_gateway_runtime_config()
            return cfg.get("provider_routing", {}) or {}
        except Exception:
            pass
        return {}

    @staticmethod
    def _load_fallback_model() -> list | None:
        """Load fallback provider chain from config.yaml.

        Returns the merged effective chain from ``fallback_providers`` plus any
        legacy ``fallback_model`` entries. ``fallback_providers`` stays first
        when both keys are present.
        """
        from gateway.run import _load_gateway_runtime_config
        try:
            # Canonical gateway loader (fail-open): managed overlay + ${VAR}
            # expansion now apply to the fallback chain too.
            cfg = _load_gateway_runtime_config()
            fb = get_fallback_chain(cfg)
            if fb:
                return fb
        except Exception:
            pass
        return None

    def _refresh_fallback_model(self) -> list | None:
        """Re-read fallback_providers from disk for the next agent create/reuse.

        Cron already does this per job via ``get_fallback_chain``; the gateway
        previously froze ``self._fallback_model`` at process start, so a chain
        configured (or changed) after ``hermes gateway`` was running never
        reached messaging sessions even though the same process's cron jobs
        fell back correctly. Fixes #60955.

        A TRANSIENT read/parse failure (user mid-edit of config.yaml with a
        non-atomic write) keeps the last known-good chain instead of wiping a
        cached agent's working fallback for that turn.  Only a successful read
        that genuinely lacks the key clears the chain.
        """
        from gateway.run import _hermes_home
        try:
            from hermes_cli.config import read_user_config_raw
            cfg_path = _hermes_home / "config.yaml"
            if not cfg_path.exists():
                self._fallback_model = None
                return self._fallback_model
            # Raw primitive (raises on parse failure) is required here: the
            # canonical fail-open loader would return {} on a torn mid-edit
            # write and WIPE the last known-good chain. The overlay/expansion
            # below fixes the managed-scope/${VAR} drift without losing that.
            cfg = read_user_config_raw(cfg_path)
            try:
                from hermes_cli import managed_scope
                cfg = managed_scope.apply_managed_overlay(cfg)
            except Exception:
                pass
            try:
                from hermes_cli.config import _expand_env_vars
                expanded = _expand_env_vars(cfg)
                if isinstance(expanded, dict):
                    cfg = expanded
            except Exception:
                pass
        except Exception:
            # Transient failure — keep last known-good chain.
            logger.debug(
                "fallback_providers refresh: config.yaml read failed; "
                "keeping last known-good chain", exc_info=True,
            )
            return self._fallback_model
        self._fallback_model = get_fallback_chain(cfg) or None
        return self._fallback_model

    @staticmethod
    def _apply_fallback_chain_to_agent(agent: Any, chain: list | None) -> None:
        """Keep a cached agent's fallback chain aligned with current config.

        Skips rewrite while a cooldown is holding the agent on an already-
        activated fallback provider — ``restore_primary_runtime`` owns that
        turn-scoped lifecycle. When primary is active (or cooldown expired),
        replace the chain so mid-uptime ``fallback_providers`` edits take
        effect without requiring a gateway restart (#60955).
        """
        if agent is None:
            return
        new_chain = list(chain or [])
        rate_limited_until = getattr(agent, "_rate_limited_until", 0) or 0
        if (
            getattr(agent, "_fallback_activated", False)
            and rate_limited_until > time.monotonic()
        ):
            return
        old_chain = list(getattr(agent, "_fallback_chain", []) or [])
        agent._fallback_chain = new_chain
        agent._fallback_model = new_chain[0] if new_chain else None
        if not getattr(agent, "_fallback_activated", False):
            agent._fallback_index = 0
        # A config edit signals the user changed something — drop the
        # session-scoped unavailability memo so re-configured entries
        # (e.g. credentials added mid-uptime for a previously-failing
        # provider) get retried instead of staying suppressed for the
        # cached agent's lifetime.  Only on actual content change, so
        # the per-message no-op refresh keeps the memo's rate-limiting
        # benefit (#60955).
        if new_chain != old_chain:
            unavailable = getattr(agent, "_unavailable_fallback_keys", None)
            if unavailable:
                unavailable.clear()
