# Changelog

All notable changes to Neon Genie are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses semantic versioning.

## [Unreleased]

### Changed

- Docs refresh: README, QUICKSTART, DEMO, ROADMAP aligned to operator surface,
  run-envelope, feedback loop, release automation, and production maturity (3.18–3.23)

## [3.23.0] — 2026-07-30

### Added

- **Release automation** (`.github/workflows/release.yml`)
  - Tag `vX.Y.Z` → release check, full smoke, hub tarball + sha256, GitHub release notes from CHANGELOG
- **`do release-check`** / `scripts/release_check.py` — version + changelog + dist gate
- **`references/gates.yaml`** — canonical gate ontology
- **CONTRIBUTING.md** + **docs/GOVERNANCE.md**
- Main branch protection: required status check `eval`, no force-push/delete; delete head branch on merge

## [3.22.0] — 2026-07-30

### Added

- **Feedback loop**
  - GitHub issue templates: behavior, hub install, operator outcome, schema proposal
  - `do learn --run-id` / `--envelope` + routing/gate quality fields
  - `do reconcile` — link learning ledger entries to `run_id` envelopes
  - Classes: `false_route`, `false_gate`
  - ADRs under `docs/adr/` (authority, skill brain, hub mirrors, Wayfinder, claims)
- Learning reconcile tests; policy: never auto-apply to canon

## [3.21.0] — 2026-07-30

### Added

- **`do run`** — operator packaging path (`scripts/run_job.py`)
  - `--recipe` / `--brief` / `--text` → workspace + receipt + envelope
  - Auto-recipe from brief profiles; `--no-auto-recipe` for scaffold-only
  - Writes `HERMES_NEXT.md` (judgment stays in SKILL.md)
- **`do capabilities --json`** — machine-readable surface for orchestrators
- Operator surface tests (`scripts/test_operator_surface.py`)
- README worked example (audio tool / missing buyer) + compressed operator docs

### Changed

- CLI help lists `run` and `capabilities` in everyday jobs

## [3.20.0] — 2026-07-30

### Added

- **Canonical run envelope** (`run-envelope.json`) for every packaging recipe
  - `schema_id` / `schema_version` (`neon-genie/run-envelope` @ `1.0.0`)
  - `run_id`, artifact lineage IDs, `content_hash`
  - `primary_artifact`, `mode_status`, `promotion`, `wayfinder` ingest hints
  - `do envelope` / `scripts/build_envelope.py` + `scripts/lineage.py`
- Schema versioning policy: `references/schema-versioning.md`
- Envelope tests (`scripts/test_run_envelope.py`); doctor validates envelope
- Validator support for JSON Schema `const` and `pattern`

### Changed

- Expanded `schemas/run-envelope.schema.json` (required entry point for consumers)
- `recipe_common.finish` always writes and checks the envelope

## [3.19.0] — 2026-07-30

### Added

- **Behavioral verification suite** (`evals/behavioral/`, `do behavioral`)
  - Six semantic cases: DataRequest, research attempt, NOT_COMPUTABLE, advisory-only mutation refusal, Gate D, Wayfinder change-control
  - `scripts/check_behavioral_invariants.py` (`NG-RUNTIME-*` diagnostics)
- **Isolated runtime smoke** (`do runtime` / `scripts/hermes_runtime_smoke.py`)
  - Hub-layout install + check + behavioral + doctor without LLM keys
  - Optional `--hermes` path with isolated `HERMES_HOME`
- CI steps for behavioral suite and runtime smoke; doctor runs behavioral
- Dual-path `evals_dir()` prefers layouts that include `cases/`

## [3.18.0] — 2026-07-30

### Added

- **Distribution spine** (`distribution.yaml` + `scripts/distribution_spine.py`)
  - Single-source Hub mirrors, package parity, generated SKILL.md support list
  - `do dist verify|write|report` with stable `NG-PKG-*` diagnostics
  - Negative packaging tests (`scripts/test_distribution_spine.py`)
- CI: distribution verify + negative tests; hub-layout doctor smoke; OS/Python smoke matrix
- Doctor runs distribution verify on full tree; skips on Hub installs

### Changed

- `sync_skill_package.sh` delegates to `distribution_spine.py write`
- SKILL.md hub support file list is generated (markers `BEGIN/END HUB_SUPPORT_FILES`)

## [3.17.0] — 2026-07-30

### Added

- Hub-hardened layout: mirrors under allowlisted dirs
  - `references/schemas/`, `references/profiles/`
  - `examples/evals/` (cases + transcripts)
  - `references/VERSION`, `references/manifest.json`
- `scripts/paths.py` dual-path resolver (full tree or hub mirrors)
- Explicit **Hermes Hub support files** index in `SKILL.md` so hub install pulls scripts, schemas, profiles, recipes, and golden tests
- `sync_skill_package.sh` refreshes hub mirrors before packaging

### Changed

- Packaging scripts resolve schemas/profiles/evals/manifest/VERSION via dual paths
- `validate_hermes_skill.py` accepts hub layout (skips full-tree-only docs/CI paths)
- Schema/profile path docs prefer `references/…` for hub portability

## [3.16.0] — 2026-07-30

### Added

- Hermes Skills Hub packaging: `skills/neon-genie/` tap package
- `skills.sh.json` Product Intelligence grouping
- `scripts/sync_skill_package.sh` to refresh the tap package
- `docs/HERMES_DISTRIBUTION.md` — install/tap/catalog submission paths
- SKILL frontmatter `metadata.hermes` (category, tags) for hub discovery

### Changed

- README/QUICKSTART install one-liners for `hermes skills install …`

## [3.15.0] — 2026-07-30

### Changed

- README: plain-English **How to use** (Hermes chat vs CLI), command cheat sheet, agent rules
- Simplified CLI help: jobs grouped (everyday / verify / outcomes)
- QUICKSTART aligned with the same command shape
- SKILL packaging section shortened to the `do <job>` table

## [3.14.0] — 2026-07-30

### Added

- Recipes: `evidence` (find/request scaffold) and `opportunity` (blocked transition + completion_proof)
- Briefs: `examples/evidence.brief.yaml`, `examples/opportunity.brief.yaml`
- Transcripts 08–09 (9 total goldens)
- Doctor/CI cover evidence + opportunity

### Changed

- Full profile-adjacent recipe surface (10 recipes)

## [3.13.0] — 2026-07-30

### Added

- Recipes: `agentic` (x402 REJECT scaffold) and `memetic` (Gate D promotion cap)
- Briefs: `examples/agentic.brief.yaml`, `examples/memetic.brief.yaml`
- Transcripts: `06-agentic-x402-misfit.md`, `07-memetic-cannot-promote.md` (7 total)
- Doctor suite smokes agentic + memetic recipes

### Changed

- Gallery/examples indexes; ROADMAP maintenance 3.13.0

## [3.12.0] — 2026-07-30

### Added

- Corpus depth: `commercial` and `audit` packaging recipes + briefs
- `do doctor` full-suite operator smoke (`scripts/doctor.py`)
- Gallery/examples index updates for new recipes

### Changed

- CI runs commercial/audit recipes and doctor suite
- ROADMAP maintenance section documents 3.12.0

## [3.11.0] — 2026-07-30

### Added

- Category ownership (Wave P3): `docs/PREMIERE.md` thesis & comparison vs idea agents
- `docs/DEMO.md` — 10-minute install → check → eval → recipe → learn path
- `examples/gallery/README.md` — sanitized briefs, packets, transcripts index
- README short comparison table + premiere/demo nav links
- RELEASE-CHECKLIST-v3.11

### Changed

- Premiere program P0–P3 marked complete on ROADMAP and design spec
- Skill validator requires PREMIERE.md, DEMO.md, gallery README

## [3.10.0] — 2026-07-30

### Added

- Outcome density (Wave P2): `completion_proof` required on opportunity/product/zero-option schemas
- `proof_path` property on those packets; recipes emit proof paths
- Learning ledger: `schemas/learning-ledger-entry.schema.json`, `do learn` / `record_learning.py` (PROPOSED only)
- `references/post-seal-verification.md` checklist
- Golden evals: completion-proof required/present (16 total)

### Changed

- SKILL SEAL/memory doctrine ties packets to proof + ledger
- Docs/ROADMAP premiere P2 marked shipping

## [3.9.0] — 2026-07-30

### Added

- Golden prose transcripts (`evals/transcripts/`) for five premiere scenarios
- Transcript rubric + structural checker: `do transcripts` / `scripts/check_transcripts.py`
- CI step for transcript checks
- Docs: premiere positioning in README, RELEASE-CHECKLIST-v3.8/v3.9, docs index

### Changed

- ROADMAP: P0 shipped, P1 shipped as 3.9.0
- Premiere design spec: P0 success criteria checked off

## [3.8.0] — 2026-07-30

### Added

- Evidence Request Protocol: find public → request private → only then `NOT_COMPUTABLE`
- `schemas/data-request.schema.json` and `examples/packets/sample-data-request.json`
- Anti-overclaim gates **P–R** (skip-find, skip-request, silent private invent)
- Receipt evidence fields: `data_requests`, `open_blocking_requests`, `research_attempts`, `evidence_protocol`
- `build_receipt.py --data-requests`; product-audit recipe surfaces open DataRequests
- Golden evals: public-gap find (P), private-gap request (Q), silent invent (R) — 14 total cases
- Premiere program roadmap section (P0–P3)

### Changed

- Skill doctrine, Hermes runtime contract, and CAPABILITY_MAP gates **A–R**
- Skill validator requires `schemas/data-request.schema.json`

## [3.7.0] — 2026-07-30

### Added

- Multi-recipe runner: `do recipe --name product-audit|zero-option|zero-option-executable|fragmentation`
- `scripts/recipe_common.py`, `scripts/recipe_run.py`
- Briefs: `fragmentation.brief.yaml`, `zero-option-with-skills.brief.yaml`
- Sample packets under `examples/packets/`
- Deeper packet validator (typed JSON Schema subset + `--strict-authority`)
- Eval cases: `fictional-resource`, `scorecard-cannot-override-gate`

### Changed

- `do recipe` defaults to multi-recipe dispatcher (product-audit still default name)
- CI runs all packaging recipes + sample packet validation

## [3.6.0] — 2026-07-30

### Added

- Golden gate eval runner: `python scripts/neon_genie.py do eval` (`scripts/run_hermes_evals.py`)
- Product-audit packaging recipe: `python scripts/neon_genie.py do recipe`
- CLI intents `eval` and `recipe`; aliases `run-evals`, `product-audit`
- CI runs golden evals + recipe; uploads `out/neon-genie/` artifacts
- `.gitignore` for generated `out/`

### Notes

- Eval runner enforces packaging-level gate logic against `evals/cases/*` expected fields
- Recipe emits route + receipt + Wayfinder handoff stub (no product invention)

## [3.5.0] — 2026-07-30

### Added

- GitHub Actions: `.github/workflows/hermes-evals.yml` (check, CLI tests, fixtures, version audit)
- `scripts/run_fixture_invariants.py`
- `scripts/audit_release_version.py`
- `docs/RELEASE-CHECKLIST-v3.5.md`

### Changed

- Roadmap Wave 4 marked shipped

## [3.4.0] — 2026-07-30

### Added

- Thin packaging CLI: `python scripts/neon_genie.py do <check|validate|route|receipt>`
- `scripts/validate_packet.py`, `route_profiles.py`, `build_receipt.py`
- `scripts/test_wave3_cli.py` regression harness

### Notes

- CLI is packaging-only (no product brain, no research, no execution authority)

## [3.3.0] — 2026-07-30

### Added

- Packet schemas: `evidence-intelligence`, `memetic-pressure`, `audit-delivery`
- `references/anti-overclaim-patterns.md` (gates A–O)
- Eval cases: memetic promote block, offline fabrication, buyer conflation, authority leakage
- Thickened profile contracts (triggers, required fields, CLEAR rules, schema pointers)

### Changed

- Profiles expanded for operational clarity without changing mission or authority
- SKILL output selection links all packet schemas

## [3.2.0] — 2026-07-30

### Added

- Root-level Hermes skill packaging (installable skill root)
- `install.sh` → `~/.hermes/skills/neon-genie`
- `references/hermes-runtime-contract.md`
- `scripts/validate_hermes_skill.py` smoke validator
- `VERSION`, `QUICKSTART.md`, `docs/ROADMAP.md`, `docs/README.md`
- `examples/` operator briefs
- `evals/` fixture home + rubric skeleton

### Changed

- Flattened nested `neon-genie/` package to repository root
- Version alignment across `VERSION`, `SKILL.md`, and `manifest.json`

### Migration

- Replace `cp -R neon-genie …` with `./install.sh` or copy the **repository root** into Hermes skills

## [3.1.0] — prior

- Proactive research by default
- MIT license
- Packaging README polish and social assets
