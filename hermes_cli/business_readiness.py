"""Standardized readiness findings for the governed business runtime.

This module is an orchestration layer only. Security, deployment, payment,
compliance, drift, and intervention subsystems remain authoritative for their
own decisions; this projection normalizes their findings for operators.
"""

from __future__ import annotations

import sqlite3
import time
import os
from dataclasses import dataclass
from typing import Any, Mapping

from hermes_cli import business_security, company_email, compliance_db, payments


@dataclass(frozen=True)
class ReadinessFinding:
    code: str
    summary: str
    details: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "summary": self.summary}
        if self.details:
            value.update(dict(self.details))
        return value


def security_findings(config: Mapping[str, Any]) -> list[ReadinessFinding]:
    posture = business_security.evaluate_security_readiness(config)
    if posture.ready:
        return []
    return [
        ReadinessFinding(
            "security_readiness_blocked",
            "Security readiness checks block autonomous operation",
            {
                "violations": list(posture.violations),
                "authority_boundary": "No action was attempted",
            },
        )
    ]


def runtime_findings(snapshot: Mapping[str, Any]) -> list[ReadinessFinding]:
    findings: list[ReadinessFinding] = []
    autonomy = snapshot.get("autonomy") or {}
    if autonomy.get("mode") != "autonomous":
        findings.append(
            ReadinessFinding(
                "autonomy_not_enabled",
                f"Autonomy mode is {autonomy.get('mode')!r}",
            )
        )
    drift = snapshot.get("runtime_drift") or {}
    if drift.get("blocked"):
        findings.append(
            ReadinessFinding(
                "runtime_drift_blocked",
                "Runtime drift gate is blocking autonomous operation",
                {"details": drift},
            )
        )
    return findings


def intervention_findings(snapshot: Mapping[str, Any]) -> list[ReadinessFinding]:
    return [
        ReadinessFinding(
            "advisor_intervention_open",
            str(item.get("summary") or item.get("category")),
            {
                "intervention_id": item.get("id"),
                "category": item.get("category"),
            },
        )
        for item in (snapshot.get("interventions") or [])
        if item.get("status") == "open"
    ]


def payment_findings(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    charter: Mapping[str, Any],
) -> list[ReadinessFinding]:
    capabilities = {str(value) for value in (charter.get("allowed_capabilities") or [])}
    directions = set()
    if "payments.receive" in capabilities:
        directions.add("inbound")
    if "payments.send" in capabilities:
        directions.add("outbound")
    if not directions:
        return []

    findings: list[ReadinessFinding] = []
    rails = payments.payment_rail_status()
    compliance_db.ensure_schema(conn)
    profile = conn.execute(
        "SELECT custody_model FROM compliance_profiles WHERE organization_id=?",
        (organization_id,),
    ).fetchone()
    if profile is None or profile["custody_model"] != "non_custodial":
        findings.append(
            ReadinessFinding(
                "payment_compliance_profile_missing",
                "Non-custodial payment compliance profile is not configured",
            )
        )
    for direction in sorted(directions):
        available = [item for item in rails.get(direction, []) if item.get("available")]
        available_providers = {
            str(item.get("rail_name") or item.get("name") or "").strip()
            for item in available
            if str(item.get("rail_name") or item.get("name") or "").strip()
        }
        if not available:
            findings.append(
                ReadinessFinding(
                    "payment_rail_unavailable",
                    f"No credential-ready {direction} payment rail is available",
                    {"direction": direction, "discovered": rails.get(direction, [])},
                )
            )
        assessed_rows = conn.execute(
            """SELECT provider FROM payment_provider_assessments
               WHERE organization_id=? AND direction=? AND status='verified'
                 AND expires_at>? AND aml_screening_delegated=1
                 AND sanctions_screening_delegated=1
                 AND NOT EXISTS (
                     SELECT 1 FROM payment_provider_assessments newer
                      WHERE newer.supersedes_id = payment_provider_assessments.id
                 )""",
            (organization_id, direction, int(time.time())),
        ).fetchall()
        assessed_providers = {
            str(row["provider"]).strip()
            for row in assessed_rows
            if str(row["provider"]).strip()
        }
        provider_match = (
            not available_providers
            or bool(available_providers & assessed_providers)
        )
        if not assessed_rows or not provider_match:
            findings.append(
                ReadinessFinding(
                    "payment_provider_assessment_missing",
                    f"No current screened provider assessment exists for {direction} payments",
                    {
                        "direction": direction,
                        "available_providers": sorted(available_providers),
                        "assessed_providers": sorted(assessed_providers),
                    },
                )
            )
    return findings


def email_findings(
    config: Mapping[str, Any], *, charter: Mapping[str, Any]
) -> list[ReadinessFinding]:
    """Gate declared company-email authority on the configured provider edge."""
    capabilities = {
        str(value) for value in (charter.get("allowed_capabilities") or [])
    }
    if "email.send" not in capabilities:
        return []
    email = ((charter.get("communications") or {}).get("email") or {})
    provider = str(email.get("provider") or "").strip().lower()
    if provider != "agentmail":
        return [
            ReadinessFinding(
                "company_email_provider_unconfigured",
                "Declared email.send authority has no supported AgentMail provider",
                {"configured_provider": provider or None},
            )
        ]
    if company_email.configured_agentmail(config) is None:
        return [
            ReadinessFinding(
                "company_email_unavailable",
                "AgentMail inbox and API key are required for email.send authority",
                {
                    "provider": "agentmail",
                    "inbox_configured": bool(str(email.get("inbox_id") or "").strip()),
                    "api_key_configured": bool(os.getenv("AGENTMAIL_API_KEY", "").strip()),
                },
            )
        ]
    return []


def authority_store_findings(config: Mapping[str, Any]) -> list[ReadinessFinding]:
    """Check authority store connectivity and schema version.

    Returns a finding if:
    - Postgres is configured but unreachable
    - The schema version is incompatible (behind or ahead)
    - psycopg is not installed but the postgres backend is configured
    """
    from hermes_cli.postgres_authority import get_authority_backend

    backend = get_authority_backend()
    if backend != "postgres":
        return []  # SQLite always available; no connectivity finding needed

    try:
        from hermes_cli.postgres_authority import connect, get_schema_version, SCHEMA_VERSION

        conn = connect()
        try:
            version = get_schema_version(conn)
        finally:
            conn.close()

        if version < SCHEMA_VERSION:
            return [
                ReadinessFinding(
                    "authority_store_schema_outdated",
                    f"Postgres authority schema version {version} < required {SCHEMA_VERSION}",
                    {"current": version, "required": SCHEMA_VERSION,
                     "action": "run: charterforge db upgrade"},
                )
            ]
        if version > SCHEMA_VERSION:
            return [
                ReadinessFinding(
                    "authority_store_schema_too_new",
                    f"Postgres authority schema version {version} > supported {SCHEMA_VERSION}",
                    {"current": version, "supported": SCHEMA_VERSION,
                     "action": "upgrade charterforge package"},
                )
            ]
        return []

    except ImportError:
        return [
            ReadinessFinding(
                "authority_store_driver_missing",
                "Postgres backend configured but psycopg not installed",
                {"action": "pip install charterforge[postgres]"},
            )
        ]
    except Exception as exc:
        # Connectivity failure — surface without leaking credentials
        short = str(exc).split("\n")[0][:120]
        return [
            ReadinessFinding(
                "authority_store_unreachable",
                "Postgres authority store is not reachable",
                {"error": short,
                 "action": "check AUTHORITY_POSTGRES_URL and Postgres service"},
            )
        ]


def project(
    conn: sqlite3.Connection,
    *,
    snapshot: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine subsystem findings into the stable operator projection."""
    if not snapshot.get("configured"):
        return {
            "ready": False,
            "state": "unconfigured",
            "blockers": [
                {
                    "code": "bootstrap_required",
                    "summary": "Solo-founder business has not been bootstrapped",
                }
            ],
            "next_step": snapshot.get("next_step"),
            "source": "authoritative_state",
        }
    organization_id = str(snapshot["organization"]["id"])
    charter = config.get("agentic") or {}
    findings = [
        *security_findings(config),
        *runtime_findings(snapshot),
        *intervention_findings(snapshot),
        *payment_findings(
            conn, organization_id=organization_id, charter=charter
        ),
        *email_findings(config, charter=charter),
        *authority_store_findings(config),
    ]
    deployment = snapshot.get("runtime_deployment") or {}
    autonomy = snapshot.get("autonomy") or {}
    blockers = [finding.as_dict() for finding in findings]
    return {
        "ready": not blockers,
        "state": "ready" if not blockers else "blocked",
        "blockers": blockers,
        "organization_id": organization_id,
        "selected_host": deployment.get("selected_host"),
        "runtime_active": bool(deployment.get("ready")),
        "autonomy_mode": autonomy.get("mode"),
        "source": "authoritative_state",
    }
