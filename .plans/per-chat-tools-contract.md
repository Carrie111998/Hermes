# CONTRACT: Per-Chat Tool / MCP / Skill Presets (Desktop)

This is the frozen interface spec shared by all implementation sub-agents. Build to this. If you must
deviate, note it clearly in your final report so downstream agents can adjust.

## Goal (why this exists)
Let each chat (session) pick its own tool surface so "chat-only" chats ship a minimal tool set (saving
the thousands of tokens that tool schemas + their system-prompt guidance + the `<available_skills>`
index consume), while other chats stay tool-heavy. Selection is per-chat, changeable mid-chat, on the
Desktop app. Users can also author reusable **named presets** in settings, with an estimated **token
cost per item**.

The filtering engine already exists (`model_tools.get_tool_definitions(enabled_toolsets,
disabled_toolsets)` → `_compute_tool_definitions`); the system prompt + skills index already gate on
`agent.valid_tool_names`. This work is plumbing + UI, NOT new filtering logic.

---

## Core semantics (MEMORIZE — these are the invariants)

1. **`enabled_toolsets` empty-list vs None:**
   - `[]` (empty list) = **chat-only** = zero non-core tools. It is FALSY — never use `x or default`.
     Use `x is not None` or an `_UNSET` sentinel everywhere.
   - `None` = "no per-session override" = fall back to profile/platform default (full tools).
2. **Precedence:** per-session stored value  >  platform/coding-posture (`_load_enabled_toolsets`)  >
   profile/global `config.yaml`.
3. **Snapshot, not reference:** a session stores the RESOLVED `enabled_toolsets` / `disabled_toolsets` /
   `allowed_tool_names` / `denied_tool_names` (source of truth) PLUS a `tool_preset` name string (for UI
   display only). Editing a preset later must NOT retroactively change existing chats.
4. **Turn-boundary only:** mid-chat rebuilds run only when the session is not actively generating
   (`session["running"] is False`) — changing `tools=` mid-turn breaks the provider prompt cache.
5. **Two virtual built-in presets** always exist even with no user presets configured:
   - `"Chat-only"` → `enabled_toolsets: []`
   - `"Full"` → `enabled_toolsets: null` (profile default)

---

## 1. `config.yaml` — new `tool_presets` key (profile-scoped)

```yaml
tool_presets:
  - name: "Research"                 # unique, user-facing
    enabled_toolsets: ["web"]        # list[str] | null  (null = profile default = "Full")
    disabled_tools: ["browser_screenshot"]   # list[str] | null  (optional per-tool subtraction)
    allowed_tools: null              # list[str] | null  (optional per-tool whitelist on top of toolsets)
    disabled_skills: ["dataviz"]     # list[str] | null  (optional skill hiding)
```
- All fields except `name` optional; absent = null. Loaded/saved via existing `hermes_cli/config.py`.
- `"Chat-only"` and `"Full"` are reserved virtual names — do not persist them as rows; synthesize them.

---

## 2. `sessions.model_config` JSON blob — new keys (no new DB column)

Add to the existing `model_config` dict (written at session create, live-persisted on change, read on
resume):
```jsonc
{
  // ...existing keys (max_iterations, reasoning_config, max_tokens)...
  "enabled_toolsets": [] | ["web", ...] | null,   // resolved snapshot; null = no override
  "disabled_toolsets": [...] | null,
  "allowed_tool_names": [...] | null,
  "denied_tool_names":  [...] | null,
  "disabled_skills":    [...] | null,
  "tool_preset": "Research" | "Chat-only" | "Full" | "Custom" | null  // display label only
}
```
Persist with explicit `is not None` checks so `[]` survives (never drop an empty list).

---

## 3. Runtime filter additions (Python)

- `_compute_tool_definitions` (in `model_tools.py`) gains per-tool filtering AFTER
  `registry.get_definitions()`: `allowed_tool_names` (whitelist; if not None, keep only these) and
  `denied_tool_names` (blacklist; drop these). Core tools that must never be dropped stay protected the
  same way `disabled_toolsets` already protects them.
- `get_tool_definitions(...)` and `AIAgent.__init__` / `agent_init` gain matching optional params
  (`allowed_tool_names`, `denied_tool_names`), threaded through to the choke point.
- Skill hiding reuses the existing `get_disabled_skill_names` path, extended to also accept a
  per-session `disabled_skills` list.

---

## 4. Mid-chat rebuild helper (Python)

`agent/agent_runtime_helpers.py::rebuild_agent_toolsets(agent, *, enabled, disabled, allowed=None,
denied=None, disabled_skills=None, quiet_mode=True) -> None`
- Calls `refresh_agent_mcp_tools(agent, enabled_override=enabled, disabled_override=disabled, ...)` to
  recompute `agent.tools` / `agent.valid_tool_names` and update `agent.enabled_toolsets` /
  `agent.disabled_toolsets`.
- Then invalidates the system prompt (`agent._invalidate_system_prompt()`), so guidance + skills index
  shrink.
- Stores allowed/denied/disabled_skills on the agent so persistence + subsequent rebuilds see them.
- Do NOT rely on `refresh_agent_mcp_tools`'s return (it reports added names only).

---

## 5. JSON-RPC methods (in `tui_gateway/server.py`) — the UI-facing contract

### `tools.session_configure`
Set the live session's tool posture (mid-chat) and persist it.
- **params:** `{ "session_id": str, "preset": str | null, "enabled_toolsets": [str]|null,
  "disabled_toolsets": [str]|null, "allowed_tool_names": [str]|null, "denied_tool_names": [str]|null,
  "disabled_skills": [str]|null }`
  - If `preset` is a known name ("Chat-only"/"Full"/a config preset), the backend resolves it to the
    lists and ignores the explicit list fields. If `preset` is "Custom"/null, the explicit lists are used
    and `tool_preset` is stored as "Custom" (or null).
- **behavior:** turn-boundary gated; calls `rebuild_agent_toolsets`; persists into `model_config`; emits
  `session.info`.
- **returns:** `{ "ok": true, "session": <session.info payload> }`. If busy (running), return
  `{ "ok": false, "reason": "busy" }`.

### `tools.catalog`
Return the full selectable catalog with per-item token estimates (for the preset editor).
- **params:** `{}` (optionally `{ "profile": str }`)
- **returns:**
  ```jsonc
  {
    "core_tokens": 1234,                 // always-present baseline (core tools, never removable)
    "toolsets": [
      { "name": "web", "description": "...", "est_tokens": 2200,
        "tools": [ { "name": "web_search", "est_tokens": 800 }, ... ] }
    ],
    "mcp_servers": [
      { "name": "firecrawl", "toolset": "mcp-firecrawl", "est_tokens": 1500,
        "tools": [ { "name": "mcp_firecrawl_scrape", "est_tokens": 500 }, ... ] }
    ],
    "skills": [ { "name": "dataviz", "category": "...", "est_tokens": 60 }, ... ]
  }
  ```
- Token estimate = `len(json.dumps(schema)) / 4` (same `chars/4` rule as Tool Search
  `TOOL_JSON_CHARS_PER_TOKEN`). Skill estimate = its `<available_skills>` index-entry char count / 4.
  Reuse logic from `hermes_cli/prompt_size.py`. Put the estimator in a NEW standalone module
  (e.g. `tool_catalog.py`) that server.py imports as a thin wrapper.

### Preset CRUD
- `tools.presets_list` → `{ "presets": [ {name, enabled_toolsets, disabled_tools, allowed_tools,
  disabled_skills, builtin: bool} ] }` (includes the 2 virtual built-ins with `builtin: true`).
- `tools.preset_save` → params `{ preset: {name, enabled_toolsets?, disabled_tools?, allowed_tools?,
  disabled_skills?} }`; upserts by name; rejects reserved names "Chat-only"/"Full". Returns updated list.
- `tools.preset_delete` → params `{ name: str }`; returns updated list.
- Put preset load/save/resolve in a NEW standalone module (e.g. `tool_presets.py`) importing
  `hermes_cli/config.py`; server.py wraps it.

---

## 6. `session.info` payload additions

The existing `_session_info(agent, session)` payload gains:
```jsonc
{
  // ...existing fields...
  "enabled_toolsets": [] | [...] | null,
  "disabled_toolsets": [...] | null,
  "tool_preset": "Chat-only" | "Full" | "Custom" | "<name>" | null,
  "tool_count": 7,          // len(agent.tools)
  "tools_est_tokens": 5400  // sum est tokens of current agent.tools (chars/4)
}
```

---

## 7. TypeScript types (`apps/shared/src/json-rpc-gateway.ts`)

Added by the Phase 1 (backend) agent so the two UI agents only consume them:
- Extend the `SessionInfo` type with the §6 fields.
- Add request/response types + client methods for `tools.session_configure`, `tools.catalog`,
  `tools.presets_list`, `tools.preset_save`, `tools.preset_delete`.

---

## Verification hooks for backend agent (no UI needed)
- Script: build `AIAgent(enabled_toolsets=[])` → assert `len(agent.tools)` is ~core-only and the built
  system prompt is much shorter than a default agent's (compare via `agent/system_prompt.build_system_prompt`).
- `hermes prompt-size` before/after conceptually (tools JSON + skills block shrink).
- Raw JSON-RPC round-trip for each new method.
