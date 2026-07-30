# Neon Genie Roadmap

Sibling skill to Kubrick: **shared architecture patterns**, separate domain (opportunity / product intelligence, not cinematic engineering).

## Wave 1 — Ops shell

Installable skill root, Hermes runtime contract, `install.sh`, VERSION/CHANGELOG/QUICKSTART, smoke validator, examples + evals skeleton, flatten nested package.

**Status:** shipped as **v3.2.0**.

## Wave 2 — Domain depth

- Thicken profile contracts
- Fill missing packet schemas (`EvidenceIntelligencePacket`, `MemeticPressurePacket`, `AuditDeliveryPacket`)
- Richer golden/eval cases and anti-overclaim references

**Status:** shipped as **v3.3.0**.

## Wave 3 — Thin operator surface

Optional packaging CLI only (no product brain in Python):

```text
python scripts/neon_genie.py do check | validate | route | receipt
```

**Status:** shipped as **v3.4.0**.

## Wave 4 — Release maturity

CI evals workflow, release checklist, version audit, fixture invariants.

**Status:** shipped as **v3.5.0**.

## Wave 5 — Executable evals + recipes

- Deterministic golden gate runner (`do eval`) comparing case `expected` to gate logic
- Product-audit packaging recipe (`do recipe`) for first operator path
- CI artifacts from recipe output

**Status:** shipped as **v3.6.0**.

## Wave 6 — Recipe family + deeper validation

- Multi-recipe dispatcher (product-audit, zero-option variants, fragmentation)
- Typed packet validation subset + strict authority checks
- Expanded golden gates (fictional resource, scorecard override ban)
- Sample packets for offline validation demos

**Status:** shipped as **v3.7.0**.

## Premiere program (post packaging waves)

See `docs/superpowers/specs/2026-07-30-neon-genie-premiere-program-design.md`.

| Wave | Status |
|------|--------|
| P0 Evidence spine | shipped **3.8.0** |
| P1 Prose excellence | shipped **3.9.0** (golden transcripts + `do transcripts`) |
| P2 Outcome density | shipped **3.10.0** (completion_proof, learn ledger, post-SEAL) |
| P3 Category ownership | shipped **3.11.0** (`docs/PREMIERE.md`, `docs/DEMO.md`, gallery) |

## Premiere program complete (P0–P3)

Public entry points:

- [PREMIERE.md](./PREMIERE.md) — thesis & comparison  
- [DEMO.md](./DEMO.md) — 10-minute path  
- [examples/gallery/](../examples/gallery/) — sanitized exemplars  

## Maintenance / corpus depth

| Release | Focus |
|---------|--------|
| **3.12.0** | Commercial + audit recipes; `do doctor` full-suite smoke |
| **3.13.0** | Agentic/memetic recipes; transcripts 06–07 (x402 / Gate D) |
| **3.14.0** | Evidence + opportunity recipes; transcripts 08–09 (10 recipes, 9 goldens) |
| **3.15.0** | README How to use; simplified `do <job>` help for agents & users |
| **3.16.0** | Hermes Skills Hub tap package (`skills/neon-genie/`) + distribution docs |
| **3.17.0** | Hub-hardened package: allowlisted mirrors + dual-path resolver so hub install runs `do doctor` |
| **3.18.0** | Distribution spine: single-source mirrors, generated hub contract, negative packaging tests, OS smoke matrix |
| **3.19.0** | Live Hermes behavioral verification: semantic invariants + isolated hub runtime smoke |
| **3.20.0** | Canonical run-envelope.json, lineage IDs, schema versioning policy |
| **3.21.0** | Operator surface: do run, do capabilities, worked example, tighter README |
| **3.22.0** | Feedback loop: issue templates, learn↔run_id reconcile, ADRs |
| **3.23.0** | Release automation, branch protection, gates.yaml registry, CONTRIBUTING |

## Production maturity (3.18–3.23) — complete

| Theme | Shipped |
|-------|---------|
| Distribution spine | `distribution.yaml`, mirrors, generated Hub support list |
| Runtime proof | Behavioral suite + hub-layout smoke (no LLM keys required) |
| Artifact protocol | Mandatory `run-envelope.json`, lineage IDs, schema versioning |
| Operator surface | `do run`, `do capabilities`, worked README example |
| Feedback loop | Issue templates, learn↔run_id reconcile, ADRs |
| Governance | Tag-driven Release workflow, branch protection, CONTRIBUTING |

Public entry: [README](../README.md) · [DEMO](./DEMO.md) · [HERMES_DISTRIBUTION](./HERMES_DISTRIBUTION.md) · [GOVERNANCE](./GOVERNANCE.md)

### Optional later (not blocking)

- Live multi-turn Hermes LLM evals (needs API keys / host tools in CI)
- Official Nous `optional-skills/` PR (only if desired)
- Discord/social announce with install one-liner
- Profile capability-contract router (beyond keyword routing)
- Required PR reviews when collaborators join

## Non-goals (all waves)

- Merging Neon Genie and Kubrick domains
- Auto-execution, spending, or canon promotion
- Shared monorepo runtime library (revisit only after Wave 2 if a third skill needs a scaffold)
