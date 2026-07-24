---
name: qmd
description: Search personal knowledge bases, notes, docs, and meeting transcripts locally using qmd — a hybrid retrieval engine with BM25, vector search, and LLM reranking. Supports CLI and MCP integration.
version: 1.0.0
author: Hermes Agent + Teknium
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [Search, Knowledge-Base, RAG, Notes, MCP, Local-AI]
    related_skills: [obsidian, native-mcp, arxiv]
---

# QMD — Query Markup Documents

Local, on-device search engine for personal knowledge bases. Indexes markdown
notes, meeting transcripts, documentation, and any text-based files, then
provides hybrid search combining keyword matching, semantic understanding, and
LLM-powered reranking — all running locally with no cloud dependencies.

Created by [Tobi Lütke](https://github.com/tobi/qmd). MIT licensed.

## When to Use

- User asks to search their notes, docs, knowledge base, or meeting transcripts
- User wants to find something across a large collection of markdown/text files
- User wants semantic search ("find notes about X concept") not just keyword grep
- User has already set up qmd collections and wants to query them
- User asks to set up a local knowledge base or document search system
- Keywords: "search my notes", "find in my docs", "knowledge base", "qmd"

## Routing

| Intent | Read |
|--------|------|
| Install qmd, Node 22 / Homebrew SQLite prerequisites, model downloads | `references/setup.md` |
| Add collections, add context descriptions, generate embeddings, verify index | `references/setup.md` |
| Search modes in depth, structured multi-mode queries, lex query syntax, HyDE, output formats | `references/search-patterns.md` |
| How the search pipeline works (expansion, RRF fusion, reranking, chunking) and best practices | `references/search-patterns.md` |
| MCP server config (stdio or HTTP daemon), launchd/systemd units, MCP tool table, terminal-only usage | `references/mcp.md` |
| Cold start latency, macOS extension errors, "no collections found", CJK embedding override | `references/troubleshooting.md` |

## Most Common Invocations

```bash
qmd search "authentication middleware"      # BM25, instant, no models
qmd vsearch "how does throttling work"      # semantic vector search
qmd query "what was decided about the API redesign" --json   # hybrid + rerank, best quality
qmd get "#abc123"                           # retrieve a hit's full document
qmd status                                  # index health, collections, models
```

## End-to-End Skeleton

```bash
qmd collection add ~/notes --name notes                      # 1. register a directory
qmd context add qmd://notes "Personal notes and ideas"       # 2. describe it (big quality win)
qmd embed                                                    # 3. index / re-index
qmd query "what did I decide about the migration" --json     # 4. search, then parse JSON hits
qmd get "path/to/file.md"                                    # 5. pull the full document
```

## Red Lines

- **Always add context descriptions** (`qmd context add`) — retrieval accuracy drops sharply without them.
- **Re-run `qmd embed`** after any new files are added to a collection, or they are invisible to vector search.
- **Use `--json`** whenever output will be parsed rather than shown to the user.
- **Never invent results** — report only documents qmd returned, with their paths/IDs.
- Cold start is ~19s when models are not warm; prefer `qmd search` (BM25, no models) for quick lookups and the HTTP daemon for frequent use.
- Everything is local; there are no cloud calls and no data leaves the machine.

## Quick Reference

| Command | What It Does | Speed |
|---------|-------------|-------|
| `qmd search "query"` | BM25 keyword search (no models) | ~0.2s |
| `qmd vsearch "query"` | Semantic vector search (1 model) | ~3s |
| `qmd query "query"` | Hybrid + reranking (all 3 models) | ~2-3s warm, ~19s cold |
| `qmd get <docid>` | Retrieve full document content | instant |
| `qmd multi-get "glob"` | Retrieve multiple files | instant |
| `qmd collection add <path> --name <n>` | Add a directory as a collection | instant |
| `qmd context add <path> "description"` | Add context metadata to improve retrieval | instant |
| `qmd embed` | Generate/update vector embeddings | varies |
| `qmd status` | Show index health and collection info | instant |
| `qmd mcp` | Start MCP server (stdio) | persistent |
| `qmd mcp --http --daemon` | Start MCP server (HTTP, warm models) | persistent |

## Data Storage

- **Index & vectors:** `~/.cache/qmd/index.sqlite`
- **Models:** Auto-downloaded to local cache on first run
- **No cloud dependencies** — everything runs locally

## References

- [GitHub: tobi/qmd](https://github.com/tobi/qmd)
- [QMD Changelog](https://github.com/tobi/qmd/blob/main/CHANGELOG.md)
