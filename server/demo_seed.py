"""Idempotent, tenant-scoped Silverine dataset for local product testing."""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
from typing import Any

from .auth import hash_password
from .db import json_dump, now
from .quality import content_hash, normalize_name


COMPANY_ID = "company_silverline"
USER_ID = "user_silverline_client"
FIXTURE = Path(__file__).with_name("demo_data") / "silverline.json"


def _parse_stamp(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _rebase_fixture(data: dict) -> dict:
    """Keep the demo narrative recent without changing its relative timing."""
    result = copy.deepcopy(data)
    stamps: list[float] = []

    def collect(value, key=""):
        if isinstance(value, dict):
            for child_key, child in value.items():
                collect(child, child_key)
        elif isinstance(value, list):
            for child in value:
                collect(child, key)
        elif key == "at" or key.endswith("_at"):
            try:
                parsed = _parse_stamp(value)
            except (TypeError, ValueError):
                parsed = None
            if parsed is not None:
                stamps.append(parsed)

    collect(result)
    shift = now() - 3600 - max(stamps) if stamps else 0

    def apply(value, key=""):
        if isinstance(value, dict):
            return {child_key: apply(child, child_key) for child_key, child in value.items()}
        if isinstance(value, list):
            return [apply(child, key) for child in value]
        if key == "at" or key.endswith("_at"):
            try:
                parsed = _parse_stamp(value)
            except (TypeError, ValueError):
                parsed = None
            return parsed + shift if parsed is not None else value
        return value

    return apply(result)


def _load() -> dict:
    return _rebase_fixture(json.loads(FIXTURE.read_text(encoding="utf-8")))


def seed_silverline(db, *, email: str, password: str) -> dict:
    """Replace only the deterministic Silverine tenant and leave all others intact."""
    if len(password) < 10:
        raise ValueError("Demo client password must contain at least 10 characters")
    data = _load()
    company = data["company"]
    stamp = now()

    cleanup = [
        "run_events", "delivery_attempts", "outreach_messages", "linkedin_actions",
        "research", "contacts", "leads", "lead_scans", "selected_countries",
        "company_brain_snapshots", "documents", "products", "outreach_campaigns",
        "cc_rules", "integrations", "data_sources", "exports", "agent_runs",
        "activity_log", "chat_sessions", "onboarding", "company_sections",
    ]
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM auth_sessions WHERE user_id IN "
            "(SELECT id FROM users WHERE company_id=? OR id=?)",
            (COMPANY_ID, USER_ID),
        )
        conn.execute(
            "DELETE FROM password_reset_tokens WHERE user_id IN "
            "(SELECT id FROM users WHERE company_id=? OR id=?)",
            (COMPANY_ID, USER_ID),
        )
        for table in cleanup:
            conn.execute(f"DELETE FROM {table} WHERE company_id=?", (COMPANY_ID,))
        conn.execute("DELETE FROM users WHERE company_id=? OR id=?", (COMPANY_ID, USER_ID))
        conn.execute("DELETE FROM companies WHERE id=?", (COMPANY_ID,))

        conn.execute(
            "INSERT INTO companies VALUES(?,?,?,?,?,?,?)",
            (COMPANY_ID, company["name"], company.get("legal_name"), "active",
             json_dump({"plan": "pilot", "demo_profile": True}), stamp, stamp),
        )
        conn.execute(
            "INSERT INTO users(id,email,password_hash,role,company_id,status,data,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (USER_ID, email.lower(), hash_password(password), "customer", COMPANY_ID, "active",
             json_dump({"name": data["user"].get("name", "Silverline Client")}), stamp, stamp),
        )

        profile_keys = {
            "name", "legal_name", "website", "headquarters_country", "city", "founded_year",
            "industry", "business_model", "employee_count", "main_language",
            "sales_regions_current", "sales_regions_target",
        }
        profile = {key: company.get(key) for key in profile_keys}
        sections = {
            "profile": profile,
            "positioning": company.get("positioning", {}),
            "sales_preferences": company.get("sales_preferences", {}),
            "market_preferences": {
                "target_markets": company.get("sales_regions_target", []),
                "no_outreach_markets": [],
                "no_research_markets": [],
            },
            "internal_sales_data": {"seeded_demo_data": True},
            "current_contacts": {"seeded_contacts": len(data.get("contacts", []))},
            "integrations": {"email": "not_connected", "whatsapp": "not_connected"},
            "brain_review": {"approved_version": data["brain"].get("version", 1)},
        }
        for section, section_data in sections.items():
            conn.execute("INSERT INTO company_sections VALUES(?,?,?,?)",
                         (COMPANY_ID, section, json_dump(section_data), stamp))

        completed_steps = [item["key"] for item in data["onboarding"].get("steps", [])
                           if item.get("status") == "done"]
        step_index = int(data["onboarding"].get("current_step", 0))
        step_names = [item["key"] for item in data["onboarding"].get("steps", [])]
        current_step = step_names[step_index] if step_names else "company-identity"
        conn.execute(
            "INSERT INTO onboarding(company_id,status,current_step,completed_steps,started_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (COMPANY_ID, "in_progress", current_step, json_dump(completed_steps), stamp - 20 * 86400, stamp),
        )

        for document in data.get("documents", []):
            created = _parse_stamp(document.get("uploaded_at")) or stamp
            conn.execute(
                "INSERT INTO documents(id,company_id,document_type,name,content_type,size_bytes,status,data,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (document["id"], COMPANY_ID, document.get("type", "other"), document["name"],
                 "application/octet-stream", int(document.get("size_kb", 0)) * 1024,
                 document.get("status", "processed"), json_dump({"demo_record": True}), created, created),
            )

        for product in data.get("products", []):
            product_data = {key: value for key, value in product.items() if key not in {"id", "name"}}
            product_data["product_name"] = product["name"]
            conn.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?)",
                         (product["id"], COMPANY_ID, product["name"], normalize_name(product["name"]),
                          json_dump(product_data), stamp - 18 * 86400, stamp - 18 * 86400))

        brain = data["brain"]
        snapshots = brain.get("snapshots", [])
        for snapshot in snapshots:
            status = "approved" if snapshot.get("approved") else "archived"
            created = _parse_stamp(snapshot.get("created_at")) or stamp
            conn.execute(
                "INSERT INTO company_brain_snapshots(id,company_id,version,status,content,sources,approved_by,created_at,approved_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (snapshot["id"], COMPANY_ID, snapshot["version"], status,
                 json_dump(brain.get("sections", {})), json_dump([snapshot.get("note", "Demo seed")]),
                 USER_ID if status == "approved" else None, created,
                 _parse_stamp(brain.get("approved_at")) if status == "approved" else None),
            )

        for code in data.get("selected_countries", []):
            conn.execute("INSERT INTO selected_countries VALUES(?,?,?)", (COMPANY_ID, code, stamp))

        for scan in data.get("lead_scans", []):
            created = _parse_stamp(scan.get("created_at")) or stamp
            config = {
                "name": scan.get("name"), "countries": scan.get("countries", []),
                "scan_depth": scan.get("depth", "standard"), "data_sources": scan.get("sources", []),
                "product_ids": scan.get("products", []), "industries": scan.get("industries", []),
                "max_leads_per_country": scan.get("leads_per_country", 50),
            }
            scan_status = "draft" if scan.get("status") == "running" else scan.get("status", "completed")
            conn.execute("INSERT INTO lead_scans VALUES(?,?,?,?,?,?,?)",
                         (scan["id"], COMPANY_ID, scan_status, json_dump(config),
                          None if scan_status == "draft" else scan.get("run_id"), created,
                          _parse_stamp(scan.get("completed_at")) or created))

        for lead in data.get("leads", []):
            created = _parse_stamp(lead.get("created_at")) or stamp
            lead_data = {key: value for key, value in lead.items() if key not in {
                "id", "scan_id", "company_name", "website", "country", "status", "created_at",
            }}
            conn.execute("INSERT INTO leads VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (lead["id"], COMPANY_ID, lead.get("scan_id"), lead["company_name"],
                          lead.get("website"), lead.get("country"), lead.get("status", "new"),
                          int(bool(lead.get("do_not_contact"))), json_dump(lead_data), created, created))

        for research in data.get("research", []):
            created = _parse_stamp(research.get("created_at")) or stamp
            insights = {key: value for key, value in research.items()
                        if key not in {"id", "lead_id", "status", "created_at"}}
            conn.execute("INSERT INTO research VALUES(?,?,?,?,?,?,?,?)",
                         (research["id"], COMPANY_ID, research.get("lead_id"), "succeeded",
                          json_dump(insights), None, created, created))

        for contact in data.get("contacts", []):
            created = _parse_stamp(contact.get("created_at")) or stamp
            safe_email = f"buyer+{contact['id']}@example.test" if contact.get("email") else None
            contact_data = {"full_name": contact.get("name", ""), "title": contact.get("title", "")}
            conn.execute("INSERT INTO contacts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (contact["id"], COMPANY_ID, contact.get("lead_id"), safe_email,
                          contact.get("phone"), contact.get("linkedin_url"),
                          contact.get("email_status", "unverified"), int(bool(contact.get("do_not_contact"))),
                          json_dump(contact_data), created, created))

        for campaign in data.get("campaigns", []):
            created = _parse_stamp(campaign.get("created_at")) or stamp
            campaign_data = {key: value for key, value in campaign.items()
                             if key not in {"id", "name", "status", "created_at"}}
            conn.execute("INSERT INTO outreach_campaigns VALUES(?,?,?,?,?,?,?,?)",
                         (campaign["id"], COMPANY_ID, campaign["name"], "email",
                          campaign.get("status", "draft"), json_dump(campaign_data), created, created))

        for message in data.get("messages", []):
            created = _parse_stamp(message.get("created_at")) or stamp
            recipient = f"buyer+{message['id']}@example.test"
            safe_body = str(message.get("body", "")).translate(str.maketrans({
                "ı": "i", "İ": "I", "ğ": "g", "Ğ": "G", "ş": "s", "Ş": "S",
            }))
            content = {
                "to": recipient, "subject": message.get("subject", ""),
                "body": safe_body, "cc": message.get("cc", []),
                "language": message.get("language", "en"),
            }
            digest = content_hash(content)
            original_status = message.get("status", "draft_generated")
            status = "pending_approval" if original_status == "draft_generated" else original_status
            approved = status in {"sent", "replied"}
            sent_at = _parse_stamp(message.get("sent_at"))
            replied_at = sent_at + 12 * 3600 if status == "replied" and sent_at else None
            conn.execute(
                "INSERT INTO outreach_messages(id,company_id,campaign_id,lead_id,contact_id,channel,status,revision,"
                "content_hash,content,approval_hash,approved_by,approved_at,provider_message_id,sent_at,replied_at,data,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (message["id"], COMPANY_ID, message.get("campaign_id"), message.get("lead_id"),
                 message.get("contact_id"), message.get("channel", "email"), status, 1, digest,
                 json_dump(content), digest if approved else None, USER_ID if approved else None,
                 created if approved else None, f"demo-{message['id']}" if sent_at else None,
                 sent_at, replied_at, json_dump({"demo_history": True}), created, created),
            )

        for rule in data.get("cc_rules", []):
            conn.execute("INSERT INTO cc_rules VALUES(?,?,?,?,?,?)",
                         (rule["id"], COMPANY_ID, rule["name"], json_dump(rule), stamp, stamp))

        conn.execute(
            "INSERT INTO integrations VALUES(?,?,?,?,?,?,?,?,?)",
            ("int_silverline_test", COMPANY_ID, "email", "stub", "connected", None,
             json_dump({"label": "Local test mailbox", "mailbox": "sales@silverline.test",
                        "test_only": True}), stamp, stamp),
        )

        for action in data.get("linkedin_actions", []):
            created = _parse_stamp(action.get("created_at")) or stamp
            conn.execute("INSERT INTO linkedin_actions VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (action["id"], COMPANY_ID, action.get("lead_id"), action.get("contact_id"),
                          action.get("status", "generated"), action.get("profile_url"), action.get("note"),
                          json_dump({"demo_history": True}), created, created))

        valid_run_types = {
            "document_processing", "product_extraction", "company_brain_build", "lead_scan",
            "lead_research", "contact_discovery", "outreach_generation", "email_send",
            "whatsapp_send", "linkedin_note_generation", "analytics_refresh",
        }
        for run in data.get("agent_runs", []):
            run_type = run.get("type")
            if run_type not in valid_run_types:
                continue
            created = _parse_stamp(run.get("created_at")) or stamp
            completed = _parse_stamp(run.get("finished_at")) or created
            conn.execute(
                "INSERT INTO agent_runs(id,company_id,run_type,status,payload,output,created_at,started_at,completed_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (run["id"], COMPANY_ID, run_type, "succeeded", json_dump(run.get("related", {})),
                 json_dump({"demo_history": True}), created, created, completed, completed),
            )

        for activity in data.get("activity", []):
            ref = activity.get("ref", {})
            entity_key = next((key for key in ref if key.endswith("_id")), None)
            entity_type = entity_key[:-3] if entity_key else activity.get("kind")
            conn.execute("INSERT INTO activity_log VALUES(?,?,?,?,?,?,?,?)",
                         (activity["id"], COMPANY_ID, USER_ID, activity.get("label", "activity"),
                          entity_type, ref.get(entity_key) if entity_key else None,
                          json_dump({"kind": activity.get("kind"), "ref": ref}),
                          _parse_stamp(activity.get("at")) or stamp))

    return {
        "company_id": COMPANY_ID,
        "company": company["name"],
        "user_id": USER_ID,
        "email": email.lower(),
        "counts": {
            key: len(data.get(key, []))
            for key in ("products", "documents", "leads", "contacts", "campaigns", "messages", "agent_runs")
        },
    }
