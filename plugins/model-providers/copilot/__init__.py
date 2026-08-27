"""Copilot / GitHub Models provider profile.

Copilot uses per-model api_mode routing:
  - GPT-5+ / Codex models → codex_responses
  - Claude models → anthropic_messages
  - Everything else → chat_completions (this profile covers that subset)

Key quirks for the chat_completions subset:
  - Editor attribution headers (via copilot_default_headers())
  - GitHub Models reasoning extra_body (model-catalog gated)
"""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

COPILOT_USAGE_URL = "https://api.github.com/copilot_internal/user"
# The endpoint is Copilot-client-only and gates on these headers; the values
# mirror what the editor plugin sends.
COPILOT_EDITOR_VERSION = "vscode/1.96.2"
COPILOT_API_VERSION = "2025-04-01"



class CopilotProfile(ProviderProfile):
    """GitHub Copilot / GitHub Models — editor headers + reasoning."""

    def build_api_kwargs_extras(
        self,
        *,
        model: str | None = None,
        reasoning_config: dict | None = None,
        supports_reasoning: bool = False,
        **ctx,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        if supports_reasoning and model:
            try:
                from hermes_cli.models import github_model_reasoning_efforts

                supported_efforts = github_model_reasoning_efforts(model)
                if supported_efforts and reasoning_config:
                    effort = reasoning_config.get("effort", "medium")
                    # Honor the requested level when the live Copilot catalog
                    # lists it as supported: gpt-5.5/gpt-5.4 DO support
                    # ``xhigh``. Otherwise clamp to the nearest WEAKER
                    # supported level via the shared ladder helper — the old
                    # ad-hoc rules dropped everything unrecognized to
                    # ``medium``, which inverted the ladder: ``ultra`` (the
                    # strongest ask) resolved weaker than an explicit
                    # ``high`` (#74295).
                    if effort not in supported_efforts:
                        from hermes_cli.models import (
                            clamp_reasoning_effort_to_supported,
                        )

                        effort = clamp_reasoning_effort_to_supported(
                            effort, list(supported_efforts)
                        )
                        if effort not in supported_efforts:
                            # Unrecognized/bespoke level the ladder can't
                            # place — fall back to medium, then to the
                            # catalog's first entry.
                            effort = (
                                "medium"
                                if "medium" in supported_efforts
                                else supported_efforts[0]
                            )
                    if effort in supported_efforts:
                        extra_body["reasoning"] = {"effort": effort}
                elif supported_efforts:
                    extra_body["reasoning"] = {"effort": "medium"}
            except Exception:
                pass
        return extra_body, {}


    def fetch_usage(
        self,
        *,
        credential=None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ):
        """Copilot plan quotas — counts of interactions, not percent or money.

        ``GET api.github.com/copilot_internal/user`` returns a
        ``quota_snapshots`` map (chat / completions / premium_interactions),
        each with ``entitlement`` and ``remaining``, plus one shared monthly
        reset date. Snapshots flagged ``has_quota: false`` are plan features
        the account does not have — skipped rather than drawn as a full bar.

        The token is the aggregator's resolved pool entry, which for this
        provider is the EXCHANGED Copilot token: re-deriving it here would mean
        re-running the exchange, which is exactly the side effect the two-phase
        design exists to avoid.
        """
        import httpx

        from agent.provider_usage_types import (
            UNIT_COUNT,
            ProviderUsage,
            UsageWindow,
            to_datetime,
            to_decimal,
        )

        token = str(getattr(credential, "access_token", "") or "").strip()
        if not token:
            return None

        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                COPILOT_USAGE_URL,
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/json",
                    "Editor-Version": COPILOT_EDITOR_VERSION,
                    "X-GitHub-Api-Version": COPILOT_API_VERSION,
                },
            )
            response.raise_for_status()
            payload = response.json() or {}

        reset_at = to_datetime(payload.get("quota_reset_date_utc"))
        snapshots = payload.get("quota_snapshots")
        windows = []
        if isinstance(snapshots, dict):
            for quota_id, snapshot in sorted(snapshots.items()):
                if not isinstance(snapshot, dict) or not snapshot.get("has_quota"):
                    continue
                windows.append(
                    UsageWindow(
                        label=str(quota_id),
                        unit=UNIT_COUNT,
                        limit=to_decimal(snapshot.get("entitlement")),
                        remaining=to_decimal(snapshot.get("remaining")),
                        reset_at=reset_at,
                    )
                )

        plan = str(payload.get("copilot_plan") or "").title() or None

        return ProviderUsage(
            provider="copilot",
            display_name="GitHub Copilot",
            plan=plan,
            windows=tuple(windows),
        )


copilot = CopilotProfile(
    name="copilot",
    aliases=("github-copilot", "github-models", "github-model", "github"),
    env_vars=("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),
    base_url="https://api.githubcopilot.com",
    auth_type="copilot",
    # Monthly entitlements — they barely move within a session.
    usage_ttl=600,
)

register_provider(copilot)
