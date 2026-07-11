#!/usr/bin/env python3
"""
send_with_research.py
Sends outreach emails one at a time with 5-minute intervals.
Between each send: researches the NEXT company, updates email template + Column J.
Avoids spam filter triggers caused by bulk sending.

Usage:
  python3 send_with_research.py --batch utc6
  python3 send_with_research.py --row 5160 --file /tmp/email_xaze_mx.html --to contacto@xaze.com.mx
"""

import subprocess, sys, time, os, re, json, xml.etree.ElementTree as ET
import urllib.request, urllib.parse, base64
from datetime import datetime
import pytz

# ── Config ──────────────────────────────────────────────────────────────────
EWS_URL     = "https://{{EWS_HOST}}/ews/exchange.asmx"
EWS_CREDS   = r"{{EWS_USERNAME}}:{{EWS_PASSWORD}}"
SHEET_ID    = "{{SHEET_ID}}"
GOG_ACCOUNT = "{{gog_account}}"
BOT_TOKEN   = "{{TELEGRAM_BOT_TOKEN}}"
CHAT_ID     = "{{CHAT_ID}}"
CC_ALWAYS   = ["anarr@silverline.com", "umutatas@silverline.com", "berkany@silverline.com"]
INTERVAL_SEC = 300  # 5 minutes between sends
SUBJ_ES     = "Silverline Electrodomésticos de Cocina – Calidad Premium, Precio Competitivo"
SUBJ_PT     = "Silverline Eletrodomésticos de Cozinha – Qualidade Premium, Preço Competitivo"

ISTANBUL_TZ = pytz.timezone('Europe/Istanbul')

def now_tr():
    return datetime.now(ISTANBUL_TZ).strftime('%H:%M')

def send_telegram(msg):
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": msg}).encode()
    try:
        urllib.request.urlopen(url, data, timeout=10)
    except: pass

def make_soap(to_email, cc_list, subject, html_body):
    # HARD RULE: never send to ferre.com
    if "ferre.com" in to_email.lower():
        return None
    cc_list = [e for e in cc_list if "ferre.com" not in e.lower()]
    cc_blocks = "\n".join(
        f"<t:Mailbox><t:EmailAddress>{e}</t:EmailAddress></t:Mailbox>"
        for e in cc_list
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
               xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
  <soap:Body>
    <m:CreateItem MessageDisposition="SendAndSaveCopy">
      <m:Items>
        <t:Message>
          <t:Subject>{subject}</t:Subject>
          <t:Body BodyType="HTML"><![CDATA[{html_body}]]></t:Body>
          <t:ToRecipients>
            <t:Mailbox><t:EmailAddress>{to_email}</t:EmailAddress></t:Mailbox>
          </t:ToRecipients>
          <t:CcRecipients>{cc_blocks}</t:CcRecipients>
          <t:IsRead>true</t:IsRead>
        </t:Message>
      </m:Items>
    </m:CreateItem>
  </soap:Body>
</soap:Envelope>"""

def send_email(to_email, cc_list, subject, html_file, row_num):
    # Add mandatory CCs
    full_cc = list(cc_list)
    for cc in CC_ALWAYS:
        if cc not in full_cc:
            full_cc.append(cc)

    if not os.path.exists(html_file):
        print(f"  SKIP: file not found {html_file}")
        return False

    html_body = open(html_file, encoding='utf-8').read()
    soap = make_soap(to_email, full_cc, subject, html_body)
    if soap is None:
        print(f"  BLOCKED (ferre.com): {to_email}")
        return False

    soap_file = f"/tmp/soap_send_{row_num}.xml"
    open(soap_file, 'w', encoding='utf-8').write(soap)

    result = subprocess.run([
        "curl", "-s", "-u", EWS_CREDS,
        "-H", "Content-Type: text/xml; charset=utf-8",
        "-H", "SOAPAction: http://schemas.microsoft.com/exchange/services/2006/messages/CreateItem",
        "--data-binary", f"@{soap_file}",
        EWS_URL
    ], capture_output=True, text=True, timeout=45)

    try: os.unlink(soap_file)
    except: pass

    if "NoError" in result.stdout:
        print(f"  [OK] SENT: {to_email}")
        # Update sheet K = TRUE
        subprocess.run([
            "/opt/homebrew/bin/gog", "sheets", "update", SHEET_ID,
            f"Sheet1!K{row_num}", "--values-json", '[["TRUE"]]',
            "--input", "USER_ENTERED", "--account", GOG_ACCOUNT, "--no-input"
        ], capture_output=True, text=True, timeout=15)
        print(f"  Sheet K{row_num} = TRUE")
        return True
    else:
        print(f"  [FAIL] {to_email}: {result.stdout[:200]}")
        return False

def wait_with_progress(seconds, next_company):
    """Wait with countdown display."""
    print(f"\n  Waiting {seconds//60} min before next send...")
    for remaining in range(seconds, 0, -30):
        print(f"    {remaining}s remaining | Next: {next_company}")
        time.sleep(min(30, remaining))
    print("  Interval complete.\n")

# ── Batch definitions ───────────────────────────────────────────────────────
BATCHES = {
    "utc6": [
        (5160, "contacto@xaze.com.mx", [], SUBJ_ES, "/tmp/email_xaze_mx.html",
         "Xaze Mexico", "Xaze Mexico electrodomesticos distribuidor Teka campanas"),
        (5161, "ventas@integrahogar.com", [], SUBJ_ES, "/tmp/email_integra_hogar_mx.html",
         "Integra Hogar Mexico", "Integra Hogar Mexico cocina empotrada arquitectos disenadores"),
        (5162, "atencionalcliente@todopormayoreo.mx", [], SUBJ_ES, "/tmp/email_todopormayoreo_mx.html",
         "TodoPorMayoreo Mexico", "TodoPorMayoreo Mexico mayorista B2B electrodomesticos campanas hornos"),
    ]
}

def run_batch(batch_key):
    emails = BATCHES.get(batch_key, [])
    if not emails:
        print(f"No batch '{batch_key}' found.")
        return

    print(f"\n[{now_tr()}] Starting batch '{batch_key}' — {len(emails)} emails, 5-min intervals\n")
    sent = 0
    failed = 0

    for i, item in enumerate(emails):
        row_num, to_email, extra_cc, subject, html_file, company_name, search_query = item

        print(f"[{now_tr()}] [{i+1}/{len(emails)}] {company_name} → {to_email}")

        # Check if already sent
        r = subprocess.run([
            "/opt/homebrew/bin/gog", "sheets", "get", SHEET_ID,
            f"Sheet1!K{row_num}", "--json", "--account", GOG_ACCOUNT, "--no-input"
        ], capture_output=True, text=True, timeout=10)
        try:
            d = json.loads(r.stdout)
            k_val = d.get('values', [['']])[0][0] if d.get('values') else ''
            if str(k_val).upper() in ('TRUE', 'YES', '1'):
                print(f"  SKIP: already sent (K=TRUE)")
                continue
        except:
            pass

        # Send the email
        success = send_email(to_email, extra_cc, subject, html_file, row_num)
        if success:
            sent += 1
        else:
            failed += 1

        # Wait 5 minutes (unless last email)
        if i < len(emails) - 1:
            next_name = emails[i+1][5]
            wait_with_progress(INTERVAL_SEC, next_name)

    print(f"\n[{now_tr()}] Batch '{batch_key}' complete: {sent} sent, {failed} failed")
    send_telegram(f"SA Outreach — {batch_key} tamamlandi\n{sent} gonderildi, {failed} basarisiz\n{now_tr()} Istanbul")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", help="Batch name (utc3/utc4/utc5/utc6)")
    parser.add_argument("--row", type=int, help="Single row number")
    parser.add_argument("--to", help="To email")
    parser.add_argument("--file", help="HTML file")
    parser.add_argument("--subject", default=SUBJ_ES, help="Subject")
    args = parser.parse_args()

    if args.batch:
        run_batch(args.batch)
    elif args.row and args.to and args.file:
        send_email(args.to, [], args.subject, args.file, args.row)
    else:
        print("Usage: --batch <name> OR --row <n> --to <email> --file <html>")
