"""Approval-safe outreach state machine and provider dispatch."""
from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from . import compliance
from .crypto import CredentialCipher
from .db import Database, json_dump, json_load, new_id, now
from .email_providers import EMAIL_PROVIDERS, OutgoingEmail
from .quality import EMAIL_IN_TEXT_RE, content_hash, is_bounce, preflight_message
from .whatsapp_provider import WhatsAppCloudProvider


COUNTRY_TZ = {
    "AE": "Asia/Dubai", "DE": "Europe/Berlin", "EG": "Africa/Cairo", "FR": "Europe/Paris",
    "GB": "Europe/London", "IQ": "Asia/Baghdad", "JO": "Asia/Amman", "KE": "Africa/Nairobi",
    "KW": "Asia/Kuwait", "MA": "Africa/Casablanca", "NG": "Africa/Lagos", "NL": "Europe/Amsterdam",
    "OM": "Asia/Muscat", "PK": "Asia/Karachi", "SA": "Asia/Riyadh", "TR": "Europe/Istanbul",
    "US": "America/New_York", "ZA": "Africa/Johannesburg",
}


def message_dict(row) -> dict:
    return {
        "id": row["id"], "company_id": row["company_id"], "campaign_id": row["campaign_id"],
        "lead_id": row["lead_id"], "contact_id": row["contact_id"], "channel": row["channel"],
        "status": row["status"], "revision": row["revision"],
        "content": json_load(row["content"], {}), "content_hash": row["content_hash"],
        "approved": bool(row["approval_hash"]), "approved_by": row["approved_by"],
        "approved_at": row["approved_at"], "provider_message_id": row["provider_message_id"],
        "sent_at": row["sent_at"], "replied_at": row["replied_at"],
        "bounced_at": row["bounced_at"], "data": json_load(row["data"], {}),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


class OutreachService:
    def __init__(self, db: Database, cipher: CredentialCipher,
                 *, public_base_url: str = "", credential_key: str = ""):
        self.db = db
        self.cipher = cipher
        self.public_base_url = public_base_url
        # Reused as the opt-out HMAC secret: any tenant able to send email
        # already requires this key, so it needs no separate configuration.
        self.cipher_secret = credential_key

    def get(self, company_id: str, message_id: str):
        row = self.db.one("SELECT * FROM outreach_messages WHERE id=? AND company_id=?",
                          (message_id, company_id))
        if not row:
            raise HTTPException(404, "Outreach message not found")
        return row

    def create_message(self, company_id: str, content: dict, *, channel: str = "email",
                       campaign_id: str | None = None, lead_id: str | None = None,
                       contact_id: str | None = None, data: dict | None = None) -> dict:
        verdict = self._preflight(company_id, channel, content)
        status = "pending_approval" if verdict["pass"] else "qa_failed"
        message_id, stamp = new_id("msg"), now()
        self.db.execute(
            "INSERT INTO outreach_messages(id,company_id,campaign_id,lead_id,contact_id,channel,status,revision,"
            "content_hash,content,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (message_id, company_id, campaign_id, lead_id, contact_id, channel, status, 1,
             content_hash(content), json_dump(content), json_dump({**(data or {}), "qa_verdict": verdict}),
             stamp, stamp),
        )
        return message_dict(self.get(company_id, message_id))

    @staticmethod
    def _whatsapp_preflight(content: dict) -> dict:
        failures = []
        if not content.get("to"):
            failures.append("missing_recipient")
        if not content.get("body") and not content.get("template_name"):
            failures.append("missing_body_or_template")
        if "{{" in str(content.get("body", "")):
            failures.append("unresolved_placeholder")
        return {"pass": not failures, "failures": failures}

    def _preflight(self, company_id: str, channel: str, content: dict) -> dict:
        if channel != "email":
            return self._whatsapp_preflight(content)
        section = self.db.one(
            "SELECT data FROM company_sections WHERE company_id=? AND section='sales_preferences'",
            (company_id,),
        )
        preferences = json_load(section["data"], {}) if section else {}
        fixed_subject = preferences.get("fixed_subject_line")
        if isinstance(fixed_subject, dict):
            fixed_subject = fixed_subject.get(str(content.get("language") or "en").lower())
        return preflight_message(content, fixed_subject=fixed_subject).as_dict()

    def update_message(self, company_id: str, message_id: str, patch: dict) -> dict:
        row = self.get(company_id, message_id)
        if row["status"] in {"sent", "draft"}:
            raise HTTPException(409, "Delivered messages are immutable")
        content = {**json_load(row["content"], {}), **patch}
        verdict = self._preflight(company_id, row["channel"], content)
        status = "pending_approval" if verdict["pass"] else "qa_failed"
        data = {**json_load(row["data"], {}), "qa_verdict": verdict}
        self.db.execute(
            "UPDATE outreach_messages SET content=?,content_hash=?,revision=revision+1,status=?,"
            "approval_hash=NULL,approved_by=NULL,approved_at=NULL,data=?,updated_at=? WHERE id=?",
            (json_dump(content), content_hash(content), status, json_dump(data), now(), message_id),
        )
        return message_dict(self.get(company_id, message_id))

    def approve(self, company_id: str, message_id: str, actor_id: str) -> dict:
        row = self.get(company_id, message_id)
        content = json_load(row["content"], {})
        verdict = self._preflight(company_id, row["channel"], content)
        if not verdict["pass"]:
            raise HTTPException(422, {"message": "Message failed deterministic preflight", **verdict})
        digest, stamp = content_hash(content), now()
        self.db.execute(
            "UPDATE outreach_messages SET status='approved',content_hash=?,approval_hash=?,approved_by=?,"
            "approved_at=?,data=?,updated_at=? WHERE id=?",
            (digest, digest, actor_id, stamp,
             json_dump({**json_load(row["data"], {}), "qa_verdict": verdict}), stamp, message_id),
        )
        self.db.activity(company_id, actor_id, "outreach_message_approved", "outreach_message", message_id,
                         {"revision": row["revision"]})
        return message_dict(self.get(company_id, message_id))

    def send(self, company_id: str, message_id: str, *, mode: str = "send") -> dict:
        if mode not in {"send", "draft"}:
            raise HTTPException(422, "mode must be send or draft")
        row = self.get(company_id, message_id)
        content = json_load(row["content"], {})
        digest = content_hash(content)
        key = f"delivery:{message_id}:revision:{row['revision']}:mode:{mode}"
        existing = self.db.one("SELECT * FROM delivery_attempts WHERE idempotency_key=?", (key,))
        if existing:
            return {"idempotent": True, "status": existing["status"],
                    "provider_message_id": existing["provider_message_id"]}
        if row["status"] != "approved" or not row["approval_hash"] or row["approval_hash"] != digest:
            raise HTTPException(409, "The exact current message revision must be approved before delivery")
        self._eligibility(company_id, row, content, mode)
        attempt_id, stamp = new_id("delivery"), now()
        try:
            self.db.execute(
                "INSERT INTO delivery_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, company_id, message_id, mode, key, "reserved", None, None, stamp, stamp),
            )
        except Exception:
            existing = self.db.one("SELECT * FROM delivery_attempts WHERE idempotency_key=?", (key,))
            return {"idempotent": True, "status": existing["status"],
                    "provider_message_id": existing["provider_message_id"]}
        run_id = self._start_delivery_run(company_id, row, content, mode, key)
        try:
            if row["channel"] == "email":
                result = self._deliver_email(company_id, content, mode)
                provider_id, delivery_status = result.provider_message_id, result.status
            elif row["channel"] == "whatsapp":
                provider_id, delivery_status = self._deliver_whatsapp(company_id, content)
            else:
                raise HTTPException(422, "Unsupported automated delivery channel")
        except Exception as exc:
            self.db.execute("UPDATE delivery_attempts SET status='failed',error=?,updated_at=? WHERE id=?",
                            (str(exc)[:2000], now(), attempt_id))
            self._finish_delivery_run(run_id, "failed", error=str(exc))
            raise
        delivered_at = now()
        self.db.execute(
            "UPDATE delivery_attempts SET status=?,provider_message_id=?,updated_at=? WHERE id=?",
            (delivery_status, provider_id, delivered_at, attempt_id),
        )
        message_status = "draft" if mode == "draft" else "sent"
        self.db.execute(
            "UPDATE outreach_messages SET status=?,provider_message_id=?,sent_at=?,idempotency_key=?,updated_at=? "
            "WHERE id=?",
            (message_status, provider_id, delivered_at if mode == "send" else None, key, delivered_at, message_id),
        )
        self._finish_delivery_run(
            run_id, "succeeded",
            output={"provider_message_id": provider_id, "status": message_status},
            output_ref=message_id,
        )
        return {"idempotent": False, "status": message_status, "provider_message_id": provider_id}

    def _start_delivery_run(self, company_id: str, message, content: dict,
                            mode: str, idempotency_key: str) -> str:
        run_id, stamp = new_id("run"), now()
        run_type = "email_send" if message["channel"] == "email" else "whatsapp_send"
        payload = {"message_id": message["id"], "revision": message["revision"], "mode": mode,
                   "content_hash": content_hash(content)}
        self.db.execute(
            "INSERT INTO agent_runs(id,company_id,run_type,status,payload,idempotency_key,started_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, company_id, run_type, "running", json_dump(payload),
             f"run:{idempotency_key}", stamp, stamp, stamp),
        )
        self.db.execute(
            "INSERT INTO run_events(run_id,company_id,ts,kind,message,data) VALUES(?,?,?,?,?,?)",
            (run_id, company_id, stamp, "started", f"{run_type} provider dispatch", json_dump({})),
        )
        return run_id

    def _finish_delivery_run(self, run_id: str, status: str, *, output: dict | None = None,
                             error: str | None = None, output_ref: str | None = None) -> None:
        stamp = now()
        self.db.execute(
            "UPDATE agent_runs SET status=?,output=?,error=?,output_ref=?,completed_at=?,updated_at=? WHERE id=?",
            (status, json_dump(output) if output is not None else None,
             error[:4000] if error else None, output_ref, stamp, stamp, run_id),
        )
        row = self.db.one("SELECT company_id FROM agent_runs WHERE id=?", (run_id,))
        self.db.execute(
            "INSERT INTO run_events(run_id,company_id,ts,kind,message,data) VALUES(?,?,?,?,?,?)",
            (run_id, row["company_id"], stamp, status, error[:1000] if error else "", json_dump({})),
        )

    def _eligibility(self, company_id: str, message, content: dict, mode: str) -> None:
        if mode == "draft":
            return
        lead = self.db.one("SELECT * FROM leads WHERE id=? AND company_id=?",
                           (message["lead_id"], company_id)) if message["lead_id"] else None
        contact = self.db.one("SELECT * FROM contacts WHERE id=? AND company_id=?",
                              (message["contact_id"], company_id)) if message["contact_id"] else None
        if (lead and lead["do_not_contact"]) or (contact and contact["do_not_contact"]):
            raise HTTPException(409, "Lead or contact is marked do-not-contact")
        if message["channel"] == "email":
            for address in [content.get("to"), *content.get("cc", [])]:
                if compliance.is_suppressed(self.db, company_id, str(address or "")):
                    raise HTTPException(409, f"{address} has unsubscribed from this company's outreach")
        country = str(content.get("country") or (lead["country"] if lead else "")).upper()
        section = self.db.one("SELECT data FROM company_sections WHERE company_id=? AND section='market_preferences'",
                              (company_id,))
        prefs = json_load(section["data"], {}) if section else {}
        if country and country in {str(code).upper() for code in prefs.get("no_outreach_markets", [])}:
            raise HTTPException(409, f"Outreach is disabled for {country}")
        today = dt.datetime.now(dt.timezone.utc).timestamp() - 86400
        if message["lead_id"]:
            prior = self.db.one(
                "SELECT id FROM outreach_messages WHERE company_id=? AND lead_id=? AND status='sent' "
                "AND sent_at>? AND id<>? LIMIT 1", (company_id, message["lead_id"], today, message["id"]),
            )
            if prior:
                raise HTTPException(409, "One-channel-per-customer-per-day policy blocked this send")
        preferences = self.db.one("SELECT data FROM company_sections WHERE company_id=? AND section='sales_preferences'",
                                  (company_id,))
        sales = json_load(preferences["data"], {}) if preferences else {}
        limit = int(sales.get("daily_email_limit" if message["channel"] == "email" else "daily_whatsapp_limit", 50))
        count = self.db.one(
            "SELECT COUNT(*) AS n FROM outreach_messages WHERE company_id=? AND channel=? AND status='sent' AND sent_at>?",
            (company_id, message["channel"], today),
        )["n"]
        if count >= limit:
            raise HTTPException(429, "Daily outreach limit reached")
        self._enforce_window(country, sales)

    @staticmethod
    def _enforce_window(country: str, sales: dict) -> None:
        timezone = COUNTRY_TZ.get(country)
        if not timezone:
            raise HTTPException(409, "Recipient timezone is required before sending")
        local = dt.datetime.now(ZoneInfo(timezone))
        windows = sales.get("send_windows", "09:00-12:00,13:00-15:00")
        if isinstance(windows, str):
            windows = [item.strip() for item in windows.split(",") if item.strip()]
        minute = local.hour * 60 + local.minute
        allowed = False
        for window in windows:
            try:
                start, end = window.split("-", 1)
                sh, sm = map(int, start.split(":"))
                eh, em = map(int, end.split(":"))
            except ValueError:
                raise HTTPException(422, {"message": "Invalid send window; expected HH:MM-HH:MM",
                                          "window": window})
            allowed |= sh * 60 + sm <= minute < eh * 60 + em
        if not allowed:
            raise HTTPException(409, {"message": "Outside recipient-local send window",
                                      "recipient_time": local.isoformat(), "windows": windows})

    def _integration(self, company_id: str, kind: str):
        row = self.db.one(
            "SELECT * FROM integrations WHERE company_id=? AND kind=? AND status='connected' "
            "AND (kind<>'email' OR provider NOT IN ('google','microsoft') "
            "OR COALESCE(encrypted_credentials,'')<>'') "
            "ORDER BY updated_at DESC LIMIT 1", (company_id, kind),
        )
        if not row:
            raise HTTPException(409, f"No connected {kind} integration")
        return row, self.cipher.decrypt(row["encrypted_credentials"])

    def _deliver_email(self, company_id: str, content: dict, mode: str):
        integration, credentials = self._integration(company_id, "email")
        provider = EMAIL_PROVIDERS.get(integration["provider"])
        if not provider:
            raise HTTPException(422, "Unsupported email provider")
        adapter = provider()
        adapter.connect_account(credentials)
        # Compliance is applied here, at the single adapter boundary, so no
        # caller can construct a send that skips the opt-out link.
        opt_out = compliance.unsubscribe_url(
            self.public_base_url, self.cipher_secret, company_id, content["to"])
        body = compliance.inject_footer(
            content.get("body", ""), opt_out, content.get("language"))
        email = OutgoingEmail(to=content["to"], cc=list(content.get("cc", [])),
                              subject=content.get("subject", ""), body=body,
                              language=content.get("language"), reply_to=content.get("reply_to"),
                              headers={"List-Unsubscribe": f"<{opt_out}>",
                                       "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
        result = adapter.create_draft(email) if mode == "draft" else adapter.send_email(email)
        if hasattr(adapter, "credentials") and integration["provider"] != "stub":
            self.db.execute("UPDATE integrations SET encrypted_credentials=?,updated_at=? WHERE id=?",
                            (self.cipher.encrypt(adapter.credentials), now(), integration["id"]))
        return result

    def poll_email_replies(self, company_id: str) -> dict:
        integration, credentials = self._integration(company_id, "email")
        provider_cls = EMAIL_PROVIDERS.get(integration["provider"])
        if not provider_cls:
            raise HTTPException(422, "Unsupported email provider")
        adapter = provider_cls()
        adapter.connect_account(credentials)
        matched, bounces = 0, 0
        for item in adapter.list_recent_replies():
            sender = self._reply_sender(item)
            subject = str(item.get("subject") or "")
            if not sender:
                continue
            if is_bounce(sender, subject):
                inbound_id = str(item.get("id") or item.get("internet_message_id") or "")
                if inbound_id and self.db.one(
                    "SELECT id FROM activity_log WHERE company_id=? AND entity_type='email_bounce' AND entity_id=?",
                    (company_id, inbound_id),
                ):
                    continue
                bounces += 1
                suppressed = self._suppress_bounced_recipient(company_id, subject)
                self.db.activity(company_id, None, "email_bounce_observed", "email_bounce", inbound_id or None,
                                 {"sender": sender, "subject": subject, "suppressed": suppressed})
                continue
            email = sender.lower()
            contact = self.db.one(
                "SELECT * FROM contacts WHERE company_id=? AND lower(email)=lower(?) LIMIT 1",
                (company_id, email),
            )
            if not contact and "@" in email:
                domain = email.rsplit("@", 1)[1]
                contact = self.db.one(
                    "SELECT * FROM contacts WHERE company_id=? AND lower(email) LIKE ? LIMIT 1",
                    (company_id, f"%@{domain}"),
                )
            if not contact:
                continue
            message = self.db.one(
                "SELECT * FROM outreach_messages WHERE company_id=? AND contact_id=? AND channel='email' "
                "AND status IN ('sent','delivered') ORDER BY sent_at DESC LIMIT 1",
                (company_id, contact["id"]),
            )
            if message and not message["replied_at"]:
                stamp = now()
                self.db.execute(
                    "UPDATE outreach_messages SET status='replied',replied_at=?,updated_at=? WHERE id=?",
                    (stamp, stamp, message["id"]),
                )
                matched += 1
                self.db.activity(company_id, None, "email_reply_detected", "outreach_message", message["id"],
                                 {"sender": sender, "subject": subject})
        circuit = self._bounce_circuit(company_id)
        return {"matched_replies": matched, "bounces_observed": bounces,
                "bounce_circuit": circuit}

    def _suppress_bounced_recipient(self, company_id: str, subject: str) -> str | None:
        """Suppress the failed address when the bounce names one.

        ponytail: the adapters fetch headers only, so the address is available
        only when the bounce subject carries it — which many mailers omit. A
        named address is suppressed immediately; the rest are still counted and
        caught by the bounce circuit breaker. Upgrade path when hard-bounce
        precision matters: fetch the DSN body and parse its
        message/delivery-status part for Final-Recipient.
        """
        candidates = {compliance.normalize_email(item)
                      for item in EMAIL_IN_TEXT_RE.findall(subject or "")}
        if not candidates:
            return None
        # Compared in Python rather than with json_extract: that function is
        # SQLite-only and this service also runs on Postgres.
        recent = self.db.all(
            "SELECT content FROM outreach_messages WHERE company_id=? AND channel='email' "
            "AND status IN ('sent','delivered') ORDER BY sent_at DESC LIMIT 200",
            (company_id,),
        )
        sent_to = {compliance.normalize_email(json_load(row["content"], {}).get("to", ""))
                   for row in recent}
        for address in candidates & sent_to:
            if compliance.suppress(self.db, company_id, address, "hard_bounce"):
                return address
        return None

    def _bounce_circuit(self, company_id: str) -> dict:
        since = now() - 7 * 86400
        sent = self.db.one(
            "SELECT COUNT(*) AS n FROM outreach_messages WHERE company_id=? AND channel='email' "
            "AND sent_at>?", (company_id, since),
        )["n"]
        bounces = self.db.one(
            "SELECT COUNT(*) AS n FROM activity_log WHERE company_id=? AND action='email_bounce_observed' "
            "AND created_at>?", (company_id, since),
        )["n"]
        rate = bounces / sent if sent else 0
        tripped = sent >= 20 and rate >= 0.05
        if tripped:
            self.db.execute(
                "UPDATE outreach_campaigns SET status='paused_bounce_rate',updated_at=? "
                "WHERE company_id=? AND channel='email' AND status NOT IN ('cancelled','sent')",
                (now(), company_id),
            )
            self.db.activity(company_id, None, "bounce_circuit_tripped", "campaign", None,
                             {"sent": sent, "bounces": bounces, "rate": rate})
        return {"tripped": tripped, "sent_7d": sent, "bounces_7d": bounces,
                "rate": round(rate, 4)}

    @staticmethod
    def _reply_sender(item: dict) -> str:
        sender = item.get("from") or item.get("sender") or ""
        if isinstance(sender, dict):
            sender = (sender.get("emailAddress") or {}).get("address", "")
        sender = str(sender)
        if "<" in sender and ">" in sender:
            sender = sender.rsplit("<", 1)[1].split(">", 1)[0]
        return sender.strip()

    def _deliver_whatsapp(self, company_id: str, content: dict) -> tuple[str, str]:
        _, credentials = self._integration(company_id, "whatsapp")
        adapter = WhatsAppCloudProvider(credentials)
        if content.get("template_name"):
            response = adapter.send_template(content["to"], content["template_name"],
                                             content.get("language", "en"), content.get("components"))
        else:
            response = adapter.send_text(content["to"], content["body"])
        provider_id = response.get("messages", [{}])[0].get("id")
        if not provider_id:
            raise RuntimeError("WhatsApp API did not return a message ID")
        return provider_id, "sent"
