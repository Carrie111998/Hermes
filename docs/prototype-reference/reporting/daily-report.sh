#!/bin/bash
# daily-report.sh
# Daily outreach report — reads sent data from Google Sheet (Column J date + K=TRUE)
# Checks replies via Exchange EWS ({{sender_email}}), NOT Gmail
# On Fridays includes a weekly summary.

BOT_TOKEN="{{TELEGRAM_BOT_TOKEN}}"
CHAT_IDS=("{{CHAT_ID}}" "{{CHAT_ID}}" "{{CHAT_ID}}")  # Efe, Anar, Emir Bilal
SHEET_ID="{{SHEET_ID}}"
GOG_ACCOUNT="{{gog_account}}"
ANTHROPIC_KEY="{{ANTHROPIC_API_KEY}}"
EWS_URL="https://{{EWS_HOST}}/ews/exchange.asmx"
EWS_USER="tempo\\emir.muhammed"
EWS_PASS="{{EWS_PASSWORD}}"
LOG_FILE="{{WORKSPACE}}/data/activity-log.json"

TODAY=$(date "+%Y-%m-%d")
TODAY_LABEL=$(date "+%d %B %Y")
DAY_OF_WEEK=$(date "+%u")
IS_FRIDAY=false
[ "$DAY_OF_WEEK" = "5" ] && IS_FRIDAY=true

# ── 1. Count today's sent emails from Google Sheet ───────────────────────────
# Column K = TRUE means sent; Column J contains date in "YYYY-MM-DD" somewhere
# We match rows where K=TRUE and J contains today's date string

SENT_DATA=$(/opt/homebrew/bin/gog sheets get "$SHEET_ID" "Sheet1!A1:K5200" \
  --json --account "$GOG_ACCOUNT" --no-input 2>/dev/null)

TODAY_STATS=$(echo "$SENT_DATA" | python3 -c "
import json, sys
today = '$TODAY'
data = json.load(sys.stdin)
rows = data.get('values', [])
sent = []
bounced = []
for i, row in enumerate(rows[1:], 2):
    k = str(row[10]).strip().upper() if len(row) > 10 else ''
    j = str(row[9]).strip() if len(row) > 9 else ''
    b = str(row[1]).strip() if len(row) > 1 else ''
    country = str(row[0]).strip() if len(row) > 0 else ''
    email = str(row[4]).strip() if len(row) > 4 else ''
    # Sent today: K=TRUE and J contains today's date
    if k == 'TRUE' and today in j:
        sent.append({'row': i, 'company': b, 'country': country, 'email': email, 'notes': j[:200]})
    # Bounced today
    if k == 'BOUNCE' and today in j:
        bounced.append({'company': b, 'country': country, 'email': email})

countries = list(dict.fromkeys(s['country'] for s in sent if s['country']))
print(json.dumps({
    'total_sent': len(sent),
    'countries': countries,
    'country_count': len(countries),
    'customers': sent,
    'bounced': bounced
}, ensure_ascii=False))
" 2>/dev/null)

TOTAL_SENT=$(echo "$TODAY_STATS" | python3 -c "import json,sys; print(json.load(sys.stdin)['total_sent'])" 2>/dev/null || echo "0")
COUNTRY_COUNT=$(echo "$TODAY_STATS" | python3 -c "import json,sys; print(json.load(sys.stdin)['country_count'])" 2>/dev/null || echo "0")
COUNTRIES_LIST=$(echo "$TODAY_STATS" | python3 -c "import json,sys; print(', '.join(json.load(sys.stdin)['countries']))" 2>/dev/null || echo "Yok")
BOUNCE_COUNT=$(echo "$TODAY_STATS" | python3 -c "import json,sys; print(len(json.load(sys.stdin)['bounced']))" 2>/dev/null || echo "0")
BOUNCE_DETAIL=$(echo "$TODAY_STATS" | python3 -c "
import json,sys
d = json.load(sys.stdin)
lines = ['%s (%s) - %s' % (b['company'], b['country'], b['email']) for b in d['bounced']]
print('\n'.join(lines) if lines else 'Yok')
" 2>/dev/null)

# ── 2. Check Exchange inbox for customer replies (EWS) ───────────────────────
REPLIES_DATA=$(python3 - << 'PYEOF'
import urllib.request, base64, xml.etree.ElementTree as ET, json, subprocess, sys

EWS_URL  = "https://{{EWS_HOST}}/ews/exchange.asmx"
EWS_CRED = base64.b64encode(rb"{{EWS_USERNAME}}:{{EWS_PASSWORD}}").decode()
SHEET_ID = "{{SHEET_ID}}"
GOG_ACCT = "{{gog_account}}"
TODAY    = __import__('datetime').date.today().isoformat()

# Get sent customer emails from sheet (K=TRUE)
try:
    result = subprocess.run(
        ["/opt/homebrew/bin/gog","sheets","get",SHEET_ID,"Sheet1!A1:K5200",
         "--json","--account",GOG_ACCT,"--no-input"],
        capture_output=True, text=True, timeout=60)
    rows = json.loads(result.stdout).get("values", [])
except:
    rows = []

sent_map = {}
for row in rows[1:]:
    k = str(row[10]).strip().upper() if len(row) > 10 else ''
    if k == "TRUE":
        company = row[1].strip() if len(row) > 1 else ''
        country = row[0].strip() if len(row) > 0 else ''
        name    = row[2].strip() if len(row) > 2 else ''
        if len(row) > 4 and row[4] and "@" in row[4]:
            sent_map[row[4].strip().lower()] = {"company": company, "country": country, "name": name}
        if len(row) > 5 and row[5]:
            for e in row[5].split(","):
                e = e.strip().lower()
                if "@" in e:
                    sent_map[e] = {"company": company, "country": country, "name": name}

# Query Exchange inbox — all messages from today
soap = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
               xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
  <soap:Body>
    <m:FindItem Traversal="Shallow">
      <m:ItemShape>
        <t:BaseShape>IdOnly</t:BaseShape>
        <t:AdditionalProperties>
          <t:FieldURI FieldURI="message:From"/>
          <t:FieldURI FieldURI="item:Subject"/>
          <t:FieldURI FieldURI="item:DateTimeReceived"/>
        </t:AdditionalProperties>
      </m:ItemShape>
      <m:IndexedPageItemView MaxEntriesReturned="50" Offset="0" BasePoint="Beginning"/>
      <m:SortOrder>
        <t:FieldOrder Order="Descending">
          <t:FieldURI FieldURI="item:DateTimeReceived"/>
        </t:FieldOrder>
      </m:SortOrder>
      <m:ParentFolderIds>
        <t:DistinguishedFolderId Id="inbox"/>
      </m:ParentFolderIds>
    </m:FindItem>
  </soap:Body>
</soap:Envelope>"""

try:
    req = urllib.request.Request(EWS_URL, data=soap.encode(),
        headers={"Content-Type":"text/xml","Authorization":f"Basic {EWS_CRED}",
                 "SOAPAction":'"http://schemas.microsoft.com/exchange/services/2006/messages/FindItem"'})
    with urllib.request.urlopen(req, timeout=20) as r:
        xml_text = r.read().decode()
except:
    print(json.dumps({"replies": [], "total": 0}))
    sys.exit()

NS_T = "{http://schemas.microsoft.com/exchange/services/2006/types}"
root = ET.fromstring(xml_text)
replies = []
for msg in root.iter(f"{NS_T}Message"):
    from_el = msg.find(f".//{NS_T}From")
    subj_el = msg.find(f".//{NS_T}Subject")
    date_el = msg.find(f".//{NS_T}DateTimeReceived")
    from_email = from_name = subj = date_str = ""
    if from_el is not None:
        em = from_el.find(f".//{NS_T}EmailAddress")
        nm = from_el.find(f".//{NS_T}Name")
        if em is not None: from_email = (em.text or "").lower().strip()
        if nm is not None: from_name = nm.text or ""
    if subj_el is not None: subj = subj_el.text or ""
    if date_el is not None: date_str = (date_el.text or "")[:10]

    # Skip internal Silverline senders and system messages
    skip_domains = ["silverline.com","microsoft.com","teams.mail","mailer-daemon","postmaster","no-reply","noreply","dogrumail.com"]
    if any(d in from_email for d in skip_domains):
        continue
    # Only today
    if date_str != TODAY:
        continue
    # Check if sender is a customer we emailed
    is_customer = from_email in sent_map
    customer_info = sent_map.get(from_email, {})
    replies.append({
        "from_email": from_email,
        "from_name": from_name,
        "subject": subj,
        "date": date_str,
        "is_customer": is_customer,
        "company": customer_info.get("company",""),
        "country": customer_info.get("country",""),
    })

customer_replies = [r for r in replies if r["is_customer"]]
other_replies    = [r for r in replies if not r["is_customer"]]
print(json.dumps({"replies": replies, "customer_replies": customer_replies, "other_replies": other_replies, "total": len(replies), "customer_total": len(customer_replies)}, ensure_ascii=False))
PYEOF
)

REPLY_TOTAL=$(echo "$REPLIES_DATA" | python3 -c "import json,sys; print(json.load(sys.stdin).get('customer_total',0))" 2>/dev/null || echo "0")
REPLY_TEXT=$(echo "$REPLIES_DATA" | python3 -c "
import json,sys
d = json.load(sys.stdin)
lines = []
for r in d.get('customer_replies', []):
    lines.append('- %s (%s) | Gonderen: %s | Konu: %s' % (r['company'], r['country'], r['from_email'], r['subject']))
print('\n'.join(lines) if lines else 'Yok')
" 2>/dev/null)

# ── 3. Weekly data (Friday only) ─────────────────────────────────────────────
WEEKLY_SECTION=""
if [ "$IS_FRIDAY" = true ]; then
  WEEK_START=$(python3 -c "
from datetime import datetime, timedelta
today = datetime.today()
monday = today - timedelta(days=today.weekday())
print(monday.strftime('%Y-%m-%d'))
")
  WEEK_SENT=$(echo "$SENT_DATA" | python3 -c "
import json,sys
week_start = '$WEEK_START'
today = '$TODAY'
data = json.load(sys.stdin)
rows = data.get('values', [])
total = 0
countries = set()
for row in rows[1:]:
    k = str(row[10]).strip().upper() if len(row) > 10 else ''
    j = str(row[9]).strip() if len(row) > 9 else ''
    country = str(row[0]).strip() if len(row) > 0 else ''
    if k == 'TRUE':
        import re
        dates = re.findall(r'20\d\d-\d\d-\d\d', j)
        for d in dates:
            if week_start <= d <= today:
                total += 1
                countries.add(country)
                break
print('%d|%d|%s' % (total, len(countries), ', '.join(countries)))
" 2>/dev/null)
  WEEK_TOTAL=$(echo "$WEEK_SENT" | cut -d'|' -f1)
  WEEK_COUNTRIES=$(echo "$WEEK_SENT" | cut -d'|' -f2)
  WEEK_COUNTRIES_LIST=$(echo "$WEEK_SENT" | cut -d'|' -f3)
  WEEKLY_SECTION="HAFTALIK OZET ($WEEK_START - $TODAY):
- Toplam gonderilen: ${WEEK_TOTAL:-0}
- Ulasilan ulke sayisi: ${WEEK_COUNTRIES:-0}
- Ulkeler: ${WEEK_COUNTRIES_LIST:-Yok}"
fi

# ── 4. Build report via Anthropic API ────────────────────────────────────────
if [ "$IS_FRIDAY" = true ]; then
  WEEKLY_PROMPT="
HAFTALIK VERILER ($WEEK_START - $TODAY):
- Bu hafta gonderilen toplam e-posta: ${WEEK_TOTAL:-0}
- Bu hafta ulasilan ulkeler (${WEEK_COUNTRIES:-0} ulke): ${WEEK_COUNTRIES_LIST:-Yok}"
else
  WEEKLY_PROMPT=""
fi

PROMPT="Asagidaki verileri kullanarak gunluk outreach raporu yaz. Dil Turkce. Tum e-postalar {{sender_name}} adina ({{sender_email}}) gonderilmistir.

TARIH: $TODAY_LABEL

GONDERILEN E-POSTALAR:
- Ulasilan musteri sayisi: ${TOTAL_SENT:-0}
- Ulasilan ulke sayisi: ${COUNTRY_COUNT:-0}
- Ulkeler: ${COUNTRIES_LIST:-Yok}
- Bounce (geri donen): ${BOUNCE_COUNT:-0}
$([ "$BOUNCE_COUNT" -gt 0 ] 2>/dev/null && echo "Bounce detayi: $BOUNCE_DETAIL")

MUSTERILERDEN GELEN YANITLAR ({{sender_email}} Exchange uzerinden):
- Yanit sayisi: ${REPLY_TOTAL:-0}
Yanit verenler:
$REPLY_TEXT
$WEEKLY_PROMPT

RAPOR YAPISI:
1. Gunun ozeti (1-2 cumle)
2. Sayisal veriler: gonderilen e-posta, ulasilan ulke sayisi ve isimleri, bounce sayisi
3. Musterilerden gelen yanitlar (varsa detayli, yoksa 'yanit yok')
$([ "$IS_FRIDAY" = true ] && echo "4. Haftalik ozet")

Kural: Sadece gerceklesen verileri aktar. Yorum ekleme. Kusa ve oze. Emoji kullanma. Cift tire (--) kullanma."

REPORT=$(curl -s -X POST "https://api.anthropic.com/v1/messages" \
  -H "x-api-key: $ANTHROPIC_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"claude-haiku-4-5\",
    \"max_tokens\": $([ "$IS_FRIDAY" = true ] && echo "800" || echo "500"),
    \"messages\": [{\"role\": \"user\", \"content\": $(echo "$PROMPT" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))")}]
  }" 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
content=d.get('content',[])
if content: print(content[0].get('text','').strip())
" 2>/dev/null)

[ -z "$REPORT" ] && REPORT="${TOTAL_SENT:-0} e-posta gonderildi, ${REPLY_TOTAL:-0} musteri yaniti alindi."

REPORT=$(echo "$REPORT" | sed 's/--/-/g')

if [ "$IS_FRIDAY" = true ]; then
  HEADER="GUNLUK VE HAFTALIK RAPOR - $TODAY_LABEL"
else
  HEADER="GUNLUK RAPOR - $TODAY_LABEL"
fi

MESSAGE="$HEADER

$REPORT"

# ── 5. Send to supervisors ───────────────────────────────────────────────────
for CHAT_ID in "${CHAT_IDS[@]}"; do
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    --data-urlencode "text=${MESSAGE}" > /dev/null 2>&1
done

echo "Report sent for $TODAY_LABEL"

# ── 6. Save activity log ─────────────────────────────────────────────────────
mkdir -p "$(dirname "$LOG_FILE")"
python3 -c "
import json, os
from datetime import datetime
log_file = '$LOG_FILE'
entry = {
    'date': '$TODAY',
    'sent': ${TOTAL_SENT:-0},
    'countries': ${COUNTRY_COUNT:-0},
    'customer_replies': ${REPLY_TOTAL:-0},
    'bounces': ${BOUNCE_COUNT:-0},
    'timestamp': datetime.now().isoformat()
}
log = []
if os.path.exists(log_file):
    try:
        with open(log_file) as f: log = json.load(f)
    except: log = []
updated = False
for i, e in enumerate(log):
    if e.get('date') == '$TODAY':
        log[i] = entry; updated = True; break
if not updated: log.append(entry)
with open(log_file, 'w') as f: json.dump(log, f, ensure_ascii=False, indent=2)
" 2>/dev/null
