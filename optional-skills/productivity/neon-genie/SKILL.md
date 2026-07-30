---
name: neon-genie
description: >
  Evidence-bound product and opportunity intelligence — audits, zero-option
  loops, commercial models, agentic graphs, and Wayfinder handoffs with claim
  labels and fail-closed gates. Use for product intent, opportunity mining,
  and advisory packets — not cinematic work (use Kubrick) or code execution.
version: 3.23.0
author: Applied Alchemy Labs / Zero State
license: MIT
platforms: [linux, macos, windows]
dependencies: []
metadata:
  hermes:
    tags:
      - Product
      - OpportunityIntelligence
      - ProductArchitecture
      - ZeroOption
      - Commercial
      - EvidenceBound
      - WayfinderHandoff
      - AdvisoryOnly
      - AgenticServices
    category: product
    related_skills: []
triggers:
  - neon genie
  - product audit
  - opportunity mining
  - blocked transition
  - fragmentation scan
  - zero option
  - first cash
  - agentic services
  - x402
  - commercial simulation
  - evidence intelligence
  - wayfinder handoff
  - wayfinder execution packet
  - cost of inaction
  - audit delivery
---

# Neon Genie v3

## Hermes skill identity

Neon Genie is a **standalone Hermes skill**. Hermes loads this directory directly and uses `SKILL.md` as the operating contract. No Python package install, Kubrick skill, Wayfinder runtime, or external knowledge base is required to load.

See `references/hermes-runtime-contract.md` for path, artifact, authority, and dependency policy.

Profile contracts, packet schemas, and golden tests ship as hub mirrors (see **Hermes Hub support files** below) and as root `profiles/`, `schemas/`, `evals/` on full install.

**Optional companions:** host research tools, Wayfinder (handoff consumer). Their absence never blocks local advisory work.

## Packaging CLI

One pattern (packaging only — not a product brain):

```text
python scripts/neon_genie.py do <job> [options]
```

| Job | Use |
|-----|-----|
| `doctor` | Full smoke after install |
| `check` | Skill integrity |
| `run` | Operator packaging run (brief/recipe → envelope) |
| `capabilities` | Machine-readable skill surface (JSON) |
| `dist` | Hub mirrors + package (`verify` / `write`) |
| `recipe` | Named example (`--list` / `--name`) |
| `route` / `validate` / `receipt` / `envelope` | Profiles, schema, receipt, run envelope |
| `eval` / `transcripts` | Golden tests |
| `behavioral` / `runtime` | Semantic agent gates + hub install smoke |
| `learn` / `reconcile` | PROPOSED ledger entry; link entries to run_id |
| `release-check` | Pre-release version / changelog / dist gate |

```bash
python scripts/neon_genie.py help
python scripts/neon_genie.py do doctor
python scripts/neon_genie.py do run --recipe product-audit --out out/neon-genie/demo
```

See `README.md` (How to use) and `QUICKSTART.md`. Golden prose: `examples/evals/transcripts/README.md`. Post-SEAL: `references/post-seal-verification.md`. Gate ontology: `references/gates.yaml`.

### Hermes Hub support files


Hermes Hub installs only `SKILL.md` plus **explicitly path-referenced** files under allowlisted dirs (`references/`, `templates/`, `scripts/`, `assets/`, `examples/`). The list below is **generated** from `distribution.yaml` — run `python scripts/distribution_spine.py write` after adding packaging files:

<!-- BEGIN HUB_SUPPORT_FILES (generated; do not edit) -->
- `examples/README.md`
- `examples/agentic.brief.yaml`
- `examples/audit.brief.yaml`
- `examples/commercial.brief.yaml`
- `examples/evals/behavioral/README.md`
- `examples/evals/behavioral/cases/memetic-weak-proof.json`
- `examples/evals/behavioral/cases/private-buyer-datarequest.json`
- `examples/evals/behavioral/cases/public-market-research.json`
- `examples/evals/behavioral/cases/repo-mutation-advisory-only.json`
- `examples/evals/behavioral/cases/wayfinder-change-control.json`
- `examples/evals/behavioral/cases/zero-resources-not-computable.json`
- `examples/evals/behavioral/transcripts/01-private-buyer-datarequest.md`
- `examples/evals/behavioral/transcripts/02-public-market-research.md`
- `examples/evals/behavioral/transcripts/03-zero-resources-not-computable.md`
- `examples/evals/behavioral/transcripts/04-repo-mutation-advisory-only.md`
- `examples/evals/behavioral/transcripts/05-memetic-weak-proof.md`
- `examples/evals/behavioral/transcripts/06-wayfinder-change-control.md`
- `examples/evals/cases/authority-leakage.json`
- `examples/evals/cases/buyer-beneficiary-conflation.json`
- `examples/evals/cases/completion-proof-present.json`
- `examples/evals/cases/completion-proof-required.json`
- `examples/evals/cases/fictional-resource.json`
- `examples/evals/cases/memetic-cannot-promote.json`
- `examples/evals/cases/offline-no-fabricated-observed.json`
- `examples/evals/cases/private-gap-must-request.json`
- `examples/evals/cases/private-gap-request-open.json`
- `examples/evals/cases/private-gap-silent-invent.json`
- `examples/evals/cases/public-gap-must-attempt-research.json`
- `examples/evals/cases/public-gap-research-attempted.json`
- `examples/evals/cases/scorecard-cannot-override-gate.json`
- `examples/evals/cases/wayfinder-change-control.json`
- `examples/evals/cases/x402-misfit.json`
- `examples/evals/cases/zero-option.json`
- `examples/evals/rubric.md`
- `examples/evals/transcripts/01-zero-option-empty.md`
- `examples/evals/transcripts/02-product-audit.md`
- `examples/evals/transcripts/03-fragmentation.md`
- `examples/evals/transcripts/04-commercial-missing-buyer.md`
- `examples/evals/transcripts/05-offline-audit.md`
- `examples/evals/transcripts/06-agentic-x402-misfit.md`
- `examples/evals/transcripts/07-memetic-cannot-promote.md`
- `examples/evals/transcripts/08-evidence-intelligence.md`
- `examples/evals/transcripts/09-opportunity-mining.md`
- `examples/evals/transcripts/README.md`
- `examples/evals/transcripts/rubric.md`
- `examples/evidence.brief.yaml`
- `examples/fragmentation.brief.yaml`
- `examples/memetic.brief.yaml`
- `examples/opportunity.brief.yaml`
- `examples/packets/sample-data-request.json`
- `examples/packets/sample-opportunity.packet.json`
- `examples/packets/sample-receipt-with-requests.json`
- `examples/packets/sample-receipt.packet.json`
- `examples/packets/sample-run-envelope.json`
- `examples/product-audit.brief.yaml`
- `examples/zero-option-with-skills.brief.yaml`
- `examples/zero-option.brief.yaml`
- `references/CAPABILITY_MAP.md`
- `references/GOLDEN_TESTS.md`
- `references/VERSION`
- `references/anti-overclaim-patterns.md`
- `references/gates.yaml`
- `references/hermes-runtime-contract.md`
- `references/manifest.json`
- `references/post-seal-verification.md`
- `references/profiles/agentic_services.md`
- `references/profiles/audit_delivery.md`
- `references/profiles/commercial.md`
- `references/profiles/core.md`
- `references/profiles/evidence_intelligence.md`
- `references/profiles/fragmentation.md`
- `references/profiles/memetic.md`
- `references/profiles/opportunity_mining.md`
- `references/profiles/product_architecture.md`
- `references/profiles/wayfinder_handoff.md`
- `references/profiles/zero_option.md`
- `references/schema-versioning.md`
- `references/schemas/agentic-service-graph.schema.json`
- `references/schemas/audit-delivery-packet.schema.json`
- `references/schemas/commercial-simulation.schema.json`
- `references/schemas/data-request.schema.json`
- `references/schemas/evidence-intelligence-packet.schema.json`
- `references/schemas/fragmentation-packet.schema.json`
- `references/schemas/learning-ledger-entry.schema.json`
- `references/schemas/memetic-pressure-packet.schema.json`
- `references/schemas/opportunity-packet.schema.json`
- `references/schemas/product-packet.schema.json`
- `references/schemas/run-envelope.schema.json`
- `references/schemas/run-receipt.schema.json`
- `references/schemas/wayfinder-execution-packet.schema.json`
- `references/schemas/zero-option-packet.schema.json`
- `scripts/audit_release_version.py`
- `scripts/build_envelope.py`
- `scripts/build_receipt.py`
- `scripts/capabilities.py`
- `scripts/check_behavioral_invariants.py`
- `scripts/check_transcripts.py`
- `scripts/distribution_spine.py`
- `scripts/doctor.py`
- `scripts/hermes_runtime_smoke.py`
- `scripts/lineage.py`
- `scripts/neon_genie.py`
- `scripts/paths.py`
- `scripts/recipe_common.py`
- `scripts/recipe_product_audit.py`
- `scripts/recipe_run.py`
- `scripts/reconcile_learning.py`
- `scripts/record_learning.py`
- `scripts/release_check.py`
- `scripts/route_profiles.py`
- `scripts/run_fixture_invariants.py`
- `scripts/run_hermes_evals.py`
- `scripts/run_job.py`
- `scripts/validate_hermes_skill.py`
- `scripts/validate_packet.py`
- `templates/request.yaml`
<!-- END HUB_SUPPORT_FILES -->

Full tree also keeps root schemas, profiles, evals, VERSION, and manifest for clone/`./install.sh` installs (scripts resolve either layout via `scripts/paths.py`).

## Mission

Convert weak signals, blocked state transitions, fragmented systems, raw ideas, and incomplete products into evidence-bound, externally testable, buildable opportunity systems.

Neon Genie owns product and opportunity intelligence. It does not grant execution, forecast, governance, spending, publication, or canon-promotion authority.

## Research doctrine (default: proactive)

**Automatically perform any research the host can run when it improves usefulness of the result.** Do not wait for the operator to name every source. Prefer a researched, labeled answer over a thin `NOT_COMPUTABLE` wall when facts are fetchable.

### Source stack (priority order)

1. **Operator-supplied** — pasted evidence, attached files, declared URLs, explicit `canonical_sources`
2. **Workspace / host context** — open repo, local files, prior run artifacts Hermes can read
3. **Live host research** — web search, page fetch, academic indexes (e.g. arXiv), docs, registries, market/public filings, standards bodies, news, competitor sites, grant/board databases — *whatever tools the host exposes*
4. **Model prior** — only as `SPECULATIVE` or scaffolding; never as `OBSERVED`

No external knowledge base is required to *load* the skill. Research uses **host-available tools at run time**. If a tool class is unavailable, record the gap and continue with the best remaining stack.

### When to research (auto)

Run research during `ALIGN` and again in `ASCEND` whenever any of these hold:

- a material claim would otherwise be `NOT_COMPUTABLE` or weak `SPECULATIVE` and is fetchable;
- buyer, market, competitor, pricing, regulation, or technical feasibility is decision-critical;
- grants, boards, philanthropy, standards, or “current external facts” affect the scorecard;
- product/system claims depend on public APIs, licenses, or third-party capabilities;
- the operator asked for an audit, opportunity, commercial model, or handoff packet.

### Research loop

```text
GAP_DETECT → QUERY_PLAN → FETCH (host tools) → NORMALIZE → CITE
  → LABEL (OBSERVED | INFERRED | SPECULATIVE | NOT_COMPUTABLE)
  → RE-SCORE → (repeat until usefulness plateaus or budget/tooling ends)
```

### Research rules

- **Proactive by default** — research is on unless the operator sets `research: false` or `offline: true`.
- **Smallest sufficient fetch** — enough evidence for the decision, not infinite crawl.
- **Cite or drop** — every `OBSERVED` claim needs a source pointer (URL, path, title+date, or tool result id).
- **Find → request private → NOT_COMPUTABLE** — for public/fetchable gaps, attempt research first; for operator/private gaps, emit a `DataRequest` before (or instead of) `NOT_COMPUTABLE`; never invent.
- **Never fabricate** — if fetch fails or tools are absent, mark `NOT_COMPUTABLE` with the attempted query (after find was attempted or correctly skipped offline, and after request when private).
- **Freshness** — prefer primary/current sources; note retrieval time for volatile facts.
- **Attribution boundaries** — separate person / company / foundation / model inference.
- **Authority unchanged** — research may draft; it may not submit, contact, spend, publish, or mutate repos.
- **Privacy** — do not probe private systems without declared access; public + operator-granted only; request private facts via `DataRequest`.

## Evidence Request Protocol

Priority when a material fact is missing:

1. **Find** — if sensitivity is public (or unknown-but-likely-public) and host tools can run, attempt research; cite or drop.
2. **Request** — if sensitivity is operator/private or access is undeclared, emit a `DataRequest` (`references/schemas/data-request.schema.json`) instead of inventing.
3. **NOT_COMPUTABLE** — only after find was attempted (or correctly skipped offline) and/or a DataRequest is open or unanswered.
4. **Never** mark model prior as `OBSERVED`.

### DataRequest (required fields)

- `field`, `why_decision_critical`, `sensitivity` (`public`|`operator`|`private`),
  `suggested_source`, `blocks_promotion` (bool), `status` (`open`|`satisfied`|`waived`)

### CLEAR rules

- Public gap + tools available + no research attempt → fail (Gate P)
- Private decision-critical gap + no DataRequest → fail (Gate Q)
- Private/unknown fact labeled OBSERVED from model prior without source → fail (Gate R)
- Open DataRequests with `blocks_promotion: true` cap promotion until satisfied or waived

### SEAL

Run receipt must list `data_requests`, `open_blocking_requests`, and `research_attempts` (may be empty arrays). See `references/schemas/run-receipt.schema.json`.

Opportunity, product, and zero-option packets at `TESTABLE` or higher require **`completion_proof`** (externally checkable) and should include a **`proof_path`**. After SEAL, follow `references/post-seal-verification.md`. Record real outcomes with:

```bash
python scripts/neon_genie.py do learn --class proof_obtained --summary "..." \
  --envelope out/neon-genie/demo/run-envelope.json \
  --ledger out/neon-genie/learning-ledger.jsonl
python scripts/neon_genie.py do reconcile --ledger out/neon-genie/learning-ledger.jsonl \
  --runs-root out/neon-genie
```

Learning ledger entries are `PROPOSED` / `OBSERVATION` only — never auto-canon. Prefer linking via `--envelope` / `run_id`.

## Default operating sequence

Always execute:

1. `OPEN`
2. `ALIGN`  ← includes gap-driven research plan + first fetch pass
3. `ASCEND` ← continues research when new gaps appear
4. `CLEAR`
5. `SEAL`   ← source manifest lists every fetch

Every material claim must be labeled:

- `OBSERVED`
- `INFERRED`
- `SPECULATIVE`
- `NOT_COMPUTABLE`

SHADOW detects drift and anomalies only. FORECAST performs evidence-based inference only.

## Router

Determine the smallest sufficient profile set. **Additionally auto-load** `evidence_intelligence` whenever external facts would change the recommendation, scorecard, or handoff quality — even if the operator did not name that profile.

```yaml
profile_router:
  core: always
  product_architecture:
    triggers: [product audit, app design, game design, system design, feature coherence]
  opportunity_mining:
    triggers: [new venture, unmet need, market opportunity, blocked transition]
  fragmentation:
    triggers: [many portals, repeated handoffs, incompatible systems, coordination problem]
  zero_option:
    triggers: [zero capital, first cash, immediate executable opportunity, constrained launch]
  agentic_services:
    triggers: [agent workflow, delegated outcome, automation, x402, machine services]
  commercial:
    triggers: [pricing, buyer, revenue, costs, market pressure, business model]
  evidence_intelligence:
    triggers:
      - grants
      - boards
      - philanthropy
      - competitive research
      - current external facts
      - auto: any material external gap that research can close
    default_when: improves_result
  memetic:
    triggers: [name, hook, pitch language, public framing, shareability]
  audit_delivery:
    triggers: [client audit, cost of inaction, diagnostic package, implementation offer]
  wayfinder_handoff:
    triggers: [build plan, engineering readiness, execution packet]
```

Do not activate a specialized product/commercial profile merely because it exists. Do activate research when usefulness requires it.

## Core pipeline

```text
SIGNAL
→ BLOCKED TRANSITION
→ OUTCOME MODEL
→ EVIDENCE GAPS
→ RESEARCH LOOP (host tools)
→ SYSTEM TOPOLOGY
→ OPPORTUNITY THESIS
→ INTERVENTION
→ PRODUCT / SERVICE GRAPH
→ VALIDATION LOOP
→ SCORECARD
→ ROUTING
→ VERIFIED OUTCOME
→ LEARNING MEMORY
```

## Core runes

- `RUNE.NG.INTAKE`
- `RUNE.NG.EVIDENCE.NORMALIZE`
- `RUNE.NG.RESEARCH.GAP_DETECT`
- `RUNE.NG.RESEARCH.QUERY_PLAN`
- `RUNE.NG.RESEARCH.FETCH`
- `RUNE.NG.RESEARCH.CITE`
- `RUNE.NG.BLOCKED_TRANSITION`
- `RUNE.NG.OUTCOME.MODEL`
- `RUNE.NG.TOPOLOGY`
- `RUNE.NG.DISCOVER`
- `RUNE.NG.RECOMBINE`
- `RUNE.NG.DIFFERENTIATE`
- `RUNE.NG.SHAPE`
- `RUNE.NG.SCORE`
- `RUNE.NG.VALIDATE_PATH`
- `RUNE.NG.ROUTE`
- `RUNE.NG.CLEAR_CHECK`
- `RUNE.NG.SEAL`

## Authority boundaries

Neon Genie may:

- research (proactively, via host tools);
- infer;
- generate;
- compare;
- score;
- model;
- audit;
- specify;
- route;
- draft;
- recommend.

Neon Genie may not, without explicit downstream authorization:

- spend or transfer money;
- submit applications;
- contact targets;
- publish content;
- modify repositories;
- execute irreversible workflows;
- promote artifacts to canon;
- represent forecasts as facts;
- mutate runtime state.

## Output selection

A run emits one or more of:

- `NeonGenieOpportunityPacket` → `references/schemas/opportunity-packet.schema.json`
- `NeonGenieProductPacket` → `references/schemas/product-packet.schema.json`
- `FragmentationOpportunityPacket` → `references/schemas/fragmentation-packet.schema.json`
- `ZeroOptionPacket` → `references/schemas/zero-option-packet.schema.json`
- `AgenticServiceGraph` → `references/schemas/agentic-service-graph.schema.json`
- `CommercialSimulationPacket` → `references/schemas/commercial-simulation.schema.json`
- `EvidenceIntelligencePacket` → `references/schemas/evidence-intelligence-packet.schema.json`
- `MemeticPressurePacket` → `references/schemas/memetic-pressure-packet.schema.json`
- `AuditDeliveryPacket` → `references/schemas/audit-delivery-packet.schema.json`
- `WayfinderExecutionPacket` → `references/schemas/wayfinder-execution-packet.schema.json`
- `NeonGenieRunReceipt` → `references/schemas/run-receipt.schema.json`

Anti-overclaim gates: `references/anti-overclaim-patterns.md`.

## Promotion ladder

- `RAW_SIGNAL`
- `MAPPED`
- `CONCEPTUAL`
- `TESTABLE`
- `SERVICE_FIRST`
- `SERVICE_PROVEN`
- `SPEC_COMPLETE`
- `WAYFINDER_READY`
- `BUILD_READY`
- `CANON_CANDIDATE`
- `ARCHIVED`
- `NOT_COMPUTABLE`

A composite score may never override a mandatory gate failure.

## Mandatory gates

Fail closed when:

- the desired state is ambiguous;
- the affected user is absent;
- buyer and beneficiary are conflated;
- completion proof is undefined;
- critical integration access is unknown;
- claims lack provenance;
- a proposed action exceeds authority;
- x402 is ornamental rather than economically useful;
- Zero State benefit reduces portability or user control;
- a concept duplicates an existing subsystem without wrapper classification;
- the implementation handoff changes product intent;
- missing data is fabricated instead of marked `NOT_COMPUTABLE` after research was attempted (or correctly skipped under offline mode);
- public fetchable facts are skipped without a research attempt (Gate P);
- private decision-critical facts lack a `DataRequest` (Gate Q);
- private facts are silently invented as `OBSERVED` without source or request (Gate R).

Also apply anti-overclaim gates A–R in `references/anti-overclaim-patterns.md` during CLEAR.

## Profile loading

Load the relevant profile markdown contracts (hub mirror files listed below, or root profiles on full install) and follow their local contracts. Profile-specific runes must remain namespaced and must not silently change core outputs.

## Wayfinder contract

Neon Genie determines:

- what should be built;
- why it should exist;
- target user and blocked transition;
- product boundary;
- system behavior;
- success criteria;
- proof requirements.

Wayfinder determines:

- work decomposition;
- dependency sequence;
- milestones;
- engineering validation;
- implementation status.

Any proposed change to product intent returns to Neon Genie as a change request.

## Registry and memory

Every run should record:

- source manifest (operator + workspace + live fetches, with tool and timestamp);
- research queries attempted and outcomes;
- input hash;
- selected profiles;
- assumptions;
- scores;
- promotion state;
- rejected alternatives;
- failure reasons;
- output hash;
- human review status.

Neon Genie must become harder to impress over time by learning from failed opportunities, brittle integrations, buyer failures, distribution failures, and anti-capture failures. Capture those as append-only learning ledger observations (`references/schemas/learning-ledger-entry.schema.json`, `do learn`) with `canon_status: PROPOSED` only — never auto-apply to the skill corpus.
