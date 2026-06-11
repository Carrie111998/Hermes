# Marketplace

Status: spec-only as a product surface — no Marketplace page, route, or catalog
API exists in code; adjacent machinery (skill install/enable, plugin manifests,
skills hub) is implemented and is the natural substrate.
Date: 2026-06-11

Sources:

- Docs: `docs/ultra-studio-product-specs/01-product-surface.md`
  (§Information Architecture, §Left Nav Shell),
  `05-memory-marketplace-files.md` (§Marketplace, §Search, §Access Control,
  §Acceptance), `04-skill-tool-prompt-contract.md` (§Required Skill Runtime
  Objects, §Required Creative Skills), `06-delivery-plan.md` (P1 item 8,
  P3 item 2), `docs/ultra-studio-agent-skill-tool-prompt-design.md`
  (§Skill Catalog Target, §Final Visible Catalog)
- Code (adjacent machinery, verified this session): `tools/skills_hub.py`,
  `tools/skill_manager_tool.py`, `tools/skills_tool.py`, `tools/skills_sync.py`,
  `tools/skills_guard.py`, `agent/skill_bundles.py`,
  `web/src/pages/SkillsPage.tsx`, `web/src/pages/PluginsPage.tsx`,
  `skills/` (category directories), `optional-skills/`

## Purpose & Scope

Marketplace is the catalog surface for reusable creative capabilities:
workflow skills, prompt recipes, storyboard templates, model recipes, reusable
Elements, character packs, and project templates
(`05-memory-marketplace-files.md` §Marketplace). It answers "what can this
product do for me" without requiring the user to know skill names.

Per the same spec, Marketplace "is not a public app store in the first
version. It can start as a local catalog backed by checked-in skill metadata
and curated templates." Publishing flows are explicitly deferred to P3
(`06-delivery-plan.md` P3 item 2).

This spec covers the catalog model, item lifecycle, browse/install/enable
behavior, and the boundary against the Skill Registry
(`11-skill-registry.md`): the Skill Registry owns runtime discovery, loading,
and allowlist enforcement; Marketplace owns the user-facing catalog,
presentation, and acquisition flow.

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Implemented (adjacent) | Skill bundle scan/list/save/delete and slash-command resolution | `agent/skill_bundles.py` (`scan_bundles`, `list_bundles`, `save_bundle`, `delete_bundle`, `resolve_bundle_command_key`) |
| Implemented (adjacent) | Skills hub metadata model and guarded remote fetch with locked install paths | `tools/skills_hub.py` (`SkillMeta`, `SkillBundle`, `_normalize_lock_install_path`, `_guarded_http_get`) |
| Implemented (adjacent) | Skill install/management tool surface for the agent | `tools/skill_manager_tool.py`, `tools/skills_tool.py`, `tools/skills_sync.py` |
| Implemented (adjacent) | Skill safety gates on acquisition | `tools/skills_guard.py`, `tools/skills_ast_audit.py`, `tools/skill_provenance.py` |
| Implemented (adjacent) | Dashboard pages listing skills and plugins with enable/disable | `web/src/pages/SkillsPage.tsx`, `web/src/pages/PluginsPage.tsx` |
| Implemented (adjacent) | Checked-in skill corpus organized by category | `skills/` (e.g. `skills/creative/`), `optional-skills/` |
| Specified, not built | Marketplace nav entry and browsable catalog page | `01-product-surface.md` §Left Nav Shell; zero `marketplace` hits in `web/src`, `agent/`, `plugins/`, `gateway/` (rg, this session) |
| Specified, not built | Marketplace item envelope (`kind`, `inputs_schema`, `provider_constraints`, `status`) | `05-memory-marketplace-files.md` §Marketplace |
| Specified, not built | Non-skill item kinds: recipes, templates, element packs, character packs | `05-memory-marketplace-files.md` §Marketplace |
| Specified, not built | Marketplace search integration with typed result cards | `05-memory-marketplace-files.md` §Search |
| Specified, not built | Marketplace local catalog milestone | `06-delivery-plan.md` P1 item 8 |
| Specified, not built | Publishing flow | `06-delivery-plan.md` P3 item 2 |

## User Entry Points

- `Marketplace` entry in the left nav (planned; see `01-left-nav-shell.md`).
- Cross-surface Search returning marketplace items as typed cards (planned,
  `05-memory-marketplace-files.md` §Search).
- Router fallback: when `workflow-router` finds no matching workflow, it may
  point the user at the Marketplace entry for the closest available workflow
  (planned; router contract in `12-workflow-router.md`).
- Today's closest entry points: the dashboard Skills page
  (`web/src/pages/SkillsPage.tsx`) and Plugins page
  (`web/src/pages/PluginsPage.tsx`), which list and toggle capabilities but
  are admin-oriented, not catalog-oriented.

## Feature List

| Feature | Status |
|---|---|
| Browse catalog grouped by category and kind | Planned |
| Item detail view: description, inputs schema, output type, required tools, provider constraints, version | Planned |
| Install / enable / disable an item per workspace | Planned (skill-level enable/disable machinery exists: `tools/skill_manager_tool.py`, `web/src/pages/SkillsPage.tsx`) |
| Show `installed / available / disabled / deprecated` status per item | Planned |
| Local catalog backed by checked-in skill metadata | Planned (metadata source exists: `agent/skill_bundles.py`, `skills/`) |
| Curated template entries (storyboard, project templates) | Planned; no template registry exists in code |
| Element packs / character packs as catalog items | Planned; depends on Asset Service references (`09-asset-service.md`) |
| Model recipes as catalog items | Planned; depends on Model Catalog (`19-model-catalog-provider-constraints.md`) |
| Visible-without-enabled items | Planned (`05-memory-marketplace-files.md` §Access Control) |
| Search marketplace entries with typed result cards | Planned |
| Publishing user-authored items | Planned, P3 only |
| Safety review on acquisition (provenance, AST audit) | Implemented for skills (`tools/skills_guard.py`, `tools/skills_ast_audit.py`); not wired to a Marketplace UI |

## State Machine

Item status, per `05-memory-marketplace-files.md` §Marketplace:

```text
available -> installed -> disabled -> installed
installed -> deprecated (catalog owner action)
available -> deprecated
```

- `available`: visible in catalog, not active in any runtime profile.
- `installed`: acquired into the workspace; the Skill Registry may still
  filter it out of the active profile (see `11-skill-registry.md`).
- `disabled`: acquired but explicitly off; must stay visible
  (`05-memory-marketplace-files.md` §Access Control: "Marketplace items can be
  visible without being enabled").
- `deprecated`: remains inspectable for provenance; cannot be newly installed.

Transitions are user actions except `deprecated`, which is a catalog-owner
action. No automatic install is allowed; installing a skill item must not
bypass the skills guard path (`tools/skills_guard.py`).

## APIs & Events

No marketplace API exists in code. Proposed contract (consistent with the
asset/service envelope style of the pack):

```http
GET  /api/marketplace/items?kind=&category=&status=&q=&cursor=
GET  /api/marketplace/items/{item_id}
POST /api/marketplace/items/{item_id}/install
POST /api/marketplace/items/{item_id}/disable
POST /api/marketplace/items/{item_id}/enable
```

Item envelope (verbatim from `05-memory-marketplace-files.md`):

```yaml
id:
kind: skill | recipe | template | element_pack | character_pack
title:
description:
category:
inputs_schema:
output_type:
required_tools:
provider_constraints:
version:
status: installed | available | disabled | deprecated
```

Events (proposed, following the gateway event naming of
`02-agent-runtime-contract.md`):

- `marketplace.item.installed`
- `marketplace.item.status_changed`

Existing adjacent surface: skill management runs through agent tools
(`tools/skill_manager_tool.py`, `tools/skills_tool.py`) and the dashboard
config API used by `web/src/pages/SkillsPage.tsx`; a Marketplace API should
wrap these rather than duplicate them.

## Data Model

Planned entities (no persistence exists today):

```text
marketplace_items
- id
- kind: skill | recipe | template | element_pack | character_pack
- title, description, category
- inputs_schema (JSON Schema)
- output_type
- required_tools (list)
- provider_constraints (ref into model catalog constraints)
- version
- source: checked_in | curated | user_published (P3)

marketplace_install_state
- item_id
- workspace_id
- status: installed | available | disabled | deprecated
- installed_by, installed_at
```

For `kind=skill`, the item must reference the existing skill metadata as the
single source of truth (`agent/skill_bundles.py` bundle files, `skills/*/`
SKILL.md frontmatter) instead of copying it. For `kind=element_pack` /
`character_pack`, items reference `asset_references` owned by the Asset
Service (`docs/hermes-asset-library-backend-design.md` §核心实体); the
Marketplace must not own asset state.

## UI Behavior

- Catalog grid grouped by category; each card shows kind badge, title,
  one-line description, status chip, and version.
- Detail view shows the full item envelope, including `inputs_schema`
  rendered as a field list, `required_tools`, and `provider_constraints`.
- Install/enable/disable actions are explicit buttons with confirmation; no
  hover-install or auto-enable.
- Disabled and deprecated items render with distinct visual state but remain
  clickable for inspection.
- A marketplace search result card must be visually typed so "a model recipe
  does not look like a generated image" (`05-memory-marketplace-files.md`
  §Search).
- Non-goal: do not merge Marketplace into a generic "Assets" page
  (`01-product-surface.md` §Non-Goals).

## Permissions & Error Handling

Permissions (from `05-memory-marketplace-files.md` §Access Control):

- `read`: any workspace member can browse the catalog, including disabled
  items.
- `use`: requires `installed` status and registry-side profile inclusion.
- `update` / `delete`: catalog curation rights; in the local-catalog phase
  this means repo maintainers, not end users.

Error contract (typed, per `02-agent-runtime-contract.md` §Error Contract
style):

| Error | Trigger |
|---|---|
| `marketplace_item_not_found` | Unknown or cross-workspace item id. |
| `marketplace_item_deprecated` | Install attempt on deprecated item. |
| `skill_guard_rejected` | Skill item failed the acquisition safety gate (`tools/skills_guard.py`); the rejection reason must be shown, not swallowed. |
| `required_tool_missing` | Item declares `required_tools` that the current deployment does not provide. |

Failures must be explicit. An item that cannot run because a required tool or
provider is missing renders as blocked-with-reason, never as silently hidden.

## Acceptance Criteria

- Left nav exposes a Marketplace entry that opens a catalog page.
- The catalog lists checked-in workflow skills with correct
  installed/available/disabled status (P1 gate:
  "Marketplace shows available workflows and status",
  `06-delivery-plan.md`).
- Installing a skill item routes through the existing guard path and the
  result is visible in both Marketplace and the Skill Registry state.
- A disabled item remains visible and inspectable.
- Search returns marketplace entries as typed cards distinct from assets,
  files, and memory.
- No item kind other than `skill` is required for P1; recipes, templates, and
  packs may render as "coming soon" categories without fake entries.

## Non-Goals

- Public app store, payments, or third-party publishing in P0-P2.
- Replacing the Skill Registry: Marketplace never loads skill content into
  the agent context; it only changes acquisition/enable state.
- Auto-installing skills based on router decisions.
- Owning Element/character pack asset state (Asset Service owns it).
- Marketplace-driven removal of upstream Hermes skills
  (`04-skill-tool-prompt-contract.md` §Non-Goals: disable/allowlist first).

## Open Questions

1. Catalog source of truth for non-skill kinds: checked-in YAML next to
   skills, or a small DB table seeded at startup?
2. Is install state per-workspace or per-user? `05-memory-marketplace-files.md`
   scopes memory by user/workspace/project but is silent for marketplace
   install state.
3. Version pinning: when a checked-in skill updates, do installed items
   auto-upgrade or hold the installed version? (`tools/skills_hub.py` lock
   install paths suggest pinning is intended for hub installs.)
4. Does the Ultra allowlist profile (`04-skill-tool-prompt-contract.md`)
   filter the catalog view itself, or only runtime activation?
5. How do Marketplace search results rank against assets/files/memory in the
   unified Search surface?
6. Whether `PluginsPage`-style per-plugin visibility flags should be unified
   into marketplace install state or remain a separate admin concept.
