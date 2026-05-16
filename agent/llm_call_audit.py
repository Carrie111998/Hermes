"""Best-effort PA LLM call audit writer.

The audit row answers "which tenant, model, and key source made this call"
without storing secrets. Cost is computed from dev.token_pricing when the
Marshal database is reachable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from agent.usage_pricing import CanonicalUsage

logger = logging.getLogger(__name__)

_AUDIT_DISABLED_REASON: str | None = None


def key_fingerprint(api_key: str | None) -> str:
    value = (api_key or "").strip()
    if not value:
        return "none"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _read_marshal_secret(name: str) -> str:
    if not name:
        return ""
    path = Path.home() / ".marshal" / "secrets.env"
    try:
        if not path.exists():
            return ""
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() != name:
                continue
            cleaned = value.strip()
            if (
                len(cleaned) >= 2
                and cleaned[0] == cleaned[-1]
                and cleaned[0] in {"'", '"'}
            ):
                cleaned = cleaned[1:-1]
            return cleaned.strip()
    except Exception as exc:
        logger.debug("Could not read %s from marshal secrets env: %s", name, exc)
    return ""


def _database_url() -> str:
    return (
        os.getenv("PA_LLM_AUDIT_DATABASE_URL", "").strip()
        or os.getenv("SUPABASE_DATABASE_URL", "").strip()
        or _read_marshal_secret("SUPABASE_DATABASE_URL")
    )


def _connect(dsn: str):
    try:
        import psycopg

        return psycopg.connect(dsn, connect_timeout=3)
    except ImportError:
        import psycopg2

        return psycopg2.connect(dsn, connect_timeout=3)


def audit_llm_call(
    *,
    tenant_slug: str | None,
    session_id: str | None,
    provider: str | None,
    model: str,
    api_key: str | None,
    usage: CanonicalUsage,
    metadata: Optional[dict[str, Any]] = None,
) -> bool:
    """Insert one dev.llm_call_audit row. Never raises into model execution."""
    global _AUDIT_DISABLED_REASON
    dsn = _database_url()
    if not dsn:
        if _AUDIT_DISABLED_REASON != "missing-dsn":
            logger.warning("LLM call audit disabled: SUPABASE_DATABASE_URL is unavailable")
            _AUDIT_DISABLED_REASON = "missing-dsn"
        return False

    cached_tokens = int(usage.cache_read_tokens or 0)
    cache_write_tokens = int(usage.cache_write_tokens or 0)
    params = (
        tenant_slug or None,
        session_id or None,
        (provider or "").strip() or None,
        model,
        key_fingerprint(api_key),
        int(usage.input_tokens or 0),
        int(usage.output_tokens or 0),
        cached_tokens,
        cache_write_tokens,
        json.dumps(metadata or {}, sort_keys=True),
    )
    try:
        with _connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO dev.llm_call_audit (
                      tenant_slug,
                      session_id,
                      provider,
                      model,
                      key_fingerprint,
                      input_tokens,
                      output_tokens,
                      cached_tokens,
                      cache_write_tokens,
                      cost_estimate,
                      metadata
                    )
                    VALUES (
                      %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      (
                        SELECT (
                          (
                            %s::numeric * tp.input_usd_per_mtok
                            + %s::numeric * COALESCE(tp.cache_creation_input_usd_per_mtok, tp.input_usd_per_mtok)
                            + %s::numeric * tp.cached_input_usd_per_mtok
                            + %s::numeric * tp.output_usd_per_mtok
                          ) / 1000000
                        )::numeric(18,8)
                        FROM dev.token_pricing tp
                        WHERE tp.model = %s
                          AND now() >= tp.effective_from
                          AND (tp.effective_to IS NULL OR now() < tp.effective_to)
                        ORDER BY tp.effective_from DESC
                        LIMIT 1
                      ),
                      %s::jsonb
                    )
                    """,
                    params[:9]
                    + (
                        int(usage.input_tokens or 0),
                        cache_write_tokens,
                        cached_tokens,
                        int(usage.output_tokens or 0),
                        model,
                        params[9],
                    ),
                )
        return True
    except Exception as exc:
        logger.debug("LLM call audit insert failed: %s", exc)
        return False
