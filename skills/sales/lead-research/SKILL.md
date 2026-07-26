---
name: lead-research
description: Deep-research a specific lead company against the Company Brain — who they are, what they distribute, fit signals — producing structured insights and score inputs for outreach personalization.
version: 1.0.0
author: Interfaze
metadata:
  hermes:
    tags: [sales, research, leads, insights, scoring]
    category: sales
    config:
      - key: sales.research_tool_budget
        description: Max web search/fetch calls per lead research run (cost control)
        default: "12"
        prompt: Max research tool calls per lead?
---

# Lead Research

Research one lead company (§7.13) after discovery and before outreach.
Discovery finds *that* a company exists; research finds *why and how* to
approach it. Every cold message the product sends is grounded in this step —
never message blind.

## Inputs

- The lead record (company, country, website, segment, source).
- Company Brain: product understanding, ideal customer profile, buyer roles,
  sales arguments (research is always *relative to what we sell*).

## Output — structured insights per lead

- **Business profile**: what they actually do/distribute/import, brands they
  carry, market position, size signals. Verified from their website and
  independent sources, not just directory blurbs.
- **Fit analysis**: which of our products fit their range and why; explicit
  mismatch flags (e.g. exclusion-filter hit → recommend do-not-contact).
- **Signals**: expansion, new showrooms, trade-fair presence, competitor
  brands carried (a distributor of comparable brands is a strong signal).
- **Approach angle**: the one concrete, personalized reason to contact them —
  this becomes the outreach bridge. In the target market's language.
- **Score inputs** (§7.12): product_fit, market_fit, company_quality,
  intent_signal, source_confidence — each a value plus a one-line
  justification (feeds the score explanation endpoint).

## Rules

- Hard tool budget per lead (`sales.research_tool_budget`); log coverage gaps
  rather than silently stopping.
- Facts get sources (URL); inferences are marked as inferences. The insight
  record is auditable.
- Exclusion filters and market preferences apply here too: research that
  reveals an excluded segment marks the lead, and the run says so. Research may
  run in a `no_outreach_market` (for intelligence) but not in a
  `no_research_market`.
- Research output is data for the pipeline, never customer-facing text —
  the outreach skills compose customer messages from it.
- Read-only: research never contacts the lead, on any channel.
- Bulk research (§7.13 `/research/bulk`) is this skill fanned out one worker
  per lead, each within its own budget.

## Evidence-bound campaign enrichment

When invoked by the Research campaign pipeline, the input is one resolved
organization, its compact evidence bundle, missing applicable fields, sector
playbook, and explicit page/time/token budget. Structured sources always run
first. Stop when the completeness target, source exhaustion, or any configured
budget is reached; report remaining fields as `unknown` rather than guessing.

Return claims as JSON objects matching the application `Claim` contract:

```json
{
  "field": "store_count",
  "value": 84,
  "unit": "stores",
  "currency": null,
  "period": "FY2025",
  "status": "observed",
  "confidence": 0.86,
  "method": "observed",
  "evidence_ids": ["ev_example"],
  "applicability": "useful"
}
```

- Numeric claims require resolvable evidence, an explicit period when the value
  changes over time, and separate unit/currency fields where applicable.
- Aggregate market or trade evidence may inform market attractiveness. It must
  never become a named-company metric, buying-intent claim, or lead by itself.
- Unsupported numeric output is rejected by the application validator; do not
  restate it as narrative to bypass validation.
- Keep public market capitalization, reported company valuation, estimated
  private value ranges, and addressable market value as distinct concepts.
- Use `observed`, `calculated`, `estimated_range`, `conflicted`, `unknown`, or
  `not_applicable`; never convert missing evidence to zero.
