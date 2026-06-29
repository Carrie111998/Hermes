## STAGE 4 — Schedule & Publish to Meta Ads

### Steps

**1a. Meta MCP connection check — FIRST question, always**

Before asking anything else in Stage 4, fire a single AskUserQuestion that checks whether
the Meta Ads MCP is actually connected to the user's workspace. This is the first thing the
user sees in Stage 4.

> "Quick check before we schedule — is your Meta MCP connected to Meta Ads?"
> - "Yes — Meta MCP is connected (Recommended)"
> - "Not connected — help me install it now"
> - "Skip the live scheduling — give me an exportable calendar instead"

**If "Yes — Meta MCP is connected":** proceed to Step 1b (the objective / budget / dates card).

**If "Not connected — help me install it now":** surface the install path in chat:

1. Call `search_mcp_registry` with `["meta ads", "facebook ads", "meta marketing"]` to find
   available Meta Ads connectors in the registry.
2. Call `suggest_connectors` with the matching IDs so an install card renders directly in
   chat — the user clicks once to authenticate.
3. Include a fallback link line so they have a manual path:
   > "If the install card doesn't show, open Settings → Connections in your Claude app
   > and search for 'Meta Ads'. The connector hooks into your Meta Business Suite via
   > the standard Marketing API auth flow. [Anthropic connector docs →
   > https://docs.claude.com/en/docs/agents-and-tools/mcp]"
4. After the user reports the install is done, loop back to Step 1a and re-ask the
   connection check.

**If "Skip the live scheduling — give me an exportable calendar instead":** drop the live
Meta integration entirely and export `[brand]-content-calendar.csv` (or `.xlsx`) with
columns: Date · Time · Format · Preset · Video filename · Image filename · Social post
caption · Goal · Notes. Save to `/mnt/user-data/outputs/`. Skip the rest of Stage 4 and
proceed directly to Stage 5. The user can hand the CSV to a media buyer or paste it into
Ads Manager themselves.

**1b. Confirm Meta Ads campaign details (only fires if Meta MCP is connected — single AskUserQuestion — all buttons)**

- Campaign objective: "Awareness" / "Traffic" / "Conversions" / "Mixed"
- Budget tier: "$500" / "$1,500 (Recommended)" / "$5,000" / "Custom"
- Date range: "Match the content plan dates (Recommended)" / "Next 30 days" / "Custom"

Ad Account ID is detected automatically from the Meta Ads MCP — only ask if multiple
accounts are returned, and present them as a button list.

**2. Content calendar review (button approval)**
Present the calendar, then AskUserQuestion: "Schedule looks good?" — buttons:
"Yes — schedule everything" / "Yes — but start with week 1 only" / "Adjust dates first".

**3. Create campaigns via Meta Ads MCP**
For each batch:
- Create Ad Campaign with the objective
- Create Ad Sets with targeting (audience details prompted via AskUserQuestion buttons —
  e.g. "Auto-target lookalike from product image" / "Use saved audience" / "Define new")
- Upload generated Higgsfield videos/images as creatives
- Schedule per the plan's dates

**4. Confirm scheduling**
Summary table by week. Final AskUserQuestion: "Scheduling done — continue to Stage 5?" buttons:
"Yes — render the cost report (Recommended)" / "Pause one of the campaigns" / "Generate more
content" / "Skip Stage 5 — close pipeline".

---

## STAGE 5 — Cost Comparison Report

### Goal
After everything is generated and scheduled, render a polished HTML report comparing the
**actual Higgsfield credit + USD spend** for this campaign against the **estimated cost of
producing the same volume traditionally** (creators, studios, DOPs, post-production, FOOH
crews, photographers, etc.). Output the savings ratio + time savings, save to outputs, and
present.

### Steps

**1. Pull live spend from Higgsfield**

Call `transactions(limit=200)` (or the highest available limit) to fetch the actual credit
spend during the current campaign. Filter to jobs created during the Stage 3 generation
window (use the campaign's date-range start through "now" as the filter). Sum:
- credits per preset (UGC, Product Review, Hyper Motion, TV Spot, Wild Card, Tutorial,
  Unboxing, Virtual Try On)
- credits for image generation (Marketing Studio Image / GPT Image 2 / Soul / Nano Banana)
- total credits

Convert credits → USD using the user's plan rate (or omit USD if the rate isn't surfaced).
Common Higgsfield rates: Creator plan ≈ $0.02/credit · Team/Pro plans ≈ $0.01–$0.005/credit.
If unsure, present credits-only and note the rate is plan-dependent.

**2. Apply the traditional production cost model (baked-in industry averages)**

Use the table below as the default cost model. These are 2026 industry-average midpoints in
USD; the report should show a low–mid–high range, not a single number. Cite the model as
"Industry-average estimates, 2026" and let the user override if they have their own rate card.

| Asset type | Low (USD) | Mid (USD) | High (USD) | Why the range |
|---|---:|---:|---:|---|
| UGC creator video (TikTok / Reels) | 250 | 750 | 1,500 | Creator fee + light production |
| Product Review video | 300 | 900 | 2,000 | Talent + setting + edit |
| Tutorial / Recipe video | 400 | 1,200 | 2,500 | Recipe shoot + post |
| Unboxing video | 300 | 800 | 1,500 | Box creation + shoot + edit |
| Hyper Motion CGI hero (single product, ≤15s) | 3,000 | 9,000 | 15,000 | CGI/VFX studio half-day to two-day |
| TV Spot 15s | 15,000 | 50,000 | 150,000 | Production + DOP + cast + post |
| Wild Card / FOOH stunt | 30,000 | 100,000 | 500,000+ | Real FOOH = full production crew |
| UGC Virtual Try On video | 200 | 600 | 1,000 | Quick try-on shoot |
| Pro Virtual Try On video | 1,000 | 3,000 | 5,000 | Studio quality |
| Social media post (1:1 lifestyle still) | 100 | 250 | 500 | Photographer half-day rate |
| Hero banner (16:9 cinematic) | 1,000 | 2,500 | 5,000 | Studio + retouching |
| Product photoshoot WITH people | 500 | 1,500 | 3,000 | Half-day with talent |
| Product photoshoot WITHOUT people | 200 | 700 | 1,500 | Studio product photographer |

**Time-savings benchmark** (also baked in):

| Channel | Higgsfield typical turnaround | Traditional turnaround |
|---|---|---|
| 80 mixed videos | 1–3 hours render time | 4–12 weeks production |
| 19 image asset pack | 5–15 minutes render time | 1–3 weeks photographer + retouch |
| Scheduling | Minutes via Meta MCP | Days of trafficking |

**3. Compute savings**

- `traditional_low = sum(asset_count × low_cost)` per asset type
- `traditional_mid = sum(asset_count × mid_cost)` per asset type
- `traditional_high = sum(asset_count × high_cost)` per asset type
- `higgsfield_usd = total_credits × user_plan_rate_usd` (with explicit rate disclosed)
- `savings_pct_mid = 1 − higgsfield_usd / traditional_mid` (capped at 99.99%)
- `time_savings = traditional_weeks − higgsfield_hours / (24×7)`

**4. Render the HTML report**

Save to `/mnt/user-data/outputs/[brand]-cost-comparison.html`. Required sections:

1. **Hero number card** — "Pure HiGG: 100% Natural delivered for **$X** instead of **$Y–$Z**.
   You saved **N%** and **W weeks** of production time."
2. **Volume summary table** — what we generated (e.g. 50 UGC + 24 PR + 2 HM + 2 TV + 2 WC +
   8 social + 3 hero + 4 with-people + 4 without-people)
3. **Higgsfield spend breakdown** — credits per preset / per asset type, USD equivalent at
   the user's plan rate (clearly labeled)
4. **Traditional cost breakdown** — same volumes priced at low / mid / high industry-average
   rates, with a per-asset-type subtotal
5. **Side-by-side comparison** — total Higgsfield USD vs traditional mid-USD vs traditional
   high-USD; bar chart or simple horizontal bars in HTML/CSS (no external chart libs needed)
6. **Time savings panel** — Higgsfield render time vs traditional weeks
7. **Methodology footer** — disclose that traditional costs are 2026 industry-average
   estimates (not a quote), Higgsfield USD is based on the user's plan rate at the time of
   the report, and that prices vary by region / agency tier

Visual style: same brand-blue header treatment as the video and static plans (consistency).
Title: `[Campaign name] — Cost Comparison Report`.

**5. Present the report (button confirm)**

Show the user the saved file via a `computer://` link. Final AskUserQuestion:
"Cost report ready. What next?"
- "Done — close the pipeline (Recommended)"
- "Email this report to my team"
- "Adjust the traditional-cost rate card and re-render"
- "Run the pipeline again for another product"

---

## General Guidelines

- **Button-driven rule (HARD):** Every clarifying question is an AskUserQuestion call with
  2–4 concrete option buttons. Free-form typing is NEVER required to navigate, confirm, or
  route between stages. Only product image upload (file attach) and product URL paste are
  acceptable non-button inputs — and both are still click-based, not commands. Always offer
  a smart default the user can accept with one click.
- **No-pause rule:** Bundle every clarifying question into a single AskUserQuestion call.
- **5-format split (HARD):** Every campaign distributes evenly across 5 UGC formats —
  UGC Entertainment, Street Interview, Unboxing, Product Review, ASMR. Allocate
  `floor(VIDEO_COUNT / 5)` per format; distribute any remainder starting at format 1.
  Cinematic presets (Hyper Motion, TV Spot, Wild Card) are OFF by default and only
  activated on explicit user request.
- **Producibility rule:** Every idea must be producible inside an active Marketing Studio
  preset within its 4–15s cap, OR explicitly labeled "Outside Marketing Studio."
- **Per-batch permission gate (Stage 3):** ALWAYS ask before generating each preset's batch.
  Never auto-run the full plan. Use button choices, not typed confirmations.
- **Hook vs caption clarity:** A "hook line" the user writes is VO/caption copy. The system
  `hook_id` is a structured visual template from the picklist — these are different.
- **Confirm between stages with buttons:** Each stage ends with an AskUserQuestion button
  confirmation before advancing.
- **Multi-variant brands:** distribute content evenly across SKU variants within each preset.
- **Visual identity consistency:** same color palette, tone, reference image throughout.
- **Failure handling:** log failed IDs and offer "Retry / Skip / Pause" buttons per stage.
