# Skill Registry

Status: partial — skill discovery, metadata scanning, progressive prompt
loading, install/sync/guard tooling, usage tracking, and a Skills admin page
are implemented; the Ultra Studio allowlist profile and the two skill evals
are spec-only.
Date: 2026-06-11

Sources:

- Docs: `docs/ultra-studio-product-specs/04-skill-tool-prompt-contract.md`
  (§Skill Layers, §Required Skill Runtime Objects, §Required Creative
  Skills, §Non-Goals, §Acceptance),
  `docs/ultra-studio-agent-skill-tool-prompt-design.md` (§Skill Catalog
  Target, §Final Visible Catalog, §Progressive Disclosure Strategy, §Skill
  Package Structure, §Disabling and Deletion Plan),
  `06-delivery-plan.md` (P0 item 1, P3 item 1)
- Code (verified this session): `agent/skill_bundles.py` (`scan_bundles`,
  `list_bundles`, `save_bundle`, `delete_bundle`,
  `resolve_bundle_command_key`, `build_bundle_invocation_message`),
  `agent/prompt_builder.py` (`_build_skills_manifest`,
  `_load_skills_snapshot`, `_write_skills_snapshot`, `_parse_skill_file`,
  `clear_skills_system_prompt_cache`), `agent/skill_commands.py`,
  `agent/skill_preprocessing.py`, `agent/skill_utils.py`,
  `tools/skills_hub.py` (`SkillMeta`, `SkillBundle`, locked install paths,
  `_guarded_http_get`), `tools/skill_manager_tool.py`, `tools/skills_tool.py`,
  `tools/skills_sync.py`, `tools/skills_guard.py`,
  `tools/skills_ast_audit.py`, `tools/skill_provenance.py`,
  `tools/skill_usage.py`, `skills/` (category tree incl.
  `skills/creative/`, `skills/index-cache`), `web/src/pages/SkillsPage.tsx`,
  `agent/insights.py` (`_get_skill_usage`)

## Purpose & Scope

The Skill Registry owns discovery, enable/disable, versioning, and profile
filtering of skills (`04-skill-tool-prompt-contract.md` §Required Skill
Runtime Objects). Its product purpose: prevent the agent from "behaving like
a generic assistant with 87 unrelated skills" — Ultra Studio exposes a
focused creative skill set with progressive loading.

Skill layering contract (§Skill Layers): startup loads only skill metadata;
`SKILL.md` loads after routing; large schemas/compilers/rubrics live in
`references/` and load only when needed.

Scope: discovery and metadata, progressive loading, allowlist profiles,
install/update/removal, safety gates, usage telemetry, and the two required
evals. Routing decisions are `12-workflow-router.md`; the catalog shopping
surface is `04-marketplace.md`.

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Implemented | Skill tree on disk organized by category, with an index cache | `skills/` (e.g. `skills/creative/infographic-md-flow/SKILL.md`), `skills/index-cache` |
| Implemented | Bundle scanning, listing, save/delete, slash-command resolution, invocation message assembly | `agent/skill_bundles.py` |
| Implemented | Progressive disclosure into the system prompt: skills manifest + snapshot cache keyed by mtime, frontmatter parsing | `agent/prompt_builder.py` (`_build_skills_manifest`, `_load_skills_snapshot`, `_parse_skill_file`) |
| Implemented | Skill command handling and preprocessing on invocation | `agent/skill_commands.py`, `agent/skill_preprocessing.py`, `agent/skill_utils.py` |
| Implemented | Hub install path with metadata model, locked install paths, guarded HTTP fetch | `tools/skills_hub.py` |
| Implemented | Agent-facing skill management tools (list/install/manage/sync) | `tools/skill_manager_tool.py`, `tools/skills_tool.py`, `tools/skills_sync.py` |
| Implemented | Safety gates: acquisition guard, AST audit, write-origin provenance | `tools/skills_guard.py`, `tools/skills_ast_audit.py`, `tools/skill_provenance.py` |
| Implemented | Usage tracking and analytics | `tools/skill_usage.py`, `agent/insights.py` (`_get_skill_usage`) |
| Implemented | Skills admin page (browse/toggle) | `web/src/pages/SkillsPage.tsx` |
| Specified, not built | Skill Allowlist Profile — the Ultra Studio visible skill set as a named, enforced profile | `04-skill-tool-prompt-contract.md` §Required Skill Runtime Objects; `06-delivery-plan.md` P0 item 1 "Ultra profile/allowlist bootstrap" |
| Specified, not built | Skill Trigger Eval (routing picks the right skill) | `04-skill-tool-prompt-contract.md`; `06-delivery-plan.md` P3 item 1 |
| Specified, not built | Skill Output Contract Eval (handoff schema, missing fields) | `04-skill-tool-prompt-contract.md` |
| Specified, not built | P0 creative skills: `workflow-router`, `media-qa`, `prompt-repair`, `product-photoshoot`, `product-md-flow` as shipped skills (`infographic-md-flow` exists: `skills/creative/infographic-md-flow/`) | `04-skill-tool-prompt-contract.md` §Required Creative Skills |
| Specified, not built | Phase A disable-first / Phase B archive plan for upstream skills | `ultra-studio-agent-skill-tool-prompt-design.md` §Disabling and Deletion Plan |

## User Entry Points

- Implicit: every routed request may activate a skill chosen by
  `workflow-router` (planned routing) or slash command (implemented:
  `resolve_bundle_command_key`).
- Slash commands `/skill-name` in chat/TUI (implemented).
- Skills admin page for browsing and toggling (implemented:
  `SkillsPage.tsx`).
- Marketplace install flow (planned; wraps the hub install path).
- Agent self-service: skill management tools let the agent list/inspect
  skills (implemented), gated by guard rules.

## Feature List

| Feature | Status |
|---|---|
| Metadata-only startup loading (name/description/commands) | Implemented (manifest + snapshot in `prompt_builder.py`) |
| Load `SKILL.md` body only on activation | Implemented (bundle invocation path) |
| `references/` / `scripts/` / `assets/` lazy loading | Partial — package structure exists in skills tree; a formal Skill Resource Loader object is spec language (`04` §Required Skill Runtime Objects) |
| Enable/disable individual skills | Implemented (admin page + config) |
| Named allowlist profiles (Ultra profile filtering visible set) | Planned |
| Install from hub with locked paths + guarded fetch | Implemented (`tools/skills_hub.py`) |
| Acquisition safety: guard + AST audit + provenance | Implemented |
| Version tracking per skill | Partial — hub bundles carry versions (`SkillBundle`); local tree versioning is git-level only |
| Usage telemetry per skill | Implemented (`tools/skill_usage.py`, insights) |
| Trigger eval harness | Planned |
| Output contract eval harness | Planned |
| Upstream skill disable-first migration | Planned (Phase A/B plan) |
| Skill profiles per session (active skill profile in session state) | Planned (`02-agent-runtime-contract.md` §Session Lifecycle) |

## State Machine

Per-skill administrative state:

```text
discovered (on disk, scanned into manifest)
  -> enabled    (visible to routing/prompt for the active profile)
  -> disabled   (hidden from routing; files remain — disable-first rule)
enabled <-> disabled (admin/profile toggle)
disabled -> archived  (Phase B physical move, only after allowlist verification)
```

Per-invocation lifecycle:

```text
metadata in prompt -> routed/invoked -> SKILL.md loaded
  -> references loaded on demand -> executed -> usage recorded
```

Rules: upstream skills are never deleted before disable/allowlist
verification (`04-skill-tool-prompt-contract.md` §Non-Goals); enabling a
hub-installed skill requires the guard + audit pass to have succeeded.

## APIs & Events

Implemented (in-process / tool surface):

- `scan_bundles()` / `list_bundles()` / `get_bundle(name)` /
  `save_bundle(...)` / `delete_bundle(name)` — `agent/skill_bundles.py`.
- Slash resolution `resolve_bundle_command_key(command)` and invocation
  message assembly `build_bundle_invocation_message(...)`.
- Skill manifest/snapshot for the system prompt with cache invalidation
  (`clear_skills_system_prompt_cache`).
- Agent tools: skill listing/install/sync (`tools/skills_tool.py`,
  `tools/skill_manager_tool.py`, `tools/skills_sync.py`).
- Dashboard skills API consumed by `SkillsPage.tsx` (page verified; route
  shapes not re-derived here).

Planned:

- Profile API: resolve active profile -> visible skill set; persisted as
  "active skill profile" in session state.
- Eval harness entry points (trigger eval, output contract eval) runnable in
  CI.

No gateway events; registry changes surface through prompt rebuild and admin
page refresh.

## Data Model

Implemented:

- Skill package: directory with `SKILL.md` (frontmatter: name, description,
  triggers), optional `references/`, `scripts/`, `assets/`
  (`ultra-studio-agent-skill-tool-prompt-design.md` §Skill Package
  Structure; parsing in `_parse_skill_file`).
- Bundle records: `SkillMeta` / `SkillBundle` (hub metadata, install path,
  category) — `tools/skills_hub.py`.
- Manifest snapshot: name -> [offsets/mtimes] cache for prompt assembly
  (`_build_skills_manifest`).
- Usage records in the insights DB (`_get_skill_usage`).

Planned:

```text
skill_profiles
- profile_id            (e.g. "ultra-studio")
- visible_skills[]      (allowlist, not blocklist)
- default_for_surface: web | tui | cli
```

## UI Behavior

- Skills page groups by category, shows enabled state, description, and
  source (bundled vs hub-installed); toggling updates the active manifest.
- A disabled skill stays listed (visible-but-disabled), consistent with the
  Marketplace visibility rule.
- Hub installs surface guard/audit results before enablement; a rejected
  install shows the reason verbatim.
- The chat surface never shows the raw skill registry; users see the effect
  (workflows available) via Marketplace and router behavior.
- Slash command popover (`web/src/components/SlashPopover.tsx`) lists
  command-capable skills for the active profile.

## Permissions & Error Handling

- Skill installation/modification is admin-scoped; agent-initiated writes
  carry write-origin provenance (`tools/skill_provenance.py`
  `set_current_write_origin`) so background/agent writes are
  distinguishable from user actions.
- Guard failures are terminal for the install (no partial enable);
  `tools/skills_guard.py` rejections must surface verbatim.
- Errors: `skill_not_found` (unknown slash/bundle key), `skill_disabled`
  (invocation against disabled skill — must state which profile disabled
  it), `skill_install_rejected` (guard/audit), `skill_sync_conflict`
  (sync divergence via `tools/skills_sync.py`).
- Profile misconfiguration (empty visible set) must fail loudly at startup,
  not silently expose all skills — the failure mode the Ultra profile
  exists to prevent.

## Acceptance Criteria

- With the Ultra profile active, the visible skill list contains only the
  catalog target set ("The visible skills list is Ultra-focused",
  `04-skill-tool-prompt-contract.md` §Acceptance).
- A generic video request does not trigger unrelated skills (ASCII / Comfy /
  Manim) — verified by trigger eval cases.
- Startup prompt contains metadata only; `SKILL.md` bodies load on
  activation (observable via prompt size/snapshot inspection).
- Disabling a skill removes it from routing within one prompt rebuild and
  the files remain on disk.
- A hub install with a failing AST audit never reaches enabled state.
- Skill usage rows appear in insights after invocations.

## Non-Goals

- Being the catalog UI (Marketplace) or the router (workflow-router).
- Deleting upstream skills as a cleanup strategy — disable/allowlist first.
- Letting plugins replace workflow skills
  (`04-skill-tool-prompt-contract.md` §Non-Goals).
- Skill marketplace publishing (P3).
- Per-skill sandboxing/runtime isolation (skills are prompt+resources;
  execution isolation belongs to `14-sandbox-lifecycle.md`).

## Open Questions

1. Profile storage and precedence: config file vs DB; what happens when
   session state's "active skill profile" conflicts with the deployment
   default?
2. Eval harness shape: do trigger/output evals block CI (the existing
   workflow-router spec leaves the pass bar open — its Open Question 9)?
3. Version pinning for hub installs vs git-tracked bundled skills — is an
   installed skill upgradeable in place, and who approves?
4. Does the Ultra profile filter the admin Skills page too, or only
   runtime visibility?
5. The formal Skill Resource Loader: is current lazy loading sufficient, or
   does `references/` need explicit load APIs with budget accounting?
6. Multi-surface profiles: do TUI/CLI sessions get the Ultra profile or the
   full Hermes set by default?
