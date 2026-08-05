"""Mixin extracted verbatim from ``run_agent.py`` (godfile extraction wave 1).

The methods in this module were moved character-for-character from the
``AIAgent`` class in ``run_agent.py``; class attributes referenced via
``self.``/``cls.`` still resolve through the MRO on ``AIAgent``.
"""

import json
import re
from typing import Any, Dict, Optional

from agent.redact import redact_sensitive_text


class ApiErrorMixin:
    @staticmethod
    def _is_entitlement_failure(
        error_context: Optional[Dict[str, Any]],
        status_code: Optional[int],
    ) -> bool:
        """Detect subscription/entitlement 403s that masquerade as auth failures.

        Returned True only when the body text matches a known entitlement
        shape AND the status is 401/403.  Refreshing an OAuth token cannot
        fix an unsubscribed account, so callers should surface the error
        instead of looping the credential pool.

        Current matches:
          * xAI OAuth: "do not have an active Grok subscription" /
            "out of available resources" / "does not have permission" + "grok"

        Disambiguator for xAI (#29344): the same ``code`` text ("The caller
        does not have permission to execute the specified operation") is
        returned for BOTH an unsubscribed account AND a stale OAuth access
        token.  xAI ships an explicit signal in the ``error`` field that
        tells the two apart: a ``[WKE=unauthenticated:...]`` suffix (and/or
        the ``OAuth2 access token could not be validated`` phrasing) means
        the credentials failed validation — that's recoverable by refreshing
        the token, NOT by surfacing an entitlement message.  When either
        signal is present we return False eagerly so the credential-pool
        refresh path runs, letting long-running TUI sessions recover from
        stale tokens without an exit/reopen cycle.

        Extend here for new providers as we discover them (Anthropic's
        Claude Max OAuth entitlement errors look distinct enough today that
        the existing 1M-context-beta branch handles them; revisit if other
        subscription tiers start producing the same loop signature).
        """
        if status_code not in {401, 403, None}:
            return False
        if not isinstance(error_context, dict):
            return False
        # Build a single lowercase haystack covering every field shape the
        # body might land in.  ``_extract_api_error_context`` normalises to
        # ``message``/``reason``, but callers (and the test suite) may also
        # hand us the raw body with ``code``/``error`` keys; cover both so
        # the WKE disambiguator below fires regardless of entry point.
        message = str(error_context.get("message") or "").lower()
        reason = str(error_context.get("reason") or "").lower()
        code = str(error_context.get("code") or "").lower()
        err = str(error_context.get("error") or "").lower()
        haystack = f"{message} {reason} {code} {err}"
        if not haystack.strip():
            return False
        # xAI's authoritative disambiguator for "stale token" vs
        # "unsubscribed account".  Both conditions share the same
        # permission-denied ``code`` text; only one carries this suffix.
        # Bail out before the entitlement keyword checks so a stale OAuth
        # token routes through the credential-refresh path instead of the
        # surface-error-as-entitlement path.  See #29344 for the long-
        # running TUI failure mode this closes.
        if "[wke=unauthenticated:" in haystack:
            return False
        if "oauth2 access token could not be validated" in haystack:
            return False
        if "do not have an active grok subscription" in haystack:
            return True
        if "out of available resources" in haystack and "grok" in haystack:
            return True
        if "does not have permission" in haystack and "grok" in haystack:
            return True
        return False

    @staticmethod
    def _decorate_xai_entitlement_error(detail: str) -> str:
        """Append a neutral hint when xAI's OAuth surface returns the
        permission-denied 403.

        xAI's ``/v1/responses`` endpoint replies to several distinct failure
        modes with the SAME body::

            {"code": "The caller does not have permission to execute the
             specified operation", "error": "You have either run out of
             available resources or do not have an active Grok subscription.
             Manage subscriptions at https://grok.com/?_s=usage or subscribe
             at https://grok.com/supergrok"}

        That body covers several real causes we cannot distinguish without
        more info from xAI.  The most common (and least obvious) one is
        that **X Premium+ does NOT include API access** — only standalone
        SuperGrok subscribers can use Hermes against xai-oauth.  Lots of
        users see Grok in their X app, assume it works here too, and hit
        this 403 with no idea why.  Lead the hint with that.

        Other possible causes:
          * No Grok subscription at all
          * SuperGrok tier doesn't include the requested model (e.g.
            grok-4.3 may need a higher tier)
          * Monthly quota exhausted (the ``?_s=usage`` URL hints at this)

        Surface the raw xAI text verbatim and point at
        https://grok.com/?_s=usage where the user can see WHICH applies.

        Matched once per detail string — won't double-decorate if the
        upstream already concatenated the same text.
        """
        if not detail:
            return detail
        lower = detail.lower()
        is_entitlement = (
            "do not have an active grok subscription" in lower
            or ("out of available resources" in lower and "grok" in lower)
            or ("does not have permission" in lower and "grok" in lower)
        )
        if not is_entitlement:
            return detail
        hint = (
            " — xAI rejected this OAuth account. NOTE: X Premium+ does NOT "
            "include xAI API access — only standalone SuperGrok subscribers "
            "can use this provider. Other possible causes: no Grok "
            "subscription, your tier doesn't include this model, or your "
            "quota is exhausted. Check https://grok.com/?_s=usage to see "
            "which, or run `/model` to switch providers."
        )
        # Idempotency: detect prior decoration by a substring unique to the
        # hint (not present in xAI's own body text).
        if "X Premium+ does NOT include" in detail:
            return detail
        return f"{detail}{hint}"

    @staticmethod
    def _coerce_api_error_detail(value: Any) -> str:
        """Return a display-safe string for structured provider error fields."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("message", "detail", "error", "code", "type"):
                nested = value.get(key)
                if isinstance(nested, str) and nested.strip():
                    return nested
            for key in ("message", "detail", "error", "code", "type"):
                if key in value:
                    nested_detail = AIAgent._coerce_api_error_detail(value[key])
                    if nested_detail:
                        return nested_detail
            try:
                return json.dumps(value, ensure_ascii=False, sort_keys=True)
            except TypeError:
                return str(value)
        if isinstance(value, (list, tuple)):
            parts = [
                AIAgent._coerce_api_error_detail(item)
                for item in value
            ]
            return "; ".join(part for part in parts if part)
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _summarize_api_error(error: Exception) -> str:
        """Extract a human-readable one-liner from an API error.

        Handles Cloudflare HTML error pages (502, 503, etc.) by pulling the
        <title> tag instead of dumping raw HTML.  Falls back to a truncated
        str(error) for everything else.
        """
        raw = str(error)

        if (
            isinstance(error, ValueError)
            and "expected ident at line" in raw.lower()
        ):
            return f"Malformed provider streaming response: {raw[:300]}"

        # Cloudflare / proxy HTML pages: grab the <title> for a clean summary
        if "<!DOCTYPE" in raw or "<html" in raw:
            m = re.search(r"<title[^>]*>([^<]+)</title>", raw, re.IGNORECASE)
            title = m.group(1).strip() if m else "HTML error page (title not found)"
            # Also grab Cloudflare Ray ID if present
            ray = re.search(r"Cloudflare Ray ID:\s*<strong[^>]*>([^<]+)</strong>", raw)
            ray_id = ray.group(1).strip() if ray else None
            status_code = getattr(error, "status_code", None)
            parts = []
            if status_code:
                parts.append(f"HTTP {status_code}")
            parts.append(title)
            if ray_id:
                parts.append(f"Ray {ray_id}")
            return " — ".join(parts)

        # GeminiAPIError (agent/gemini_native_adapter.py) already composes a
        # clean one-liner and may have appended actionable guidance (free-tier
        # 429, legacy Standard-key 401). Prefer its message over re-extracting
        # the raw response body below, which would strip that guidance.
        if type(error).__name__ == "GeminiAPIError":
            return redact_sensitive_text(raw[:1000])

        # JSON body errors from OpenAI/Anthropic SDKs
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            msg = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else body.get("message")
            if msg:
                status_code = getattr(error, "status_code", None)
                prefix = f"HTTP {status_code}: " if status_code else ""
                msg = AIAgent._coerce_api_error_detail(msg)
                return AIAgent._decorate_xai_entitlement_error(f"{prefix}{msg[:300]}")

        # SDK may leave body empty while httpx still has the payload (#36109).
        # Redact before returning: the raw provider/proxy error body is
        # attacker-influenced and may echo Authorization / x-api-key / request
        # JSON, which would otherwise leak into final_response + logs (this path
        # widens exposure vs the old empty-body "HTTP 400" string).
        response = getattr(error, "response", None)
        if response is not None:
            try:
                snippet = (getattr(response, "text", None) or "").strip()
            except Exception:
                snippet = ""
            if snippet:
                status_code = getattr(error, "status_code", None)
                prefix = f"HTTP {status_code}: " if status_code else ""
                try:
                    payload = json.loads(snippet)
                except (json.JSONDecodeError, TypeError):
                    payload = None
                if isinstance(payload, dict):
                    err = payload.get("error")
                    if isinstance(err, dict) and err.get("message"):
                        return redact_sensitive_text(f"{prefix}{str(err['message'])[:300]}")
                    if payload.get("message"):
                        return redact_sensitive_text(f"{prefix}{str(payload['message'])[:300]}")
                return redact_sensitive_text(f"{prefix}{snippet[:300]}")

        # Fallback: truncate the raw string but give more room than 200 chars
        status_code = getattr(error, "status_code", None)
        prefix = f"HTTP {status_code}: " if status_code else ""
        return AIAgent._decorate_xai_entitlement_error(f"{prefix}{raw[:500]}")

    def _mask_api_key_for_logs(self, key: Any) -> Optional[str]:
        # Azure Foundry Entra ID bearer providers are callables — never
        # invoke them in log paths; identify the auth surface instead.
        if callable(key) and not isinstance(key, str):
            return "<entra-id-bearer>"
        if not key:
            return None
        if len(key) <= 12:
            return "***"
        return f"{key[:8]}...{key[-4:]}"

    def _clean_error_message(self, error_msg: str) -> str:
        """
        Clean up error messages for user display, removing HTML content and truncating.
        
        Args:
            error_msg: Raw error message from API or exception
            
        Returns:
            Clean, user-friendly error message
        """
        if not error_msg:
            return "Unknown error"
            
        # Remove HTML content (common with CloudFlare and gateway error pages)
        if error_msg.strip().startswith('<!DOCTYPE html') or '<html' in error_msg:
            return "Service temporarily unavailable (HTML error page returned)"
            
        # Remove newlines and excessive whitespace
        cleaned = ' '.join(error_msg.split())
        
        # Truncate if too long
        if len(cleaned) > 150:
            cleaned = cleaned[:150] + "..."
            
        return cleaned

    @staticmethod
    def _extract_api_error_context(error: Exception) -> Dict[str, Any]:
        """Forwarder — see ``agent.agent_runtime_helpers.extract_api_error_context``."""
        from agent.agent_runtime_helpers import extract_api_error_context
        return extract_api_error_context(error)


# ``AIAgent`` lives in ``run_agent.py``, which imports this module at the
# top; resolve it lazily so the moved staticmethods can reference it
# verbatim at call time without a circular import.
class _LazyAIAgent:
    def __getattr__(self, name):
        from run_agent import AIAgent
        return getattr(AIAgent, name)


AIAgent = _LazyAIAgent()
