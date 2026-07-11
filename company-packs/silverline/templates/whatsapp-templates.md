# WhatsApp Outreach Templates

## Master Template (English)

```
Good morning,
This is {{sender_name}} from Silverline Built-In Appliances Company Türkiye.
We are leading High End Built-In Products producer and hope we can find a mutual cooperation possibilities together.
Best Regards,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

**Pattern rules (MUST follow for every send):**
- 5-line body max, plain text, no HTML, no images, no links, no emojis
- Sign-off block: name + phone + email (always in this exact form, regardless of language)
- Greeting adapts to recipient local time-of-day: "Good morning" / "Good afternoon" / "Good evening"
- "Türkiye" stays in the company name (official name) regardless of language
- "mutual cooperation possibilities" — verbatim phrasing
- "Best Regards," — closing line in English for non-Latin scripts, localized in Romance/Germanic languages

## HARD RULES
1. **NO FOLLOW-UPS. Only initial reach.** Never message the same customer twice. If no reply, move on.
2. **NO AUTO-REPLY.** `channels.whatsapp.dmPolicy: "disabled"` — agent never processes inbound DMs.
3. **Localized text only.** Every body in customer's native language per country. Never English to ES/FR/PT/etc. markets.
4. **Always send full 10-file bundle.** JPG + 2 PDFs + 7 videos, every time.
5. **Operator alert on responses.** The platform notifies the operator on inbound replies. No auto-reply.

## Send Hours
- 09:00-12:00 and 13:00-15:00 **recipient local time** (mirrors email rule)
- Calculate per target country's timezone, not Istanbul

## Sending (abstracted)

Legacy prototype sent via the OpenClaw CLI over a paired personal WhatsApp
session. The product sends through the WhatsApp Business Cloud API instead —
mechanics, delivery-verification, and no-blind-retry rules live in the
`whatsapp-outreach` skill. Durable lessons preserved there: one media file per
send call; a transport timeout on large files is NOT a delivery failure;
always verify delivery status before any retry; text message first, then
assets sequentially.

## Attachment Bundle Inventory
| File | Size | Delivery |
|------|------|----------|
| 2f3654b2-...jpg | 128K | image (in-chat) |
| SILVERLINE PREMIUM PRODUCT_19.04 (3).pdf | 11M | document (auto) |
| 2026_Silverline_Catalogue (1).pdf | 49M | document (auto) |
| 104a8738-...mp4 | 52M | document (force) |
| 1afd62c1-...mp4 | 60M | document (force) |
| 7844cc37-...mp4 | 53M | document (force) |
| 9581ee70-...mp4 | 109M | document (force) |
| cc84acb3-...mp4 | 114M | document (force) |
| d078a437-...mp4 | 49M | document (force) |
| e2fdbcd6-...mp4 | 115M | document (force) |

---

## Language Variants

### Turkish (Türkiye)
```
Günaydın,
Silverline Built-In Appliances Company Türkiye'den {{sender_name}}.
Önde gelen Yüksek Kalite Ankastre Ürünler üreticisiyiz ve umarım karşılıklı iş birliği imkanları bulabiliriz.
Saygılarımızla,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### Spanish (LATAM + España)
```
Buenos días,
Soy {{sender_name}} de Silverline Built-In Appliances Company Türkiye.
Somos un productor líder de productos empotrables de alta gama y esperamos encontrar posibilidades de cooperación mutua.
Un cordial saludo,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### French (France, West Africa, Belgique)
```
Bonjour,
Je suis {{sender_name}} de Silverline Built-In Appliances Company Türkiye.
Nous sommes un producteur leader de produits encastrables haut de gamme et espérons trouver des possibilités de coopération mutuelle.
Cordialement,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### Portuguese (Brasil + Portugal)
```
Bom dia,
Sou {{sender_name}} da Silverline Built-In Appliances Company Türkiye.
Somos um produtor líder de produtos de embutir de alta gama e esperamos encontrar possibilidades de cooperação mútua.
Com os melhores cumprimentos,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### German (Deutschland, Österreich, Schweiz)
```
Guten Morgen,
mein Name ist {{sender_name}} von Silverline Built-In Appliances Company Türkiye.
Wir sind ein führender Hersteller hochwertiger Einbauprodukte und hoffen, gegenseitige Kooperationsmöglichkeiten zu finden.
Mit freundlichen Grüßen,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### Italian
```
Buongiorno,
sono {{sender_name}} di Silverline Built-In Appliances Company Türkiye.
Siamo un produttore leader di prodotti da incasso di alta gamma e speriamo di trovare possibilità di cooperazione reciproca.
Cordiali saluti,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### Arabic (MENA — Arabic)
```
صباح الخير،
أنا إمير بلال محمد من شركة سيلفرلاين للأجهزة المدمجة تركيا.
نحن منتج رائد للمنتجات المدمجة عالية الجودة ونأمل في إيجاد فرص تعاون متبادل.
مع أطيب التحيات،
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### Russian
```
Доброе утро,
меня зовут Эмир Билаль Мухаммед, компания Silverline Built-In Appliances Company Türkiye.
Мы являемся ведущим производителем встраиваемой техники высокого класса и надеемся найти возможности для взаимного сотрудничества.
С уважением,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### Dutch
```
Goedemorgen,
mijn naam is {{sender_name}} van Silverline Built-In Appliances Company Türkiye.
Wij zijn een toonaangevende producent van hoogwaardige inbouwproducten en hopen mogelijkheden voor wederzijdse samenwerking te vinden.
Met vriendelijke groet,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### Polish
```
Dzień dobry,
jestem {{sender_name}} z firmy Silverline Built-In Appliances Company Türkiye.
Jesteśmy wiodącym producentem wysokiej klasy urządzeń do zabudowy i mamy nadzieję na znalezienie możliwości wzajemnej współpracy.
Z poważaniem,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

---

## Country → Language Mapping
- Türkiye → Turkish
- LATAM (Mexico, Colombia, Chile, Argentina, Peru, Ecuador, etc.) → Spanish
- España → Spanish
- France, Belgium (Wallonia), Luxembourg, West/Central Africa (FR-speaking) → French
- Brasil → Portuguese (BR)
- Portugal → Portuguese (PT)
- Deutschland, Österreich, Schweiz → German
- Italia → Italian
- MENA (UAE excluded as SLV, Saudi, Egypt, etc.) → Arabic (respect the client's market preferences)
- Россия, Беларусь, Україна, Қазақстан → Russian
- Nederland, België (Flanders) → Dutch
- Polska → Polish
- UK, Ireland, Nordics, English-speaking Africa, Southeast Asia (SG, MY, PH), India, Australia → English
- Default (any other) → English

---

