"""Registry of the 11 agent run types (PRODUCT.md §7.24) → skill + prompt.

Each run type maps to a skill in skills/sales/ and a prompt builder that turns
the run payload into the agent instruction. The company pack directory is
injected so the agent reads that tenant's identity/rules/templates.

This is the single source of truth the dispatcher uses; adding a run type is
one entry here, not a code change elsewhere.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, Optional

REPO = Path(__file__).resolve().parent.parent
PACKS = REPO / "company-packs"

# Read-only run types never contact the outside world; send types do (gated by
# approval). analytics_refresh has no skill — pure DB aggregation.
READ_ONLY = {
    "document_processing", "product_extraction", "company_brain_build",
    "lead_scan", "lead_research", "contact_discovery", "outreach_generation",
    "linkedin_note_generation",
}
SEND_TYPES = {"email_send", "whatsapp_send"}


def _pack_dir(company: str) -> Path:
    d = PACKS / company
    if not d.is_dir():
        raise ValueError(f"unknown company pack: {company} (looked in {d})")
    return d


def _ctx(company: str, context: dict | None = None) -> str:
    """Common tenant context.

    SaaS runs pass a database-derived context object. The local CLI can still
    use a scrubbed demo company pack, but production never maps another tenant
    onto Silverline's files.
    """
    if context is not None:
        return (
            f"You are the Sales Agent for company '{company}'. The following "
            "tenant-scoped Company Brain context was loaded by the server. Use "
            "only this tenant's data, honor its market preferences and business "
            "rules, and never inspect another company directory.\n"
            f"COMPANY_CONTEXT:\n{_p(context)}\nPAYLOAD:\n"
        )
    d = _pack_dir(company)
    return (
        f"You are the Sales Agent for company '{company}'. Its Company Brain "
        f"pack is at {d} (company.yaml, business-rules.md, "
        f"market-preferences.yaml, cc-rules.yaml, templates/). Read what you "
        f"need from it. Honor the client's market preferences (target / "
        f"no-outreach / no-research markets) and the industry exclusion filters "
        f"in every step. Payload for this run:\n"
    )


def _p(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


# Each builder: (company, payload) -> prompt string.
def _document_processing(company, payload, context=None):
    return (_ctx(company, context) + _p(payload) + "\n\nUsing the document-processing "
            "skill, extract validated structured records from the document(s) "
            "in the payload. Output JSON: {records:[...], rejects:[{row,reason}]}.")


def _product_extraction(company, payload, context=None):
    return (_ctx(company, context) + _p(payload) + "\n\nUsing the document-processing "
            "skill (catalog path), extract product records only. Dedupe by "
            "normalized product name. Output JSON: {products:[...]}.")


def _company_brain_build(company, payload, context=None):
    return (_ctx(company, context) + _p(payload) + "\n\nUsing the company-brain-build "
            "skill, synthesize the seven brain sections from the pack and any "
            "processed records in the payload. Output JSON with keys: "
            "product_understanding, ideal_customer_profile, buyer_roles, "
            "market_assumptions, sales_arguments, business_rules_digest, "
            "missing_data. This is a draft snapshot.")


def _lead_scan(company, payload, context=None):
    return (_ctx(company, context) + _p(payload) + "\n\nUsing the lead-discovery skill, "
            "scan the payload countries (already territory-checked) for the "
            "target segments. Dedupe. Output JSON: {leads:[...], "
            "dropped_duplicates:int, excluded_by_industry:int}.")


def _lead_research(company, payload, context=None):
    return (_ctx(company, context) + _p(payload) + "\n\nUsing the lead-research skill, "
            "research the lead in the payload against the Company Brain. Output "
            "JSON: {profile, fit, signals, approach_angle, score_inputs}.")


def _contact_discovery(company, payload, context=None):
    return (_ctx(company, context) + _p(payload) + "\n\nUsing the contact-discovery "
            "skill, find buyer-role contacts for the lead(s). Respect the "
            "per-company cap. Output JSON: {contacts:[{name,title,email,phone,"
            "linkedin_url,verification,source}]}.")


def _outreach_generation(company, payload, context=None):
    channel = payload.get("channel", "email")
    skill = "whatsapp-outreach" if channel == "whatsapp" else "cold-email-outreach"
    return (_ctx(company, context) + _p(payload) + f"\n\nUsing the {skill} skill, compose "
            "an outreach message for the lead/contact in the payload. Run the "
            "preflight QA checklist. Output JSON: {subject, body, language, "
            "to, cc, qa_verdict:{pass:bool, failures:[...]}}. Do NOT send.")


def _email_send(company, payload, context=None):
    return (_ctx(company, context) + _p(payload) + "\n\nThe message is approved. Hand it "
            "to the email provider adapter for the tenant. Output JSON: "
            "{provider_message_id, status}.")


def _whatsapp_send(company, payload, context=None):
    return (_ctx(company, context) + _p(payload) + "\n\nThe message is approved. Send via "
            "the WhatsApp Business adapter, verifying delivery before any retry. "
            "Output JSON: {provider_message_id, status}.")


def _linkedin_note(company, payload, context=None):
    return (_ctx(company, context) + _p(payload) + "\n\nUsing the linkedin-notes skill, "
            "find the canonical /in/ profile and generate a connection note in "
            "the contact's language. Output JSON: {profile_url, note}. Manual "
            "send by the user; do not automate.")


# run_type -> (skill or None, prompt_builder or None)
REGISTRY: Dict[str, tuple] = {
    "document_processing":     ("document-processing", _document_processing),
    "product_extraction":      ("document-processing", _product_extraction),
    "company_brain_build":     ("company-brain-build", _company_brain_build),
    "lead_scan":               ("lead-discovery",      _lead_scan),
    "lead_research":           ("lead-research",       _lead_research),
    "contact_discovery":       ("contact-discovery",   _contact_discovery),
    "outreach_generation":     ("cold-email-outreach", _outreach_generation),
    "email_send":              ("cold-email-outreach", _email_send),
    "whatsapp_send":           ("whatsapp-outreach",   _whatsapp_send),
    "linkedin_note_generation":("linkedin-notes",      _linkedin_note),
    "analytics_refresh":       (None, None),  # DB aggregation, no agent
}


def build(run_type: str, company: str, payload: dict,
          context: dict | None = None) -> tuple[Optional[str], Optional[str]]:
    """Return (skill, prompt). skill/prompt are None for analytics_refresh."""
    if run_type not in REGISTRY:
        raise ValueError(f"unknown run_type: {run_type}. "
                         f"Known: {sorted(REGISTRY)}")
    skill, builder = REGISTRY[run_type]
    if builder is None:
        return None, None
    # territory gate for lead_scan is enforced at creation, not here (see cli)
    return skill, builder(company, payload, context)
