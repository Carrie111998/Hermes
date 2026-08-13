# Evidence contract and handoff

One row represents one claim-source relationship. Preserve exact buyer
language in `evidence` when it is safe and necessary; otherwise use a faithful
summary. Never fabricate a quote or fill a missing field from memory.

## Required Evidence Matrix fields

| Field | Meaning |
|---|---|
| `claim` | Atomic proposition the evidence supports or contradicts |
| `source_url` | Exact retrieved target URL |
| `source_title` | Human-readable source name |
| `published_or_observed_at` | Publication date when known; otherwise retrieval timestamp |
| `source_lane` | `primary`, `independent`, `community`, or `browser_fallback` |
| `evidence` | Exact relevant passage or faithful observation from the opened target |
| `signal_type` | `observed_fact`, `source_claim`, `inference`, or `hypothesis` |
| `corroboration` | Independent supporting/contradicting row IDs, or `none` |
| `confidence` | `high`, `medium`, or `low`, with a short reason |
| `limitations` | Access, sampling, age, bias, ambiguity, or applicability constraints |

Compact example:

```yaml
- id: E1
  claim: Buyers describe manual reconciliation as a recurring delay.
  source_url: https://example.com/public-thread
  source_title: Public operations discussion
  published_or_observed_at: 2026-08-13T10:00:00Z
  source_lane: community
  evidence: Multiple participants describe weekly spreadsheet reconciliation.
  signal_type: source_claim
  corroboration: none
  confidence: low — small self-selected sample
  limitations: Public thread; identities and representativeness are unverified.
```

## Role handoffs

- **Scout → Sentinel:** query, candidate URL, expected lane, expected relevance,
  and why the source may answer the decision question. No conclusion yet.
- **Sentinel → Quant:** access status, content-health result, retrieved title and
  date, exact evidence, failure/fallback log, and any safety or trust warning.
- **Quant → Orchestrator:** complete Evidence Matrix, deduplication notes,
  corroboration links, contradiction clusters, and source-lane coverage.
- **Orchestrator → user:** decision, confidence, strongest evidence, contrary
  evidence, gaps/limitations, and cheapest next validation step.

## Final artifact order

1. **Decision:** `proceed`, `validate cheaply`, or `stop`, with confidence.
2. **Evidence Matrix:** all required fields for every accepted finding.
3. **Contradictions and uncertainty:** disagreements, weak inference, and what
   would change the decision.
4. **Coverage report:** each lane marked covered, partial, or gap; include the
   bounded retry/fallback log for failures.
5. **Next validation step:** the cheapest ethical action that reduces the most
   important uncertainty; do not initiate outreach or paid actions.
