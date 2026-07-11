#!/usr/bin/env python3
"""
Full all-time Exchange bounce check.
Scans ALL MAILER-DAEMON messages in inbox (paginated),
extracts bounced email addresses, finds matching rows in Google Sheet,
updates Column K = BOUNCE, reports summary.
"""
import subprocess
import json
import re
import xml.etree.ElementTree as ET

SHEET_ID = "{{SHEET_ID}}"
ACCOUNT = "{{gog_account}}"
EWS_URL = "https://{{EWS_HOST}}/ews/exchange.asmx"
EWS_AUTH = "tempo\\emir.muhammed:{{EWS_PASSWORD}}"

ns = {
    's': 'http://schemas.xmlsoap.org/soap/envelope/',
    'm': 'http://schemas.microsoft.com/exchange/services/2006/messages',
    't': 'http://schemas.microsoft.com/exchange/services/2006/types'
}

def ews(soap_body, action):
    with open('/tmp/_ews_req.xml', 'w') as f:
        f.write(f'''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages"
               xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
               xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>{soap_body}</soap:Body>
</soap:Envelope>''')
    r = subprocess.run([
        'curl', '-s', '-u', EWS_AUTH,
        '-H', 'Content-Type: text/xml',
        '-H', f'SOAPAction: "http://schemas.microsoft.com/exchange/services/2006/messages/{action}"',
        '--data-binary', '@/tmp/_ews_req.xml', EWS_URL
    ], capture_output=True, text=True)
    return ET.fromstring(r.stdout)

def gog(args):
    r = subprocess.run(['gog'] + args + ['--account', ACCOUNT, '--json'],
                       capture_output=True, text=True)
    return r.stdout, r.stderr

# ── Step 1: Collect all MAILER-DAEMON item IDs from inbox ──────────────────
print("Step 1: Scanning inbox for all MAILER-DAEMON bounce messages...")
all_ids = []
offset = 0
page_size = 100
total_in_view = None

while True:
    find_body = f'''
    <m:FindItem Traversal="Shallow">
      <m:ItemShape><t:BaseShape>IdOnly</t:BaseShape></m:ItemShape>
      <m:IndexedPageItemView MaxEntriesReturned="{page_size}" Offset="{offset}" BasePoint="Beginning"/>
      <m:Restriction>
        <t:Contains ContainmentMode="Substring" ContainmentComparison="IgnoreCase">
          <t:FieldURI FieldURI="message:From"/>
          <t:Constant Value="MAILER-DAEMON"/>
        </t:Contains>
      </m:Restriction>
      <m:ParentFolderIds>
        <t:DistinguishedFolderId Id="inbox"/>
      </m:ParentFolderIds>
    </m:FindItem>'''
    
    root = ews(find_body, "FindItem")
    rf = root.find('.//m:RootFolder', ns)
    if rf is None:
        print("  No RootFolder found — stopping.")
        break
    
    if total_in_view is None:
        total_in_view = int(rf.get('TotalItemsInView', 0))
        print(f"  Total MAILER-DAEMON messages: {total_in_view}")
    
    items = root.findall('.//t:Message', ns)
    page_ids = [(it.find('t:ItemId', ns).get('Id'), it.find('t:ItemId', ns).get('ChangeKey'))
                for it in items if it.find('t:ItemId', ns) is not None]
    all_ids.extend(page_ids)
    
    includes_last = rf.get('IncludesLastItemInRange', 'false').lower() == 'true'
    print(f"  Page offset={offset}: got {len(page_ids)} items. Total so far: {len(all_ids)}")
    
    if includes_last or len(page_ids) == 0:
        break
    offset += page_size

print(f"\nTotal bounce IDs collected: {len(all_ids)}")

# ── Step 2: GetItem in batches to extract bounced addresses ────────────────
print("\nStep 2: Extracting bounced email addresses from bounce messages...")
bounced_emails = set()
batch_size = 25

for batch_start in range(0, len(all_ids), batch_size):
    batch = all_ids[batch_start:batch_start + batch_size]
    item_xml = ''.join(f'<t:ItemId Id="{id_}" ChangeKey="{ck}"/>' for id_, ck in batch)
    
    get_body = f'''
    <m:GetItem>
      <m:ItemShape>
        <t:BaseShape>Default</t:BaseShape>
        <t:BodyType>Text</t:BodyType>
        <t:AdditionalProperties>
          <t:FieldURI FieldURI="item:Body"/>
        </t:AdditionalProperties>
      </m:ItemShape>
      <m:ItemIds>{item_xml}</m:ItemIds>
    </m:GetItem>'''
    
    root = ews(get_body, "GetItem")
    msgs = root.findall('.//t:Message', ns)
    
    skip_patterns = ['silverline.com', 'tempo-timay.com', 'dogrumail.com',
                     'microsoft.com', 'outlook.com', 'office365.com', 'protection.outlook.com']
    
    for msg in msgs:
        body = msg.findtext('.//t:Body', '', ns) or ''
        emails_in_body = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', body)
        for e in emails_in_body:
            e_lower = e.lower()
            if not any(p in e_lower for p in skip_patterns):
                bounced_emails.add(e_lower)
    
    print(f"  Processed batch {batch_start//batch_size + 1} ({len(batch)} msgs). Unique bounced so far: {len(bounced_emails)}")

print(f"\nTotal unique bounced addresses: {len(bounced_emails)}")
for e in sorted(bounced_emails):
    print(f"  {e}")

# ── Step 3: Read sheet and find matching rows ───────────────────────────────
print("\nStep 3: Reading Google Sheet to find matching rows...")
stdout, stderr = gog(["sheets", "get", SHEET_ID, "A1:K6000"])
try:
    data = json.loads(stdout)
    rows = data.get("values", data) if isinstance(data, dict) else data
except Exception as e:
    print(f"Sheet read error: {e}\n{stdout[:300]}")
    exit(1)

print(f"Sheet rows loaded: {len(rows)}")

matches = []
for i, row in enumerate(rows):
    sheet_row = i + 1
    if len(row) < 5:
        continue
    email_e = str(row[4]).strip().lower()
    email_f = str(row[5]).strip().lower() if len(row) > 5 else ""
    current_k = str(row[10]).strip() if len(row) > 10 else ""
    company = str(row[1]).strip() if len(row) > 1 else ""
    
    for bounced in bounced_emails:
        if bounced == email_e or bounced == email_f:
            if current_k != "BOUNCE":  # Only update if not already marked
                matches.append({
                    "sheet_row": sheet_row,
                    "company": company,
                    "email": bounced,
                    "old_k": current_k
                })

print(f"Rows to update (not already BOUNCE): {len(matches)}")

# ── Step 4: Update Column K = BOUNCE ───────────────────────────────────────
print("\nStep 4: Updating Column K = BOUNCE...")
updated = []
errors = []

for m in matches:
    cell = f"K{m['sheet_row']}"
    stdout, stderr = gog(["sheets", "update", SHEET_ID, cell, "BOUNCE"])
    if stderr.strip() and "error" in stderr.lower():
        print(f"  ERROR {cell} ({m['company']}): {stderr.strip()[:80]}")
        errors.append(m)
    else:
        print(f"  {cell} ({m['company']}) [{m['email']}] old={m['old_k']} -> BOUNCE")
        updated.append(m)

# ── Summary ────────────────────────────────────────────────────────────────
print(f"""
========================================
FULL BOUNCE CHECK COMPLETE
========================================
Total MAILER-DAEMON messages scanned : {len(all_ids)}
Unique bounced addresses found       : {len(bounced_emails)}
Sheet rows updated to BOUNCE         : {len(updated)}
Errors                               : {len(errors)}
========================================
""")

# Save report
report = {
    "bounced_emails": sorted(bounced_emails),
    "updated_rows": updated,
    "errors": errors
}
with open("/tmp/full_bounce_report.json", "w") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
print("Report saved to /tmp/full_bounce_report.json")
