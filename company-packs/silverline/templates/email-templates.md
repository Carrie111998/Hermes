# Email Templates

## ⚠️ GLOBAL RULES (updated 2026-07-10 operator feedback — override anything below that conflicts)

### 1. Subject line — fixed title, nothing else
Every outreach email subject is EXACTLY the title below in the email's language. No company name, no "Re:", no punctuation, no personalization, nothing appended or prepended.

| Lang | Subject |
|------|---------|
| EN | Silverline Premium Built-In Kitchen Appliances |
| TR | Silverline Premium Ankastre Mutfak Cihazları |
| AR | أجهزة المطبخ المدمجة الفاخرة من سيلفرلاين |
| ES | Electrodomésticos de cocina empotrados premium Silverline |
| FR | Électroménager de cuisine encastrable premium Silverline |
| PT | Eletrodomésticos de cozinha de embutir premium Silverline |
| DE | Silverline Premium-Einbauküchengeräte |
| IT | Elettrodomestici da incasso premium Silverline |
| RU | Премиальная встраиваемая кухонная техника Silverline |
| NL | Silverline premium inbouwkeukenapparatuur |
| PL | Urządzenia kuchenne do zabudowy premium Silverline |

### 2. One language per email
The ENTIRE email — greeting, bridge, body, section headers, link labels, signature title, and disclaimer — is written in ONE language: the target market's language. Never mix languages in a single email. The old dual Turkish+English disclaimer is retired; translate the disclaimer below into the email's language and use only that version.

Exceptions that stay untranslated: brand and product names (Silverline, SilverMute, DUET, Soft Close, SelfClean+), award names (Red Dot, iF, Good Design, German Design Award), partner names (BSH, Gorenje), "OEM", technical units (dB, W, L, cm, °C).

Canonical disclaimer (translate into email language): *"This email and any attachments are confidential. They are intended solely for the named recipient(s). Unauthorized access, use, or disclosure is prohibited. If you have received this email in error, please notify the sender and delete the message. Thank you."*

### 3. One send per company — all addresses at once
- **To:** primary email (Sheet column E)
- **CC:** every additional address from Sheet column F (comma-separated)
- Never send separate copies per address. One EWS send per company covers everyone.

### 4. Response tracking & 30-day re-reach
- Column M = Email Response?, Column N = WhatsApp Response?, Column O = Last Contact Date (YYYY-MM-DD, written on every send).
- A company already contacted (K or L set) may be re-reached ONLY if column O is ≥ 30 days ago AND M and N are both empty.
- If M or N has a response, NEVER auto re-reach — the salesman owns that thread.

### 5. Target filter — no industrial kitchen companies
Skip any company whose business is industrial/commercial kitchen equipment (HORECA, catering equipment, professional/industrial ovens, stainless catering lines). Silverline sells DOMESTIC built-in appliances. If research reveals an industrial focus: write "SKIP — industrial kitchen" in column J, leave K empty, do not send.

### 6. Internal salesman notification
After each firm is researched and its outreach handled, send ONE internal email to `{{sender_email}}`:
- Subject: `[Internal] {Company} — {Country} — researched`
- Body: research summary (the bridge), what was sent (email/WhatsApp/LinkedIn), to which addresses, and the send timestamp.
- This informs the salesman; it is not customer-facing and may be in Turkish.

## English Template (Bridge + Body + Disclaimer)

```html
[Bridge sentence about customer's market]<br><br>
{{product_photo_block}}<br>
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
<b>Silverline Product Catalogue:</b> <a href="https://disk.yandex.com.tr/d/Okzuoi-jpu7a4Q">Silverline Product Catalogue</a><br>
<b>Silverline Factory Tour:</b> <a href="https://disk.yandex.com.tr/i/wiHJtku8_4_YaQ">Silverline Factory Tour</a><br><br>
<b>{{sender_name}}</b><br>
Silverline<br>
<a href="mailto:{{sender_email}}">{{sender_email}}</a><br><br>
<i>Bu e-posta ve ekleri gizlidir. Yalnızca yukarıda belirtilen alıcı(lar) için tasarlanmıştır. Yetkisiz erişim, kullanım veya ifşa edilmesi yasaktır. Bu e-postayı bir hata sonucu aldıysanız, lütfen göndereni bilgilendirin ve mesajı silin. Thank you.</i><br>
<i>This email and any attachments are confidential. They are intended solely for the named recipient(s). Unauthorized access, use, or disclosure is prohibited. If you have received this email in error, please notify the sender and delete the message. Thank you.</i>
```

## Arabic Template (MENA — Saudi, Egypt, MENA — exclude Lebanon per matrix)

**Subject:** `أجهزة سيلفرلاين للمطبخ – جودة استثنائية، أسعار تنافسية`

**Body:**
```
السلام عليكم،<br><br>
[Bridge sentence about customer's market in Arabic]<br><br>
{{product_photo_block}}<br>
<b>تشمل مجموعتنا:</b><br>
• <b>شفاطات المطبخ</b> - سعة إنتاج 1.2 مليون وحدة سنوياً، أوسع تشكيلة موديلات في تركيا. شفاطات جدارية، مدمجة، طاولة/سحب (SilverMute، 39 ديسيبل)، معلقة، جزيرة، سقف. تطبيق ذكي، تحكم بالإيماءات، استشعار الحركة، استشعار البخار.<br>
• <b>أفران مدمجة</b> - تجويف 72 لتر، 14 وظيفة، حتى 310°م، تجويف أزرق ملكي فريد، إغلاق ناعم، تنظيف ذاتي+، تدفق هواء ثلاثي الأبعاد<br>
• <b>مواقد</b> - غاز، زجاج سيراميك، حث، هجين. عرض 30-90 سم. شعلات wok حتى 3800 واط، وظيفة الجسر، وحدة DUET مدمجة 2 في 1<br>
• <b>ثلاجات، غسالات أطباق، غسالات ملابس، أفران ميكروويف</b> - مجموعة مطبخ كاملة<br><br>
<b>لماذا تتميز سيلفرلاين:</b><br>
• من أكبر مصنعي شفاطات المطبخ في العالم<br>
• أكثر من 160 جائزة تصميم (Red Dot، iF، Good Design، German Design Award)<br>
• تعاون تصنيع أصلي مع BSH وGorenje<br>
• مصنع زجاج خاص، مختبر معتمد خاص<br>
• معتمد CE، فئة الطاقة من D إلى A+<br>
• تجويف أزرق ملكي، إغلاق ناعم، تنظيف ذاتي+ حتى في الموديلات الأساسية<br><br>
<b>كتالوج منتجات سيلفرلاين:</b> <a href="https://disk.yandex.com.tr/d/Okzuoi-jpu7a4Q">Silverline Product Catalogue</a><br>
<b>جولة في مصنع سيلفرلاين:</b> <a href="https://disk.yandex.com.tr/i/wiHJtku8_4_YaQ">Silverline Factory Tour</a><br><br>
<b>أمير بلال محمد</b><br>
سيلفرلاين<br>
<a href="mailto:{{sender_email}}">{{sender_email}}</a><br><br>
<i>Bu e-posta ve ekleri gizlidir. Yalnızca yukarıda belirtilen alıcı(lar) için tasarlanmıştır. Yetkisiz erişim, kullanım veya ifşa edilmesi yasaktır. Bu e-postayı bir hata sonucu aldıysanız, lütfen göndereni bilgilendirin ve mesajı silin. Thank you.</i><br>
<i>This email and any attachments are confidential. They are intended solely for the named recipient(s). Unauthorized access, use, or disclosure is prohibited. If you have received this email in error, please notify the sender and delete the message. Thank you.</i>
```

**Pattern rules:**
- Opening: `السلام عليكم،` for Arabic — formal B2B greeting
- Bridge sentence in Arabic, personalized per customer research
- Photo block identical to English template (base64-embedded)
- Product list in Arabic with key differentiators
- "OEM Collaboration" / "تعاون تصنيع أصلي" for the BSH/Gorenje partnership (NOT "تعاون مع العلامة التجارية سيلفرلاين")
- Links: catalogue + factory video as bold HTML anchors
- Signature: name in Arabic + phone in E.164 international + email (same as English template)
- Disclaimer: single Arabic translation of the canonical disclaimer (see Global Rule 2 — the dual TR+EN block in the sample above is retired)

