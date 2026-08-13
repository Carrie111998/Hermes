# Evidence contract and handoff

One row represents one claim-source relationship. Redact personal contact
details and sensitive personal attributes from buyer language. If redaction
would distort the meaning, use a faithful summary instead. Never infer a
sensitive attribute, fabricate a quote, or fill a missing field from memory.

Before emitting a row, register its URL with `grounded-citations`. Append the
ledger's `[n]` marker to the external `claim`, and keep the exact registered URL
in `source_url`. This carries citation state without adding an eleventh field.

## Required Evidence Matrix fields

| Field | Meaning |
|---|---|
| `claim` | Atomic proposition the evidence supports or contradicts |
| `source_url` | Exact retrieved URL registered in the grounded-citation ledger |
| `source_title` | Human-readable source name |
| `published_or_observed_at` | Publication date when known; otherwise retrieval timestamp |
| `source_lane` | `primary`, `independent`, or `community`; retrieval method does not change provenance |
| `evidence` | Privacy-redacted passage or faithful observation from the opened target |
| `signal_type` | `demand`, `pain`, `pricing`, `competition`, `buyer_language`, `risk`, or `counter_evidence` |
| `corroboration` | Independent grounded-citation ledger identifiers such as `[2]`, or `none` |
| `confidence` | `high`, `medium`, or `low`, with a short reason |
| `limitations` | Access, sampling, age, bias, ambiguity, or applicability constraints |

Compact example:

```yaml
- claim: Buyers describe manual reconciliation as a recurring delay.[1]
  source_url: https://example.com/public-thread
  source_title: Public operations discussion
  published_or_observed_at: 2026-08-13T10:00:00Z
  source_lane: community
  evidence: Multiple participants describe weekly spreadsheet reconciliation.
  signal_type: pain
  corroboration: none
  confidence: low — small self-selected sample
  limitations: Public thread; personal details redacted; representativeness unverified.
```

## Role handoffs

- **Scout → Sentinel:** cited Evidence Matrix, query and retrieval log, coverage
  gaps, opportunity hypotheses kept separate, and privacy-redacted evidence.
- **Sentinel → Quant:** underlying rows unchanged, with legality, privacy,
  representativeness, unsafe-method, and overclaiming blocks or downgrades.
- **Quant → Orchestrator:** only cited demand, price, cost, and competitor inputs;
  label inferred financial assumptions, deduplication, and contradictions. Never
  turn weak community signals into precise market-size estimates.
- **Orchestrator → user:** decision, confidence, strongest evidence, contrary
  evidence, gaps/limitations, and cheapest next validation step. A high-impact
  gap is a user checkpoint, not an implicit assumption.

## Final artifact order

1. **Decision summary:** `proceed`, `validate cheaply`, or `stop`, with
   confidence and the cheapest ethical next validation step.
2. **Evidence Matrix:** all required fields for every accepted finding.
3. **Contradictions and uncertainty:** disagreements, weak inference, and what
   would change the decision.
4. **Coverage report:** each lane marked covered, partial, or gap; include the
   bounded retry/fallback log for failures and the citation ledger's rendered
   `Sources:` list from `render --style plain` as a subsection. Do not emit the
   default `## Sources` heading or add a fifth top-level section. Do not initiate
   outreach or paid actions.
