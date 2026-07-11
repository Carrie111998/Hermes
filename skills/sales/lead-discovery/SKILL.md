---
name: lead-discovery
description: Discover new B2B leads per country and segment via multi-source web research, dedup against existing customers and leads, and append scored lead records. Read-only toward the outside world.
version: 1.0.0
author: Interfaze
metadata:
  hermes:
    tags: [sales, leads, discovery, research, prospecting, b2b]
    category: sales
    config:
      - key: sales.max_leads_per_country
        description: Cap on new leads captured per country per scan
        default: "50"
        prompt: Max leads per country per scan?
      - key: sales.scan_depth
        description: Default scan depth (quick, standard, deep)
        default: "standard"
        prompt: Default lead scan depth?
      - key: sales.excluded_industries
        description: Comma-separated industry keywords that disqualify a lead
        default: "industrial kitchen,commercial kitchen,HORECA,catering equipment,professional kitchen equipment"
        prompt: Which industries should discovery exclude?
---

# Lead Discovery Skill

Find new prospective buyers for the company's products, country by country,
and turn them into clean lead records. Discovery is **read-only**: it never
contacts a prospect, never sends anything, never writes outside the lead
database. Outreach is a separate, separately-approved step.

## Inputs

- Countries to scan (max 5 per scan — PRODUCT.md lead-map rule). Never scan a
  country in the client's `no_research_markets` (market preferences); any
  market not excluded is allowed. `target_markets` are the default selection.
- Target segments from the Company Brain buyer profile (e.g. appliance
  distributors, importers, hotel equipment suppliers, kitchen design
  companies, retail chains).
- `sales.scan_depth` and `sales.max_leads_per_country`.

## Pipeline

1. **Research (per country × segment)** — multi-source sweep:
   - Web search: `"{segment}" "{country}" contact email`, product-category
     variants, and competitor-dealer angles (public dealer/distributor lists
     of comparable brands).
   - Directories: yellow pages, industry association member lists.
   - Trade shows: exhibitor lists of relevant fairs (e.g. Big 5, HostMilano,
     Ambiente).
   - Add negative keywords for every excluded industry:
     `-"industrial kitchen" -"commercial kitchen" -HORECA -"catering equipment"`.
   - Fetch top results to extract contact emails/phones. Respect a hard
     tool-call budget per country (cost control); log what was not covered
     rather than silently stopping.
2. **Validate** — per candidate record:
   - Email passes regex validation; phone normalized to E.164; country as ISO
     3166-1 alpha-2; company name ≤ 60 chars.
   - Suspected excluded-industry companies are marked (`industrial?`) rather
     than silently kept — they are dropped at dedup.
3. **Dedup** — against BOTH the existing customer base and already-discovered
   leads: normalized company-name match and website-domain match. Expect
   20–30% duplicates; report the drop count, never hide it.
4. **Append** — write surviving records as new leads with `source_url`,
   segment, and scan id, ready for scoring and research
   (`POST /api/v1/lead-scans/:scanId/results` flow).
5. **Report** — one summary per scan: added, duplicates skipped, excluded by
   industry filter, per-country breakdown.

## Lead record fields

`company_name, country, city?, website, email, phone, segment, source_url,
scan_id, notes` — matching the product lead schema (PRODUCT.md §7.11).

## Worker discipline (hard rules)

- Discovery workers never contact customers, never send email/messages, never
  write to anything but the lead store.
- Research output is data, not prose dumps: structured records only.
- If a decision point is not covered by these rules (e.g. an ambiguous
  market case), stop and ask the user rather than guessing.
