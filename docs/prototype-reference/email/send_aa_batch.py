#!/usr/bin/env python3
"""
Generic A&A email batch sender with language support.
Reads batch JSON, bridges, sends via EWS SOAP, updates Sheet K=TRUE.
"""
import os, sys, json, subprocess, time
from datetime import datetime
import pytz

EWS_URL = "https://{{EWS_HOST}}/ews/exchange.asmx"
EWS_CREDS = r"{{EWS_USERNAME}}:{{EWS_PASSWORD}}"
SHEET_ID = "{{SHEET_ID}}"
GOG_ACCOUNT = "{{gog_account}}"
GOG_BIN = "/opt/homebrew/bin/gog"
CC_ALWAYS = ["anarr@silverline.com", "umutatas@silverline.com", "berkany@silverline.com"]
PHOTO_BLOCK = open('{{WORKSPACE}}/photo_block_b64.html').read()
CATALOGUE_URL = "https://disk.yandex.com.tr/d/Okzuoi-jpu7a4Q"
FACTORY_URL = "https://disk.yandex.com.tr/i/wiHJtku8_4_YaQ"
ISTANBUL_TZ = pytz.timezone('Europe/Istanbul')

SUBJECTS = {
    'fr': 'Silverline Appareils de Cuisine Encastrables – Qualité Exceptionnelle, Prix Compétitifs',
    'en': 'Silverline Built-In Kitchen Appliances – Exceptional Quality, Competitive Pricing',
    'es': 'Silverline Electrodomésticos Empotrados – Calidad Excepcional, Precios Competitivos',
    'pt': 'Silverline Eletrodomésticos Embutidos – Qualidade Excepcional, Preços Competitivos',
    'ar': 'أجهزة سيلفرلاين للمطبخ المدمجة – جودة استثنائية، أسعار تنافسية',
}

TEMPLATES = {
    'fr': f"""<html><body>
<p>{{bridge}}</p>
<br>
{PHOTO_BLOCK}<br>
<b>Notre gamme comprend :</b><br>
• <b>Hottes de cuisine</b> - capacité de 1,2 million d'unités/an, la plus large gamme de modèles en Turquie. Murales, encastrées, plan de travail/downdraft (SilverMute, 39 dB), suspendues, îlot, plafond. App intelligent, contrôle gestuel, détecteur de mouvement, capteur de vapeur.<br>
• <b>Fours encastrables</b> - cavité 72L, 14 fonctions, 310°C max, cavité Royal Blue, Soft Close, SelfClean+, flux d'air 3D<br>
• <b>Tables de cuisson</b> - gaz, vitrocéramique, induction, hybride. 30-90cm. Brûleurs WOK 3800W, fonction Bridge, DUET 2-en-1<br>
• <b>Réfrigérateurs, lave-vaisselle, machines à laver, micro-ondes</b> - suite cuisine complète<br><br>
<b>Pourquoi Silverline se distingue :</b><br>
• L'un des plus grands fabricants de hottes au monde<br>
• Plus de 160 prix de design (Red Dot, iF, Good Design, German Design Award)<br>
• Collaboration OEM avec BSH et Gorenje<br>
• Propre usine de verre, propre laboratoire accrédité<br>
• Certifié CE, classe énergétique D à A+<br>
• Cavité Royal Blue, Soft Close, SelfClean+ sur modèles d'entrée de gamme<br><br>
<b>Catalogue produits Silverline :</b> <a href="{CATALOGUE_URL}">Silverline Product Catalogue</a><br>
<b>Visite d'usine Silverline :</b> <a href="{FACTORY_URL}">Silverline Factory Tour</a><br><br>
<b>{{sender_name}}</b><br>
Silverline<br>
<a href="mailto:{{sender_email}}">{{sender_email}}</a><br><br>
<i>Bu e-posta ve ekleri gizlidir. Yalnızca yukarıda belirtilen alıcı(lar) için tasarlanmıştır. Yetkisiz erişim, kullanım veya ifşa edilmesi yasaktır. Bu e-postayı bir hata sonucu aldıysanız, lütfen göndereni bilgilendirin ve mesajı silin. Thank you.</i><br>
<i>This email and any attachments are confidential. They are intended solely for the named recipient(s). Unauthorized access, use, or disclosure is prohibited. If you have received this email in error, please notify the sender and delete the message. Thank you.</i>
</body></html>""",
    'en': f"""<html><body>
<p>{{bridge}}</p>
<br>
{PHOTO_BLOCK}<br>
<b>Our range includes:</b><br>
• <b>Range Hoods</b> - 1.2M units/year capacity, widest model range in Turkey. Wall-mounted, built-in, worktop/downdraft (SilverMute, 39 dB), suspended, island, ceiling. Smart app, gesture control, motion sense, steam sensor.<br>
• <b>Built-in Ovens</b> - 72L cavity, 14 functions, 310°C max, Royal Blue cavity, Soft Close, SelfClean+, 3D Air Flow<br>
• <b>Hobs</b> - Gas, vitro ceramic, induction, hybrid. 30-90cm. WOK burners 3800W, Bridge function, DUET 2-in-1<br>
• <b>Refrigerators, dishwashers, washing machines, microwaves</b> - full kitchen suite<br><br>
<b>Why Silverline stands out:</b><br>
• One of the world's largest hood manufacturers<br>
• 160+ design awards (Red Dot, iF, Good Design, German Design Award)<br>
• OEM Collaboration with BSH and Gorenje<br>
• Own glass factory, own accredited laboratory<br>
• CE certified, energy class D to A+<br>
• Royal Blue cavity, Soft Close, SelfClean+ on entry-level models<br><br>
<b>Silverline Product Catalogue:</b> <a href="{CATALOGUE_URL}">Silverline Product Catalogue</a><br>
<b>Silverline Factory Tour:</b> <a href="{FACTORY_URL}">Silverline Factory Tour</a><br><br>
<b>{{sender_name}}</b><br>
Silverline<br>
<a href="mailto:{{sender_email}}">{{sender_email}}</a><br><br>
<i>Bu e-posta ve ekleri gizlidir. Yalnızca yukarıda belirtilen alıcı(lar) için tasarlanmıştır. Yetkisiz erişim, kullanım veya ifşa edilmesi yasaktır. Bu e-postayı bir hata sonucu aldıysanız, lütfen göndereni bilgilendirin ve mesajı silin. Thank you.</i><br>
<i>This email and any attachments are confidential. They are intended solely for the named recipient(s). Unauthorized access, use, or disclosure is prohibited. If you have received this email in error, please notify the sender and delete the message. Thank you.</i>
</body></html>""",
    'es': f"""<html><body>
<p>{{bridge}}</p>
<br>
{PHOTO_BLOCK}<br>
<b>Nuestra gama incluye:</b><br>
• <b>Campanas extractoras</b> - capacidad de 1,2 millones de unidades/año, la gama más amplia de modelos en Turquía. Murales, empotradas, de encimera/downdraft (SilverMute, 39 dB), suspendidas, de isla, de techo. App inteligente, control gestual, sensor de movimiento, sensor de vapor.<br>
• <b>Hornos empotrados</b> - cavidad 72L, 14 funciones, 310°C máx, cavidad Royal Blue, Soft Close, SelfClean+, flujo de aire 3D<br>
• <b>Placas de cocción</b> - gas, vitrocerámica, inducción, híbrida. 30-90cm. Quemadores WOK 3800W, función Bridge, DUET 2-en-1<br>
• <b>Refrigeradores, lavavajillas, lavadoras, microondas</b> - suite de cocina completa<br><br>
<b>Por qué Silverline destaca:</b><br>
• Uno de los mayores fabricantes de campanas del mundo<br>
• Más de 160 premios de diseño (Red Dot, iF, Good Design, German Design Award)<br>
• Colaboración OEM con BSH y Gorenje<br>
• Fábrica de vidrio propia, laboratorio acreditado propio<br>
• Certificado CE, clase energética D a A+<br>
• Cavidad Royal Blue, Soft Close, SelfClean+ en modelos de entrada<br><br>
<b>Catálogo de productos Silverline:</b> <a href="{CATALOGUE_URL}">Silverline Product Catalogue</a><br>
<b>Tour por la fábrica Silverline:</b> <a href="{FACTORY_URL}">Silverline Factory Tour</a><br><br>
<b>{{sender_name}}</b><br>
Silverline<br>
<a href="mailto:{{sender_email}}">{{sender_email}}</a><br><br>
<i>Bu e-posta ve ekleri gizlidir. Yalnızca yukarıda belirtilen alıcı(lar) için tasarlanmıştır. Yetkisiz erişim, kullanım veya ifşa edilmesi yasaktır. Bu e-postayı bir hata sonucu aldıysanız, lütfen göndereni bilgilendirin ve mesajı silin. Thank you.</i><br>
<i>This email and any attachments are confidential. They are intended solely for the named recipient(s). Unauthorized access, use, or disclosure is prohibited. If you have received this email in error, please notify the sender and delete the message. Thank you.</i>
</body></html>""",
}

def get_language(country, email='', company=''):
    c = country.lower()
    e = email.lower() if email else ''
    co = company.lower() if company else ''
    
    # TLD-based detection (most reliable)
    import re
    m = re.search(r'@[\w.-]+\.(\w+)$', e)
    tld = m.group(1) if m else ''
    
    ar_tlds = {'sa', 'ae', 'eg', 'iq', 'jo', 'kw', 'qa', 'bh', 'om', 'ye', 'lb', 'sy', 'ps', 'tn', 'ly', 'ma', 'mr', 'so', 'sd', 'dj'}
    fr_tlds = {'fr', 'bi', 'ga', 'cg', 'cd', 'cm', 'sn', 'bf', 'mg', 'mu', 'yt', 'km', 'sc', 're', 'nc', 'ne', 'ml', 'tg', 'bj', 'td', 'cf', 'gn', 'gw', 'lr', 'sl', 'ci', 'ht'}
    es_tlds = {'es', 'mx', 'ar', 'cl', 'co', 'pe', 've', 'uy', 'py', 'bo', 'ec', 'do', 'gt', 'hn', 'sv', 'ni', 'pa', 'cr', 'cu', 'pr'}
    pt_tlds = {'pt', 'br', 'ao', 'mz', 'cv', 'gw', 'st', 'gq', 'tl'}
    
    if tld in ar_tlds: return 'ar'
    if tld in fr_tlds: return 'fr'
    if tld in es_tlds: return 'es'
    if tld in pt_tlds: return 'pt'
    
    # Company name hints (for .com, .org, .net etc.)
    ar_companies = ['mashhor', 'alkamal', 'elsaeedy', 'kods', 'maktab', 'elsheikh', 'galaxy electronics', 'abouseif', 'mashhor', 'sherif']
    es_companies = ['luzanel', 'superco', 's.r.l.', 'sa de cv', 'srl', 'mexico', 'colombia', 'argentina']
    fr_companies = ['s.p.r.l.', 'sprl', 'fokou', 'groupe']
    for kw in ar_companies:
        if kw in co: return 'ar'
    for kw in es_companies:
        if kw in co: return 'es'
    for kw in fr_companies:
        if kw in co: return 'fr'
    
    # Country-based fallback
    fr = ['burundi', 'haiti', 'togo', 'gabon', 'mayotte', 'demokratik kongo', 'kongo - kinşasa', 'kongo brazavil', 'kongo - brazavil', 'senegal', 'benin', 'burkina faso', 'côte', 'madagascar', 'komorlar', 'seychelles', 'seyrşeller']
    es = ['peru', 'panama', 'paraguay', 'arjantin', 'meksika', 'kolombiya', 'şili', 'ekvador', 'bolivya', 'uruguay', 'dominik', 'jamaika', 'honduras']
    pt = ['angola', 'brezilya', 'mozambik']
    ar = ['sudan', 'mısır', 'suudi arabistan', 'irak', 'katar', 'küveyt', 'umman', 'bahreyn', 'yemen', 'ürdün', 'filistin', 'suriiye', 'lübnan', 'libya', 'tunus', 'fas', 'mauritanya', 'somali', 'cibuti']
    if any(k in c for k in fr): return 'fr'
    if any(k in c for k in es): return 'es'
    if any(k in c for k in pt): return 'pt'
    if any(k in c for k in ar): return 'ar'
    return 'en'

def build_email(bridge, language='en'):
    template = TEMPLATES.get(language, TEMPLATES['en'])
    return template.replace('{bridge}', bridge)

def make_soap(to_email, cc_list, subject, html_body):
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

def send_email(to_email, cc_list, subject, html_body, row_num, language):
    full_cc = list(cc_list)
    for cc in CC_ALWAYS:
        if cc not in full_cc:
            full_cc.append(cc)
    
    soap = make_soap(to_email, full_cc, subject, html_body)
    soap_file = f"/tmp/soap_aa_{language}_{row_num}.xml"
    open(soap_file, 'w', encoding='utf-8').write(soap)
    
    result = subprocess.run([
        "curl", "-s", "-u", EWS_CREDS,
        "-H", "Content-Type: text/xml; charset=utf-8",
        "-H", "SOAPAction: http://schemas.microsoft.com/exchange/services/2006/messages/CreateItem",
        "--data-binary", f"@{soap_file}",
        EWS_URL
    ], capture_output=True, text=True, timeout=30)
    
    if result.returncode != 0:
        print(f"  CURL FAIL: {result.stderr[:200]}")
        return False
    
    response = result.stdout
    if "NoError" in response or "MessageId" in response:
        return True
    if "Error" in response:
        print(f"  EWS ERROR: {response[:400]}")
        return False
    return False

def mark_sent(row_num, country):
    """Mark K=TRUE in Google Sheet, append to J. NO COMMAS in note (gog splits on commas)."""
    timestamp = datetime.now(ISTANBUL_TZ).strftime('%Y-%m-%d')
    # No commas - use dash separator
    note = f"Email sent {timestamp} via Silverline EWS outreach - {country} batch R{row_num}"
    
    r2 = subprocess.run([
        GOG_BIN, "sheets", "update", SHEET_ID,
        f"Sheet1!J{row_num}:J{row_num}", note,
        "--account", GOG_ACCOUNT
    ], capture_output=True, text=True, timeout=15)
    
    r3 = subprocess.run([
        GOG_BIN, "sheets", "update", SHEET_ID,
        f"Sheet1!K{row_num}:K{row_num}", "TRUE",
        "--account", GOG_ACCOUNT
    ], capture_output=True, text=True, timeout=15)
    
    return r2.returncode == 0 and r3.returncode == 0

if __name__ == "__main__":
    batch_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/email_batch1_25.json'
    bridge_prefix = sys.argv[2] if len(sys.argv) > 2 else 'bi'
    start = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    count = int(sys.argv[4]) if len(sys.argv) > 4 else 5
    
    batch = json.load(open(batch_path))
    batch = batch[start:start+count]
    
    print(f"=== AA BATCH ({len(batch)} customers, prefix={bridge_prefix}, start={start}) ===\n")
    sent = 0
    failed = 0
    
    for c in batch:
        row = c['row']
        company = c['company']
        email = c['email']
        country = c['country']
        language = get_language(country, email, company)
        subject = SUBJECTS.get(language, SUBJECTS['en'])
        
        bridge_file = f"/tmp/{bridge_prefix}_R{row}_bridge.txt"
        if not os.path.exists(bridge_file):
            print(f"  SKIP no bridge: R{row} {company}")
            failed += 1
            continue
        bridge = open(bridge_file).read().strip()
        
        html_body = build_email(bridge, language)
        html_file = f"/tmp/email_aa_{language}_R{row}.html"
        open(html_file, 'w', encoding='utf-8').write(html_body)
        
        # CC
        cc_list = []
        oe = c.get('other_emails', '')
        if oe:
            for o in oe.split(','):
                o = o.strip()
                if o and o not in cc_list:
                    cc_list.append(o)
        
        print(f"[{datetime.now(ISTANBUL_TZ).strftime('%H:%M:%S')}] R{row} {country:15} {company[:30]:30} -> {email} ({language})")
        ok = send_email(email, cc_list, subject, html_body, row, language)
        if ok:
            print(f"  -> sent OK")
            mark_ok = mark_sent(row, country)
            if mark_ok:
                print(f"  -> Sheet updated")
            else:
                print(f"  -> Sheet FAILED")
            sent += 1
        else:
            print(f"  -> SEND FAILED")
            failed += 1
        time.sleep(2)
    
    print(f"\n=== COMPLETE: {sent} sent, {failed} failed ===")
