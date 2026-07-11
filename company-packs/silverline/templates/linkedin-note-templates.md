# LinkedIn Connection Request Templates

**Source:** prototype template (2026-06-15)
**Channel:** LinkedIn connection request note
**Character limit:** 300 characters (hard cap enforced by LinkedIn)
**Identity:** Always sent as **{{sender_name}}** (Silverline Appliances)
**Rule:** No follow-up connection notes to the same person. One reach, move on.

---

## Master Template (English) — 4 lines, 241 chars

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
- Greeting adapts to recipient local time-of-day in their country: "Good morning" / "Good afternoon" / "Good evening"
- "Türkiye" stays in the company name (official name) regardless of language
- "mutual cooperation possibilities" — verbatim phrasing (do not paraphrase)
- Sign-off name + phone + email: same in every language
- "Best Regards," — closing line stays English in non-Latin scripts, localized in Romance/Germanic languages
- Strip down to 4 lines + sign-off if recipient's language has stricter character economy
- If the contact's profile name suggests non-binary or no obvious gender cue, default to neutral "Hello," or "Good day," opening

---

## Language Variants (each ≤ 300 chars)

### Turkish (Türkiye) — 218 chars
```
Günaydın,
Silverline Built-In Appliances Company Türkiye'den {{sender_name}}.
Önde gelen Yüksek Kalite Ankastre Ürünler üreticisiyiz ve umarım karşılıklı iş birliği imkanları bulabiliriz.
Saygılarımızla,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### Spanish (LATAM + España) — 270 chars
```
Buenos días,
Soy {{sender_name}} de Silverline Built-In Appliances Company Türkiye.
Somos un productor líder de productos empotrables de alta gama y esperamos encontrar posibilidades de cooperación mutua.
Un cordial saludo,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### French (France, West Africa, Belgique) — 281 chars
```
Bonjour,
Je suis {{sender_name}} de Silverline Built-In Appliances Company Türkiye.
Nous sommes un producteur leader de produits encastrables haut de gamme et espérons trouver des possibilités de coopération mutuelle.
Cordialement,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### Portuguese (Brasil + Portugal) — 264 chars
```
Bom dia,
Sou {{sender_name}} da Silverline Built-In Appliances Company Türkiye.
Somos um produtor líder de produtos de embutir de alta gama e esperamos encontrar possibilidades de cooperação mútua.
Com os melhores cumprimentos,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### German (Deutschland, Österreich, Schweiz) — 286 chars
```
Guten Morgen,
mein Name ist {{sender_name}} von Silverline Built-In Appliances Company Türkiye.
Wir sind ein führender Hersteller hochwertiger Einbauprodukte und hoffen, gegenseitige Kooperationsmöglichkeiten zu finden.
Mit freundlichen Grüßen,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### Italian — 258 chars
```
Buongiorno,
sono {{sender_name}} di Silverline Built-In Appliances Company Türkiye.
Siamo un produttore leader di prodotti da incasso di alta gamma e speriamo di trovare possibilità di cooperazione reciproca.
Cordiali saluti,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### Arabic (MENA — Arabic) — 224 chars
```
صباح الخير،
أنا إمير بلال محمد من شركة سيلفرلاين للأجهزة المدمجة تركيا.
نحن منتج رائد للمنتجات المدمجة عالية الجودة ونأمل في إيجاد فرص تعاون متبادل.
مع أطيب التحيات،
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### Russian — 280 chars
```
Доброе утро,
меня зовут Эмир Билаль Мухаммед, компания Silverline Built-In Appliances Company Türkiye.
Мы являемся ведущим производителем встраиваемой техники высокого класса и надеемся найти возможности для взаимного сотрудничества.
С уважением,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### Dutch — 268 chars
```
Goedemorgen,
mijn naam is {{sender_name}} van Silverline Built-In Appliances Company Türkiye.
Wij zijn een toonaangevende producent van hoogwaardige inbouwproducten en hopen mogelijkheden voor wederzijdse samenwerking te vinden.
Met vriendelijke groet,
{{sender_name}}
{{sender_phone}}
{{sender_email}}
```

### Polish — 277 chars
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

## Country → Language Quick Map (mirrors email + WhatsApp)

- Türkiye → TR
- LATAM (Mexico, Colombia, Chile, Argentina, Peru, Ecuador, etc.) → ES
- España → ES
- France, Belgium (Wallonia), Luxembourg, West/Central Africa (FR-speaking) → FR
- Brasil → PT (BR)
- Portugal → PT (PT)
- Deutschland, Österreich, Schweiz → DE
- Italia → IT
- MENA (UAE excluded as SLV, Saudi, Egypt, etc.) → AR (respect the client's market preferences)
- Россия, Беларусь, Україна, Қазақстан → RU
- Nederland, België (Flanders) → NL
- Polska → PL
- UK, Ireland, Nordics, English-speaking Africa, Southeast Asia (SG, MY, PH), India, Australia → EN
- Default (any other) → EN

---

## LinkedIn Reach Workflow (per prospect)

1. **Confirm market preferences** — skip any country the client marked no-outreach. (Same rule as email/WhatsApp.)
2. **Identify target company** — pick from `Sheet1` (existing customer) OR discover new (industry/country search).
3. **Research company**:
   - Web search the official site, capture: location, phone, primary email
   - Note the email domain (e.g. `@companyname.com`)
   - Search `"@companyname.com"` on the web (Bing/Google) to harvest additional employee emails
   - Cross-reference with Hunter.io / Apollo.io / LinkedIn Sales Navigator if available
4. **Update Sheet1 (if existing customer)** — add harvested emails to Column F, phone to Column G, note country context
5. **Update Sheet1 (if new company)** — append a new row with harvested contact info
6. **Search LinkedIn for the company**:
   - `linkedin.com/company/<slug>/people/`
   - Filter by **Title** keyword: `Sales`, `Owner`, `Founder`, `CEO`, `Managing Director`, `Purchasing`, `Procurement`, `Import`, `Export`, `Business Development`
   - Geographic filter: target country / region
7. **Open the prospect's profile** → click **Connect** → **Add a note** → paste the localized template
8. **Log in "LinkedIn Reach" tab** of the customer sheet — 15 columns:
   - A: Date Sent | B: First Name | C: Last Name | D: Company | E: Country | F: Title
   - G: LinkedIn URL | H: Company Website | I: Primary Email | J: Other Emails
   - K: Phone | L: Connection Status (Pending → Connected / Replied / Not Accepted)
   - M: Notes (research summary, why this person, language used)
   - N: Follow-up Date (7 days after Date Sent if no response)
   - O: Sheet1 Row Ref (row number in Sheet1 if existing customer, else blank)
9. **Mark Sheet1 column K** = TRUE if prospect is an existing customer AND the LinkedIn reach was made for a known contact already in the email pipeline
10. **Daily limit** — hard caps per LINKEDIN_WORKFLOW.md safety rules (2026-07-10): 15-20/day, 80/week, 3-5 min random gaps, business hours only, withdraw pending invites older than 3 weeks.

---

## HARD RULES

1. **NEVER send two connection requests to the same person.** One reach, move on. If no reply, log in Notes and skip.
2. **ALWAYS log every send** in `LinkedIn Reach` tab BEFORE clicking Send (so an interrupted run leaves a recoverable record).
3. **NEVER send a connection request without a note** — empty notes get flagged as spam by LinkedIn.
4. **NEVER include URLs or attachments** in the connection note (LinkedIn strips them).
5. **ALWAYS check the target profile's recent activity** before sending — if they posted about a topic that Silverline's products could support, weave that into the language variant. Generic cold notes are not our style.
6. **Operator alert on any reply** — when a prospect accepts the request AND replies, immediately notify the operator with company, name, language, and reply summary.
7. **Skip no-outreach markets** without exception (per the client's market preferences).

---

## Daily Cadence

- Morning research (before 09:00 recipient local time): harvest 10-15 companies
- Outreach window: 09:00-12:00 and 13:00-15:00 **recipient local time** (mirror email rule)
- End of day: include totals in the daily outreach report (sent count, accepted count, replied count)
- Follow-up check: any pending >7 days → mark "Not Accepted" in column L
