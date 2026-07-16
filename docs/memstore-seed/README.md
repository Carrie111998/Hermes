# Memstore Seeding & Dreams

Bootstrap an agent's memory *before* the first conversation, and consolidate
what it already knows with an offline "dream" pass.

The **store of record is a canonical markdown file tree** — plain files any
agent framework can read, which is what makes the design provider-agnostic. An
external DB provider (Honcho, Supermemory, …), when configured, is mirrored as
a bonus.

## The canonical file tree

Written under `HERMES_HOME` (profile-scoped):

```
SOUL.md                     agent identity prose (Hermes reads this at boot)
memories/
  IDENTITY.md               structured agent persona/identity
  USER.md                   the user profile
  AGENTS.md                 operating instructions (memstore-scoped —
                            does NOT touch a project's own AGENTS.md)
  TOOLS.md                  tools & environment
  MEMORY.md                 rolled-up notes + synthesised insights
  daily/
    2026-07-15.md           one digest per day, built from that day's
    2026-07-16.md           transcript — the base layer of the tree
```

Facts live inside `<!-- hermes:seed:begin … -->` managed blocks, so re-seeding
merges (de-duplicating bullets) and never clobbers hand-written content.

## What's here

| File | Purpose |
|------|---------|
| `persona.md` | An "about the user / project / agent" doc. Headings route to files: *About the User* → USER.md, *About the Agent* → IDENTITY.md + SOUL.md, *Project* → AGENTS.md, *Tools* → TOOLS.md. |
| `transcript.json` | A dated two-day conversation. Split into `memories/daily/*.md` and mined for preferences, identity, decisions, and explicit `remember …` cues. |

## Seed via the CLI

```bash
# Preview the extraction (no writes):
hermes memory seed \
  --persona docs/memstore-seed/persona.md \
  --transcript docs/memstore-seed/transcript.json \
  --dry-run

# Seed the canonical file tree (+ daily digests, + provider mirror):
hermes memory seed \
  --persona docs/memstore-seed/persona.md \
  --transcript docs/memstore-seed/transcript.json

# File tree only, no external provider:
hermes memory seed --persona docs/memstore-seed/persona.md --no-mirror
```

## Dreams: two-layer consolidation

The **base layer** is the per-day digests. The **higher layer** is the roll-up:

```bash
# Read recent memories/daily/*.md, dedupe / de-conflict / synthesise, and
# fold the result into MEMORY.md and USER.md:
hermes memory dream                 # all days
hermes memory dream --days 7        # last 7 daily digests

# Consolidate a standalone JSONL corpus instead:
hermes memory dream --corpus corpus.jsonl --min-trust 0.3 --export refined.jsonl
```

## Standalone driver (CI / cron)

```bash
python scripts/seed_memstore.py \
  --persona docs/memstore-seed/persona.md \
  --transcript docs/memstore-seed/transcript.json \
  --home /tmp/hermes-home

python scripts/seed_memstore.py --home /tmp/hermes-home --rollup
```

## Programmatic use

```python
from agent.memstore_files import CanonicalMemstore
from agent.memstore_seeding import build_corpus_from_sources, load_transcript

store = CanonicalMemstore(root="/path/to/HERMES_HOME")   # or None → HERMES_HOME
corpus = build_corpus_from_sources(
    persona_paths=["about-me.md"],
    transcript_paths=["last-session.json"],
)
store.seed_facts(corpus)                                  # write canonical files
store.write_daily_digests(load_transcript("last-session.json"))  # base layer
store.roll_up(days=7)                                     # higher layer
```

## Transcript formats accepted

`load_transcript` handles a bare JSON list, a `{"messages": [...]}` wrapper,
JSONL, and multimodal `content` parts. Day-grouping recognises `timestamp`,
`created_at`, `ts`, `time`, `date`, `datetime` fields (epoch seconds/millis or
ISO-8601); undated messages fall under `--date` (today if omitted).
