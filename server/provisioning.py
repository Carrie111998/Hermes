"""Safe, idempotent provisioning for a clean demo account."""
from __future__ import annotations

from typing import Any

from .auth import hash_password
from .db import json_dump, new_id, now
from .routes.onboarding import REQUIRED_STEPS
from .schemas import unknown_section_fields


OPERATIONAL_TABLES = (
    "selected_countries", "lead_scans", "leads", "research", "contacts",
    "outreach_campaigns", "outreach_messages", "delivery_attempts", "agent_runs",
    "research_campaigns", "dataset_snapshots", "organizations", "organization_links",
    "evidence_records", "feature_claims", "campaign_partitions", "campaign_metrics",
    "research_issues", "research_results",
)


def assert_clean_tenant(conn, company_id: str) -> None:
    dirty = [
        table for table in OPERATIONAL_TABLES
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE company_id=? LIMIT 1", (company_id,),
        ).fetchone()
    ]
    if dirty:
        raise RuntimeError("demo tenant contains operational data: " + ", ".join(dirty))


def provision_demo_account(
    db,
    *,
    email: str,
    password: str,
    company_profile: dict,
    onboarding_sources: list[dict],
) -> dict:
    """Converge one customer account on a completed, operationally empty tenant."""
    normalized_email = email.strip().lower()
    if not normalized_email:
        raise ValueError("email is required")
    if not isinstance(company_profile, dict) or not company_profile.get("name"):
        raise ValueError("company_profile must include a name")
    # Same §6.1 contract the API enforces. An operator hands this in as a JSON
    # file, where a misspelled field would otherwise be written and ignored.
    unknown = unknown_section_fields("profile", company_profile)
    if unknown:
        raise ValueError(f"company_profile has unknown fields: {', '.join(unknown)}")
    if not isinstance(onboarding_sources, list):
        raise ValueError("onboarding_sources must be a list")

    stamp = now()
    with db.transaction() as conn:
        user = conn.execute(
            "SELECT id,company_id FROM users WHERE lower(email)=lower(?)", (normalized_email,),
        ).fetchone()
        if user and not user["company_id"]:
            raise RuntimeError("demo account email is already assigned outside a customer tenant")
        company_id = user["company_id"] if user else new_id("cmp")
        user_id = user["id"] if user else new_id("usr")
        assert_clean_tenant(conn, company_id)

        company_data = {"public_sources": onboarding_sources}
        conn.execute(
            "INSERT INTO companies(id,name,legal_name,status,data,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name,legal_name=excluded.legal_name,"
            "status=excluded.status,data=excluded.data,updated_at=excluded.updated_at",
            (
                company_id,
                company_profile["name"],
                company_profile.get("legal_name"),
                "active",
                json_dump(company_data),
                stamp,
                stamp,
            ),
        )
        if user:
            conn.execute(
                "UPDATE users SET email=?,password_hash=?,role='customer',company_id=?,status='active',"
                "data=?,updated_at=? WHERE id=?",
                (normalized_email, hash_password(password), company_id, json_dump({}), stamp, user_id),
            )
        else:
            conn.execute(
                "INSERT INTO users(id,email,password_hash,role,company_id,status,data,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    user_id,
                    normalized_email,
                    hash_password(password),
                    "customer",
                    company_id,
                    "active",
                    json_dump({}),
                    stamp,
                    stamp,
                ),
            )

        sections: dict[str, dict[str, Any]] = {
            "profile": {**company_profile, "public_sources": onboarding_sources},
            "positioning": {},
            "products": {},
            "internal_sales_data": {"public_sources": onboarding_sources},
            "market_preferences": {},
        }
        for section, data in sections.items():
            conn.execute(
                "INSERT INTO company_sections(company_id,section,data,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(company_id,section) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
                (company_id, section, json_dump(data), stamp),
            )
        conn.execute(
            "INSERT INTO onboarding(company_id,status,current_step,completed_steps,started_at,completed_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(company_id) DO UPDATE SET status=excluded.status,current_step=excluded.current_step,"
            "completed_steps=excluded.completed_steps,started_at=excluded.started_at,"
            "completed_at=excluded.completed_at,updated_at=excluded.updated_at",
            (
                company_id,
                "completed",
                None,
                json_dump(sorted(REQUIRED_STEPS)),
                stamp,
                stamp,
                stamp,
            ),
        )

    return {
        "company_id": company_id,
        "user_id": user_id,
        "email": normalized_email,
        "onboarding_status": "completed",
    }
