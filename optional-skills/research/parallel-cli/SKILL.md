---
name: parallel-cli
description: Parallel CLI (paid vendor) — agent-native deep research, web extraction, enrichment, FindAll, and monitoring with JSON output. Use for structured research/enrichment jobs, not quick keyword lookups or single-page scraping.
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Research, Web, Search, Deep-Research, Enrichment, CLI]
    related_skills: [duckduckgo-search, mcporter]
---

# Parallel CLI

Use `parallel-cli` when the user explicitly wants Parallel, or when a terminal-native workflow would benefit from Parallel's vendor-specific stack for web search, extraction, deep research, enrichment, entity discovery, or monitoring.

This is an optional third-party workflow, not a Hermes core capability.

Important expectations:
- Parallel is a paid service with a free tier, not a fully free local tool.
- It overlaps with Hermes native `web_search` / `web_extract`, so do not prefer it by default for ordinary lookups.
- Prefer this skill when the user mentions Parallel specifically or needs capabilities like Parallel's enrichment, FindAll, or monitor workflows.

`parallel-cli` is designed for agents:
- JSON output via `--json`
- Non-interactive command execution
- Async long-running jobs with `--no-wait`, `status`, and `poll`
- Context chaining with `--previous-interaction-id`
- Search, extract, research, enrichment, entity discovery, and monitoring in one CLI

## When to use it

Prefer this skill when:
- The user explicitly mentions Parallel or `parallel-cli`
- The task needs richer workflows than a simple one-shot search/extract pass
- You need async deep research jobs that can be launched and polled later
- You need structured enrichment, FindAll entity discovery, or monitoring

Prefer Hermes native `web_search` / `web_extract` for quick one-off lookups when Parallel is not specifically requested.

## Routing

| Intent | Read |
|--------|------|
| Install via brew / npm / pip / pipx / standalone installer | `references/install-auth.md` |
| Login, `--device` headless auth, `PARALLEL_API_KEY`, auth status | `references/install-auth.md` |
| `search` flags, domain filters, recency, saving output | `references/commands.md` |
| `extract` / `fetch`, `--objective`, `--full-content` | `references/commands.md` |
| `research run/status/poll/processors`, processor tiers, context chaining | `references/commands.md` |
| `enrich suggest/plan/run/status/poll`, CSV + YAML config runs | `references/commands.md` |
| `findall` entity discovery, `monitor` change detection | `references/commands.md` |
| Recommended Hermes workflow recipes (fast answer, URL investigation, long research, enrichment) | `references/commands.md` |
| Exit codes, auth failures, update / maintenance commands | `references/troubleshooting.md` |

## Most common invocations

```bash
parallel-cli search "What is Anthropic's latest AI model?" --json
parallel-cli extract https://example.com --json
parallel-cli research run "Compare the leading AI coding agents" --processor core --json
parallel-cli findall run "Find AI coding agent startups with enterprise offerings" --json
parallel-cli auth
```

## End-to-end skeleton (async research)

```bash
parallel-cli research run "<question>" --processor ultra --no-wait --json   # capture the returned trun_xxx ID
parallel-cli research status trun_xxx --json                                # later: check progress
parallel-cli research poll trun_xxx --json                                  # block until done, then summarize
```

Summarize the final report with citations taken only from the URLs the CLI returned.

## Core rule set

1. Always prefer `--json` when you need machine-readable output.
2. Prefer explicit arguments and non-interactive flows.
3. For long-running jobs, use `--no-wait` and then `status` / `poll`.
4. Cite only URLs returned by the CLI output.
5. Save large JSON outputs to a temp file when follow-up questions are likely.
6. Use background processes only for genuinely long-running workflows; otherwise run in foreground.
7. Prefer Hermes native tools unless the user wants Parallel specifically or needs Parallel-only workflows.

## Quick reference

```text
parallel-cli
├── auth
├── login
├── logout
├── search
├── extract / fetch
├── research run|status|poll|processors
├── enrich run|status|poll|plan|suggest|deploy
├── findall run|ingest|status|poll|result|enrich|extend|schema|cancel
└── monitor create|list|get|update|delete|events|event-group|simulate
```

## Common flags and patterns

Commonly useful flags:
- `--json` for structured output
- `--no-wait` for async jobs
- `--previous-interaction-id <id>` for follow-up tasks that reuse earlier context
- `--max-results <n>` for search result count
- `--mode one-shot|agentic` for search behavior
- `--include-domains domain1.com,domain2.com`
- `--exclude-domains domain1.com,domain2.com`
- `--after-date YYYY-MM-DD`

Read from stdin when convenient:

```bash
echo "What is the latest funding for Anthropic?" | parallel-cli search - --json
echo "Research question" | parallel-cli research run - --json
```

## Pitfalls

- Do not omit `--json` unless the user explicitly wants human-formatted output.
- Do not cite sources not present in the CLI output.
- `login` may require PTY/browser interaction.
- Prefer foreground execution for short tasks; do not overuse background processes.
- For large result sets, save JSON to `/tmp/*.json` instead of stuffing everything into context.
- Do not silently choose Parallel when Hermes native tools are already sufficient.
- Remember this is a vendor workflow that usually requires account auth and paid usage beyond the free tier.
