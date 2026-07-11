---
name: company-brain-build
description: Synthesize onboarding data, documents, and internal sales history into the Company Brain — the shared intelligence layer every agent reads. Produces a reviewable, versioned brain snapshot.
version: 1.0.0
author: Interfaze
metadata:
  hermes:
    tags: [sales, company-brain, knowledge, synthesis, onboarding]
    category: sales
---

# Company Brain Build

Build (or rebuild) the Company Brain from everything the company has provided:
identity, positioning, products, processed documents, past sales, current
contacts, and business rules. The output is the context layer that lead
discovery, research, and outreach all read (product-architecture.md) — not a
dashboard summary.

## Inputs

- Company profile + positioning (onboarding §6.1–6.2).
- Product records, including those extracted by `document-processing`.
- Internal sales data: past sales/customers, lost deals, previous outreach,
  objections, price lists (§6.4).
- Current contact lists (§6.5).
- Business rules pack: market preferences, outreach rules, CC rules
  (company-packs schema).

## Output — the brain document (per section)

1. **Product understanding** — what the company sells, per product: category,
   differentiators, target industries, restricted markets. Grounded in the
   catalog, not invented.
2. **Ideal customer profile** — company types, sizes, and industries that buy,
   derived from past customers first, positioning second.
3. **Buyer roles** — per product/market, the titles that make purchase
   decisions (feeds contact discovery).
4. **Market assumptions** — which countries/segments look strongest and why:
   past-sales revenue breakdown beats intuition; flag assumption vs evidence
   explicitly.
5. **Sales arguments** — value propositions and objection answers, mined from
   past emails, proposals, and recorded objections.
6. **Business rules digest** — market preferences, exclusions, contact policy, in
   machine-usable form.
7. **Missing data** — what the brain is guessing at and which onboarding
   input would fix it (feeds the MissingDataPanel).

## Rules

- **Evidence over inference.** Every claim tagged with its source (document,
  past-sales row, user input). Inferences are marked as such.
- **Versioned snapshots.** Each build writes a new snapshot (§7.8); never
  mutate an approved brain in place. Rebuild diffs against the previous
  snapshot so review is cheap.
- **User approval gates activation.** A built brain is a draft until approved;
  agents keep reading the last approved snapshot.
- **Agent-agnostic.** The brain serves future agents (operations, stock,
  procurement) — keep sections structured, not sales-prose.
- Read-only toward the outside world: building the brain never contacts
  anyone or sends anything.
