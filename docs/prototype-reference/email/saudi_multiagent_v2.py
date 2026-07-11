#!/usr/bin/env python3
"""
saudi_multiagent_v2.py — EFE onaylı token-efficient Saudi outreach workflow.

Pattern:
  1. Parent reads batch JSON, finds first N unsent customers (max 5/wave)
  2. Parent spawns 5 sessions_spawn subagents in parallel (local-qwen, cleanup=delete)
  3. Each subagent: 1 web_search + write Arabic bridge to /tmp/sa_R{row}_bridge.txt
  4. Parent polls for bridge files (max 120s), then sends via EWS SOAP
  5. Parent updates Google Sheet (K=TRUE, J=note)
  6. Repeat for next wave

Usage:
  python3 saudi_multiagent_v2.py --batch /tmp/saudi_batchN.json --start N --count M --max-waves W

Each subagent prompt is intentionally minimal (≤ 6 lines) to save tokens.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

EWS_URL = "https://{{EWS_HOST}}/ews/exchange.asmx"
EWS_USER = "tempo\\emir.muhammed"
EWS_PASS = "{{EWS_PASSWORD}}"
CC_LIST = ["anarr@silverline.com", "umutatas@silverline.com", "berkany@silverline.com"]
SHEET_ID = "{{SHEET_ID}}"
SUBJECT = "أجهزة سيلفرلاين للمطبخ – جودة استثنائية، أسعار تنافسية"
GOG = "/opt/homebrew/bin/gog"
GOG_ACCOUNT = "{{gog_account}}"

P1_TEMPLATE = """السلام عليكم،<br><br>{bridge}<br><br><b>تشمل مجموعتنا:</b><br>• <b>شفاطات المطبخ</b> - سعة 1.2 مليون وحدة/سنة، أوسع تشكيلة في تركيا. جدارية، مدمجة، طاولة/سحب (SilverMute، 39 ديسيبل)، معلقة، جزيرة، سقف. تطبيق ذني، تحكم بالإيماءات.<br>• <b>أفران مدمجة</b> - تجويف 72 لتر، 14 وظيفة، حتى 310°م، تجويف أزرق ملكي فريد، إغلاق ناعم، تنظيف ذاتي+<br>• <b>مواقد</b> - غاز، زجاج سيراميك، حث، هجين. عرض 30-90 سم. شعلات wok حتى 3800 واط، وظيفة الجسر، DUET 2 في 1<br>• <b>ثلاجات، غسالات أطباق، غسالات ملابس، أفران ميكروويف</b><br><br><b>لماذا سيلفرلاين:</b><br>• من أكبر مصنعي شفاطات المطبخ في العالم<br>• 160+ جائزة تصميم (Red Dot، iF، Good Design، German Design Award)<br>• تعاون تصنيع أصلي مع BSH وGorenje<br>• مصنع زجاج خاص، مختبر معتمد<br>• معتمد CE، فئة D إلى A+<br><br><b>صور المنتجات:</b> <a href="https://files.catbox.moe/bvm40j.jpeg">عرض الصور</a><br><b>كتالوج:</b> <a href="https://disk.yandex.com.tr/d/Okzuoi-jpu7a4Q">Silverline Catalogue</a><br><b>جولة المصنع:</b> <a href="https://disk.yandex.com.tr/i/wiHJtku8_4_YaQ">Factory Tour</a><br><br><b>أمير بلال محمد</b><br>سيلفرلاين<br><a href="mailto:{{sender_email}}">{{sender_email}}</a><br><br><i>Bu e-posta ve ekleri gizlidir. / This email and any attachments are confidential.</i>"""

SUBAGENT_PROMPT = """Write Arabic bridge sentence for Silverline (kitchen appliances) to R{row}.

Steps:
1. Read {batch_file} to get company name for row {row}
2. web_search "{{company}} Saudi Arabia"
3. Write 1-2 sentence Arabic bridge to /tmp/sa_R{row}_bridge.txt (max 350 chars, no Türkiye mention, em-dash not --)

Reply only "OK"."""


def xe(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_soap(to_email: str, cc_emails: list, subject: str, body: str) -> str:
    cc_block = "".join(f"<t:Mailbox><t:EmailAddress>{e}</t:EmailAddress></t:Mailbox>" for e in cc_emails)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types" xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
<soap:Header><t:RequestServerVersion Version="Exchange2010_SP2"/></soap:Header>
<soap:Body><m:CreateItem MessageDisposition="SendAndSaveCopy"><m:Items><t:Message>
<t:Subject>{xe(subject)}</t:Subject>
<t:Body BodyType="HTML">{xe(body)}</t:Body>
<t:ToRecipients><t:Mailbox><t:EmailAddress>{to_email}</t:EmailAddress></t:Mailbox></t:ToRecipients>
<t:CcRecipients>{cc_block}</t:CcRecipients>
</t:Message></m:Items></m:CreateItem></soap:Body></soap:Envelope>'''


def send_email(to_email: str, cc_emails: list, bridge: str, row: int) -> bool:
    body = P1_TEMPLATE.format(bridge=bridge)
    soap = build_soap(to_email, cc_emails, SUBJECT, body)
    Path(f"/tmp/soap_R{row}.xml").write_text(soap)
    r = subprocess.run(
        ["curl", "-s", "-u", EWS_USER + ":" + EWS_PASS,
         "-H", "Content-Type: text/xml; charset=utf-8",
         "-H", "SOAPAction: \"http://schemas.microsoft.com/exchange/services/2006/messages/CreateItem\"",
         "--data-binary", f"@/tmp/soap_R{row}.xml", EWS_URL],
        capture_output=True, text=True, timeout=20,
    )
    return "NoError" in r.stdout or "ServerVersionInfo" in r.stdout


def update_sheet(row: int, note: str) -> None:
    subprocess.run([GOG, "sheets", "update", SHEET_ID, f"Sheet1!J{row}",
                    "--values-json", json.dumps([[note]], ensure_ascii=False),
                    "--account", GOG_ACCOUNT],
                   capture_output=True, text=True, timeout=15)
    subprocess.run([GOG, "sheets", "update", SHEET_ID, f"Sheet1!K{row}",
                    "--values-json", '[["TRUE"]]',
                    "--account", GOG_ACCOUNT],
                   capture_output=True, text=True, timeout=15)


def is_already_sent(row: int) -> bool:
    """Check Sheet K for row."""
    r = subprocess.run([GOG, "sheets", "get", SHEET_ID, f"Sheet1!K{row}",
                        "--account", GOG_ACCOUNT, "--json"],
                       capture_output=True, text=True, timeout=10)
    return "TRUE" in r.stdout


def shorten_bridge_if_needed(text: str, max_chars: int = 320) -> str:
    """Trim bridge to max_chars if exceeded (parent-side safety net)."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True, help="Path to batch JSON file")
    ap.add_argument("--start", type=int, default=0, help="Start index in batch")
    ap.add_argument("--count", type=int, default=5, help="Customers per wave (max 5)")
    ap.add_argument("--max-waves", type=int, default=1, help="Max waves to run")
    ap.add_argument("--wave-pause-s", type=int, default=5, help="Pause between waves")
    args = ap.parse_args()

    if args.count > 5:
        print(f"[WARN] count={args.count} > 5, capping to 5 (Ollama serializes)")
        args.count = 5

    customers = json.load(open(args.batch))
    wave_num = 0
    sent_total = 0
    idx = args.start

    while wave_num < args.max_waves and idx < len(customers):
        wave = customers[idx:idx + args.count]
        idx += args.count
        wave_num += 1
        print(f"\n=== Wave {wave_num}: {len(wave)} customers (rows {[c['row'] for c in wave]}) ===")

        # Filter out already-sent
        todo = [c for c in wave if not is_already_sent(c["row"])]
        skipped = len(wave) - len(todo)
        if skipped:
            print(f"  Skipped {skipped} already-sent")
        if not todo:
            continue

        # Step 1: write bridge files using parent-side mini-LLM call (saves subagent cost)
        # OR: caller can pre-spawn subagents; we just check for files here
        for c in todo:
            bridge_path = Path(f"/tmp/sa_R{c['row']}_bridge.txt")
            if not bridge_path.exists():
                print(f"  R{c['row']}: ❌ no bridge file at {bridge_path}")
                continue
            bridge = bridge_path.read_text().strip()
            bridge = shorten_bridge_if_needed(bridge)

            other = c.get("other_emails", "")
            cc = CC_LIST + ([e.strip() for e in other.split(",") if e.strip()] if other else [])

            if send_email(c["email"], cc, bridge, c["row"]):
                update_sheet(c["row"], f"W{wave_num} v2: {bridge[:160]}")
                sent_total += 1
                print(f"  R{c['row']} ({c['company'][:30]}): ✅")
            else:
                print(f"  R{c['row']} ({c['company'][:30]}): ❌ EWS error")

        if idx < len(customers) and wave_num < args.max_waves:
            time.sleep(args.wave_pause_s)

    print(f"\n=== Done: {sent_total} emails sent in {wave_num} waves ===")


if __name__ == "__main__":
    main()
