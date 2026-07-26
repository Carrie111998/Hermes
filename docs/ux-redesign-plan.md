# interfaze-agent — UX collapse: 15 destinations → 4

**Applies to:** `server/webui`
**Status:** frontend complete — Phases 0–5 implemented and verified. Only the
backend scheduler track remains.
**Companion document:** [`ux-redesign-proposal.html`](./ux-redesign-proposal.html) — open in a browser
**Related:** [`research-page-UI-guidelines.md`](./research-page-UI-guidelines.md)

---

## 0. Implementation status — 26 July 2026

Phases 0–5 are implemented in `server/webui`. The work preserved the vanilla-ES-module
stack and the existing `api.js` → `adapters.js` → `real-state.js` boundary. The customer
navigation is now the four destinations the plan called for.

### Completed

- **Phase 0 trust fixes**
  - Added the shared customer-facing `LABELS` table and the missing status colours.
  - Added a true *Not researched yet* score state instead of rendering `0`.
  - Added `qa_failed` to message filters and made it an error state.
  - Corrected required-setup counting and the completed-but-not-finished setup state.
  - Removed the contradictory Company Brain version tile.
  - Replaced raw integration, WhatsApp, provider and delivery errors with controlled
    customer-facing copy.
  - Corrected send/draft receipts and included `sent_manually` in sent totals.

- **Phase 1 Approvals**
  - Added `/app/approvals`, `reviewCard()`, responsive review CSS and a live nav count.
  - The queue is email-only, excludes do-not-contact records, includes `qa_failed` and
    already-approved emails that still need delivery, and hides regenerated originals
    throughout customer message views.
  - Added keyboard review (`A`, `E`, `S`, arrows, `Esc`), inline editing, field
    validation, *Save and approve*, rewrite, skip, do-not-contact, draft/send outcomes,
    plain-language refusal states and scoped bounce-pause handling.
  - The action bar remains reachable while the email scrolls; mobile navigation is
    inert while closed and restores focus correctly.
  - The preview now states that the required opt-out footer is appended at delivery,
    rather than claiming the stored body is byte-for-byte final.
  - Missing research/source context renders as missing, never as invented evidence.

- **Phase 2 Today**
  - Added `/app/today` as the customer home, redirected the old `/app/dashboard`
    bookmark, moved direct home links, and removed the superseded dashboard page.
  - Replaced the metric grid, inline chat, percentages and run-log links with an honest
    current-picture briefing, review prompt, latest recorded activity, one summary line,
    a compact target-market map, and loading, empty and mailbox states.
  - Added a bounded manual workflow: create/start scan → fetch persisted scan results →
    research up to six buyers → discover contacts → create campaign → generate drafts.
    It reports only sentences from `runSentence()`, never percentages, run ids or logs,
    and finishes in Approvals without sending anything.
  - The action reads `/health` and stays unavailable with plain copy when this server
    cannot run agent work, instead of exposing a control that is guaranteed to fail.
  - Added the global Cmd/Ctrl-K **Ask a question** dialog with the explicit contract:
    *“I can answer questions about your workspace. I can't send anything.”*

- **Phase 3 Buyers**
  - Added `/app/buyers` as one ruled company ledger with the six-stage
    **Found → Researched → Contacts → Written → Sent → Replied** rail and one
    contextual next action.
  - Nested people, company research, source evidence, email state and LinkedIn steps
    inside each expandable company instead of making the operator move between data
    tables.
  - Preserved the old capabilities in the new job-shaped surface: research, find or
    add a person, verify email, write an email for Approvals, do-not-contact at company
    or person level, set aside, recalculate fit, inspect evidence and export separate
    company/people CSVs.
  - Reused the world map as a collapsed, keyboard-operable market filter and kept
    unlinked imported people visible in a recovery section.
  - Extracted the manual buyer modal from the legacy Leads page and translated evidence,
    unknown values, fit states and errors at the shared UI boundary.
  - Kept legacy customer pages and routes alive for the Phase 5 redirect cutover; no
    bookmark is removed while the old navigation is still present.

- **Phase 4 Setup**
  - Added `/app/setup` as one progressive, ruled surface for company identity,
    positioning, products, trusted sales material and target markets.
  - Setup follows the backend's five required keys exactly. Current contacts, mailbox
    and Company Brain review are useful additions and never affect the completion
    headline or block the ready state.
  - The first missing required item opens automatically. Completing the fifth required
    item calls `onboarding.complete`; an already-complete workspace becomes a calm
    settings ledger with no step counter, percentage or nag.
  - Merged the customer-facing parts of Company Brain, Integrations, Email Templates and
    Settings into Setup: missing knowledge, editable draft review/approval, email
    preferences, language templates, mailbox connect/check/disconnect, WhatsApp Business,
    manual LinkedIn guidance, never-contact summary and account context.
  - Target markets now update the company profile, selected-country replacement list and
    onboarding market preferences together, so Today, Buyers and Setup cannot disagree.
  - Build, rebuild and document-processing work stays inline in plain sentences; it never
    links to a run id, percentage, log or Agent Runs.
  - Redirected `/app/onboarding` to `/app/setup`, moved Today's market/mailbox links to
    deep-linked Setup sections, added Setup to the transitional nav, and added the
    Buyers **Never contact** filter used by Setup.
  - Kept the other legacy page modules and routes alive for the Phase 5 cutover. They are
    not deleted while old navigation and bookmarks can still reach them.
  - Made mock onboarding completion obey the same five-required-step boundary and added
    modal focus containment/restoration for Setup's product, file and credential dialogs.

- **Phase 5 cutover**
  - The customer navigation is now flat and exactly four: **Today · Approvals · Buyers ·
    Setup**. Group headings are gone — at four items a heading is noise.
  - Operator machinery moved behind the admin guard: `/admin/research`,
    `/admin/research/new`, `/admin/research/:campaignId(/edit)`, `/admin/data-sources`
    and `/admin/agent-runs(/:runId)`. Their internal links were rewritten to match.
  - All 20 superseded customer routes now redirect instead of 404ing, and they carry
    context the destination can use: a lead id becomes an expanded company
    (`?buyer=`), a contact id a highlighted person (`?person=`), a message id opens
    that email in the queue (`?message=`), and Lead Map / Custom Outreach arrive with
    the map or add-buyer affordance already open.
  - Redirects resolve *after* the auth check, so a signed-out visitor holding an old
    bookmark lands on `/login` rather than bouncing through a redirect first.
  - Deleted ten superseded page modules (leads, contacts, custom-outreach,
    email-templates, integrations, settings, company-brain, onboarding, outreach and
    the old agent-runs list) — about 2,400 lines. `lead-map.js` lost its page mount and
    survives only as the shared `renderMiniMap()` component.
  - Fixed every internal link left pointing at a removed route, including the avatar
    menu, the Approvals mailbox prompts, and the Analytics map link. Removed the
    Analytics "Watch" toast that navigated into the run-log viewer.

### Verified

- Playwright CLI against real Chrome and the seeded local API:
  - all six seeded emails completed using only the keyboard;
  - forced `qa_failed` explains the unresolved placeholder in plain language;
  - desktop and 390 px mobile layouts pass;
  - no page, runtime or console errors.
- Phase 2 Playwright acceptance also passes for:
  - the seeded Today briefing, the legacy-dashboard redirect and global Ask;
  - a truly empty workspace with one clear next step and no zero-filled grid;
  - the complete enabled manual pipeline with sentence-only progress and no run id,
    percentage or log copy.
- Phase 3 Playwright CLI acceptance against Chrome passes at 1440 px and 390 px:
  - the complete pipeline, next action, search and state filters render;
  - a company expands into research, evidence, people, LinkedIn and outreach controls;
  - export and add-buyer modals open, and the map filters the ledger by country;
  - no raw status enum, page error or console error reaches the rendered workspace.
- Phase 4 Playwright CLI acceptance against real Chrome passes at 1440 px and 390 px:
  - the seeded workspace moves from one required item left to **Setup is ready** after
    saving its fifth required item;
  - all five required and three optional rows remain visible without an eight-step
    counter, and email preferences, product dialogs and mailbox controls remain usable;
  - `/app/onboarding` redirects to `/app/setup`, modal focus returns to its trigger,
    mobile has no horizontal overflow, and no raw enum, run/log language, page error or
    console error reaches Setup.
- Phase 5 is covered by `test_phase5_cutover_collapses_nav_and_keeps_every_legacy_bookmark_alive`
  in `tests/server/test_webui.py`, which asserts the nav is exactly the four customer
  destinations, that no removed route appears in it, that all three admin-only surfaces
  do, that all 20 legacy paths still resolve, that redirects carry their context, and
  that the deleted modules 404 while the map component survives without a page mount.
- All `24` focused server/WebUI checks pass (`21` in `test_webui.py`, `3` in
  `test_research_webui.py`).
- Full `tests/server` suite: `91` passed. The one failure seen under parallel load,
  `test_run_harness.py::test_stub_dispatch_and_events`, is a pre-existing race in the
  stub executor's event recording — it passes 3/3 in isolation and is unrelated to this
  work.
- All `35` WebUI JavaScript modules pass `node --check`; no surviving module links to a
  removed route.
- `git diff --check` passes.

### Still pending

Only the backend track. There is still no scheduler and no message-supersession
contract, so daily-rhythm copy remains disabled — Today ships the honest retrospective
briefing with a manual "Find buyers and write to them" action — and regenerated
originals are suppressed by the frontend until the backend can persist that
relationship. See the parallel track in §5.

---

## 1. Context

`server/webui` is a ~10,000-line vanilla-ES-module SPA fronting a 216-route FastAPI
backend. It works. The problem is not quality — it is that **the navigation is a
picture of the database, not a picture of the job.**

The user is a non-technical export manager at a Turkish appliance manufacturer.
Their job has three verbs: *find buyers, write to them, get replies.* The sidebar
gives them fifteen nouns across five groups and no indication that these are seven
stages of one pipeline. To get a single reply the user must know to go: Lead Map →
Research → Leads → research each lead → Contacts → Campaigns → Message queue →
approve → send. Nothing in the product says so. It makes the human the orchestrator
of machinery they were never shown.

The product's own spec already says this is wrong.
[`../company-packs/silverline/business-rules.md:17-21`](../company-packs/silverline/business-rules.md):

> Daily operation runs without per-day approval, but the operator gets a daily plan
> notification each morning and a results report each evening. […] Supervisors get
> clean business reports (what was done, results, next steps) — **never technical
> logs, error dumps, or workflow mechanics.**

The shipped UI does the opposite: a customer-facing **Agent Runs** log viewer, a raw
`Hermes command unavailable` string on Integrations, and status enums printed into
the interface. The intended product is an assistant that works and reports back. The
built product is a control panel for a pipeline.

### 1.1 Confirmed defects (verified in code)

| # | Defect | Evidence |
|---|---|---|
| 1 | **`badge()` prints raw enums by default** — `String(status).replace(/_/g,' ')`. This is the mechanical root cause of system language everywhere | `../server/webui/js/ui.js:105` |
| 2 | `STATUS_KIND` has no entry for `qa_failed`, `pending_approval`, `opted_out`, `sent_manually`, `paused_bounce_rate`, `qualified` — all render as neutral grey, so a QA-failed message looks as calm as a normal one | `../server/webui/js/ui.js:93-102` |
| 3 | The dashboard's largest element is a chat box that **cannot act** — read-only by design | `../server/chat_bridge.py:152-160` |
| 4 | "Onboarding step 5 of 8" — only **5 of 8 steps are required**; finished users are told they are 62% done | `../server/routes/onboarding.py:22-24` |
| 5 | Leads table renders `scoreBadge` = **0** on every row; band maths makes 0 a "low" score rather than "not scored yet" | `../server/webui/js/ui.js:110-114`, `../server/webui/js/pages/leads.js:122` |
| 6 | The approval queue's filter chips **omit `qa_failed`** — messages that failed preflight are invisible under every filter but "All" | `../server/webui/js/pages/outreach.js:119` |
| 7 | The review modal offers **six equally-weighted buttons** (Save / Regenerate / Approve / Create draft / Send now / Mark replied) with no primary path; "Send now" toasts *"Message marked sent"* | `../server/webui/js/pages/_page-utils.js:143-159` |
| 8 | Lead detail shows **two competing scoring systems on one card** — `fit_score`/`evidence_confidence` beside the legacy `score.value`/`band`/`factors` | `../server/webui/js/pages/leads.js:210-240` |
| 9 | Lead detail's action row **is the pipeline**: Research → Find contacts → Generate email, clicked manually, per company, 25 times | `../server/webui/js/pages/leads.js:311` |
| 10 | Starting work toasts *"Watch"* → navigates the customer into the **run log viewer** | `../server/webui/js/pages/leads.js:185` |
| 11 | Company Brain shows `approved` and `v3 / not built` simultaneously | `../server/webui/js/pages/company-brain.js` |
| 12 | Integrations shows `Hermes command unavailable` to a customer | `../server/webui/js/pages/integrations.js` |
| 13 | **Two parallel discovery systems** both in nav, relationship unexplained: Research is empty while Leads holds 25 | legacy `lead_scan` vs. `../server/lead_research/` |
| 14 | The daily job — approving 6 emails — is **three clicks deep**: Campaigns → Message queue tab → row → modal | `../server/webui/js/pages/outreach.js:39-47` |

Checked and **not** a bug: `waitForRun` polls for `completed`, and
`../server/webui/js/adapters.js:186` normalizes `succeeded → completed`.

### 1.2 Decisions taken

1. **Radical IA collapse, same stack.** Vanilla ES modules, no React, no build step.
   [`research-page-UI-guidelines.md:15`](./research-page-UI-guidelines.md) forbids
   drifting to the unused React app in `web/src`.
2. **Daily rhythm.** The agent works; the human judges. The backend has **no scheduler** —
   named dependency; the UI must degrade to a manual "Run today's work" button until it ships.
3. **English only.**
4. **Deliverable: visual proposal + this plan.**

### 1.3 Intended outcome

Four destinations — **Today, Approvals, Buyers, Setup** — plus a global Ask. A user who
has never read a manual logs in, understands what happened, clears the queue in under two
minutes, and never sees a status enum, a run id, a progress percentage, or a score of zero.

---

## 2. Target information architecture

```
Today       /app/today       what happened, what needs you, what's next
Approvals   /app/approvals   the daily job — one email at a time      [6]
Buyers      /app/buyers      companies, the people in them, where each stands
Setup       /app/setup       everything configured once
─────────────────────────────────────────────────────────────────────
Ask         Cmd/Ctrl-K       a question-answerer, clearly labelled as such
```

Nav drops from five groups to a flat four — at four items, grouping is noise.

### 2.1 Fate of every current destination

| Today | Fate | Detail |
|---|---|---|
| Dashboard | → **Today** | rewritten as a briefing, not a metrics grid |
| Lead Map | → **Buyers** (filter) + **Setup** (target markets) | `renderMiniMap()` already reused; keep it on Today |
| Research | → **admin** | customer never configures scoring weights, enrichment or model profiles |
| Leads | → **Buyers** | becomes the main list |
| Contacts | → **Buyers** | contacts become rows *inside* a company, not a parallel list |
| Campaigns | → **Approvals** | a campaign becomes a *filter* over the queue, not a destination |
| Custom Outreach | → **Buyers** | becomes `+ Add a buyer`, feeding the normal pipeline |
| Email Templates | → **Setup** | one "how our emails sound" section |
| Company Brain | → **Setup** | "What we know about you"; versioning buried, *Missing data* promoted |
| Analytics | → **Today** (one sentence) + kept route, off nav | `/app/analytics` stays reachable via "See the numbers" |
| Agent Runs | → **admin only** | it is a log viewer; violates business-rules.md:20-21 |
| Integrations | → **Setup** | |
| Settings | → **Setup** | |
| Admin ×3 | unchanged | admin nav keeps Agent Runs, Research config, Data sources |

Old routes **redirect** rather than 404 — bookmarks and the `?message=` deep link from
lead detail must keep working.

---

## 3. Screen designs

### 3.1 Today — `/app/today`

Answers *"what happened, what needs me, what's next"* in one glance. Replaces a metrics
dashboard with a short note from a competent assistant.

```
┌────────────────────────────────────────────────────────────────────┐
│  Good morning.                                                     │
│                                                                    │
│  Overnight I looked through Germany and the United Arab            │
│  Emirates, found 12 companies worth approaching, and wrote         │
│  6 emails.                                                         │
│                                                                    │
│      ┌──────────────────────────────────────────────┐              │
│      │  6 emails are waiting for you    [Review →]  │              │
│      └──────────────────────────────────────────────┘              │
│                                                                    │
│  Since yesterday                                                   │
│  ·  NordHaus Geräte replied — they asked about a sample order      │
│  ·  2 more replies are in your mailbox                             │
│  ·  Al Fardan Home Retail opened your email twice                  │
│                                                                    │
│  Next                                                              │
│  Tonight I'll look through Saudi Arabia. Nothing needed from you.  │
│                                                                    │
│  ─────────────────────────────────────────────────────────────     │
│  25 buyers · 2 replies from 4 emails      See the numbers →        │
└────────────────────────────────────────────────────────────────────┘
```

**Degraded state (no scheduler — ships first):** the briefing becomes retrospective
("Two days ago I found 12 companies…") and the primary control is a single button:

```
      ┌──────────────────────────────────────────────┐
      │   ▶  Find buyers and write to them           │
      │      Germany · United Arab Emirates          │
      └──────────────────────────────────────────────┘
```

which chains `leadScans.create → .start → leads.research → contacts.discover →
campaigns.generateMessages`, reporting progress as sentences from `runSentence()`,
never as a percentage or a log line.

- **Deletions:** six stat cards, both weekly bar charts, the funnel, the run table, the
  live-run strip with its progress %, the "Next best action" list (there is one action).
- **Chat:** demoted out of the page into the global Ask (Cmd-K), relabelled from
  "Hermes Agent" to **"Ask a question"**, with the honest subtitle *"I can answer
  questions about your workspace. I can't send anything."*
- **Empty (new workspace):** no metrics, no zeros. One line — *"Let's find your first
  buyers. Tell me which countries you sell to."* → Setup.
- **Error:** if the mailbox is disconnected, one amber line at top with a Setup link.

### 3.2 Approvals — `/app/approvals`

The reason the user opens the app. Today it is a tab inside a page called Campaigns,
behind a table, behind a modal. It becomes a focused, keyboard-first review queue.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Emails waiting for you                          2 of 6   ● ● ○ ○ ○ ○ │
├────────────────────────────────────┬─────────────────────────────────┤
│  To    Layla Ibrahim               │  Dubai Hospitality Supplies     │
│        Head of Imports             │  Abu Dhabi, UAE                 │
│  CC    export@silverline.com.tr    │  Hotel equipment supplier       │
│                                    │                                 │
│  Silverline Premium Built-In       │  Why this company               │
│  Kitchen Appliances                │  Supplies 4–5★ hotel projects   │
│  ──────────────────────────────    │  in the Gulf. Two hotel fit-out │
│                                    │  contracts announced this year. │
│  Dear Ms Ibrahim,                  │                                 │
│                                    │  Where this came from           │
│  I saw that Dubai Hospitality      │  · Company registry (verified)  │
│  Supplies is fitting out the new   │  · Trade data (verified)        │
│  Marina district hotels…           │  · Project announcement, Mar    │
│                                    │    2026 (estimated)             │
│  [full email, exactly as she       │                                 │
│   will receive it]                 │  Contact                        │
│                                    │  ✓ Email verified               │
│                                    │  No phone on file               │
├────────────────────────────────────┴─────────────────────────────────┤
│  [✓ Approve — saves a draft]   [Edit]   [Skip]        [Never contact]│
│   A                              E       S                            │
└──────────────────────────────────────────────────────────────────────┘
```

Mechanics handled honestly:

- **Approve ≠ send.** The button states the outcome: *"Approve — saves a draft in your
  mailbox"* or *"Approve and send"* depending on `send_mode`. Never a bare "Approve".
- **Editing nulls the approval** (`../server/outreach_service.py:112,140`).
  So Edit is inline, and its save button reads **"Save and approve"** — one action, no trap.
- **`qa_failed` messages appear in the queue** (defect #6) as a distinct card:
  *"I wrote this one but I don't trust it — the subject line doesn't match your standard
  subject. Want me to rewrite it?"* with `[Rewrite]` / `[Edit myself]` / `[Skip]`.
  Every QA code gets a sentence (table in §4).
- **Send refused after approval** (send windows, daily cap, suppression — the gate
  fails closed with a 409): *"Approved. It wasn't sent because it is outside the
  recipient's working hours. Return during their sending window to try again."* Not an
  error toast and not a promise that an absent scheduler will retry it.
- **Bounce circuit breaker** (`paused_bounce_rate`): a campaign-scoped blocking banner —
  *"Delivery is paused for this campaign because too many recent addresses bounced."*
  The user can still rewrite, edit, approve or skip; delivery remains blocked.
- **Keyboard:** `A` approve, `E` edit, `S` skip, `←/→` move, `Esc` leave. Six in ninety seconds.
- **Empty:** *"Nothing waiting. There are no emails waiting for review."* — no promise
  of overnight work until the scheduler exists.

### 3.3 Buyers — `/app/buyers`

One list of companies, each with the people inside it and a position in the pipeline.
Replaces five destinations.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Buyers                                        [+ Add a buyer] [Export]│
│                                                                        │
│  Found 9   Researched 4   Contacts 14   Written 6   Sent 4   Replied 2│
│  ───────────────────────────────────────────────────────────────────  │
│  ▸ 9 buyers still need research                        [Research them] │
│                                                                        │
│  [Search]   All · Germany · UAE      Needs you · Waiting · Replied     │
├───────────────────────────────────────────────────────────────────────┤
│  Al Fardan Home Retail            Dubai, UAE                           │
│  Hotel equipment supplier         Replied · 2 days ago                 │
│    Layla Ibrahim · Head of Imports · email verified                    │
│                                                                        │
│  NordHaus Geräte GmbH             Hamburg, Germany       Strong fit    │
│  Appliance distributor            Replied — asked about a sample order │
│    Anna Müller · Procurement Director · email verified                 │
│    Jan Becker · Category Manager                                       │
│                                                                        │
│  Khaleej Kitchen Imports          Abu Dhabi, UAE                       │
│  Construction project supplier    Not researched yet                   │
└───────────────────────────────────────────────────────────────────────┘
```

- **Contacts live inside the company.** No parallel Contacts destination. `/app/contacts/:id`
  redirects to the company with the person expanded.
- **The pipeline rail is the headline.** It shows where every buyer stands and names the
  single next action. This is what replaces the user's missing mental model.
- **Score:** shown only when a research run has actually scored the lead. `scoreBadge`
  gains an unscored state — *"Not researched yet"*, never `0`. Bands become words:
  **Strong fit / Possible fit / Weak fit**.
- **One scoring system on screen.** Where evidence-based `fit_score`/`evidence_confidence`
  exists it wins and the legacy factor bars are dropped; otherwise the legacy score shows
  alone. Never both (defect #8). Per
  [`research-page-UI-guidelines.md:58`](./research-page-UI-guidelines.md), fit and
  confidence stay distinct — rendered as *"Strong fit · based on verified sources"* vs
  *"Strong fit · partly estimated"*.
- **Evidence is one click**, inline in the company panel — reuse `openLeadEvidence()`.
  Honour guideline §3.5: unknown renders as "Not known", never as 0.
- **The map** becomes a filter control at the top of Buyers (collapsed by default) and a
  step in Setup. Not a destination.
- **`+ Add a buyer`** replaces the 5-step Custom Outreach wizard: a modal
  (`openLeadCreateModal` already exists) that drops the company into the normal pipeline.
- **Research campaign machinery** (sources, scoring weights that must total 100, enrichment
  and model profiles) moves to admin. The customer equivalent is one control in Setup:
  which countries, which products, who to avoid.

### 3.4 Setup — `/app/setup`

One surface. A checklist when incomplete, a settings page when complete. **No separate
wizard route and no permanent nag banner.**

```
┌───────────────────────────────────────────────────────────────────────┐
│  Setup                                                                 │
│  Two things left before the agent is at full strength.                 │
│                                                                        │
│  ✓  Your company            Silverline Ev Aletleri A.Ş., Istanbul      │
│  ✓  What you sell           7 products                                 │
│  ✓  How you win             Positioning and sales arguments            │
│  ✓  Where you sell          Germany, UAE, Saudi Arabia      [Map]      │
│  ✓  Your mailbox            sales@silverline.com.tr · drafts first     │
│  ○  Your price list         Would make quotes far sharper    [Upload]  │
│  ○  Your current contacts   So the agent skips people you know [Add]   │
│                                                                        │
│  ─────────────────────────────────────────────────────────────────    │
│  What we know about you                                   [Review →]   │
│  Approved 15 days ago. The agent uses this for every email it writes.  │
│                                                                        │
│  Three things would make it sharper:                                   │
│  ·  No price list for the HoReCa series                                │
│  ·  Your current contact list isn't imported                           │
│  ·  No won/lost history — the agent is guessing why deals close        │
│                                                                        │
│  ─────────────────────────────────────────────────────────────────    │
│  How we send            Drafts first · max 50/day · English, German,   │
│                         Arabic · CC export team              [Change]  │
│  WhatsApp               Not connected                        [Connect] │
│  LinkedIn               Notes only — you send them yourself            │
└───────────────────────────────────────────────────────────────────────┘
```

- **Onboarding and Setup are the same surface at different times.** `/app/onboarding`
  redirects here. The 8-step `stepper()` is deleted.
- **Required vs optional is honest** (defect #4): the five `REQUIRED_STEPS` gate
  completeness; the other three are shown as optional improvements that never block and
  never nag. Copy is *"Two things left"*, not *"step 5 of 8"*.
- **Company Brain's *Missing data* panel is promoted** to the centre of the section — it
  is the best pattern in the app because it tells the user exactly what makes the agent
  smarter. Version numbers, snapshot history and `v3 / not built` are removed from the
  customer view (history stays in admin).
- **Every technical string is translated** (defect #12). `Hermes command unavailable` →
  *"The agent is offline right now. Your data is safe and nothing was lost — we're on it."*
- **Partially-shipped capabilities** read as offers, not breakage: WhatsApp *"Not connected"*
  with a Connect action; LinkedIn *"Notes only — you send them yourself"* stated as the
  designed behaviour it is.

---

## 4. Shared components and the copy table

Extend `../server/webui/js/ui.js` rather than adding a parallel layer.

| Component | File | Signature | Used by |
|---|---|---|---|
| `LABELS` + `badge()` fix | `js/ui.js` | `badge(status, textOverride)` consults `LABELS[status]` before falling back to the enum | everywhere |
| `scoreBadge()` unscored state | `js/ui.js` | `scoreBadge(score)` → `null`-safe; renders "Not researched yet" when unscored | Buyers |
| `runSentence(run)` | `js/ui.js` | `(run) => string` — plain-English status per run type, running and finished | Today |
| `pipelineRail(counts, nextAction)` | `js/ui.js` | returns the Found→Replied rail with one CTA | Buyers, Today |
| `reviewCard(message, handlers)` | `js/pages/_components.js` *(new)* | the full-width approval card incl. context panel and QA states | Approvals |
| `companyRow(lead, contacts)` | `js/pages/_components.js` *(new)* | company + nested people + state | Buyers |

**The copy table** lives as a real constant, `LABELS`, in `js/ui.js` beside `STATUS_KIND`.

| System value | Shown to the user |
|---|---|
| `pending_approval` / `draft_generated` | Waiting for you |
| `qa_failed` | Needs a second look |
| `approved` | Approved |
| `draft` / `draft_created` | Saved as a draft |
| `sent` | Sent |
| `sent_manually` | You sent this |
| `replied` | They replied |
| `opted_out` | Asked not to be contacted |
| `paused_bounce_rate` | Sending paused — too many bounces |
| lead `new` | Not looked at yet |
| lead `qualified` | Worth approaching |
| lead `researched` | Researched |
| lead `contacted` | Emailed |
| lead `interested` | Interested |
| lead `archived` | Set aside |
| lead `unqualified_after_source_removal` | No longer supported by its source |
| `do_not_contact` | Never contact |
| score band high / mid / low | Strong fit / Possible fit / Weak fit |
| score unscored | Not researched yet |
| run `lead_scan` | Looking for buyers in {countries}… → Found {n} companies worth approaching. |
| run `lead_research` | Reading up on {company}… → Researched {n} companies. |
| run `contact_discovery` | Looking for the right person at {company}… → Found {n} people to contact. |
| run `outreach_generation` | Writing to {company}… → Wrote {n} emails. They're waiting for you. |
| run `company_brain_build` | Reading your company documents… → Updated what I know about you. |
| run `document_processing` | Reading {filename}… → Read {filename}. |
| run `product_extraction` | Finding your products… → Found {n} products. |
| run `linkedin_note_generation` | Writing LinkedIn notes… → Wrote {n} notes for you to send. |
| QA `subject_mismatch` | The subject line doesn't match your standard subject. |
| QA `operator_language_contamination` | Some Turkish slipped into a non-Turkish email. |
| QA `unresolved_placeholder` / `unknown_placeholder` | A blank didn't get filled in. |
| QA `invalid_or_missing_recipient` | The email address doesn't look right. |
| QA `invalid_cc_recipient` | One of the CC addresses doesn't look right. |
| QA `double_dash` | Contains a double dash, which your style rules don't allow. |
| QA `internal_marker` | An internal note was left in the text. |
| QA `unapproved_link` | Contains a link that isn't on your approved list. |

---

## 5. Build plan

Phases are numbered because they are genuinely sequential — each depends on the last.
Every phase ships independently and ends in something the user can see.

### Phase 0 — Tell the truth (½ day, frontend only) — COMPLETE

Fixes the defects that cost trust, with no structural change. Ships today.

| File | Action | Detail |
|---|---|---|
| `server/webui/js/ui.js` | edit | Add `LABELS`; `badge()` consults it before the enum fallback. Add missing `STATUS_KIND` entries (`qa_failed`→error, `pending_approval`→warning, `opted_out`→neutral, `sent_manually`→success, `paused_bounce_rate`→error, `qualified`→info). `scoreBadge()` returns an unscored state instead of `0`. |
| `server/webui/js/pages/dashboard.js` | edit | Banner counts required steps only: "Two things left", not "step 5 of 8". |
| `server/webui/js/pages/outreach.js` | edit | Add `qa_failed` to the filter chips (defect #6). |
| `server/webui/js/pages/company-brain.js` | edit | Remove the `v3 / not built` tile that contradicts `approved`. |
| `server/webui/js/pages/integrations.js` | edit | Translate `Hermes command unavailable` and sibling technical strings. |
| `server/webui/js/pages/_page-utils.js` | edit | Fix the "Send now" → *"Message marked sent"* toast to say what happened. |

**Verifiable:** no raw enum, no score of 0, no contradictory brain header anywhere in the app.

**Completed and verified:** 26 July 2026.

### Phase 1 — Approvals (2–3 days, frontend only) — COMPLETE

The highest-value screen. Works entirely on today's backend.

| File | Action | Detail |
|---|---|---|
| `server/webui/js/pages/approvals.js` | create | Full-width review queue, keyboard-first, QA-state cards, inline edit with "Save and approve", 409-refusal and circuit-breaker states. Reuses `emailPreview`, `messages.*` routes, `openLeadEvidence`. |
| `server/webui/js/pages/_components.js` | create | `reviewCard()`. |
| `server/webui/css/app.css` | edit | `.ifz-review-*` block. |
| `server/webui/js/main.js` | edit | Add `/app/approvals`; keep `/app/outreach` alive. |
| `server/webui/js/shell.js` | edit | Add Approvals to nav with the pending-count badge. |

**Verifiable:** approve six seeded messages from the keyboard without touching the mouse;
a `qa_failed` message explains itself in plain language.

**Completed and verified:** 26 July 2026. The implementation also added email-only queue
gating, regenerated-message suppression across customer views, honest opt-out-footer copy,
loading/empty/focus states, and desktop/mobile Playwright coverage.

### Phase 2 — Today (2 days, frontend only) — COMPLETE

| File | Action | Detail |
|---|---|---|
| `server/webui/js/pages/today.js` | created | Retrospective briefing, review prompt, latest activity, one bounded manual action, compact map, summary, loading/empty/mailbox states. |
| `server/webui/js/ui.js` | edited | Added `runSentence()` with plain running, complete, failed and cancelled copy. |
| `server/webui/js/pages/_page-utils.js` | edited | `waitForRun()` now accepts sentence updates and no longer sends customers to Agent Runs on timeout. |
| `server/webui/js/pages/dashboard.js` | deleted | Superseded; `/app/dashboard` redirects safely. |
| `server/webui/js/main.js`, `session.js` | edited | Registered Today, made it the customer home, retained the old bookmark redirect. |
| `server/webui/js/hermes-client.js` | kept | Existing authenticated read-only chat bridge reused by global Ask. |
| `server/webui/js/shell.js` | edited | Added Cmd/Ctrl-K Ask with focus containment and honest action limits. |
| `server/webui/css/app.css` | edited | Responsive Today, loading and Ask surfaces. |

Uses `dashboard.summary`, `activity.*`, `messages.list`, setup/product/integration data and
`/health`. The manual action chains `leadScans.create → leadScans.start →
leadScans.results → leads.research → contacts.discover → campaigns.create →
campaigns.generateMessages` via `waitForRun()`. It deliberately caps the email shortlist
at six. If `/health` reports that agent runs are unavailable, the button is disabled with
customer-safe copy.

**Verifiable:** a user who has never seen the app can say what the agent did and what it
needs, from the home screen alone.

**Completed and verified:** 26 July 2026. Seeded desktop/mobile, empty-workspace and
enabled-pipeline Playwright scenarios pass; Today and Ask expose no percentages, run ids,
logs or scheduling claims.

### Phase 3 — Buyers (3–4 days, frontend only) — COMPLETE

| File | Action | Detail |
|---|---|---|
| `server/webui/js/pages/buyers.js` | created | Pipeline rail, filters, collapsed market map, company ledger, lazy company detail and all buyer/person actions. |
| `server/webui/js/pages/_components.js` | edited | Added `companyRow()` and the shared `openBuyerCreateModal()`. |
| `server/webui/js/ui.js` | edited | Added `pipelineRail()`, qualitative fit states and the customer status boundary. |
| `server/webui/js/pages/lead-map.js` | edited | Kept `renderMiniMap()` compatible with Today and added optional keyboard/click filtering for Buyers. |
| `server/webui/js/pages/research-evidence.js` | edited | Evidence, confidence, sources, missing values and failures now use customer language. |
| `server/webui/js/adapters.js` | edited | Stored LinkedIn profiles remain profiles instead of being mistaken for agent runs. |
| `server/webui/js/main.js`, `shell.js`, `css/app.css` | edited | Registered Buyers, added its transitional nav entry and built the responsive editorial ledger. |
| Legacy customer pages | retained | Phase 5 owns redirects, nav collapse and safe deletion after every bookmark is verified. |

**Verifiable:** every capability from the old Leads/Contacts/Custom Outreach pages is
reachable — research, find contacts, generate email, do-not-contact, archive, export CSV,
add manually, inspect evidence, recalculate score.

**Completed and verified:** 26 July 2026. Every listed capability is visible within two
clicks from `/app/buyers` (a destructive confirmation may follow as the safety step).
Seeded Chrome passes at desktop and 390 px mobile with no runtime or console errors; the
market map filters the ledger, export preserves separate company/people files, and fit
score `0` or missing evidence no longer masquerades as a completed assessment.

### Phase 4 — Setup (2–3 days, frontend only) — COMPLETE

| File | Action | Detail |
|---|---|---|
| `server/webui/js/pages/setup.js` | created | One progressive surface; the exact five required items, three non-blocking additions, promoted *Missing information*, Company Brain review, sending preferences, templates, mailbox, WhatsApp, LinkedIn, never-contact and account controls. |
| `server/webui/js/main.js`, `shell.js`, `pages/today.js` | edited | Registered Setup, redirected `/app/onboarding`, added transitional navigation and deep-linked Today's market/mailbox actions. |
| `server/webui/js/pages/buyers.js` | edited | Added the query-backed **Never contact** filter used by Setup. |
| `server/webui/js/ui.js`, `mocks/handlers.js` | edited | Added Setup work sentences/status labels, safe bounded chip selection, modal focus containment/restoration and mock parity for the five-step completion contract. |
| `server/webui/css/app.css` | edited | Responsive editorial Setup ledger, inline editors, knowledge/sending bands and 390 px layouts. |
| `tests/server/test_webui.py` | edited | Added Setup route/language assertions, the exact four-required rejection → fifth-required completion boundary, and sales-preference/template round trips. |
| Legacy customer modules | retained | `/app/onboarding` redirects now; the remaining old routes stay intact until Phase 5 redirects every bookmark and removes the old nav safely. |

**Verifiable:** a brand-new tenant completes setup without ever seeing a step counter,
and the completion state matches `REQUIRED_STEPS`.

**Completed and verified:** 26 July 2026. Real-Chrome Playwright passes at 1440 px and
390 px, including completion of the seeded workspace's fifth required item, the legacy
onboarding redirect, modal focus restoration and zero horizontal overflow. The full
focused server/WebUI set passes `23/23`; all `44` WebUI modules parse; `git diff --check`
passes.

### Phase 5 — Cutover (1 day)

`main.js` route table: four new routes plus redirects from all twelve old customer paths
(including `?message=` deep links → `/app/approvals?message=`). `shell.js` `NAV_GROUPS`
becomes the flat four; Agent Runs, Research config and Data sources move to the admin group.

**Verifiable:** every old bookmark lands somewhere sensible; no dead route.

### Parallel track — backend, for the true daily rhythm

Frontend ships first and degrades gracefully; none of the above is blocked on this.

| Piece | Today | Smallest change | Size |
|---|---|---|---|
| Scheduler | none — every run is HTTP-triggered | APScheduler in `server/app.py` lifespan, per-tenant daily job calling the existing pipeline | M |
| Daily plan / report | no such concept | a `daily_digest` table + `GET /activity/digest?date=` assembled from `activity_log` | M |
| Time-filtered activity | `activity.*` has no `since` param | add `since` to `server/routes/operations.py` | S |
| Notification | none | email the digest to the connected mailbox | S |

**Hard truth to state plainly to the user:** until the scheduler ships, "the agent worked
overnight" is not true, and the UI must not imply it. Phase 2's degraded state is the
honest version and should ship as such.

---

## 6. Non-goals

- No React, no build step, no touching `web/src`.
- No i18n / Turkish UI.
- No change to the 216-route API contract; the frontend adapts, the backend doesn't move
  (except the additive scheduler track).
- No redesign of the admin app beyond receiving Agent Runs, Research config and Data sources.
- No new charting, no design-token overhaul — `tokens.css` stays as is.
- Not deleting the evidence-first research subsystem; it moves behind admin and surfaces
  to customers only as plain-language fit and evidence.
- No WhatsApp or LinkedIn feature work.

---

## 7. Verification

**Run it locally** — seeded local API:

```powershell
python -m server seed-demo
python -m server serve --host 127.0.0.1 --port 8000
```

Then, per phase:

1. **Phase 0** — grep the built UI for raw enums: no `draft_generated`, `qa_failed`,
   `pending_approval`, or `do_not_contact` may reach the DOM as visible text. No `0` in a
   score badge on `/app/leads`.
2. **Phase 1** — with 6 seeded pending messages, clear the queue using only `A`/`E`/`S`.
   Force a `qa_failed` (seed a message with a mismatched subject) and confirm it appears in
   the queue and explains itself. Approve a message outside the send window and confirm the
   409 renders as a scheduling sentence, not an error.
3. **Phase 2** — load `/app/today` on a seeded tenant and on an empty tenant; neither may
   show a zero-filled metric grid.
4. **Phase 3** — walk the capability checklist above; each must be reachable in ≤2 clicks
   from `/app/buyers`.
5. **Phase 4** — create a fresh tenant via `/admin/companies`, complete only the five
   required steps, and confirm Setup reports complete with no nag.
6. **Phase 5** — visit all twelve old customer routes; each must redirect, none 404.

**Existing tests:** `tests/server/test_webui.py` and `tests/server/test_research_webui.py`
assert served routes — update route assertions in Phase 5. Phase 1 was additionally
verified with temporary Playwright CLI acceptance tests against Chrome and the seeded
tenant. Phase 2 adds empty-workspace and network-controlled enabled-pipeline scenarios;
Phase 3 adds the Buyers route and customer-language boundary assertions plus
desktop/mobile Chrome acceptance. No Playwright dependency or generated test artifact is
committed to the repo.

**The one thing, if only one phase ships this month:** Phase 1. Approving six emails is the
whole daily job, and it is currently three clicks deep behind a table and a modal.
