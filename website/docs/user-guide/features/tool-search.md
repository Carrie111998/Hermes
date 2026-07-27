---
title: Tool Search
sidebar_position: 95
---

# Tool Search

When you have many MCP servers or non-core plugin tools attached to a
session, their JSON schemas can consume a substantial fraction of the
context window on every turn — even when only a few of them are relevant
to what the user actually asked for.

**Tool Search** is Hermes' progressive-disclosure layer for that problem.
MCP and plugin schemas stay out of the model-visible tools array; three small,
byte-stable bridge tools discover the current catalog and load specific schemas
on demand.

:::info Built-in Hermes tools never defer
The tools that make up Hermes' core capability set (`terminal`,
`read_file`, `write_file`, `patch`, `search_files`, `todo`, `memory`,
`browser_*`, `web_search`, `web_extract`, `clarify`, `execute_code`,
`delegate_task`, `session_search`, and the rest of
`_HERMES_CORE_TOOLS`) are *always* loaded directly. Only MCP tools and
non-core plugin tools are eligible for deferral.
:::

## How it works

While Tool Search is enabled, the model always sees the same three bridge tools
in place of deferred schemas—even when no MCP server is currently connected:

```
tool_search(query, limit?)     — search the deferred-tool catalog
tool_describe(name)            — load the full schema for one tool
tool_call(name, arguments)     — invoke a deferred tool
```

A typical interaction looks like:

```
Model: tool_search("create a github issue")
  → { matches: [{ name: "mcp_github_create_issue", ... }, ...] }
Model: tool_describe("mcp_github_create_issue")
  → { parameters: { type: "object", properties: { ... } } }
Model: tool_call("mcp_github_create_issue", { title: "...", body: "..." })
  → { ok: true, issue_number: 42 }
```

When the model invokes `tool_call`, Hermes **unwraps the bridge** and
dispatches the underlying tool exactly as if the model had called it
directly. Pre-tool-call hooks, guardrails, approval prompts, and
post-tool-call hooks all run against the real tool name — not against
`tool_call`. The activity feed in the CLI and gateway also unwraps so you
see the underlying tool, not the bridge.

## Stable bridge and live discovery

The bridge descriptions do not contain a tool count, server summary, or catalog
listing. Every `tool_search`, `tool_describe`, and `tool_call` invocation resolves
the current live registry, filtered to the session's enabled/disabled toolsets.

Keeping the bridge present from the start makes the model-facing `tools=` prefix
identical across MCP additions, removals, equal-count swaps, and schema edits.
Hermes folds a one-shot note into the next real user turn after a catalog update,
prompting the model to search again without creating a synthetic user message.

## Configuration

```yaml
tools:
  tool_search:
    enabled: auto       # auto (default), on, or off
    search_default_limit: 5
    max_search_limit: 20
```

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `auto` | `auto`/`on` keep the stable bridge enabled; `off` exposes eligible schemas directly. |
| `search_default_limit` | `5` | Hits returned when the model calls `tool_search` without a `limit`. |
| `max_search_limit` | `20` | Hard upper bound the model can request via `limit`. Range 1–50. |

You can also flip the legacy boolean shape:

```yaml
tools:
  tool_search: true   # equivalent to {enabled: auto}
```

## When NOT to use it

Tool Search trades a fixed per-turn token cost (the three small bridge schemas)
and at least one extra round trip on cold tools (search → describe → call) for
the savings and cache stability of deferred schemas.

If you want the old always-eager behavior for a small toolset, set
`enabled: off`.

## Trade-offs that don't go away

These come from the prompt-cache integrity invariant — they are inherent
to any progressive-disclosure design, not specific to this implementation:

- **One extra round trip on cold tools.** The first time the model needs
  a deferred tool, it spends one or two extra model calls to find and
  load the schema. The token savings on the static side are real, but a
  portion is paid back at runtime.
- **No cache benefit on deferred schemas.** A loaded `tool_describe`
  result enters the conversation history (so it does get cached on
  subsequent turns) but it never benefits from the system-prompt cache
  prefix.
- **Model-quality dependence.** Tool Search assumes the model can write a
  reasonable search query for the tool it wants. Smaller models do this
  less well; the published Anthropic numbers (49% → 74% on Opus 4 with
  vs. without tool search) show the upside but also that ~26 points of
  accuracy is still retrieval failure.
- **Direct mode still invalidates cache.** With `enabled: off`, MCP/plugin
  schemas are exposed directly. Adding, removing, or editing one changes the
  model-facing `tools=` prefix and invalidates the provider prompt cache.

## Implementation details

- **Retrieval:** BM25 over tokenized tool name + description + parameter
  names. Falls back to a literal substring match on the tool name when
  BM25 returns no positive-score hits, which protects against
  zero-IDF degenerate cases (e.g. searching `"github"` against a
  catalog where every tool name contains "github").
- **Catalog is live, not embedded.** Each bridge invocation rebuilds its
  session-scoped view from the current registry. Tool changes therefore take
  effect immediately without replacing the model-facing bridge schemas.
- **The catalog is scoped to the session's toolsets.** `tool_search`,
  `tool_describe`, and `tool_call` only ever see and invoke tools the
  session was actually granted. A subagent, kanban worker, or gateway
  session restricted to a subset of toolsets cannot use the bridge to
  discover or call a tool outside that subset — the deferred catalog is
  the deferrable slice of the session's own enabled/disabled toolsets,
  not the whole process registry.
- **No JS sandbox.** Hermes uses the simpler "structured tools" mode
  (search / describe / call as plain functions). The JS-sandbox "code
  mode" some other implementations offer is a large surface area; we
  skip it.

## See also

- `tools/tool_search.py` — the implementation
- `tests/tools/test_tool_search.py` — the regression suite
