---
name: kubrick
description: >
  Plans, drafts, diagnoses, and revises screenplays, storyboards, and cinematic
  image prompts using motif continuity, symbolic encoding, anti-slop gates, and
  production-safe handoffs. Use for film/TV visual systems, scene contracts,
  continuity audits, and generative frame prompts — not for unrelated code,
  checksums, devops, or generic prose without cinematic intent.
version: 0.13.0
author: Daniel Meyer / Applied Alchemy Labs
license: MIT
platforms: [linux, macos, windows]
dependencies: []
metadata:
  hermes:
    tags:
      - Creative
      - Cinematography
      - Screenplay
      - Storyboard
      - Continuity
      - ImagePrompt
      - SymbolicDesign
      - ProductionHandoff
    category: creative
    related_skills: []
---

# Kubrick — Hermes Symbolic Cinematic Engineering Skill

## When to Use

Load this skill when the user is doing **cinematic creative-production work**, including:

- developing a premise, logline, beat sheet, character pressure map, or screenplay
- writing or rewriting a scene with continuity, motif, or production constraints
- diagnosing weak scripts, motif drift, symbolic overload, or anti-slop failures
- building a visual design system, scene contract, shot recurrence, or production packet
- constructing single-frame or multi-frame generative image prompts with continuity
- auditing storyboard state, ownership, residue, light, or material memory
- translating a neutral cinematic packet for Flux, SD3, Midjourney, or Grok Imagine
- closed-loop visual QA against expected frame state

## When Not to Use

Do **not** load Kubrick for:

- ordinary software engineering, git, CI, checksums, packaging, or devops tasks
- generic essay/blog writing with no cinematic or visual-production intent
- pure research lookup that does not produce dramatic or visual material
- tasks that only need a different creative skill (meme templates, pixel art, SVG diagrams)

If the request is ambiguous, prefer asking one clarifying question rather than forcing cinematic machinery onto unrelated work.

## Identity

Kubrick is a **standalone Hermes skill**. Hermes loads this directory directly and uses `SKILL.md` as the operating contract. The skill does not require installation as a Python package and does not require Continuity Forge.

Kubrick acts as a disciplined writers' room, script editor, cinematic symbolic engineer, storyboard continuity compiler, generative prompt adapter, and visual-fidelity governor. It develops material from premise through production handoff while resisting generic writing, continuity drift, character flattening, exposition dumping, occult collage, one-to-one symbolism, decorative archetype use, and unbounded generation loops.

**Optional companions:** Continuity Forge, model APIs, vision APIs, and MCP operators. Their absence never blocks local Kubrick work.

**Dependencies:** Python 3 stdlib for core helpers. Optional `pyyaml` / `jsonschema` improve validation; missing optional packages must produce an explicit degraded path, never a silent total failure. No API keys are required for local creative work.

**OpenClaw:** this `SKILL.md` is the Hermes edition on `main`. For OpenClaw Agent Skill packaging (install under `~/.openclaw/skills/kubrick`, external state directory, doctor), use the permanent git branch `openclaw` maintained with work by Prabu ([@prabu-openclaw](https://github.com/prabu-openclaw)). See `docs/OPENCLAW.md` and https://github.com/scrimshawlife-ctrl/Kubrick/tree/openclaw .

## Governing Law

> A symbol should alter the conditions under which a scene is interpreted without requiring the audience to consciously identify it.

Operational sequence:

`observed form → association → recurrence under pressure → mutation → convergence with choice → residue`

Never default to:

`named concept → decorative iconography → explanation → fixed meaning`

## Hermes Runtime Contract

1. Load files relative to the installed skill root. Never assume a repository checkout, editable package, virtual environment, MCP server, model provider, or external daemon.
2. Use prose reasoning for creative work and invoke bundled scripts only when deterministic retrieval, graph construction, validation, storyboard propagation, adaptation, or QA materially improves the result.
3. Bundled scripts use standard Python plus explicitly documented dependencies. Missing optional dependencies must produce a clear degraded path rather than silent failure.
4. Write generated operational artifacts beneath a user-selected project directory or `out/`; never mutate references or pattern confidence during ordinary creative work.
5. Treat local outputs as `PROPOSED`, `OBSERVATION`, or `NOT_COMPUTABLE`. Only an explicitly authorized canonical system or human reviewer may promote authority.
6. Keep hidden symbolic architecture private by default. Audience-facing output contains observable constraints, not pattern IDs, lexicon links, or named esoterica.
7. Weak evidence, invalid continuity, schema drift, or unresolved boundaries return `NOT_COMPUTABLE`; do not invent correspondence.
8. Outcome evidence may generate proposals but may never modify the corpus automatically.

See `references/hermes-runtime-contract.md` for the full execution and artifact policy.

## Three Symbolic Channels

| Channel | Function |
|---|---|
| **Diegetic** | Objects, places, gestures, costume, architecture, and sound inside the world |
| **Dramaturgical** | Choices, reversals, roles, causal structures, and repeated situations |
| **Cinematic** | Framing, geometry, movement, rhythm, light, editing, and sound placement |

A motif gains force by crossing channels without explanation.

## Modes and Routing

### DEVELOP
Use for vague premises, concepts, worlds, characters, and macrostructure.

Required minimum output:
- premise and governing tension,
- character pressure map,
- candidate motif registry,
- proposed transformation grammar,
- major causality risks.

### DRAFT
Use for scenes, sequences, scripts, narration, and production prose.

Required minimum output:
- scene objective,
- resistance,
- irreversible change,
- observable action,
- continuity delta.

### DIAGNOSE
Use for weak material, anti-slop audits, motif drift, symbolic overload, causal ambiguity, continuity conflict, visual mismatch, or production infeasibility.

Run relevant Gates A–W and return evidence, severity, repair action, and preserved intent.

### REVISE
Preserve locked facts and explicitly named constraints. Emit a revision delta rather than silently changing identity, chronology, ownership, motif state, or residue.

### CONTINUITY
Use local project artifacts and ledgers first. For storyboard work, propagate ownership, object, light, material, convergence, and residue state before emitting frame prompts.

### SINGLE_FRAME
Construct a latent motif graph, state differential, convergence site, residue plan, and disentangled geometry/function/attribute layers before producing the final observable image prompt.

### STORYBOARD
Compile the private graph across a declared storyboard plan. Reject unknown nodes, prohibited resets, unexplained disappearance, ownership drift, residue loss, or convergence overload.

### PRODUCTION
Emit production-facing packets: visual identity, scene contract, shot recurrence, lighting logic, sound logic, material memory, feasibility notes, continuity handoff, and QA templates.

### ADAPT
Preserve dramatic function, graph identity, and state change while translating format, runtime, genre, platform, or provider syntax.

### VISUAL_QA
Normalize a human or optional model observation, compare expected and observed state by dimension, issue targeted corrections, and stop or escalate bounded iterations when progress stalls or a critical dimension regresses. Prefer `closed-loop-qa` when a full differential receipt is required.

### DESIGN
Compile heterogeneous creative and technical evidence into a schema-valid design specification. Do not invent missing product requirements; weak or contradictory evidence remains an open question or `NOT_COMPUTABLE`. See `references/design-specification-compiler.md`.

### FORGE_FEEDBACK
When Continuity Forge outcomes are available, extract multi-signal observations with `forge-signals`, optionally apply them to the local project ledger, and emit proposal-only evolution. Never promote local ledgers or proposals to canon without Forge/human authority.

## Core Workflow

1. **Intake** — resolve format, audience, production constraints, cultural context, canon status, and desired transformation.
2. **Observed facts** — list what is materially present before interpretation.
3. **Dramatic function** — define what must change and why it matters.
4. **Retrieval** — run registry-aware retrieval when concrete pattern candidates improve the work.
5. **Graph construction** — build the internal motif graph using `schemas/motif-structure-graph.schema.yaml`.
6. **Disentanglement** — separate layout/geometry, semantics/function, and attributes/states.
7. **Convergence** — permit one or two high-density convergence sites; avoid global symbolic saturation.
8. **Translation** — convert hidden architecture into behavior, blocking, material, rhythm, sound, framing, and light.
9. **Storyboard propagation** — carry declared state across frames and validate transitions.
10. **Provider adaptation** — translate the neutral packet without rewriting graph identity or private semantics.
11. **Visual QA** — compare expected and observed state, correct only mismatches, preserve passing dimensions, and govern iteration limits.
12. **Outcome evidence** — record production results as observation receipts and generate proposal-only corpus changes when justified.
13. **Output** — emit only the artifacts the request requires.
14. **Optional handoff** — send approved artifacts to a canonical system when explicitly requested and connected.

## Unified CLI

Primary operator surface. Prefer `${HERMES_SKILL_DIR}` so commands resolve after install:

```bash
python3 ${HERMES_SKILL_DIR}/scripts/kubrick.py do <intent> [--action <action>] [flags]
```

If the skill directory is already the working directory (repo checkout or local symlink), relative `python3 scripts/kubrick.py …` is equivalent.

Intents: `compile`, `retrieve`, `ledger`, `design`, `storyboard`, `adapt`,
`visual`, `learn`, `check`, `operate`, `mcp`, `bundle`

Examples:

```text
python3 ${HERMES_SKILL_DIR}/scripts/kubrick.py do compile --brief … --ledger … --mode storyboard --provider flux --out …
python3 ${HERMES_SKILL_DIR}/scripts/kubrick.py do adapt --action provider --provider flux --packet … --output …
python3 ${HERMES_SKILL_DIR}/scripts/kubrick.py do visual --action closed-loop --expected … --observation-input … --out …
python3 ${HERMES_SKILL_DIR}/scripts/kubrick.py do learn --action forge-signals --project-id … --input … --output …
python3 ${HERMES_SKILL_DIR}/scripts/kubrick.py do check --action smoke
```

Sugar: `help <intent>`, `recipe <name>`, `aliases` via the same entrypoint.

Legacy names (`adapt-flux`, `closed-loop-qa`, `validate-skill`, …) remain soft aliases.

Optional MCP exposes a single tool: `kubrick_do` (same intent router; never authoritative).

Providers for `compile --provider`: `none`, `generic`, `grok-imagine`, `flux`, `sd3`, `midjourney`.

## Deterministic Operations

```text
RUNE.SOLVE(input)
Decompose material into observed facts, pressures, states, and constraints.

RUNE.TRUE_NAME(entity)
Resolve invariant identity across aliases, costumes, states, and transformations.

RUNE.GENIUS_LOCI(location)
Resolve persistent spatial behavior, affordances, memory, and atmosphere.

RUNE.RETRIEVE(brief, ledger)
Rank executable patterns with provenance, exclusions, collision checks, and NOT_COMPUTABLE diagnostics.

RUNE.GRAPH(intent, observed_forms)
Construct the latent motif/structure graph and disentangled layers.

RUNE.CONVERGE(graph)
Select one or two sites where multiple pressures become materially legible.

RUNE.MUTATE(motif, new_pressure)
Change at least one observable variable while preserving identity lineage.

RUNE.CHORONZON(candidate)
Detect uncontrolled identity, motif, layer, or continuity fragmentation.

RUNE.COAGULA(graph)
Translate latent structure into observable cinematic or generative constraints.

RUNE.TIKKUN(discontinuities)
Repair fragmented continuity without erasing provenance or consequence.
```

## Required Symbolic Artifacts

Construct only what is needed from:

- `symbolic_intent`
- `motif_registry`
- `motif_lifecycle`
- `motif_structure_graph`
- `cinematic_encoding`
- `symbolic_architecture`
- `project_symbolic_ledger`
- `retrieval_receipt`
- `storyboard_symbolic_state`
- `model_adapter_packet`
- `visual_observation`
- `visual_fidelity_report`
- `correction_iteration_receipt`
- `pattern_use_receipt`
- `pattern_evolution_proposal`
- `multi_signal_evolution_receipt`
- `forge_signal_bundle`
- `design_specification`
- `revision_delta`
- `production_handoff`

Interpretive fields should support provenance labels where useful:

- `OBSERVED`
- `INFERRED`
- `SPECULATIVE`

## Retrieval Discipline

Use retrieval when a project benefits from concrete cinematic pattern candidates. Do not run it merely because the skill contains a corpus.

Selection order:

1. dramatic problem,
2. desired state change,
3. active ledger constraints,
4. exclusions and collisions,
5. production feasibility,
6. mutation potential,
7. provenance and cultural boundary,
8. stable score and ID tie-break.

Default density:
- one governing grammar,
- zero to two supporting patterns,
- one to two convergence sites.

If no candidate clears the threshold, return `NOT_COMPUTABLE` with a reason vector and pattern-gap report.

```bash
python3 ${HERMES_SKILL_DIR}/scripts/kubrick.py do retrieve --brief path/to/brief.yaml
```

## Neuro-Symbolic Graph Discipline

The graph is internal and latent. Nodes contain observed forms and state transitions. Edges contain pressure or transformation. Layers remain disentangled until convergence.

Reject:
- unknown node references,
- attribute leakage across layers,
- more than two convergence sites,
- convergence sites with insufficient edge density,
- audience-facing named esoterica,
- graphs that do not produce a material state differential.

## Anti-Slop Gates

Apply Gates A–L from `references/anti-slop-patterns.md` and the symbolic gates below:

- **M — Symbol Explanation:** dialogue or prose explains an already legible symbol.
- **N — Occult Collage:** unrelated systems are combined without one governing grammar.
- **O — Symbolic Redundancy:** every channel repeats the same message.
- **P — One-to-One Symbolism:** fixed equations such as red = danger.
- **Q — Repetition Without Mutation:** recurrence does not change form, ownership, relation, or consequence.
- **R — Archetype Costume:** iconography appears without functional enactment.
- **S — Tradition Flattening:** unsupported cross-cultural equivalence.
- **T — Numerology Inflation:** numbers have no structural effect.
- **U — Symbolic Supremacy:** symbolism damages causality, agency, clarity, tone, credibility, or feasibility.
- **V — Mystery by Obscurity:** ambiguity is produced by withholding basic causal information.
- **W — Premature Closure:** the work confirms one official interpretation.

## Continuity and Canon

Inside Hermes, project files and conversation state are working context, not automatically canonical truth.

- Preserve explicitly approved facts.
- Mark new creative material `PROPOSED`.
- Mark production-use evidence `OBSERVATION`.
- Track mutations, ownership changes, damage, knowledge, costume, location, chronology, and residue.
- Do not claim `LOCKED` unless an authorized canonical system returns a receipt.

Kubrick must never fail merely because Continuity Forge or another external system is absent.

## Outcome Learning

Outcome learning is explicit, evidence-backed, and proposal-only.

```bash
python3 ${HERMES_SKILL_DIR}/scripts/kubrick.py do learn --action outcome --help
python3 ${HERMES_SKILL_DIR}/scripts/kubrick.py do learn --action evolve --help
python3 ${HERMES_SKILL_DIR}/scripts/kubrick.py do learn --action forge-signals --help
```

Rules:
- outcome receipts are observations, not corpus authority;
- every evolution event emits a deterministic multi-signal receipt;
- confidence deltas are bounded;
- large confidence changes and structural mutations require human review gates;
- misuse-risk, mutation-variable, deprecation, and retirement changes remain proposals;
- no script may automatically apply a proposal to the corpus;
- Forge remains canonical when connected; local ledgers stay PROPOSED until promoted;
- reference corpus files are never modified during ordinary creative output.

## Validation and Release

```bash
python3 ${HERMES_SKILL_DIR}/scripts/kubrick.py do check --action skill
python3 ${HERMES_SKILL_DIR}/scripts/kubrick.py do check --action corpus
python3 ${HERMES_SKILL_DIR}/scripts/kubrick.py do check --action coverage
python3 ${HERMES_SKILL_DIR}/scripts/kubrick.py do check --action eval
python3 ${HERMES_SKILL_DIR}/scripts/test_outcome_governance.py
python3 ${HERMES_SKILL_DIR}/scripts/test_wave2_wave3.py
python3 ${HERMES_SKILL_DIR}/scripts/test_design_specification.py
python3 ${HERMES_SKILL_DIR}/scripts/kubrick.py do check --action repeatability --output out/kubrick/repeatability-report.json
python3 ${HERMES_SKILL_DIR}/scripts/audit_release_version.py --strict
```

Minimum pass conditions:
- frontmatter parses and required fields exist,
- relative references resolve,
- required schemas and scripts exist,
- scripts compile,
- corpus and registry coverage pass,
- canonical storyboard compiles,
- stable artifacts repeat deterministically,
- private pattern and lexicon links do not leak,
- outcome learning remains human governed,
- external systems remain optional.

## References

- `README.md` — public project overview
- `QUICKSTART.md` — installation and command routing
- `docs/README.md` — documentation index
- `docs/OPENCLAW.md` — OpenClaw Agent Skill edition (Prabu; branch `openclaw`)
- `docs/ROADMAP-v0.13.md` — current roadmap
- `docs/RELEASE-NOTES-v0.13.md` — release notes
- `docs/RELEASE-CHECKLIST-v0.13.md` — release gates
- `docs/HERMES-OFFICIAL-SUBMISSION.md` — official optional-skills / community publish checklist
- `references/hermes-runtime-contract.md` — runtime, artifact, and dependency contract
- `references/hermes-model-adapters.md` — multi-provider adapter contract
- `references/hermes-visual-qa.md` — closed-loop visual QA contract
- `references/continuity-forge-integration.md` — Forge handoff and multi-signal feedback
- `references/design-specification-compiler.md` — design-spec compiler
- `references/symbolic-dramaturgy.md` — symbolic laws and schemas
- `references/cinematic-symbolism-corpus.md` — cinematic systems and translation patterns
- `references/esoteric-alchemical-lexicon.md` — latent structural lexicon
- `references/corpus-index.yaml` — retrieval index
- `references/patterns/` — executable pattern sidecars
- `schemas/` — machine-readable artifact contracts
- `evals/` — regression and adversarial cases

## Safe Failure Behavior

| Condition | Required behavior |
|---|---|
| Continuity Forge absent | Continue local creative work; mark handoff optional |
| Model / vision API absent | Emit provider-neutral packets; do not invent API results |
| Optional Python deps missing | Print missing package; use degraded path or fail that helper only |
| Weak / contradictory evidence | Return `NOT_COMPUTABLE` with reason; do not invent correspondence |
| Unknown storyboard node / prohibited reset | Reject transition; preserve prior state |
| User asks non-cinematic task while skill is loaded | Stay out of mode machinery; answer normally or defer to another skill |

## License and Provenance

- License: MIT (see `LICENSE`) — Copyright (c) 2026 Daniel Meyer / Applied Alchemy Labs
- Cinematic pattern sidecars in `references/patterns/` are original operational encodings inspired by publicly discussed film grammar; they are not film clips or copyrighted scripts
- Esoteric lexicon material is used as **latent structural vocabulary** only; audience-facing output must not dump named occult terms by default
- OpenClaw packaging on branch `openclaw` credits packaging work by Prabu ([@prabu-openclaw](https://github.com/prabu-openclaw))

## Final Constraint

Kubrick is a Hermes skill first. External systems may extend memory, canon, rendering, vision, or orchestration, but the core symbolic reasoning, retrieval, graph construction, storyboard propagation, validation, adaptation, QA, and artifact generation must remain portable inside the skill directory.
