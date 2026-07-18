from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth import Principal, company_scope, current_principal
from ..db import json_dump, json_load, new_id, now
from ..outreach_service import message_dict
from ..quality import canonical_linkedin_url
from .sales_intelligence import ContactCreate, LeadCreate, create_contact, create_lead, get_contact, get_lead


router = APIRouter(tags=["outreach"])


class CampaignCreate(BaseModel):
    name: str
    channel: str = "email"
    lead_ids: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class CampaignPatch(BaseModel):
    name: str | None = None
    channel: str | None = None
    lead_ids: list[str] | None = None
    data: dict[str, Any] | None = None


class GenerateMessages(BaseModel):
    lead_ids: list[str] | None = None
    language: str = "en"


class CampaignSend(BaseModel):
    mode: str = "draft"


class MessagePatch(BaseModel):
    content: dict[str, Any]


class MessageDelivery(BaseModel):
    message_id: str


class BulkDelivery(BaseModel):
    message_ids: list[str] = Field(min_length=1, max_length=100)
    mode: str = "send"


class CustomOutreach(BaseModel):
    lead: dict[str, Any]
    contact: dict[str, Any]
    product_id: str | None = None
    language: str = "en"
    mode: str = "draft"
    cc_rule: str | None = None


class CCRuleCreate(BaseModel):
    name: str
    market_country: str | None = None
    market_region: str | None = None
    product_id: str | None = None
    industry: str | None = None
    cc_emails: list[str] = Field(default_factory=list)
    is_default: bool = False


class WhatsAppGenerate(BaseModel):
    lead_id: str
    contact_id: str
    language: str = "en"
    template_name: str | None = None


class LinkedInFind(BaseModel):
    lead_id: str | None = None
    contact_id: str


class LinkedInNote(BaseModel):
    lead_id: str | None = None
    contact_id: str
    language: str = "en"


def _scope(principal: Principal, header: str | None) -> str:
    return company_scope(principal, header)


def _campaign(row) -> dict:
    return {"id": row["id"], "company_id": row["company_id"], "name": row["name"],
            "channel": row["channel"], "status": row["status"], "data": json_load(row["data"], {}),
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


@router.get("/outreach/campaigns")
def campaigns(request: Request, principal: Principal = Depends(current_principal),
              x_company_id: str | None = Header(default=None)):
    return [_campaign(row) for row in request.app.state.db.all(
        "SELECT * FROM outreach_campaigns WHERE company_id=? ORDER BY created_at DESC",
        (_scope(principal, x_company_id),),
    )]


@router.post("/outreach/campaigns", status_code=201)
def create_campaign(body: CampaignCreate, request: Request,
                    principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    company_id, campaign_id, stamp = _scope(principal, x_company_id), new_id("camp"), now()
    data = {**body.data, "lead_ids": body.lead_ids}
    request.app.state.db.execute("INSERT INTO outreach_campaigns VALUES(?,?,?,?,?,?,?,?)",
                                 (campaign_id, company_id, body.name, body.channel, "draft",
                                  json_dump(data), stamp, stamp))
    return get_campaign(campaign_id, request, principal, x_company_id)


@router.get("/outreach/campaigns/{campaign_id}")
def get_campaign(campaign_id: str, request: Request, principal: Principal = Depends(current_principal),
                 x_company_id: str | None = Header(default=None)):
    row = request.app.state.db.one("SELECT * FROM outreach_campaigns WHERE id=? AND company_id=?",
                                   (campaign_id, _scope(principal, x_company_id)))
    if not row:
        raise HTTPException(404, "Campaign not found")
    return _campaign(row)


@router.patch("/outreach/campaigns/{campaign_id}")
def patch_campaign(campaign_id: str, body: CampaignPatch, request: Request,
                   principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = request.app.state.db.one("SELECT * FROM outreach_campaigns WHERE id=? AND company_id=?",
                                   (campaign_id, company_id))
    if not row:
        raise HTTPException(404, "Campaign not found")
    values = body.model_dump(exclude_unset=True)
    data_patch = values.pop("data", None) or {}
    lead_ids = values.pop("lead_ids", None)
    data = {**json_load(row["data"], {}), **data_patch}
    if lead_ids is not None:
        data["lead_ids"] = lead_ids
    values["data"], values["updated_at"] = json_dump(data), now()
    request.app.state.db.execute(
        f"UPDATE outreach_campaigns SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
        (*values.values(), campaign_id),
    )
    return get_campaign(campaign_id, request, principal, x_company_id)


@router.delete("/outreach/campaigns/{campaign_id}", status_code=204)
def delete_campaign(campaign_id: str, request: Request,
                    principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    if not request.app.state.db.execute("DELETE FROM outreach_campaigns WHERE id=? AND company_id=?",
                                        (campaign_id, _scope(principal, x_company_id))):
        raise HTTPException(404, "Campaign not found")


def _generation_run(company_id: str, request: Request, *, lead_id: str, contact_id: str,
                    campaign_id: str | None = None, channel: str = "email", language: str = "en"):
    lead = request.app.state.db.one("SELECT * FROM leads WHERE id=? AND company_id=?", (lead_id, company_id))
    contact = request.app.state.db.one("SELECT * FROM contacts WHERE id=? AND company_id=?", (contact_id, company_id))
    if not lead or not contact:
        raise HTTPException(422, "Lead or contact not found")
    if lead["do_not_contact"] or contact["do_not_contact"]:
        raise HTTPException(409, "Lead or contact is do-not-contact")
    if channel == "email" and not contact["email"]:
        raise HTTPException(422, "Contact has no email address")
    to = contact["email"] if channel == "email" else contact["phone"]
    cc = _resolve_cc(company_id, lead, contact, request) if channel == "email" else []
    subject, body = _template_for(company_id, request, language, lead, contact)
    payload = {
        "campaign_id": campaign_id, "lead_id": lead_id, "contact_id": contact_id,
        "channel": channel, "language": language, "to": to,
        "recipients": {"to": to, "cc": cc},
        "delivery_context": {"country": lead["country"]},
        "draft_content": {"to": to, "cc": cc, "language": language,
                          "subject": subject, "body": body},
    }
    run = request.app.state.runs.create(company_id, "outreach_generation", payload)
    return request.app.state.runs.start(company_id, run["id"])


def _template_for(company_id: str, request: Request, language: str, lead, contact) -> tuple[str, str]:
    """UI-authored template for the language, with {{placeholder}} substitution.

    Falls back to English, then a generic partnership draft. Placeholders left
    unresolved are caught by the deterministic preflight, not silently sent.
    """
    section = request.app.state.db.one(
        "SELECT data FROM company_sections WHERE company_id=? AND section='email_templates'",
        (company_id,),
    )
    templates = (json_load(section["data"], {}) if section else {}).get("templates", {})
    tpl = templates.get(language) or templates.get("en") or {}
    subject = tpl.get("subject") or "Partnership opportunity"
    body = tpl.get("body") or (
        f"Hello, we would like to explore a potential partnership with {lead['company_name']}.")
    contact_data = json_load(contact["data"], {}) if contact["data"] else {}
    fields = {
        "company_name": lead["company_name"] or "",
        "country": lead["country"] or "",
        "contact_name": contact_data.get("name") or contact_data.get("full_name") or "",
        "contact_title": contact_data.get("title") or "",
    }
    for key, value in fields.items():
        token = "{{" + key + "}}"
        subject = subject.replace(token, value)
        body = body.replace(token, value)
    return subject, body


def _resolve_cc(company_id: str, lead, primary_contact, request: Request) -> list[str]:
    addresses = [row["email"] for row in request.app.state.db.all(
        "SELECT email FROM contacts WHERE company_id=? AND lead_id=? AND id<>? "
        "AND email IS NOT NULL AND do_not_contact=0 AND status<>'invalid'",
        (company_id, lead["id"], primary_contact["id"]),
    )]
    rules = request.app.state.db.all("SELECT data FROM cc_rules WHERE company_id=?", (company_id,))
    matching, defaults = [], []
    for row in rules:
        rule = json_load(row["data"], {})
        if rule.get("is_default"):
            defaults.append(rule)
        if rule.get("market_country") and str(rule["market_country"]).upper() == str(lead["country"]).upper():
            matching.append(rule)
    for rule in matching or defaults:
        addresses.extend(rule.get("cc_emails", []))
    primary = str(primary_contact["email"] or "").lower()
    return list(dict.fromkeys(address for address in addresses if address and address.lower() != primary))


@router.post("/outreach/campaigns/{campaign_id}/generate-messages", status_code=202)
def generate_campaign_messages(campaign_id: str, body: GenerateMessages, request: Request,
                               principal: Principal = Depends(current_principal),
                               x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    campaign = get_campaign(campaign_id, request, principal, x_company_id)
    lead_ids = body.lead_ids or campaign["data"].get("lead_ids", [])
    runs = []
    for lead_id in lead_ids:
        contact = request.app.state.db.one(
            "SELECT * FROM contacts WHERE company_id=? AND lead_id=? AND do_not_contact=0 "
            "ORDER BY CASE WHEN email IS NOT NULL THEN 0 ELSE 1 END,created_at LIMIT 1",
            (company_id, lead_id),
        )
        if contact:
            runs.append(_generation_run(company_id, request, lead_id=lead_id, contact_id=contact["id"],
                                        campaign_id=campaign_id, channel=campaign["channel"], language=body.language))
    request.app.state.db.execute("UPDATE outreach_campaigns SET status='generating',updated_at=? WHERE id=?",
                                 (now(), campaign_id))
    return runs


@router.post("/outreach/campaigns/{campaign_id}/approve")
def approve_campaign(campaign_id: str, request: Request,
                     principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    get_campaign(campaign_id, request, principal, x_company_id)
    rows = request.app.state.db.all(
        "SELECT id FROM outreach_messages WHERE company_id=? AND campaign_id=? AND status='pending_approval'",
        (company_id, campaign_id),
    )
    approved = [request.app.state.outreach.approve(company_id, row["id"], principal.id) for row in rows]
    request.app.state.db.execute("UPDATE outreach_campaigns SET status='approved',updated_at=? WHERE id=?",
                                 (now(), campaign_id))
    return approved


@router.post("/outreach/campaigns/{campaign_id}/send")
def send_campaign(campaign_id: str, body: CampaignSend, request: Request,
                  principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    get_campaign(campaign_id, request, principal, x_company_id)
    rows = request.app.state.db.all(
        "SELECT id FROM outreach_messages WHERE company_id=? AND campaign_id=? AND status='approved'",
        (company_id, campaign_id),
    )
    results, failures = [], []
    for row in rows:
        try:
            results.append(request.app.state.outreach.send(company_id, row["id"], mode=body.mode))
        except HTTPException as exc:
            failures.append({"message_id": row["id"], "status_code": exc.status_code, "detail": exc.detail})
    request.app.state.db.execute("UPDATE outreach_campaigns SET status=?,updated_at=? WHERE id=?",
                                 ("sent" if not failures else "partially_sent", now(), campaign_id))
    return {"results": results, "failures": failures}


def _campaign_state(campaign_id: str, value: str, request: Request,
                    principal: Principal, company_header: str | None):
    get_campaign(campaign_id, request, principal, company_header)
    request.app.state.db.execute("UPDATE outreach_campaigns SET status=?,updated_at=? WHERE id=?",
                                 (value, now(), campaign_id))
    return get_campaign(campaign_id, request, principal, company_header)


@router.post("/outreach/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: str, request: Request, principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    return _campaign_state(campaign_id, "paused", request, principal, x_company_id)


@router.post("/outreach/campaigns/{campaign_id}/cancel")
def cancel_campaign(campaign_id: str, request: Request, principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    return _campaign_state(campaign_id, "cancelled", request, principal, x_company_id)


@router.get("/outreach/messages")
def messages(request: Request, principal: Principal = Depends(current_principal),
             x_company_id: str | None = Header(default=None),
             campaign_id: str | None = Query(default=None),
             lead_id: str | None = Query(default=None),
             contact_id: str | None = Query(default=None),
             status: str | None = Query(default=None)):
    values = [message_dict(row) for row in request.app.state.db.all(
        "SELECT * FROM outreach_messages WHERE company_id=? ORDER BY created_at DESC",
        (_scope(principal, x_company_id),),
    )]
    filters = {
        "campaign_id": campaign_id,
        "lead_id": lead_id,
        "contact_id": contact_id,
        "status": status,
    }
    for key, expected in filters.items():
        if expected:
            values = [value for value in values if value.get(key) == expected]
    return values


@router.get("/outreach/messages/{message_id}")
def get_message(message_id: str, request: Request, principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    return message_dict(request.app.state.outreach.get(_scope(principal, x_company_id), message_id))


@router.patch("/outreach/messages/{message_id}")
def patch_message(message_id: str, body: MessagePatch, request: Request,
                  principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    return request.app.state.outreach.update_message(_scope(principal, x_company_id), message_id, body.content)


@router.post("/outreach/messages/{message_id}/regenerate", status_code=202)
def regenerate_message(message_id: str, request: Request,
                       principal: Principal = Depends(current_principal),
                       x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    message = get_message(message_id, request, principal, x_company_id)
    return _generation_run(company_id, request, lead_id=message["lead_id"], contact_id=message["contact_id"],
                           campaign_id=message["campaign_id"], channel=message["channel"],
                           language=message["content"].get("language", "en"))


@router.post("/outreach/messages/{message_id}/approve")
def approve_message(message_id: str, request: Request,
                    principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    return request.app.state.outreach.approve(_scope(principal, x_company_id), message_id, principal.id)


@router.post("/outreach/messages/{message_id}/create-draft")
def create_message_draft(message_id: str, request: Request,
                         principal: Principal = Depends(current_principal),
                         x_company_id: str | None = Header(default=None)):
    return request.app.state.outreach.send(_scope(principal, x_company_id), message_id, mode="draft")


@router.post("/outreach/messages/{message_id}/send")
def send_message(message_id: str, request: Request, principal: Principal = Depends(current_principal),
                 x_company_id: str | None = Header(default=None)):
    return request.app.state.outreach.send(_scope(principal, x_company_id), message_id, mode="send")


@router.post("/outreach/messages/{message_id}/mark-sent-manually", status_code=204)
def mark_sent_manually(message_id: str, request: Request,
                       principal: Principal = Depends(current_principal),
                       x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    request.app.state.outreach.get(company_id, message_id)
    request.app.state.db.execute("UPDATE outreach_messages SET status='sent_manually',sent_at=?,updated_at=? WHERE id=?",
                                 (now(), now(), message_id))


@router.post("/outreach/messages/{message_id}/mark-replied", status_code=204)
def mark_replied(message_id: str, request: Request, principal: Principal = Depends(current_principal),
                 x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    request.app.state.outreach.get(company_id, message_id)
    request.app.state.db.execute("UPDATE outreach_messages SET status='replied',replied_at=?,updated_at=? WHERE id=?",
                                 (now(), now(), message_id))


@router.post("/leads/{lead_id}/generate-outreach", status_code=202)
def generate_lead_outreach(lead_id: str, request: Request,
                           principal: Principal = Depends(current_principal),
                           x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    contact = request.app.state.db.one(
        "SELECT id FROM contacts WHERE company_id=? AND lead_id=? AND email IS NOT NULL AND do_not_contact=0 LIMIT 1",
        (company_id, lead_id),
    )
    if not contact:
        raise HTTPException(409, "Lead has no eligible email contact")
    return _generation_run(company_id, request, lead_id=lead_id, contact_id=contact["id"])


@router.post("/custom-outreach/create-lead-and-message", status_code=202)
def custom_create(body: CustomOutreach, request: Request,
                  principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    lead = create_lead(LeadCreate(**body.lead), request, principal, x_company_id)
    contact_payload = {**body.contact, "lead_id": lead["id"]}
    if "full_name" in contact_payload:
        data = {"full_name": contact_payload.pop("full_name")}
        if contact_payload.get("title"):
            data["title"] = contact_payload.pop("title")
        contact_payload["data"] = {**contact_payload.get("data", {}), **data}
    contact = create_contact(ContactCreate(**contact_payload), request, principal, x_company_id)
    company_id = _scope(principal, x_company_id)
    research = request.app.state.runs.create(company_id, "lead_research", {"lead_id": lead["id"]})
    request.app.state.runs.start(company_id, research["id"])
    generation = _generation_run(company_id, request, lead_id=lead["id"], contact_id=contact["id"],
                                 language=body.language)
    return {"lead": lead, "contact": contact, "research_run": research, "generation_run": generation,
            "requested_mode": body.mode}


@router.post("/custom-outreach/generate-email", status_code=202)
def custom_generate(body: CustomOutreach, request: Request,
                    principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    return custom_create(body, request, principal, x_company_id)


@router.post("/custom-outreach/send-email")
def custom_send(body: MessageDelivery, request: Request,
                principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    return request.app.state.outreach.send(_scope(principal, x_company_id), body.message_id, mode="send")


@router.post("/custom-outreach/create-draft")
def custom_draft(body: MessageDelivery, request: Request,
                 principal: Principal = Depends(current_principal),
                 x_company_id: str | None = Header(default=None)):
    return request.app.state.outreach.send(_scope(principal, x_company_id), body.message_id, mode="draft")


@router.post("/email/drafts")
def email_draft(body: MessageDelivery, request: Request,
                principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    return custom_draft(body, request, principal, x_company_id)


@router.post("/email/send")
def email_send(body: MessageDelivery, request: Request,
               principal: Principal = Depends(current_principal),
               x_company_id: str | None = Header(default=None)):
    return custom_send(body, request, principal, x_company_id)


@router.post("/email/send-bulk")
def email_send_bulk(body: BulkDelivery, request: Request,
                    principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    results, failures = [], []
    for message_id in body.message_ids:
        try:
            results.append(request.app.state.outreach.send(company_id, message_id, mode=body.mode))
        except HTTPException as exc:
            failures.append({"message_id": message_id, "detail": exc.detail})
    return {"results": results, "failures": failures}


@router.get("/email/sent")
def email_sent(request: Request, principal: Principal = Depends(current_principal),
               x_company_id: str | None = Header(default=None)):
    return [message_dict(row) for row in request.app.state.db.all(
        "SELECT * FROM outreach_messages WHERE company_id=? AND channel='email' AND status IN ('sent','replied') "
        "ORDER BY sent_at DESC", (_scope(principal, x_company_id),),
    )]


@router.get("/email/replies")
def email_replies(request: Request, principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None), refresh: bool = False):
    company_id = _scope(principal, x_company_id)
    poll = None
    if refresh:
        poll = request.app.state.outreach.poll_email_replies(company_id)
    messages = [message_dict(row) for row in request.app.state.db.all(
        "SELECT * FROM outreach_messages WHERE company_id=? AND channel='email' AND replied_at IS NOT NULL "
        "ORDER BY replied_at DESC", (company_id,),
    )]
    return {"messages": messages, "poll": poll}


@router.get("/email/status/{provider_message_id}")
def email_status(provider_message_id: str, request: Request,
                 principal: Principal = Depends(current_principal),
                 x_company_id: str | None = Header(default=None)):
    row = request.app.state.db.one(
        "SELECT * FROM outreach_messages WHERE company_id=? AND provider_message_id=?",
        (_scope(principal, x_company_id), provider_message_id),
    )
    if not row:
        raise HTTPException(404, "Message not found")
    return {"provider_message_id": provider_message_id, "status": row["status"]}


@router.get("/cc-rules")
def cc_rules(request: Request, principal: Principal = Depends(current_principal),
             x_company_id: str | None = Header(default=None)):
    return [{"id": row["id"], **json_load(row["data"], {})} for row in request.app.state.db.all(
        "SELECT * FROM cc_rules WHERE company_id=? ORDER BY created_at", (_scope(principal, x_company_id),)
    )]


@router.post("/cc-rules", status_code=201)
def create_cc_rule(body: CCRuleCreate, request: Request,
                   principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    company_id, rule_id, stamp = _scope(principal, x_company_id), new_id("cc"), now()
    request.app.state.db.execute("INSERT INTO cc_rules VALUES(?,?,?,?,?,?)",
                                 (rule_id, company_id, body.name, json_dump(body.model_dump()), stamp, stamp))
    return {"id": rule_id, **body.model_dump()}


@router.get("/cc-rules/{rule_id}")
def get_cc_rule(rule_id: str, request: Request, principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    row = request.app.state.db.one("SELECT * FROM cc_rules WHERE id=? AND company_id=?",
                                   (rule_id, _scope(principal, x_company_id)))
    if not row:
        raise HTTPException(404, "CC rule not found")
    return {"id": row["id"], **json_load(row["data"], {})}


@router.patch("/cc-rules/{rule_id}")
def patch_cc_rule(rule_id: str, body: CCRuleCreate, request: Request,
                  principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    get_cc_rule(rule_id, request, principal, x_company_id)
    request.app.state.db.execute("UPDATE cc_rules SET name=?,data=?,updated_at=? WHERE id=?",
                                 (body.name, json_dump(body.model_dump()), now(), rule_id))
    return get_cc_rule(rule_id, request, principal, x_company_id)


@router.delete("/cc-rules/{rule_id}", status_code=204)
def delete_cc_rule(rule_id: str, request: Request, principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    if not request.app.state.db.execute("DELETE FROM cc_rules WHERE id=? AND company_id=?",
                                        (rule_id, _scope(principal, x_company_id))):
        raise HTTPException(404, "CC rule not found")


@router.get("/whatsapp/messages")
def whatsapp_messages(request: Request, principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    return [message_dict(row) for row in request.app.state.db.all(
        "SELECT * FROM outreach_messages WHERE company_id=? AND channel='whatsapp' ORDER BY created_at DESC",
        (_scope(principal, x_company_id),),
    )]


@router.post("/whatsapp/messages/generate", status_code=202)
def generate_whatsapp(body: WhatsAppGenerate, request: Request,
                      principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    return _generation_run(_scope(principal, x_company_id), request, lead_id=body.lead_id,
                           contact_id=body.contact_id, channel="whatsapp", language=body.language)


@router.post("/whatsapp/messages/{message_id}/approve")
def approve_whatsapp(message_id: str, request: Request,
                     principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    return approve_message(message_id, request, principal, x_company_id)


@router.post("/whatsapp/messages/{message_id}/send")
def send_whatsapp(message_id: str, request: Request,
                  principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    return send_message(message_id, request, principal, x_company_id)


@router.get("/whatsapp/messages/{message_id}/status")
def whatsapp_status(message_id: str, request: Request,
                    principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    message = get_message(message_id, request, principal, x_company_id)
    return {"message_id": message_id, "status": message["status"],
            "provider_message_id": message["provider_message_id"]}


@router.post("/whatsapp/messages/{message_id}/mark-replied", status_code=204)
def whatsapp_replied(message_id: str, request: Request,
                     principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    return mark_replied(message_id, request, principal, x_company_id)


@router.post("/whatsapp/messages/{message_id}/mark-opt-out", status_code=204)
def whatsapp_opt_out(message_id: str, request: Request,
                     principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    message = get_message(message_id, request, principal, x_company_id)
    request.app.state.db.execute("UPDATE outreach_messages SET status='opted_out',updated_at=? WHERE id=?",
                                 (now(), message_id))
    if message["contact_id"]:
        request.app.state.db.execute(
            "UPDATE contacts SET do_not_contact=1,status='blocked',updated_at=? WHERE id=?",
            (now(), message["contact_id"]),
        )


@router.get("/linkedin/actions")
def linkedin_actions(request: Request, principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    return [{"id": row["id"], "lead_id": row["lead_id"], "contact_id": row["contact_id"],
             "status": row["status"], "profile_url": row["profile_url"], "note": row["note"],
             "data": json_load(row["data"], {})} for row in request.app.state.db.all(
        "SELECT * FROM linkedin_actions WHERE company_id=? ORDER BY created_at DESC",
        (_scope(principal, x_company_id),),
    )]


@router.post("/linkedin/find-profile", status_code=202)
def find_linkedin(body: LinkedInFind, request: Request,
                  principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    contact = get_contact(body.contact_id, request, principal, x_company_id)
    if contact["linkedin_url"]:
        return {"profile_url": contact["linkedin_url"], "source": "stored"}
    company_id = _scope(principal, x_company_id)
    run = request.app.state.runs.create(company_id, "linkedin_note_generation",
                                        {"lead_id": body.lead_id, "contact_id": body.contact_id,
                                         "find_only": True})
    return request.app.state.runs.start(company_id, run["id"])


@router.post("/linkedin/generate-note", status_code=202)
def generate_linkedin_note(body: LinkedInNote, request: Request,
                           principal: Principal = Depends(current_principal),
                           x_company_id: str | None = Header(default=None)):
    get_contact(body.contact_id, request, principal, x_company_id)
    company_id = _scope(principal, x_company_id)
    run = request.app.state.runs.create(company_id, "linkedin_note_generation", body.model_dump())
    return request.app.state.runs.start(company_id, run["id"])


def _linkedin_state(action_id: str, value: str, request: Request,
                    principal: Principal, company_header: str | None):
    company_id = _scope(principal, company_header)
    if not request.app.state.db.execute(
        "UPDATE linkedin_actions SET status=?,updated_at=? WHERE id=? AND company_id=?",
        (value, now(), action_id, company_id),
    ):
        raise HTTPException(404, "LinkedIn action not found")
    return {"id": action_id, "status": value}


@router.post("/linkedin/actions/{action_id}/mark-opened")
def linkedin_opened(action_id: str, request: Request, principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    return _linkedin_state(action_id, "opened", request, principal, x_company_id)


@router.post("/linkedin/actions/{action_id}/mark-connection-sent")
def linkedin_sent(action_id: str, request: Request, principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    return _linkedin_state(action_id, "connection_sent", request, principal, x_company_id)


@router.post("/linkedin/actions/{action_id}/mark-connected")
def linkedin_connected(action_id: str, request: Request, principal: Principal = Depends(current_principal),
                       x_company_id: str | None = Header(default=None)):
    return _linkedin_state(action_id, "connected", request, principal, x_company_id)


@router.post("/linkedin/actions/{action_id}/mark-replied")
def linkedin_replied(action_id: str, request: Request, principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    return _linkedin_state(action_id, "replied", request, principal, x_company_id)
