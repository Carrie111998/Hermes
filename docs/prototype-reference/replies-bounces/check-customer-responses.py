#!/usr/bin/env python3
"""
check-customer-responses.py
Checks the Exchange inbox ({{sender_email}}) for replies from customers
we have previously contacted. If a match is found, sends a Telegram alert to Efe.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import subprocess
import xml.etree.ElementTree as ET

# ── Config ─────────────────────────────────────────────────────────────────
EWS_URL      = "https://{{EWS_HOST}}/ews/exchange.asmx"
EWS_USER     = r"{{EWS_USERNAME}}"
EWS_PASS     = "{{EWS_PASSWORD}}"
BOT_TOKEN    = "{{TELEGRAM_BOT_TOKEN}}"
CHAT_ID      = "{{CHAT_ID}}"
SHEET_ID     = "{{SHEET_ID}}"
GOG_ACCOUNT  = "{{gog_account}}"
GOG_BIN      = "/opt/homebrew/bin/gog"
STATE_FILE   = "/tmp/openclaw-response-seen.txt"
CACHE_FILE   = "/tmp/openclaw-sent-emails-cache.json"
CACHE_TTL    = 3600  # refresh sent-emails cache every hour

# ── EWS namespaces ──────────────────────────────────────────────────────────
NS = {
    "soap": "http://schemas.xmlsoap.org/soap/envelope/",
    "m":    "http://schemas.microsoft.com/exchange/services/2006/messages",
    "t":    "http://schemas.microsoft.com/exchange/services/2006/types",
}

def ews_request(soap_body: str) -> str:
    envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
               xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
  <soap:Body>{soap_body}</soap:Body>
</soap:Envelope>"""
    import base64
    creds = base64.b64encode(f"{EWS_USER}:{EWS_PASS}".encode()).decode()
    req = urllib.request.Request(
        EWS_URL,
        data=envelope.encode("utf-8"),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": '"http://schemas.microsoft.com/exchange/services/2006/messages/FindItem"',
            "Authorization": f"Basic {creds}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return ""

def find_unread_inbox() -> str:
    body = """
    <m:FindItem Traversal="Shallow">
      <m:ItemShape>
        <t:BaseShape>IdOnly</t:BaseShape>
        <t:AdditionalProperties>
          <t:FieldURI FieldURI="message:From"/>
          <t:FieldURI FieldURI="item:Subject"/>
          <t:FieldURI FieldURI="message:IsRead"/>
          <t:FieldURI FieldURI="item:DateTimeReceived"/>
        </t:AdditionalProperties>
      </m:ItemShape>
      <m:IndexedPageItemView MaxEntriesReturned="25" Offset="0" BasePoint="Beginning"/>
      <m:Restriction>
        <t:IsEqualTo>
          <t:FieldURI FieldURI="message:IsRead"/>
          <t:FieldURIOrConstant><t:Constant Value="false"/></t:FieldURIOrConstant>
        </t:IsEqualTo>
      </m:Restriction>
      <m:SortOrder>
        <t:FieldOrder Order="Descending">
          <t:FieldURI FieldURI="item:DateTimeReceived"/>
        </t:FieldOrder>
      </m:SortOrder>
      <m:ParentFolderIds>
        <t:DistinguishedFolderId Id="inbox"/>
      </m:ParentFolderIds>
    </m:FindItem>"""
    return ews_request(body)

def parse_messages(xml_text: str) -> list:
    """Returns list of dicts: {id, from_email, from_name, subject}"""
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []

    messages = []
    for msg in root.iter("{http://schemas.microsoft.com/exchange/services/2006/types}Message"):
        item_id_el = msg.find(".//{http://schemas.microsoft.com/exchange/services/2006/types}ItemId")
        if item_id_el is None:
            continue
        item_id = item_id_el.get("Id", "")

        from_el = msg.find(".//{http://schemas.microsoft.com/exchange/services/2006/types}From")
        from_email = ""
        from_name = ""
        if from_el is not None:
            em = from_el.find(".//{http://schemas.microsoft.com/exchange/services/2006/types}EmailAddress")
            nm = from_el.find(".//{http://schemas.microsoft.com/exchange/services/2006/types}Name")
            if em is not None and em.text:
                from_email = em.text.strip().lower()
            if nm is not None and nm.text:
                from_name = nm.text.strip()

        subj_el = msg.find(".//{http://schemas.microsoft.com/exchange/services/2006/types}Subject")
        subject = subj_el.text.strip() if (subj_el is not None and subj_el.text) else ""

        if item_id:
            messages.append({
                "id": item_id,
                "from_email": from_email,
                "from_name": from_name,
                "subject": subject,
            })
    return messages

def load_seen() -> set:
    try:
        with open(STATE_FILE) as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()

def mark_seen(item_id: str):
    with open(STATE_FILE, "a") as f:
        f.write(item_id + "\n")

def get_sent_emails() -> dict:
    """Returns {email_lower: {company, country, name}} for all customers where K=TRUE."""
    # Check cache freshness
    try:
        age = time.time() - os.path.getmtime(CACHE_FILE)
    except FileNotFoundError:
        age = 9999

    if age < CACHE_TTL:
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass

    # Refresh from sheet
    try:
        result = subprocess.run(
            [GOG_BIN, "sheets", "get", SHEET_ID, "Sheet1!A1:K6000",
             "--json", "--account", GOG_ACCOUNT, "--no-input"],
            capture_output=True, text=True, timeout=60
        )
        data = json.loads(result.stdout)
        values = data.get("values", [])
    except Exception:
        return {}

    sent = {}
    for i, row in enumerate(values[1:], 2):  # skip header row
        if len(row) <= 10:
            continue
        k_val = str(row[10]).strip().upper()
        if k_val not in ("TRUE", "1", "YES", "SENT"):
            continue
        company = row[1].strip() if len(row) > 1 else ""
        country = row[0].strip() if len(row) > 0 else ""
        name    = row[2].strip() if len(row) > 2 else ""
        # Primary email (col E = index 4)
        emails = []
        if len(row) > 4 and row[4] and "@" in row[4]:
            emails.append(row[4].strip().lower())
        # Other emails (col F = index 5), comma-separated
        if len(row) > 5 and row[5]:
            for e in row[5].split(","):
                e = e.strip().lower()
                if "@" in e:
                    emails.append(e)
        for email in emails:
            sent[email] = {"company": company, "country": country, "name": name, "row": i}

    with open(CACHE_FILE, "w") as f:
        json.dump(sent, f)

    return sent

def update_sheet_email_response(row: int, from_email: str):
    """Write 'YES — date — sender' into column M (Email Response?) for the row."""
    if not row:
        return
    value = f"YES — {time.strftime('%Y-%m-%d')} — {from_email}"
    try:
        subprocess.run(
            [GOG_BIN, "sheets", "update", SHEET_ID, f"Sheet1!M{row}", value,
             "--account", GOG_ACCOUNT, "--no-input"],
            capture_output=True, text=True, timeout=30
        )
    except Exception:
        pass

def send_telegram(text: str):
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    try:
        urllib.request.urlopen(url, data, timeout=10)
    except Exception:
        pass

def get_sent_domains() -> set:
    """Returns set of email domains from all K=TRUE rows for fuzzy matching."""
    sent = get_sent_emails()
    return {e.split('@')[1] for e in sent if '@' in e}

def is_bounce(from_email: str, subject: str) -> bool:
    """Detect delivery failure / bounce / auto-reply messages."""
    bounce_indicators = [
        'mailer-daemon', 'postmaster', 'noreply', 'no-reply',
        'dogrumail.com', 'mail delivery', 'undeliverable',
        'delivery failure', 'delivery status'
    ]
    subject_lower = subject.lower()
    email_lower = from_email.lower()
    bounce_subjects = ['undeliverable', 'delivery failed', 'mail delivery',
                       'returned mail', 'failure notice', 'spam quarantine']
    if any(b in email_lower for b in bounce_indicators):
        return True
    if any(b in subject_lower for b in bounce_subjects):
        return True
    return False

def main():
    sent_emails = get_sent_emails()
    sent_domains = get_sent_domains()

    xml_text = find_unread_inbox()
    if not xml_text:
        sys.exit(0)

    messages = parse_messages(xml_text)
    if not messages:
        sys.exit(0)

    seen = load_seen()

    # Internal Silverline addresses to ignore
    internal_domains = {'silverline.com', 'tempo-timay.com'}

    for msg in messages:
        item_id    = msg["id"]
        from_email = msg["from_email"]
        from_name  = msg["from_name"]
        subject    = msg["subject"]

        if item_id in seen:
            continue

        mark_seen(item_id)

        if not from_email:
            continue

        # Skip bounces and internal messages
        if is_bounce(from_email, subject):
            continue
        from_domain = from_email.split('@')[1] if '@' in from_email else ''
        if from_domain in internal_domains:
            continue

        # MATCH 1: Exact email in our sent list
        if from_email in sent_emails:
            customer = sent_emails[from_email]
            text = (
                "MUSTERI YANIT VERDI!\n\n"
                f"Sirket  : {customer['company']}\n"
                f"Ulke    : {customer['country']}\n"
                f"Kisi    : {customer['name'] or from_name or '-'}\n"
                f"E-posta : {from_email}\n"
                f"Konu    : {subject}\n\n"
                "Hemen {{sender_email}} gelen kutusunu kontrol et!"
            )
            send_telegram(text)
            update_sheet_email_response(customer.get("row"), from_email)
            continue

        # MATCH 2: Subject contains 'Silverline' and 'Re:' — reply to our cold outreach
        subj_lower = subject.lower()
        if ('re:' in subj_lower or 'aw:' in subj_lower or 'fwd:' in subj_lower) \
                and 'silverline' in subj_lower:
            text = (
                "SILVERLINE EMAIL YANITI (eslesme yok)!\n\n"
                f"Gonderen : {from_name or '-'} <{from_email}>\n"
                f"Konu     : {subject}\n\n"
                "Bu kisi veritabaninda yok ama Silverline emailine yanit verdi.\n"
                "Hemen {{sender_email}} gelen kutusunu kontrol et!"
            )
            send_telegram(text)
            continue

        # MATCH 3: Sender domain matches a domain in our sent list (different person at same company)
        if from_domain and from_domain in sent_domains:
            # Find matching company info
            matching = {e: info for e, info in sent_emails.items()
                        if e.endswith('@' + from_domain)}
            company_info = next(iter(matching.values()), {})
            text = (
                "GONDERDIGIMIZ FIRMADAN YANIT!\n\n"
                f"Firma    : {company_info.get('company', '?')}\n"
                f"Ulke     : {company_info.get('country', '?')}\n"
                f"Gonderen : {from_name or '-'} <{from_email}>\n"
                f"Konu     : {subject}\n\n"
                "Farkli bir kisi yanit verdi (ayni sirket).\n"
                "Hemen {{sender_email}} gelen kutusunu kontrol et!"
            )
            send_telegram(text)
            update_sheet_email_response(company_info.get("row"), from_email)

if __name__ == "__main__":
    main()
