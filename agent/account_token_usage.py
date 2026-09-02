"""Secret-safe provider-account identity for local token accounting.

Only stable, one-way account keys are persisted. OAuth access tokens, raw
provider account IDs, and email addresses never enter the usage database.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from agent.codex_headers import extract_chatgpt_account_id


@dataclass(frozen=True)
class AccountIdentity:
    """A stable persisted key plus an ephemeral display label."""

    account_key: str
    email: Optional[str] = None


def _jwt_claims(access_token: Any) -> dict[str, Any]:
    token = str(access_token or "").strip()
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def codex_account_identity(access_token: Any) -> Optional[AccountIdentity]:
    """Return a stable, non-secret identity for a ChatGPT Codex OAuth token."""

    claims = _jwt_claims(access_token)
    account_id = extract_chatgpt_account_id(access_token)
    if account_id is None:
        return None

    digest = hashlib.sha256(
        f"openai-codex\0{account_id.strip()}".encode("utf-8")
    ).hexdigest()
    profile = claims.get("https://api.openai.com/profile") or {}
    email = profile.get("email") if isinstance(profile, dict) else None
    if not isinstance(email, str) or not email.strip():
        email = claims.get("email")
    normalized_email = email.strip() if isinstance(email, str) and email.strip() else None
    return AccountIdentity(
        account_key=f"openai-codex:{digest}",
        email=normalized_email,
    )


def account_key_for_agent(
    agent: object,
    *,
    request_client: object | None = None,
) -> Optional[str]:
    """Return the stable account key for the credential serving this request.

    Attribution is intentionally fail-closed. Unknown providers, malformed
    tokens, and tokens without a stable provider account id return ``None`` so
    Hermes never files usage against a guessed or misleading identity.
    """
    if getattr(request_client, "is_moa_client", False) is True:
        return None
    provider = str(getattr(agent, "provider", "") or "").strip().lower()
    if provider != "openai-codex":
        return None
    identity = codex_account_identity(str(getattr(agent, "api_key", "") or ""))
    return identity.account_key if identity is not None else None


def _empty_local_usage(account_key: str) -> dict[str, Any]:
    return {
        "account_key": account_key,
        "billing_provider": "openai-codex",
        "api_call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "first_seen": None,
        "last_seen": None,
    }


def build_codex_account_usage_report(
    *,
    entries: Iterable[object],
    local_rows: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Merge current pool identities with forward-only local token totals.

    Pool entries are deduplicated by the stable account key, not by editable
    labels or rotating OAuth token values. The returned object contains no
    credentials or raw provider account ids.
    """
    groups: dict[str, dict[str, Any]] = {}
    for entry in entries:
        identity = codex_account_identity(getattr(entry, "access_token", ""))
        if identity is None:
            continue
        group = groups.setdefault(
            identity.account_key,
            {
                "account_key": identity.account_key,
                "email": identity.email,
                "pool_labels": [],
            },
        )
        if group["email"] is None and identity.email:
            group["email"] = identity.email
        label = str(getattr(entry, "label", "") or "").strip()
        if label and label not in group["pool_labels"]:
            group["pool_labels"].append(label)

    local_by_key = {
        str(row.get("account_key") or ""): dict(row)
        for row in local_rows
        if row.get("account_key")
    }
    for account_key in local_by_key:
        groups.setdefault(
            account_key,
            {"account_key": account_key, "email": None, "pool_labels": []},
        )

    accounts: list[dict[str, Any]] = []
    for account_key, group in groups.items():
        accounts.append(
            {
                **group,
                "local_usage": local_by_key.get(
                    account_key, _empty_local_usage(account_key)
                ),
            }
        )

    accounts.sort(
        key=lambda item: (
            item["local_usage"].get("last_seen") or 0,
            item.get("email") or item["account_key"],
        ),
        reverse=True,
    )
    tracked: list[float] = [
        float(value)
        for row in local_by_key.values()
        if isinstance((value := row.get("first_seen")), (int, float))
    ]
    return {
        "provider": "openai-codex",
        "tracking_mode": "forward_only",
        "attribution_scope": {
            "supported": ["codex_responses", "codex_auxiliary_authoritative"],
            "omitted": [
                "codex_app_server",
                "moa",
                "background_review",
            ],
        },
        "tracking_started_at": min(tracked) if tracked else None,
        "accounts": accounts,
    }


def format_codex_account_usage_report(report: dict[str, Any]) -> str:
    """Render a compact terminal report for ``hermes auth token-usage``."""
    lines = [
        "OpenAI Codex account usage (forward-only local accounting)",
        "Coverage: standard Codex Responses and account-aware auxiliary calls; "
        "app-server, MoA, and background-review calls are omitted.",
    ]
    started = report.get("tracking_started_at")
    if isinstance(started, (int, float)):
        started_text = dt.datetime.fromtimestamp(started).astimezone().isoformat(
            timespec="seconds"
        )
        lines.append(f"Tracking since: {started_text}")
    else:
        lines.append(
            "Tracking since: not started (restart any long-running Hermes process after upgrading)"
        )
    for account in report.get("accounts") or []:
        local = account.get("local_usage") or {}
        label = account.get("email") or account.get("account_key") or "unknown"
        lines.extend(
            [
                "",
                str(label),
                f"  Local API calls: {int(local.get('api_call_count') or 0):,}",
                f"  Local total tokens: {int(local.get('total_tokens') or 0):,}",
                "  Token detail: "
                f"input {int(local.get('input_tokens') or 0):,}, "
                f"cache-read {int(local.get('cache_read_tokens') or 0):,}, "
                f"cache-write {int(local.get('cache_write_tokens') or 0):,}, "
                f"output {int(local.get('output_tokens') or 0):,}, "
                f"reasoning {int(local.get('reasoning_tokens') or 0):,}",
            ]
        )
    return "\n".join(lines)
