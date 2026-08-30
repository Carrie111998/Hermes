# Declared repository capability — evidence

`route_repo_changes` used to withhold seven tools by name. At the frozen proof
commit below, it withheld whatever declared `repo_access="write"` inline, plus
anything that declared nothing at all. The current tree preserves that behavior
with built-ins classified in `tools/repo_access.py`; plugins and MCP tools still
declare explicitly and fail closed when undeclared. Design notes live in
`tools/delegate_routing.py`; this file records what was actually observed, so the
next reader does not have to re-run it to believe it.

Commit `80477f6531`, tag `routing-declared-capability`, branch
`session-external-turns`, on top of `225e9b37`.

## Vocabulary

| declared | meaning | while routing is ON |
|---|---|---|
| `write` | direct mutation from this process | withheld |
| `delegated_write` | mutation via the supervised route | allowed |
| `read` / `none` | no mutation | allowed |
| *undeclared* | nobody classified it | **withheld** |

MCP capability is declared per tool, never per server. A server-wide
`repo_access: none` would grant blanket permission to that server's future tools
— the same failure class one layer out — so a bare string is honoured only when
restrictive (`write`), and a permissive one is ignored with a warning. See
`_resolve_mcp_repo_access` in `tools/mcp_tool.py`.

## Unit evidence

- 24 tests in `tests/test_delegate_routing.py` pass; both new MCP rules fail when
  deliberately reverted.
- 92/92 built-in tools declare a capability; undeclared fails closed.
- Compared against a pristine worktree of `225e9b37` at
  `D:\Programs\evTEMP\fork-pristine`: **12 of 12 test targets have identical
  failed node-ID sets**, not merely equal counts — tools[a-c] (identical
  INTERNALERROR), tools[d-f], [g-l], [m-r], [s-z], agent, run_agent, plugins,
  cli, cron, gateway (2189 identical), tui_gateway (6 identical). Zero new
  failures, zero fixed. `tests/hermes_cli` deliberately not compared.

The fork's suite is massively red on Windows for platform reasons (`termios`,
`PosixPath`, shell quoting). "Green" is unreachable here; identical failed
node-ID sets against a pristine worktree is the check that means something.

## Assembled-tool evidence, under the real C5b config

`HERMES_HOME=.c5b-live-3`, `route_repo_changes: true`, delegate-wave control API
on 47332, asking `model_tools.get_tool_definitions()` what it would hand a model:

- 114 tools registered; 32 offered directly, 8 more deferred behind
  `tool_search` / `tool_describe` / `tool_call`.
- Withheld as declared `write` (7): `browser_exec`, `computer_use`, `cronjob`,
  `execute_code`, `patch`, `terminal`, `write_file`.
- Withheld as **undeclared** (12): `a2a_*` (5) and `spotify_*` (7). These are
  plugin tools, and nobody classified them. This is the fail-closed rule working
  on real tools rather than on a fixture, and it has a real cost: with the switch
  on, those tools disappear. Declaring them is a config change, not a code
  change.
- The `tool_search` bridge catalog was checked separately, because a bridge that
  can invoke tools by name would bypass the whole guarantee if it read an
  unfiltered catalog. It does not: the catalog is built through the same
  `filter_tools` call, holds all ten delegate-wave tools including
  `session_start`, and holds zero mutators and zero undeclared tools.

## Literal Desktop evidence

2026-08-28. Hermes Desktop v0.20.5, commit `80477f6`, booted against
`.c5b-live-3` and `.c5b-desktop-userdata-2`, delegate-wave data root
`delegate-wave-c5b-live-2` on port 47332. One ordinary sentence typed into a new
conversation, with no mention of delegate-wave:

> In the repo at `D:\AssistantSystem\delegate-wave\dogfood\backpack-c5`, the run
> list's empty state always says "No runs in this view." When a filter is active
> it should instead say that no runs match the filter. Client-side only, and add
> a regression test.

Conversation `20260828_115501_56fd54`. The agent log records the new filter
firing on the real path:

```
11:55:02 delegate-wave routing on: withholding browser_exec, computer_use,
         cronjob, execute_code, patch, terminal, write_file (declared write)
11:55:19 tool mcp__delegate_wave__list_projects completed
11:55:26 tool mcp__delegate_wave__session_start completed
11:55:30 tool mcp__delegate_wave__session_poll completed
```

`(declared write)` is the tell: that suffix exists only on the capability path.
Hermes had no direct mutator to reach for, found the matching project, and
delegated. The Desktop showed "Work is underway in the matching delegate-wave
project."

## Desktop toggle live proof

2026-08-28. Desktop v0.20.5 at `2fa6424e42`, using the same isolated C5b
profile and delegate-wave server:

1. The Safety switch was turned OFF in the UI and Desktop was restarted.
2. In a fresh conversation, Hermes reported these directly visible mutators:
   `browser_exec`, `execute_code`, `patch`, `process`, `skill_manage`,
   `terminal`, and `write_file`.
3. The Safety switch was turned ON in the UI. No config file was edited by
   hand. A fresh conversation received an ordinary repository-change request.
4. The agent log recorded the declared-write filter and natural delegation:

```
14:12:12 delegate-wave routing on: withholding browser_exec, computer_use,
         cronjob, execute_code, patch, terminal, write_file (declared write)
14:12:31 tool mcp__delegate_wave__list_projects completed
14:12:38 tool mcp__delegate_wave__session_start completed
```

5. Desktop was restarted again. Settings → Safety visibly reopened with
   **Route repository changes** ON, proving persistence through the UI's normal
   profile-scoped config save path.

The proof job `job_8a77ee28-17c1-4b8a-a795-b6bec4b8f11e` was cancelled after
`session_start`; no implementation was integrated.

## Traps

- The Desktop resolves its backend Python by search and will happily boot a
  DIFFERENT Hermes install than the worktree under test — it picked the
  production install first and failed with a bare `401 Unauthorized`. Pin both
  `HERMES_DESKTOP_HERMES_ROOT` and `HERMES_DESKTOP_PYTHON`, and check
  `logs/desktop.log` for the `Using Hermes source at ...` line before believing
  any Desktop result.
- Electron is not installed in this worktree by default. `npm install
  electron@40.10.2 --no-save --engine-strict=false` restores it without touching
  the lockfile; `.npmrc` pins an npm range that the installed npm violates.
