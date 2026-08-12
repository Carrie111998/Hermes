---
name: lofoten-stockfish-harvest
description: "Use when harvesting durable research corpora (web + memory gates + grounded citations + creative synthesis + publish). Lofoten-inspired preservation for long-term agent knowledge."
version: 1.0.0
author: Team Stockfish Harvesters (Hermes)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [research, harvest, durable, preservation, citations, memory-gates, synthesis, publish, lofoten, stockfish]
    category: autonomous-ai-agents
    related_skills: [grounded-citations, persistence-evolution-framework, long-horizon-agentic-workflows, agentic-test-campaigns, hermes-agent]
---

# Lofoten Stockfish Harvest

Durable research harvester inspired by Lofoten's centuries-old stockfish tradition: air-drying (preserving) the "catch" of web research without salt (no lossy compression), using memory gates, citation chains, creative synthesis weave, and publish hooks. Produces verifiable, evolvable, cross-session research artifacts that survive token limits, network fails, and agent restarts.

## Overview

Traditional research in agents is ephemeral: search once, summarize, lose provenance, drift on next session. This skill treats research as **stockfish harvest**:

- **Catch**: Systematic web research via web_search/web_extract/browser + grounded-citations ledger.
- **Cure/Dry (Preserve)**: Memory gates (atomic memory ops + CHECKPOINT.md + event.log), durable state via persistence patterns.
- **Weave**: Creative synthesis that interleaves facts, quotes, narrative (Lofoten saga-style) without hallucinating.
- **Package**: Structured packets (JSON + MD + hashes) ready for publish hooks (clearinghouse, vault mirror, skill promotion).
- **Gates**: Every admission to durable store requires evidence + hash + (optional) reviewer verdict.

Built on `grounded-citations`, `persistence-evolution-framework`, `long-horizon-agentic-workflows`. Addresses gaps in research durability for multi-wave campaigns.

## When to Use

- Long-running research missions (Lofoten corpus, tech landscape, competitive intel) where results must outlive the session.
- When you need **verifiable provenance** (every claim cited or [unverified]) + creative but grounded synthesis.
- Before publishing reports, populating MEMORY/skills, or feeding downstream agents (e.g. blog writers, planners).
- In agentic test campaigns or self-evolution loops that consume research.
- "Harvest the X domain with full citations and preserve for reuse."

**Don't use for:**
- One-off quick facts (use plain web_search + answer).
- Pure creative writing without sources.
- Real-time monitoring (use cron + lighter skills).

## Core Workflow (Harvest → Cure → Weave → Gate → Package)

1. **Prepare Ledger & State** (always first)
   - Reset grounded-citations ledger (or reuse stable one with --ledger).
   - Load/ create CHECKPOINT.md + research/ subdir in working tree or .hermes/plans/<slug>/
   - Use todo for phases.

2. **Catch Phase** (fan-out research)
   - Use web_search, web_extract, browser_navigate (with screenshots via vision if visual needed).
   - Immediately `sources.py add` or ingest every URL.
   - Record raw in research/raw/ + hashes.
   - Parallel via delegation if large (see durable-delegation-gates).

3. **Cure/Preserve (Memory Gates)**
   - After each batch: write atomic memory entries (keyed by topic+wave+id).
   - Append to CHECKPOINT.md with Kt (ι intent, ot objective, ct constraints e.g. "Lofoten-grounded only", vt verification=hashes+citations, Xt).
   - Use `persistence-evolution-framework` patterns: event.log, JSONL metrics (tokens, sources_added, gate_pass).
   - Gate: only persist if sources.py verify passes + sha256 of packet matches.

4. **Weave Phase (Creative Synthesis)**
   - Synthesize using only ledger-cited material + explicit [unverified] for gaps.
   - Weave Lofoten-inspired narrative: saga-style episodic, place-tied, resilient (e.g. "As the stockfish racks in Henningsvær dry in the Arctic wind... the facts hold").
   - Interleave verbatim quotes (from grounded-citations quote).
   - Produce layered outputs: CORPUS.md (raw facts), SYNTHESIS.md (narrative + analysis), PACKET.json (machine).

5. **Gate & Verify**
   - Run `sources.py verify --evidence --min-coverage 0.7`
   - Optional independent reviewer delegate with JSON verdict schema (see agentic-test-campaigns).
   - On pass: commit to memory + vault mirror + event.log + update CHECKPOINT with retained_state_edits.
   - On fail: retain only, log scar, retry catch or pivot (ct).

6. **Package & Publish Hooks**
   - Build packet: {wave, timestamp, ledger_snapshot, synthesis, hashes, metrics}
   - Hooks:
     - Mirror to /home/mikesai1/JeffVault/research/lofoten-stockfish/<slug>/
     - Optionally promote key facts to MEMORY or new skill (via skill_manage after human gate).
     - Publish-ready: generate blog stub or use smf-clearinghouse-publish patterns.
     - Optional: cron hook for incremental harvest waves.

7. **Recovery & Continuity**
   - On restart/drift: session_search + load CHECKPOINT + ledger (ids stable).
   - Use persistence patterns for resume: "last complete wave N, resume from ot=..."

## Key Artifacts & Locations

- `research/LOFOTEN_CORPUS.md` (or per-topic)
- `research/PACKET-<wave>.json` + .md
- `.hermes/plans/<date>-lofoten-harvest/CHECKPOINT.md`
- `~/.hermes/cache/citations/ledger.json` (or --ledger override)
- Vault mirror: `~/JeffVault/reports/lofoten-harvest-<slug>/`
- Metrics: `logs/harvest-metrics.jsonl`
- Hashes: sha256sum on all packets for drift detection.

## Commands & Recipes (Copy-Paste)

```bash
# Fresh harvest (in working dir or .hermes context)
python3 ~/.hermes/skills/research/grounded-citations/scripts/sources.py reset
mkdir -p research logs .hermes/plans/2026-08-12-lofoten-harvest

# Phase: Catch (example)
hermes chat -q "Research Lofoten stockfish tradition, history, modern sustainability, climate impacts. Use web tools and cite everything." --skills "lofoten-stockfish-harvest,grounded-citations" -Q

# After tool calls, register:
python3 .../sources.py add <url-from-tool> --title "..."
# Or ingest batch
python3 .../sources.py ingest web-results.json

# Weave + gate
python3 .../sources.py verify research/draft.md --evidence --min-coverage 0.65
python3 .../sources.py render --cited-in research/draft.md --replace-in research/SYNTHESIS.md

# Preserve gate (example in session)
# Write to memory (use memory tool)
# Append CHECKPOINT
# sha256sum research/PACKET-001.json >> event.log

# Incremental wave via cron (durable)
hermes cron create "0 */4 * * *" --name lofoten-harvest-wave --skills "lofoten-stockfish-harvest,grounded-citations,persistence-evolution-framework" --prompt "Resume from CHECKPOINT.md. Execute next harvest objective. Gate all durable writes. Output metrics + packet hash."

# Mirror + publish prep
cp -r research/ ~/JeffVault/research/lofoten-stockfish-harvest-2026-08-12/
# Then use publish hook skill if available.
```

## Integration with Other Skills

- **grounded-citations**: Mandatory for all claims. Use its scripts for ledger.
- **persistence-evolution-framework**: For CHECKPOINT, memory patterns, recovery, metrics JSONL.
- **long-horizon-agentic-workflows** + **agentic-test-campaigns**: For campaign contracts (Kt), role separation (Scout=catch, Curer=preserve, Weaver=synthesis, Breaker=oppose), wave execution.
- **durable-delegation-gates**: For parallel sub-harvests (multiple topics, fan-out delegates with gates).
- Use with todo for phase tracking.

## Metrics Tracked (append to JSONL)

```json
{"wave": 3, "mission": "stockfish-sustainability", "tokens": 18420, "sources": 27, "cited_sentences": 41, "gate_pass": true, "recovery": false, "packet_sha": "abc123...", "active_time_s": 312, "synthesis_style": "lofoten-saga"}
```

Target: >80% gate pass on mature waves, reuse reduces tokens 40%+ via memory + stable ledger.

## Common Pitfalls

1. **Registering sources after synthesis** — Ledger must be populated from live tool output *before* writing prose. Fix: strict order in workflow; use scripts to enforce.
2. **Drift on resume** — CHECKPOINT/ledger hashes don't match live files. Fix: always run sha256sum gate + session_search + "last known good wave" bootstrap. Re-catch only deltas.
3. **Over-weaving without quotes** — Creative narrative buries facts or invents. Fix: require 1+ verbatim quote per key claim via grounded-citations quote; flag [unverified] liberally.
4. **Memory bloat / no gates** — Dumping everything to MEMORY without hash+verdict. Fix: gate every memory write; use scoped keys (wave/topic); prune via persistence patterns.
5. **Ignoring network/Lofoten constraints** — Assume always-on high-bandwidth. Fix: design for intermittent (cache raw, resume partial waves, prefer extract over full browse when possible).
6. **No publish hook discipline** — Artifacts stay local only. Fix: always mirror to vault + emit packet ready for clearinghouse; log hook execution.
7. **Citation without evidence** — For high-stakes, use --evidence verify. Fix: run verify --evidence before any gate.
8. **Token explosion on large corpora** — Don't load entire history. Fix: use ledger ids + summaries in prompt; delegate sub-harvests; compress via persistence.

## Verification Checklist

- [ ] Ledger reset/loaded at start; every URL registered with sources.py before synthesis.
- [ ] All claims in final artifacts have [n] or [unverified]; sources.py verify --evidence --min-coverage >=0.6 passes.
- [ ] CHECKPOINT.md updated with Kt contract + retained edits + packet hashes.
- [ ] Memory writes are atomic/gated (key includes wave+hash).
- [ ] Artifacts mirrored to JeffVault + sha256 recorded in event.log.
- [ ] JSONL metrics appended; at least one packet produced.
- [ ] Oppositional test run (see agentic-test-campaigns): simulated net fail, token limit, concurrent harvest, cache invalidation — all recovered with gates intact.
- [ ] Skill loaded explicitly or via --skills; frontmatter valid (name, description <=1024, starts with ---).
- [ ] Usage example executed end-to-end in test (harvest small domain → verify → preserve → mirror).

## One-Shot Recipes

**Minimal viable harvest (5-10 min):**
1. Reset ledger.
2. web_search "Lofoten stockfish history sustainability" + extract top 3-5.
3. sources.py add/ingest.
4. Write 1-page SYNTHESIS.md with cites + 2 quotes.
5. sources.py verify --evidence.
6. Write simple PACKET + sha + memory entry + CHECKPOINT update.
7. Mirror to vault subdir.

**Full wave in campaign:**
Follow agentic-test-campaigns contract + this skill + delegation for parallel topics.

**Oppositional hardening target:**
- Simulate: kill network mid-catch → resume from partial ledger + CHECKPOINT.
- Token limit mid-weave → delegate sub-synthesis + gate merge.
- Concurrent writes → use file locks or atomic patterns + detect conflict via hashes.
- Cache invalid (change source page) → re-extract + re-verify, log delta.

This skill turns fleeting research into preserved, citable, evolvable stockfish — ready for the long Arctic winters of agent memory.
