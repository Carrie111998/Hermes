---
sidebar_label: Semantic Graph Memory
title: Semantic Graph Memory
description: Typed provenance graph for memory, evidence, artifacts, and output evaluation.
---

# Semantic Graph Memory

Hermes keeps `MEMORY.md` / `USER.md` for small high-value facts. Semantic Graph
is a second, opt-in memory layer for structured claims, evidence, preferences,
decisions, artifacts, and evaluations.

## Install / enable

```bash
hermes plugins enable semantic-graph
HERMES_PLUGINS_DEBUG=1 hermes plugins list
hermes semantic-graph status
```

## Config

Under `plugins.entries.semantic-graph.config`:

| Key | Default | Notes |
|-----|---------|-------|
| `capture_turns` | `true` | Store sanitized user/assistant artifacts |
| `capture_tool_events` | `false` | Opt-in tool provenance |
| `auto_extract` | `explicit` | `off` / `explicit` / `all` |
| `retrieval_enabled` | `true` | Bounded pre-LLM recall |
| `retention_days` | `365` | Operator purge uses this intent |

`auto_extract=all` adds an LLM call after each turn — watch cost.

## Tools

`semantic_graph_status`, `begin_run`, `ingest`, `submit_fragment`, `search`,
`get`, `finalize`, `evaluate_output`, `feedback`, `export`.

Physical delete is CLI-only:

```bash
hermes semantic-graph purge --before 2026-01-01 --confirm PURGE
```

## Memory layer distinction

| Layer | Role |
|-------|------|
| MEMORY.md / USER.md | Tiny always-needed facts |
| session_search | Full-text conversation evidence |
| Skills | Procedural playbooks |
| Semantic graph | Typed claims, evidence, evaluations, history |

## Privacy

- Secrets redacted before write
- Recall is data-only XML, never system-prompt mutation
- No hidden chain-of-thought storage
- Export path traversal rejected
- Windows paths: `%HERMES_HOME%\semantic-graph\`

## Example workflows

Preference capture, meeting notes deep workflow (`/semantic-graph-memory`), and
output evaluation before publish. See the optional skill
`official/autonomous-ai-agents/semantic-graph-memory`.

## Limitations

- FTS5 only (no embeddings in MVP)
- No Neo4j / external vector DB
- No automatic answer rewrite from evaluations
- Cross-run label matches become `same_as` candidates, not destructive merges

## Troubleshooting

- Plugin missing: ensure `plugins.enabled` includes `semantic-graph`
- Empty recall: need asserted/accepted nodes above `min_recall_confidence`
- FTS disabled: status shows `fts_enabled=false`; LIKE fallback still works
