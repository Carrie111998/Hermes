# PATH — what PRODUCT.md promises, what the code does, what is left

Written 2026-08-26 against `fix/promise-gaps-audit`. PRODUCT.md §10 ("MVP Page
Priority") is a plan that has already been executed; this file is the plan that
replaces it, and the "remaining release work" half of `docs/product/ROADMAP.md`.

**How every claim below was checked.** Routes from `create_app()`, not grep.
Schema from `server.db.SCHEMA` against a scratch SQLite file. Vocabulary
coverage from `load_sectors()` and `COUNTRY_LANGUAGE`. Both suites run:

```
1056 passed, 2 deselected in 93.72s          .venv/bin/python -m pytest tests -q
71 pass, 0 fail                              node --test tests/server/webui/*.mjs
1 skipped                                    pytest tests/integration/... -m integration
```

That last line is the headline. The green suite is stub-fed: every server test
injects `StubRunExecutor`, and the one test that runs a real agent skips because
no LLM provider is configured in this checkout. **No promise in PRODUCT.md §2
that depends on the agent has been observed working from this repository.**

---

## 1. Topic-by-topic verdict

| § | Topic | Verdict |
|---|---|---|
| 1 | Hermes fork, rebranded product layer | **Done.** `server/` is 85 files / 1.0 MB; the inherited layer around it is 560 files / 18.5 MB. See gap G10. |
| 2 | 20 MVP behaviours | **Surface complete, core unobserved.** Every promise has live routes. The discovery/research/generation promises run through the agent, which has never executed here. Gap G1. |
| 3 | Email providers v1.0 | **Done, unverified against Google/Microsoft.** Gmail + Microsoft implement all 8 `EmailProvider` methods. Spec drift: `smtp.py` shipped early (spec puts it in v1.1) and `browser.py` exists in code but nowhere in the spec. Zoho/Resend/Mailgun correctly absent. Gaps G4, G9. |
| 4 | WhatsApp Business Cloud API | **Real adapter, unverified.** `WhatsAppCloudProvider` calls Graph v23.0 for real (no stub), webhook GET+POST and opt-out routes exist. Never run against a Meta test number. Gap G4. |
| 5 | LinkedIn, manual only | **Done and compliant.** find-profile, generate-note, and four manual `mark-*` transitions. No browser automation shipped, which is what the spec demanded. Nothing left here. |
| 6 | Onboarding depth (~76 fields) | **Contract added 2026-08-26 (G5 closed).** 8 steps, 5 required. Section writes now go through `schemas.SECTION_FIELDS`; an undefined key is a 422 naming it. §6.1 and §6.2 are locked to the document by `tests/server/test_onboarding_contract.py`. §6.3 stays open by design; §6.4/§6.5 are upload categories, and `DOCUMENT_TYPES` covers 8 of the 25 named — see G5b. |
| 7 | API route structure | **222 documented routes, all live.** Test-enforced — but one-directionally (`expected <= actual`), so 48 live routes are undocumented, including all 16 `research-campaigns` routes. Gap G6. |
| 8–9 | Frontend structure and modules | **Honest and test-checked.** §8 was rewritten from the code after the React plan was abandoned; §8.2 is asserted against `main.js`. The seam: §9.4 says the customer deployment is built in `interagent-web`, another repo. What the customer actually sees cannot be verified from here. Gap G7. |
| 10 | Sprint priority (6 sprints) | **Executed. Now stale as a plan** — this file supersedes it. |
| 11 | Silverline demo flow (21 steps) | **Vocabulary verified, flow undemonstrable here.** All 5 demo countries resolve (DE→de, AE/SA→ar, NL→nl, GB→en, and `en` is exempt from `unmapped_markets` by design). Steps 10–16 need a live model. Gap G1, G3. |
| 12 | Final MVP rules | **True except one.** 5-country cap enforced at the service boundary; drafts-and-approved-send real; CSV export real. But "Supabase is used for auth, database, and storage" is aspirational: the Supabase paths exist and are statically parity-checked, and nothing has ever executed against live Postgres or RLS. Gap G2. |

---

## 2. The path

Ordered by what blocks the next real thing, not by size.

### P0 — nothing can ship past these

**G1. The agent has never run in this checkout.**
No provider key in `.env`; `tests/integration/test_live_lead_research_gap.py` skips.
Everything the product sells — discovery, research, contact finding, email
generation — is unobserved here. A green suite is not evidence.
*Next:* configure one provider, run the live gap test, then run the §11 chain
end to end and store scrubbed fixtures for all 14 run-type output contracts
(`server/run_types.py:185` — ROADMAP still says 11; REGISTRY has 14).

**G2. Postgres and RLS have never been executed.**
Parity is enforced statically (`test_postgres_parity.py`, `test_postgres_backend.py`)
and that coverage is genuinely good, but the suite runs SQLite. Every
production-only failure mode lives in `server/postgres.py` and the migrations.
*Next:* apply `server/supabase/migrations/001_initial.sql` in staging and run
customer-vs-customer and customer-vs-admin RLS tests against it.

**G3. `INTERFAZE_PUBLIC_BASE_URL` is still a `fly.dev` host.**
It is embedded as an absolute URL into every unsubscribe link
(`server/compliance.py:36`). Links already delivered keep pointing at the old
host forever — a KVKK/CAN-SPAM problem no later fix can reach.
*Next:* do the `agent.tugrap.dev` migration in `docs/product/TODO.md`
**before the first outbound email**, and register both hosts' OAuth callbacks
while you are in the Google/Microsoft consoles.

**G4. Three provider lifecycles verified only against stubs.**
Gmail OAuth (connect → refresh → draft → approved send → status → reply poll),
Microsoft Graph (same chain), Meta test number (template send, webhook status,
opt-out, ambiguous-timeout/no-duplicate). All three are unchecked ROADMAP gates.
*Next:* one sandbox pass each. These are the paths that touch real recipients.

### P1 — product correctness

**~~G5. Onboarding has no contract.~~ Fixed 2026-08-26.**
`schemas.SECTION_FIELDS` is now the one place that decides whether a section key
is real; `_put_section` returns 422 with the offending key named, and
`provision_demo_account` applies the same list to the operator's JSON profile.
`tests/server/test_onboarding_contract.py` locks §6.1 and §6.2 to the document
in both directions. Suite: 1063 passed.

**G5b. Ten §6.4/§6.5 upload categories have no document type.**
The remaining half of the original gap, and a product decision rather than a
contract one. `DOCUMENT_TYPES` covers 8 of the 25 categories §6.4 and §6.5 name;
`active_deals`, `previous_outreach`, `country_revenue_breakdown`,
`customer_segments`, `average_order_value`, `sales_cycle_length`,
`repeat_customers`, `customer_objections`, `support_questions`, `email_examples`
and the four `*_contact_list` variants all land under `other`.
*Next:* each new type needs processing behaviour, not just a name in the set, so
add them as the Brain learns to use them — do not widen the enum first.

**G5c. `interagent-web` may send keys the allowlist now rejects.**
`server/webui` was checked call site by call site and sends nothing outside the
list. The customer deployment is a separate repo (see G7) and could not be
checked from here.
*Next:* grep its section-patch bodies against `SECTION_FIELDS` before the next
deploy.

**G6. 48 live routes are undocumented, including the centrepiece.**
`test_all_product_routes_are_exposed` asserts `expected <= actual`, so the spec
catches deletions but never additions. Undocumented: all 16
`research-campaigns` routes, OAuth start/callback, `unsubscribe` (a compliance
surface), `data-sources` install/purge/impact, `research/*` profile and claim
reads, `candidate-datasets`, `products/import`, `activity/digest`.
*Next:* make the assertion bidirectional with an explicit allowlist for
genuinely internal paths, then write §7.29+ for what survives.

**G7. The shipped UI is not in this repo.**
`server/webui` is upstream source of record (6 customer routes, 71 node tests);
the customer deployment is built in `interagent-web`. Divergence between them is
currently invisible.
*Next:* decide which repo is authoritative for a release and say so in §9.4. If
it stays split, the handoff needs a version check, not a convention.

**G8. Admin password recovery has no delivery path.**
`auth_mode: local` has no sender, so the reset token is issued and withheld
(`server/auth.py:243`). The bootstrap password in the password manager is the
only way in.
*Next:* real reset mail once a transactional sender exists. Until then, treat
`INTERFAZE_BOOTSTRAP_ADMIN_PASSWORD` as a production secret with no backup.

### P2 — coverage and drift

**G9. Market coverage is 19 countries, 7 languages, 5 sectors.**
Enough for the Silverline demo, and honest about the rest —
`unmapped_markets` reports what it cannot search. Adding a market is a
`sectors.yaml` data task (then `python -m server.lead_research.sectors --check`,
which nothing in CI runs), not code.

**G10. Spec drift worth one editing pass.**
§3 provider versions vs. what shipped (smtp early, browser undocumented); §10
sprints executed; §12's Supabase claim; ROADMAP's "11 run types" vs 14 in
`REGISTRY`.

**G11. The inherited layer is 18× the product.**
560 files / 18.5 MB of Hermes against 85 files / 1.0 MB of `server/`. It is not
in the product's path, but it is in every clone, every image, and every agent's
search space. Pruning it is a decision to schedule, not a fix to apply.

---

## 3. What this file replaces

- PRODUCT.md §10 (sprint order) — executed; keep as history or delete.
- `docs/product/ROADMAP.md` "Release gates" and "Dashboard handoff" — folded
  into P0/P1 above with the evidence attached.
- `docs/product/TODO.md` stays: it holds the runbook detail for G3 and G8.
