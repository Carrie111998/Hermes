"""Clamp session reasoning overrides to what the runtime can actually send.

Desktop composer state can persist a Hermes-wide effort (``ultra``, ``max``,
``minimal``) that a later provider rejects with HTTP 400. When we know the
provider's accepted set, drop the override so the session builds from
config.yaml instead of shipping an invalid request.

Unknown providers return the parsed override unchanged — custom OpenAI-compat
endpoints (GLM/ARK) accept ``max`` / ``xhigh`` and must not be guessed at.
"""

from __future__ import annotations

from hermes_constants import parse_reasoning_effort

# Codex Responses documents this closed set. Sending ultra/max/minimal 400s
# with "field ReasoningEffort invalid, should be one of: low, medium, high,
# xhigh, none" (#87036).
_CODEX_REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh")
_CODEX_PROVIDERS = frozenset({"openai-codex", "codex", "openai_codex"})
_GITHUB_PROVIDERS = frozenset(
    {"github", "github-models", "github-copilot", "copilot"}
)


def _canonical_provider(provider: str | None) -> str:
    slug = str(provider or "").strip().lower()
    if not slug:
        return ""
    try:
        from hermes_cli.providers import normalize_provider

        return str(normalize_provider(slug) or slug).strip().lower()
    except Exception:
        return slug


def parse_create_reasoning_override(
    effort,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> dict | None:
    """Parse a session.create / config.set effort, or None to inherit config."""
    try:
        parsed = parse_reasoning_effort(effort)
    except Exception:
        return None
    return supported_session_reasoning_override(
        parsed, provider=provider, model=model
    )


def supported_session_reasoning_override(
    parsed: dict | None,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> dict | None:
    """Return *parsed* when the runtime can honor it, else None (use config)."""
    if not isinstance(parsed, dict):
        return None
    if parsed.get("enabled") is False:
        return parsed
    requested = str(parsed.get("effort") or "").strip().lower()
    if not requested:
        return parsed
    supported = lookup_supported_reasoning_efforts(provider, model)
    if supported is None or requested in supported:
        return parsed
    return None


def lookup_supported_reasoning_efforts(
    provider: str | None,
    model: str | None,
) -> tuple[str, ...] | None:
    """Known accepted efforts, or None when the catalog cannot say."""
    slug = _canonical_provider(provider)
    if slug in _CODEX_PROVIDERS or slug == "openai-codex":
        return _CODEX_REASONING_EFFORTS
    if slug in _GITHUB_PROVIDERS or slug == "github-copilot":
        try:
            from hermes_cli.models import github_model_reasoning_efforts

            found = github_model_reasoning_efforts(model or "")
        except Exception:
            return None
        if not found:
            return None
        return tuple(str(level).strip().lower() for level in found if str(level).strip())
    return None


def reasoning_effort_label(parsed: dict | None) -> str:
    """Wire/UI token for a parsed reasoning_config dict."""
    if not isinstance(parsed, dict):
        return ""
    if parsed.get("enabled") is False:
        return "none"
    return str(parsed.get("effort") or "").strip().lower()
