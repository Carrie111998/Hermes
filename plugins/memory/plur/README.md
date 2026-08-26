# PLUR Memory Provider

Local-first persistent memory via the PLUR engram system. Corrections, preferences, and patterns survive across sessions, tools, and machines — stored as plain YAML on disk, searched with BM25 + BGE embeddings (Reciprocal Rank Fusion), synced via git. Zero API calls, zero cloud required.

## Requirements

**CLI** (required):
```bash
npm install -g @plur-ai/cli
# or: npx @plur-ai/cli --version (to verify)
```

**Python package** (installed automatically by `hermes memory setup`):
```bash
pip install 'plur-hermes>=0.18.1'
```

## Setup

```bash
hermes memory setup    # select "plur" from the picker
```

Or manually:
```bash
hermes config set memory.provider plur
```

## Config

| Env Var | Required | Default | Description |
|---------|----------|---------|-------------|
| `PLUR_PATH` | No | `~/.plur` | Path to your engram store |
| `PLUR_INJECT_MODE` | No | `fast` | `fast` (BM25-only) or `hybrid` (BM25+embeddings) |
| `PLUR_INJECTION_FEEDBACK` | No | `true` | Auto-send relevance feedback after each turn |

## Tools

| Tool | Description |
|------|-------------|
| `plur_learn` | Create a new engram — store a correction, preference, or pattern |
| `plur_recall` | Search engrams by topic (BM25 or hybrid) |
| `plur_inject` | Get relevant engrams for a task (three-tier output) |
| `plur_list` | List all engrams with optional filtering |
| `plur_forget` | Retire an engram by ID or search query |
| `plur_feedback` | Rate an engram (positive/negative/neutral) |
| `plur_capture` | Record an episode to the timeline |
| `plur_timeline` | Query the episodic timeline |
| `plur_status` | Check PLUR system health and engram count |
| `plur_sync` | Cross-device sync via git |
| `plur_ingest` | Extract and save engrams from text or conversation logs |
| `plur_packs_list` | List installed engram packs |
| `plur_packs_install` | Install an engram pack |
| `plur_packs_export` | Export engrams as a shareable pack |
| `plur_promote` | Increase an engram's activation and priority |
| `plur_stores_add` | Add a knowledge store path |
| `plur_stores_list` | List configured knowledge stores |
| `plur_similarity_search` | Search by cosine similarity with scores |

## How it works

PLUR implements the full Hermes MemoryProvider lifecycle:

- **`prefetch(query)`** — injects relevant engrams before each turn using the fast BM25 path (switch to hybrid with `PLUR_INJECT_MODE=hybrid`)
- **`sync_turn(user, assistant)`** — auto-extracts learnings from self-reported corrections; sends relevance feedback for injected engrams
- **`on_session_end(messages)`** — captures the session as an episode at real session boundaries (not per-turn)
- **`system_prompt_block()`** — adds a brief "N engrams active" status line to the system prompt

Engram activation follows the ACT-R model: frequently recalled and recently used engrams surface first; irrelevant ones decay over time.

## Links

- [PLUR on GitHub](https://github.com/plur-ai/plur)
- [PyPI: plur-hermes](https://pypi.org/project/plur-hermes/)
- [npm: @plur-ai/cli](https://www.npmjs.com/package/@plur-ai/cli)
