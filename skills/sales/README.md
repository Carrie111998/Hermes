# Sales Agent skills

The complete agent layer for the interfaze-agent Sales Agent MVP. Every agent
run type from PRODUCT.md §7.24 maps to a skill here; the SaaS backend
dispatches runs to the Hermes runtime with the matching skill loaded.

| Run type (§7.24) | Skill | Notes |
|---|---|---|
| company_brain_build | `company-brain-build` | versioned snapshots, approval-gated |
| document_processing | `document-processing` | typed uploads → validated records |
| product_extraction | `document-processing` | catalog/spec-sheet path of the same skill |
| lead_scan | `lead-discovery` | multi-source, deduped, market-gated |
| lead_research | `lead-research` | per-lead insights + score inputs |
| contact_discovery | `contact-discovery` | buyer-role targeted, capped, passive verification |
| outreach_generation | `cold-email-outreach` / `whatsapp-outreach` | per channel |
| email_send | `cold-email-outreach` | draft default, approved send, preflight QA |
| whatsapp_send | `whatsapp-outreach` | Business Cloud API, approval-gated |
| linkedin_note_generation | `linkedin-notes` | manual send by design (no automation) |
| analytics_refresh | — | pure DB aggregation, no agent needed |

Shared invariants across all skills:

- **Company Brain is the context**: skills read company config (identity,
  rules, templates) from the tenant's pack — nothing company-specific is
  hardcoded. Demo instance: `company-packs/silverline/`.
- **Market preferences + exclusion filters** apply in every skill that selects
  or touches a lead: `no_research_markets` block discovery/research,
  `no_outreach_markets` block sending, `target_markets` are worked first. All
  client-selected in the UI; unlisted markets are allowed.
- **Discovery/research/processing are read-only** toward the outside world;
  only the two send skills contact anyone, and only after approval.
- Per-company values resolve from `metadata.hermes.config` in standalone
  hermes, and from company settings (frontend/API) in the SaaS deployment.
