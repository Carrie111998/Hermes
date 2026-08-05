"""Response-header usage telemetry (rate limits, credits, cache) for ``AIAgent``

Extracted from ``run_agent.py`` as part of the god-file decomposition
campaign (Phase 3 mechanical mixin lifts).  Behavior-neutral: every method
is lifted verbatim from ``AIAgent``; ``self.*``/``cls.*`` calls resolve
unchanged via the MRO (class attributes referenced through ``cls.`` stay on
``AIAgent``).  The module-level ``logger`` keeps the original logger name
(``"run_agent"``) so log records are unchanged.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from utils import is_truthy_value

logger = logging.getLogger("run_agent")


class UsageTelemetryMixin:
    def _capture_rate_limits(self, http_response: Any) -> None:
        """Parse x-ratelimit-* headers from an HTTP response and cache the state.

        Called after each streaming API call.  The httpx Response object is
        available on the OpenAI SDK Stream via ``stream.response``.
        """
        if http_response is None:
            return
        headers = getattr(http_response, "headers", None)
        if not headers:
            return
        try:
            from agent.rate_limit_tracker import parse_rate_limit_headers
            state = parse_rate_limit_headers(headers, provider=self.provider)
            if state is not None:
                self._rate_limit_state = state
        except Exception:
            pass  # Never let header parsing break the agent loop

    def get_rate_limit_state(self):
        """Return the last captured RateLimitState, or None."""
        return self._rate_limit_state

    def _capture_anthropic_response_headers(self, http_response: Any) -> None:
        """Capture out-of-band state from Anthropic Messages response headers.

        The Anthropic SDK's aggregated ``Message`` drops HTTP headers. Portal
        (and other providers) put rate-limit and credits state there — the same
        families the OpenAI-wire streaming path captures via
        ``stream.response``. Fail-open: each capture swallows its own errors.
        """
        self._capture_rate_limits(http_response)
        self._capture_credits(http_response)

    def _capture_credits(self, http_response: Any) -> None:
        """Parse x-nous-credits-* headers, cache CreditsState, fire threshold notices.

        Fail-open throughout — header issues never break the agent loop. The PARSE is
        swallowed (any error → treated as a miss → keep last-known). The notice
        EVALUATION/EMIT is a SEPARATE block that WARNS on failure (R1-M2): a bug in the
        depletion-notice path must not vanish silently under the parse swallow.
        """
        # Dev test fixture (HERMES_DEV_CREDITS_FIXTURE): inject a chosen notice state
        # each turn for repeatable testing, bypassing real headers. Throwaway scaffolding.
        try:
            from agent.credits_tracker import dev_fixture_credits_state
            _fixture = dev_fixture_credits_state()
        except Exception:
            _fixture = None
        if _fixture is not None:
            self._credits_state = _fixture
            if self._credits_session_start_micros is None:
                self._credits_session_start_micros = _fixture.remaining_micros
            _latch = getattr(self, "_credits_latch", None)
            if isinstance(_latch, dict):
                # Only seen_below_90 — never seen_grant_unspent (priming it would
                # fire grant_spent on a fixture's first observation, the exact
                # every-session nag the gate exists to prevent).
                _latch["seen_below_90"] = True  # let warn90 fire without a real crossing
            _used = _fixture.used_fraction
            logger.info(
                "credits ▸ [FIXTURE] remaining=%d (%s) · paid=%s · denom=%s · used=%s "
                "(real headers bypassed — `echo clear` / unset HERMES_DEV_CREDITS_FIXTURE to restore)",
                _fixture.remaining_micros,
                _fixture.remaining_usd or "?",
                _fixture.paid_access,
                _fixture.denominator_kind,
                ("%.0f%%" % (_used * 100)) if _used is not None else "n/a",
            )
            self._emit_credits_notices()
            return
        if http_response is None:
            return
        headers = getattr(http_response, "headers", None)
        if not headers:
            return
        _dev = is_truthy_value(os.environ.get("HERMES_DEV_CREDITS"))

        # ── Parse (fail-open → miss; never overwrite good state with None) ──
        try:
            from agent.credits_tracker import parse_credits_headers
            state = parse_credits_headers(headers, provider=self.provider)
        except Exception:
            return  # parse error → treat as a miss, keep last-known
        if state is None:
            if _dev:
                logger.info(
                    "credits ▸ response had no valid x-nous-credits-* headers "
                    "(miss — producer off / non-Nous path / >TTL stale)"
                )
            return

        # retain-last-known: only overwrite on a fresh valid parse
        self._credits_state = state
        # Latch session-start remaining the first time we ever see a header
        if self._credits_session_start_micros is None:
            self._credits_session_start_micros = state.remaining_micros
        if _dev:
            # HERMES_DEV_CREDITS: stream each capture to agent.log — watch live with
            # `hermes logs -f` (grep 'credits ▸'). Dev-only; silent for normal users.
            spent = self.get_credits_spent_micros()
            used = state.used_fraction
            logger.info(
                "credits ▸ remaining=%d (%s) · paid=%s · denom=%s · used=%s "
                "· Δspent=%s · age=%s%s",
                state.remaining_micros,
                state.remaining_usd or "?",
                state.paid_access,
                state.denominator_kind,
                ("%.0f%%" % (used * 100)) if used is not None else "n/a",
                ("%.1f¢" % (spent / 10000)) if spent is not None else "n/a",
                ("%.0fs" % state.age_seconds) if state.age_seconds != float("inf") else "n/a",
                (" · disabled=%s" % state.disabled_reason) if state.disabled_reason else "",
            )

        # Threshold notices — shared with the cold-start seed (see _emit_credits_notices).
        self._emit_credits_notices()

    def _emit_credits_notices(self) -> None:
        """Run the threshold policy on the current credits state and emit notices.

        Shared by the warm path (_capture_credits) and the L3 cold-start seed, so a
        session that opens already depleted warns immediately — not only after the first
        inference header. Runs only when a notice consumer is bound (messaging binds none
        → state still cached for /usage, no policy). WARNS on failure rather than
        swallowing (R1-M2): a depletion-path bug must not vanish silently. Emits clears
        FIRST, then shows (so depleted lands last in a latest-wins slot).
        """
        if getattr(self, "notice_callback", None) is None and getattr(self, "notice_clear_callback", None) is None:
            return
        if not self._credits_notices_enabled():
            return
        state = getattr(self, "_credits_state", None)
        if state is None:
            return
        try:
            from agent.credits_tracker import evaluate_credits_notices, is_free_tier_model, new_credits_latch
            latch = getattr(self, "_credits_latch", None)
            if latch is None:
                latch = self._credits_latch = new_credits_latch()
            # Free-model gate: a depleted account on a free model can still
            # inference, so the depleted error banner is suppressed. Local-data
            # only (":free" suffix + pricing-cache peek) — never a network call.
            model_is_free = is_free_tier_model(
                getattr(self, "model", "") or "",
                getattr(self, "base_url", "") or "",
            )
            to_show, to_clear = evaluate_credits_notices(state, latch, model_is_free=model_is_free)
            for key in to_clear:        # clears FIRST …
                self._emit_notice_clear(key)
            for notice in to_show:      # … then shows (depleted lands last in a latest-wins slot)
                self._emit_notice(notice)
        except Exception:
            logger.warning("credits notice evaluation/emit failed", exc_info=True)

    def _credits_notices_enabled(self) -> bool:
        """Whether credits notices are enabled (config display.credits_notices).

        Read once per agent and cached — the policy runs after every API
        response, and the setting governs UI noise, not correctness, so a
        config flip applying on the next session is fine.  Fail-open True
        (preserve current behaviour) on any config error.
        """
        cached = getattr(self, "_credits_notices_enabled_cache", None)
        if cached is not None:
            return cached
        enabled = True
        try:
            from hermes_cli.config import load_config as _load_config
            _cfg = _load_config() or {}
            _display = _cfg.get("display") if isinstance(_cfg, dict) else None
            if isinstance(_display, dict) and "credits_notices" in _display:
                enabled = bool(_display.get("credits_notices"))
        except Exception:
            enabled = True
        self._credits_notices_enabled_cache = enabled
        return enabled

    def get_credits_state(self):
        """Return the last captured CreditsState, or None."""
        return self._credits_state

    def get_credits_spent_micros(self):
        """Session-cumulative micros spent = first_seen_remaining - current_remaining. None if no data."""
        if self._credits_session_start_micros is None or self._credits_state is None:
            return None
        return self._credits_session_start_micros - self._credits_state.remaining_micros

    def _check_openrouter_cache_status(self, http_response: Any) -> None:
        """Read X-OpenRouter-Cache-Status from response headers and log it.

        Increments ``_or_cache_hits`` on HIT so callers can report savings.
        """
        if http_response is None:
            return
        headers = getattr(http_response, "headers", None)
        if not headers:
            return
        try:
            status = headers.get("x-openrouter-cache-status")
            if not status:
                return
            if status.upper() == "HIT":
                self._or_cache_hits += 1
                logger.info("OpenRouter response cache HIT (total: %d)", self._or_cache_hits)
            else:
                logger.debug("OpenRouter response cache %s", status.upper())
        except Exception:
            pass  # Never let header parsing break the agent loop

