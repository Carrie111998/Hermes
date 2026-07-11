---
name: contact-discovery
description: Find the right buyer-role people at a lead company — names, titles, emails, phones, LinkedIn URLs — validated and capped per company. Discovery only; never contacts anyone.
version: 1.0.0
author: Interfaze
metadata:
  hermes:
    tags: [sales, contacts, discovery, buyer-roles, enrichment]
    category: sales
    config:
      - key: sales.max_contacts_per_company
        description: Cap on discovered contacts per lead company
        default: "5"
        prompt: Max contacts to capture per company?
---

# Contact Discovery

Given a researched lead, find the people who make buying decisions (§7.14).
Targets the Company Brain's buyer roles (e.g. import manager, purchasing
manager, general manager) — a generic info@ address is a last resort, not a
result.

## Pipeline (per lead company)

1. **Role targeting** — take buyer roles from the Company Brain (per product/
   market); search for people holding those titles at the company: site team
   pages, LinkedIn profile search (web search only — see linkedin-notes),
   trade registries, press mentions.
2. **Enrichment (optional)** — when a licensed enrichment data source is
   configured (People Data Labs / Apollo / Clay class), query it by company
   domain + role; merge with web findings. Never use scraping wrappers or
   session automation.
3. **Channel capture** — per contact: email, phone (E.164), LinkedIn profile
   URL (canonical `/in/` only), preferred language (from country).
4. **Validate & rank** — email syntax + domain match (person@company-domain
   ranks above generic patterns); mark each contact's verification status
   (verified / pattern-guessed / unverified). Rank by role seniority and
   channel quality; keep the top `sales.max_contacts_per_company`.
5. **Record** — contact records on the lead (§6.5 fields + buyer_role +
   verification status + source per field).

## Rules

- **Discovery never contacts anyone** — no verification emails, no connection
  requests, no calls. Verification is passive (sources, patterns, enrichment
  confidence).
- Pattern-guessed emails (first.last@domain inferred) are always marked as
  guesses; outreach treats them as bounce-risk and never CCs a guess.
- Do-not-contact and opt-out lists are checked at capture time; matching
  people are recorded as blocked, not silently skipped.
- Respect the same market preferences and exclusion filters as every other skill.
- Bulk discovery (§7.14 `/contacts/discover`) fans out one worker per lead,
  each honoring the per-company cap.
