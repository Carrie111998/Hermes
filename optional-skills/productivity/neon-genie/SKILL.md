---
name: neon-genie
description: Build evidence-bound product and opportunity packets.
version: 3.23.0
author: Daniel Meyer (@scrimshawlife-ctrl), Applied Alchemy Labs / Zero State
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
    category: productivity
    related_skills: []
triggers:
  - neon genie
  - product audit
  - opportunity mining
  - zero option
  - wayfinder handoff
  - commercial simulation
  - evidence intelligence
---

# Neon Genie Skill

Neon Genie turns weak signals and blocked transitions into evidence-bound product and opportunity packets. It is **advisory only**: draft and recommend, never spend, publish, contact, or mutate repositories. Full monorepo/releases live upstream at https://github.com/scrimshawlife-ctrl/Neon-Genie-Hermes.

## When to Use

- Product audits, boundary definition, and Wayfinder-ready handoffs
- Opportunity mining, zero-capital first-cash loops, fragmentation scans
- Commercial framing when buyer/roles may be missing
- Evidence gaps that need research or a private `DataRequest`
- Fail-closed promotion decisions with claim labels

Do **not** use for cinematic work (use Kubrick), code execution, repo mutation, spending, or publication.

## Prerequisites

- Hermes Agent with optional skills enabled
- Python 3.11+ (stdlib packaging CLI; no pip install required)
- Optional: host research tools (`web_search`, `web_extract`) for public facts
- Optional: Wayfinder (or similar) as handoff consumer only

Profiles and schemas ship under `references/profiles/` and `references/schemas/`. Gate ontology: `references/gates.yaml`. Doctrine: `references/hermes-runtime-contract.md`, `references/anti-overclaim-patterns.md`.

## How to Run

Skill directory = folder containing this `SKILL.md`.

```bash
# Smoke after install
python scripts/neon_genie.py do doctor

# Operator packaging path (emits run-envelope.json)
python scripts/neon_genie.py do run --recipe product-audit --out out/neon-genie/demo

# From a brief or free text
python scripts/neon_genie.py do run --brief examples/product-audit.brief.yaml --out out/neon-genie/demo
python scripts/neon_genie.py do route --text "first cash zero capital" --json

# Validate envelope / packet
python scripts/neon_genie.py do validate --packet out/neon-genie/demo/run-envelope.json --type envelope --strict-authority

# Machine-readable surface
python scripts/neon_genie.py do capabilities --json
```

In chat: load the skill and describe the job in plain language. Always run **OPEN → ALIGN → ASCEND → CLEAR → SEAL**. Prefer the smallest profile set (`core` always).

Downstream consumers open **`run-envelope.json`** first, then `primary_artifact.path` and the receipt.

## Quick Reference

| Item | Value |
|------|--------|
| Authority | `advisory_only` — `grants_execution: false` |
| Claim labels | `OBSERVED` · `INFERRED` · `SPECULATIVE` · `NOT_COMPUTABLE` |
| Missing public fact | research via host tools, then cite or drop |
| Missing private fact | emit `DataRequest` (see `references/schemas/data-request.schema.json`) |
| Still missing | `NOT_COMPUTABLE` — never invent |
| Entry artifact | `run-envelope.json` |
| Profiles | `references/profiles/core.md` (and siblings in that directory) |
| Schemas | `references/schemas/run-envelope.schema.json` (and sibling schemas) |
| Gates | P/Q/R (research/request), D (memetic), G (proof/fiction), C (roles), AUTHORITY |

Recipes: `product-audit`, `zero-option`, `zero-option-executable`, `fragmentation`, `commercial`, `audit`, `agentic`, `memetic`, `evidence`, `opportunity`.

## Procedure

1. **OPEN** — Resolve request, actor, current/desired state, constraints, artifact type. State authority is advisory only.
2. **ALIGN** — Source stack: operator evidence → workspace → host research tools → model prior as `SPECULATIVE` only. Detect gaps; auto-load `evidence_intelligence` when external facts change the answer.
3. **ASCEND** — Topology, opportunity/product thesis, intervention, scorecard, route. Label every material claim.
4. **CLEAR** — Fail closed on authority leaks, uncited OBSERVED, missing DataRequest for private decision-critical fields, ornamental x402, missing completion proof at TESTABLE+, buyer/beneficiary conflation without evidence. Memetic strength never raises promotion past evidence failure (Gate D).
5. **SEAL** — Emit packet(s) + receipt + **`run-envelope.json`**. List `data_requests`, `research_attempts`, open blocking requests. For Wayfinder handoffs set `product_intent_changes_require_neon_genie_review: true`.

**Wayfinder boundary:** Neon Genie owns what/why/user/boundary/proof. Wayfinder owns decomposition/milestones/status. Intent changes return here.

## Pitfalls

- Treating model prior as `OBSERVED`
- Inventing buyers, capital, access, or credentials under zero-option constraints
- Granting spend/publish/mutate in packets or chat
- Letting Wayfinder rewrite product intent
- Skipping research on public fetchable gaps (Gate P) or inventing private facts without `DataRequest` (Gate Q/R)
- Promoting on memetic strength or scorecard alone when gates fail
- Opening many profiles “just because” instead of the smallest sufficient set

## Verification

```bash
python scripts/neon_genie.py do check
python scripts/neon_genie.py do doctor
python scripts/neon_genie.py do eval
python scripts/neon_genie.py do behavioral --suite
python scripts/neon_genie.py do run --recipe zero-option --out out/neon-genie/verify-zero
python scripts/neon_genie.py do validate --packet out/neon-genie/verify-zero/run-envelope.json --type envelope --strict-authority
```

Expect: doctor green; zero-option path `NOT_COMPUTABLE` without invented resources; every envelope has `authority: advisory_only` and `grants_execution: false`.

Upstream packaging, distribution spine, and releases: https://github.com/scrimshawlife-ctrl/Neon-Genie-Hermes
