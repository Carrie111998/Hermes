# Plan: bringing every agent run type live, one by one

Sequenced implementation plan for the 11 run types in PRODUCT.md §7.24.
Order is dependency order — each step is independently shippable and tested
with the Silverline demo pack (`company-packs/silverline/`) before the next
starts. Each run type has three stages: **Wire** (make it work end-to-end),
**Verify** (acceptance test), **Better** (the one improvement worth doing
after it works — not before).

## Step 0 — Shared run harness (build once, everything uses it)

The only piece built ahead of need, because all 10 agent-backed run types sit
on it.

- `agent_runs` table: id, company_id, run_type, status
  (queued/running/succeeded/failed/cancelled), input payload (JSON), output
  ref, error, cost, timestamps.
- **Dispatcher**: run type → skill name (mapping in `skills/sales/README.md`)
  → Hermes runtime invocation with the tenant's company pack as context and
  the payload as the task. One code path for all types.
- Run events + logs streamed to `agent_runs` children (§7.24 logs/events
  routes read these).
- Cancel (kill the runtime session), retry (new run, same payload), and
  idempotency keys so a double-click never runs twice.
- **CLI test harness before any UI**: `python -m server.runs run
  --company silverline --type <run_type> --payload file.json` — every step
  below is verified through this first.

Acceptance: a dummy run type executes a trivial skill, streams events,
persists output, can be cancelled and retried.

---

## 1. document_processing

The root of the data tree — everything downstream eats its output.

- **Wire**: `POST /documents/:id/process` → run with payload {document_id,
  document_type}. Skill: `document-processing`. Output: typed records
  (products/contacts/sales-history) + rejects list, written to their tables
  with `source` pointers; processing-status endpoint reads run status.
- **Verify**: upload a Silverline catalogue PDF and a messy contacts CSV →
  product records match the catalogue; invalid rows appear in rejects with
  reasons; re-processing the same document replaces rather than duplicates.
- **Better**: table-heavy PDF extraction pass (price lists are the hard
  case) + a `needs_review` queue UI so humans clear ambiguities fast.

## 2. product_extraction

Same skill, catalog path — separate run type so the products UI can trigger
it directly.

- **Wire**: `POST /products/extract-from-documents` → same dispatcher, payload
  {document_ids}, output limited to product records. `generate-buyer-roles`
  and `generate-market-fit` (§7.7) are deferred to brain-build, which owns
  that reasoning — these routes just read brain output once step 3 lands.
- **Verify**: extraction from two overlapping catalogues dedupes by
  normalized product name.
- **Better**: image-aware extraction (product photos → asset records linked
  to products, feeding template media).

## 3. company_brain_build

First reasoning-heavy run; unblocks everything intelligent.

- **Wire**: `POST /company-brain/build` (and `/rebuild`) → skill
  `company-brain-build` over profile + products + processed records. Output:
  brain document persisted as a **snapshot row** (§7.8), status `draft`;
  `POST /company-brain/approve` flips the active pointer. Agents always read
  the last approved snapshot.
- **Verify**: build from the Silverline pack → all seven sections present;
  missing-data section correctly names what was never uploaded; approve →
  active; rebuild → diff against previous snapshot is reviewable.
- **Better**: incremental rebuild — new documents trigger a section-level
  update proposal instead of a full rebuild, so the brain stays fresh without
  re-review fatigue.

## 4. lead_scan

First run type that touches the outside world (read-only).

- **Wire**: `POST /lead-scans` + `/start` → skill `lead-discovery`, payload =
  scan config (§7.10: countries ≤5, products, industries, company types,
  max per country, depth, sources). Market gate checked server-side *before*
  the run starts: reject countries the client marked `no_research_markets`
  (client-selected in the UI), and enforce max-5, at creation not mid-run.
  Output: lead records tagged with scan_id; scan results endpoint aggregates.
- **Verify**: 2-country Silverline scan returns leads with valid ISO codes,
  no excluded-industry companies, dedup against seeded existing customers
  works, drop counts reported.
- **Better**: source expansion — exhibitor/directory sources as pluggable
  data_sources (§7.27) with per-source yield stats, so scans learn which
  sources pay per market.

## 5. lead_research

- **Wire**: `POST /research/lead/:leadId` (+ `/bulk` fan-out) → skill
  `lead-research`, context = approved brain + lead record. Output: insight
  record (profile, fit, signals, approach angle, score inputs) on the lead;
  `insights` endpoint reads it.
- **Verify**: research a real distributor lead → approach angle is specific
  to that company (spot-check: could this sentence apply to any company? then
  fail), score inputs each carry a justification, tool budget respected.
- **Better**: score calibration loop — once outreach outcomes exist (step 8+),
  feed reply/bounce outcomes back to weight the score dimensions per market.

## 6. contact_discovery

- **Wire**: `POST /contacts/discover` → skill `contact-discovery`, payload =
  {lead_ids, buyer_roles, channels, max_contacts_per_company}. Output:
  contact records with verification status + per-field source; do-not-contact
  checked at write time.
- **Verify**: discovery on 5 researched leads → contacts match brain buyer
  roles, guessed emails marked as guesses, cap respected, blocked contacts
  recorded not dropped.
- **Better**: licensed enrichment integration (People Data Labs / Apollo /
  Clay) as a data_source behind a toggle — raises match rate, same
  compliance posture. This is also the LinkedIn-URL quality upgrade.

## 7. outreach_generation

Generation split from sending on purpose — approval sits between them.

- **Wire**: `POST /outreach/campaigns/:id/generate-messages` and the
  custom-lead `generate-email` (§7.16) → skill `cold-email-outreach`
  (compose+QA stages) or `whatsapp-outreach` per channel. Output: outreach
  messages in `pending_approval`, each carrying its research bridge, language,
  recipients (To/CC from cc-rules), and **preflight QA verdict**. QA failures
  regenerate up to N times, then surface as failed-generation for human edit.
- **Verify**: generate for 3 leads in 3 languages from Silverline templates →
  single-language purity holds, fixed subject translated correctly, no
  placeholders remain, CC list = other company contacts + market rule.
- **Better**: run the preflight gate as *server-side code, not agent
  judgment* (port `docs/prototype-reference/qa/preflight_check.py`) so the
  QA verdict is deterministic and cheap; agent only fixes, never grades
  itself.

## 8. email_send

First write-action to the outside world — the approval boundary must be
airtight before this ships.

- **Wire**: provider adapters first (Gmail + Microsoft Graph: connect,
  refresh, create_draft, send_email, send_draft, get_message_status,
  list_recent_replies). Then `POST /outreach/messages/:id/create-draft|send`
  → run executes through the adapter. Draft mode default; `send` requires the
  message row to be `approved` — enforced by the API, not the agent. Send
  windows + daily caps enforced by a scheduler queue, not by trust.
- **Verify**: end-to-end against a sandbox mailbox — draft appears in the
  real mailbox; approved send delivers; a second send attempt on the same
  message is rejected (idempotency); out-of-window sends are held with
  visible ETA.
- **Better**: reply/bounce polling loop (`list_recent_replies` +
  MAILER-DAEMON scan, patterns in `docs/prototype-reference/replies-bounces/`)
  → replies freeze the lead for humans, bounces mark contacts invalid, and a
  bounce-rate circuit breaker pauses the campaign.

## 9. whatsapp_send

Same approval architecture as email; new transport.

- **Wire**: WhatsApp Business Cloud API integration (§7.21: WABA id, phone
  number id, webhook verify, encrypted token). `messages/generate → approve →
  send` (§7.22) → run sends approved message + media assets sequentially,
  verifying delivery status before any retry (the prototype's
  triple-send lesson, encoded in the skill).
- **Verify**: sandbox WABA — template message delivers, media bundle arrives
  in order, a simulated timeout does NOT produce a duplicate, opt-out marks
  the contact permanently blocked.
- **Better**: Meta template lifecycle management — submit/renew message
  templates per language from the company template pack, track
  `template_status`, and block campaigns whose templates lapsed.

## 10. linkedin_note_generation

Lightest run type; intentionally last of the agent-backed set.

- **Wire**: `POST /linkedin/find-profile` + `/generate-note` → skill
  `linkedin-notes`. Output: linkedin_action record (profile URL + note +
  status); manual status marks (§7.23) are plain CRUD, no run needed.
- **Verify**: for 5 contacts → canonical `/in/` URLs only, notes ≤ cap, in
  the contact's language, personalized (same spot-check as research).
- **Better**: piggyback on step 6's enrichment source for profile URLs —
  discovery quality is the only real lever here; sending stays manual by
  design.

## 11. analytics_refresh

No agent, no skill — deliberate.

- **Wire**: scheduled SQL aggregation (or materialized views) over leads,
  messages, runs → analytics tables read by §7.25 endpoints. Runs on a cron,
  recorded in `agent_runs` for operational visibility only.
- **Verify**: numbers reconcile with raw counts after a demo campaign.
- **Better**: market-intelligence layer — country opportunity scores derived
  from scan yield + reply rates, feeding the lead map (§9.5) and the
  "recommended actions" dashboard card.

---

## Sequence summary

```text
0 harness → 1 document_processing → 2 product_extraction → 3 company_brain_build
→ 4 lead_scan → 5 lead_research → 6 contact_discovery → 7 outreach_generation
→ 8 email_send → 9 whatsapp_send → 10 linkedin_note_generation → 11 analytics_refresh
```

Rules of the sequence:

- **Nothing ships to step N+1 while step N's Verify fails.** Each step's
  acceptance test becomes a permanent regression test run against the
  Silverline pack.
- **Better-stages are backlog, not blockers** — except 7's deterministic QA
  gate and 8's bounce circuit breaker, which must land before real customer
  volume.
- The full chain 1→8 executed in order **is** the Silverline demo flow
  (PRODUCT.md §11) — when step 8's Verify passes, the MVP demo works.
