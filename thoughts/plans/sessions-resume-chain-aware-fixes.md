# Plan: Chain-Aware Sessions Listing & Resume Fixes

Branch: `sessions-resume-improvements` | Base: `main` | HEAD: `980588619`

## Overview

The sessions listing/search/resume surfaces diverge in three ways: (1) bare-number
resume selection is pagination-blind, (2) chain-aware display (tokens, rank, preview,
last-active) is computed differently across the CLI list, CLI search, and gateway
paths, and (3) lineage walking excludes different child kinds per call site. This plan
fixes all 15 findings from the review at
`/tmp/opencode/sessions-resume-improvements-review.md`, unifying the semantics onto
one shared pipeline while keeping the fix surface minimal and behavior-preserving
where no bug exists.

Scope (user-confirmed): **all 15 findings**, one PR.

## Current State

- **Listing**: `cli.py:_list_recent_sessions` (7591+) → `hermes_state.list_sessions_rich`
  (6471; projection/tip-swap at 6743-6756) → `session_listing.render_sessions_table`
  (242+; preview walk 306-331).
- **Search**: two near-identical pipelines — `hermes_cli/main.py:18003-18094` (FTS →
  dedup → rank → root-preview → `COALESCE(ended_at, started_at, 0)` sort) and
  `hermes_cli/cli_commands_mixin.py:1144-1263`. Both hardcode
  `exclude_sources=["tool","subagent","cron"]`.
- **Resume**: `cli.py:_consume_pending_resume_selection` (8050-8086; bounds bug at
  8080) → `_handle_resume_command` (`cli_commands_mixin.py:854+`; correct local-index
  conversion at 944-949).
- **Gateway**: `gateway/slash_commands.py:_list_titled_sessions` (4350-4353) calls
  `list_sessions_rich(source=user_source, limit=10)` with **no** `exclude_sources`
  and no dedup/rank; `/resume` resolves via `resolve_resume_session_id` (call at
  4413); `_handle_sessions_command` (4484-4548); second listing surface at
  `api_server.py:3036`.
- **Tests**: `tests/cli/test_cli_resume_command.py:234-280` arms selection with
  `MagicMock` (no real DB); pytest is **not installed** in `./venv`.

## Desired End State

- Bare-number resume works from any page (page-2+ selections resolve correctly).
- `Tok` column shows the **chain total** (sum across all generations) with an
  explicitly renamed header; tip-only values are gone.
- Rank, root-preview, last-active, and child-walking use one consistent definition
  across CLI list, CLI search, and gateway.
- Gateway listing excludes tool sessions, dedups compression chains, and numbers by
  the same ranked order as the CLI.
- The 8 relevant test files run real-DB (temp `HERMES_HOME`, real `SessionDB`).

## What We're NOT Doing

- No schema/migration changes (`sessions` table untouched).
- No changes to `resolve_resume_session_id` semantics — its child-follow SQL
  (`hermes_state.py:7781-7789`) is the canonical "next continuation" query and
  becomes the shared template.
- No changes to compression/compaction logic itself.
- No FTS ranking changes.
- No new core tools, no new config keys, no new env vars.
- No per-session `get_compression_tip` N+1 in listing — chain totals via batch SQL.
- F11 (rank-lookup 2000 cap) is folded into F9 as a defensive cap + `"?"` fallback;
  no unbounded lookups.
- F14 folds into F2 (one shared `last_active` definition).

## Implementation Approach

Extract shared helpers into `hermes_cli/session_listing.py`; child-kind exclusions
mirror `resolve_resume_session_id` / `get_compression_tip` (`_branched_from IS NULL`,
`_delegate_from IS NULL`, `source != 'tool'`). One commit per finding, tests
co-located, each finding carries Automated + Manual success criteria.

## Phases

### Phase 0 — Environment setup

1. `uv pip install -e ".[dev]"` (installs pytest into `./venv`).
2. Sanity run: `scripts/run_tests.sh tests/cli/test_cli_resume_command.py -q`
   (expect green baseline).

### Phase 1 — P0: F1-F7 (Must fix)

#### F1 — Offset-aware bare-number guard

- **Where**: `cli.py:8080-8083` (`_consume_pending_resume_selection`).
- **Bug**: `if index > len(pending)` bounds a **global** number (page 2 renders
  #11-20 via 7638-7640) against page-relative len=10. First valid selection on
  page 2+ dies here.
- **Fix**: compute `local = index - self._pending_resume_offset`; guard
  `local < 1 or local > len(pending)`; forward the **global** number to
  `_handle_resume_command` (its conversion at `cli_commands_mixin.py:944-949` is
  already correct). No behavior change for page 1.
- **Automated**: new real-DB test — 15 sessions, arm `/resume list 2` (offset=10),
  select #12, assert it resolves.
- **Manual**: `hermes`, `/resume list 2`, type `12` → session 12 loads.

#### F2 — Shared `last_active` (folds F14)

- **Where**: `main.py:18033-18044` + `cli_commands_mixin.py:1192-1197` (search sort
  uses `COALESCE(ended_at, started_at, 0)`); listing uses `MAX(messages.timestamp)`.
- **Fix**: one helper in `session_listing.py`, e.g.
  `last_active_of(session_db, session_ids) -> dict[id, float]` =
  `SELECT id, MAX(timestamp) FROM messages GROUP BY id`, falling back to
  `COALESCE(ended_at, started_at, 0)` for sessions with no messages. Use it for
  both the `last_active` display column **and** the search sort key (F14).
- **Automated**: real-DB test — session with ended_at but no newer messages sorts
  identically under listing and search.
- **Manual**: `hermes sessions search <term>` ordering matches
  `hermes sessions` ordering.

#### F3 — Tok = chain total, header renamed

- **Where**: `hermes_state.py:6746-6756` (projection tip-swaps 11 keys but **not**
  token columns); header row in `session_listing.py:render_sessions_table`.
- **Fix (user decision: chain total)**: when a row projects a root→tip pair, sum
  `input_tokens`/`output_tokens` across **all** generations in the chain. Batch SQL:
  collect lineage ids per projected root via `_COMPRESSION_CHILD_SQL` walk, then one
  `SELECT root, SUM(input_tokens), SUM(output_tokens) FROM sessions WHERE id IN (...)
  GROUP BY root`. Rename header (e.g. `Tok(ΣIn/ΣOut)`) to make semantics explicit.
- **Automated**: real-DB test seeding a 3-gen chain asserts displayed tokens =
  sum of all three generations (not tip's, not root's).
- **Manual**: `hermes sessions` on a branched DB shows summed tokens; header label
  differs from old `Tok(In/Out)`.

#### F4 — Preview honors `_lineage_root_id`

- **Where**: `session_listing.py:306-331` (render preview walk only follows
  `parent_session_id`; projected rows keep root's `parent_session_id=None`).
- **Fix**: when the row carries `_lineage_root_id` (already projected at
  `hermes_state.py:6754`), use it directly as the root id for the first-user-message
  fetch; keep the parent-chain walk only as fallback. Both call sites
  (`cli.py:7654`, `main.py:17170`) then render correct previews without
  `preview_lookup`.
- **Automated**: real-DB test — projected (compressed) session row renders root's
  first user message in preview.
- **Manual**: `hermes sessions` shows root previews for compressed sessions.

#### F5 — `session_rank` forward walk excludes branch/delegate/tool

- **Where**: `session_listing.py:160-163` — raw
  `WHERE parent_session_id = ? ORDER BY started_at DESC LIMIT 1`.
- **Fix**: mirror `resolve_resume_session_id` child SQL (`hermes_state.py:7781-7789`):
  `_branched_from IS NULL AND _delegate_from IS NULL AND source != 'tool'`, keep
  `ORDER BY started_at DESC, id DESC LIMIT 1`.
- **Automated**: real-DB test — subagent/tool/branched child does not break the rank
  chain; compression chain ranks 1..N tip-ward.
- **Manual**: search results on a DB with subagent children show ranks consistent
  with `resolve_resume_session_id`.

#### F6 — `_compression_root` rejects `_delegate_from` / `source='tool'`

- **Where**: `session_listing.py:184` (only `_is_branch_child_row` check).
- **Fix**: also reject `_delegate_from` and `source == 'tool'` while walking root
  (mirror walker exclusions). Optional follow-up: audit call sites of
  `_is_compression_child_row` (`hermes_state.py:9478-9483`) before widening it.
- **Automated**: real-DB test — delegate/tool children of a compression parent do
  not become the root.
- **Manual**: compression chain root detection unchanged on known-good DBs.

#### F7 — Real-DB test foundation

- **Where**: `tests/cli/test_cli_resume_command.py` (MagicMock arming 234-280 →
  real `SessionDB(tmp_path/"state.db")` + `HermesCLI.__new__` arming, seeding via
  direct SQL with `TZ=UTC`), following real-DB patterns at
  `tests/hermes_cli/test_session_listing.py:45-55` and
  `tests/hermes_cli/test_resolve_last_session.py:49-84`.
- **Fix**: replace `MagicMock` session-listing fakes with real DB for resume arming;
  add offset>0 pagination tests (F1 coverage lands here).
- **Automated**: the rewritten file passes with no `MagicMock` on session
  listing/resume paths.
- **Manual**: n/a (test-only).

### Phase 2 — P1: F8, F10, F12, F13

#### F8 — Delete dead page-parse branch

- **Where**: `cli_commands_mixin.py:1272-1277`.
- **Bug (proven)**: guard `sub in {"list","ls","browse"}` matches only a bare word;
  `list 2` falls through to `/resume list 2`. The page-parse block is unreachable
  and misleading.
- **Fix**: remove the dead block; `offset = 0` directly.
- **Automated**: real-DB test — `sessions list 2` still delegates to `/resume list 2`
  and pages correctly.
- **Manual**: `/sessions list 2` renders page 2 (numbered 11+).

#### F10 — Workspace filter before offset

- **Where**: `main.py:17103-17121` — SQL `OFFSET` applied, then Python workspace
  filter narrows the page.
- **Fix**: when `_ws_filter` is set, fetch a superset (or no SQL offset) and apply
  the Python workspace filter **first**, then slice by offset — matching
  `query_session_listing`'s filter-then-offset ordering.
- **Automated**: real-DB test with a workspace-filtered listing page >1.
- **Manual**: `hermes sessions --workspace X` paginates to the correct sessions.

#### F12 — Warn on invalid `--limit`

- **Where**: `cli_commands_mixin.py:874-878` (silent `pass` on `ValueError`).
- **Fix**: print a warning (e.g. `_cprint("  Invalid --limit value ...; using
  default N")`) and keep consuming the token pair. No behavioral change to valid
  input.
- **Automated**: test asserting warning output for `--limit abc`.
- **Manual**: `/sessions --limit abc` shows a warning, not silence.

#### F13 — Unify hop caps

- **Where**: `session_listing.py:156` (cap 20), `:171` (cap 50),
  `hermes_state.py:6389` (100).
- **Fix**: one shared constant (canonical value 100, defined in `hermes_state.py`,
  imported by `session_listing.py`), used by `session_rank`, `_compression_root`,
  and `get_compression_tip`.
- **Automated**: no change-detector test; unit test that the constant is used by all
  three walkers.
- **Manual**: n/a (internal).

### Phase 3 — P2: F9, F15

#### F9 — Extract shared search pipeline (folds F11)

- **Where**: `main.py:18003-18094` + `cli_commands_mixin.py:1144-1263` → target
  `session_listing.py`.
- **Fix**: extract one function, e.g.
  `search_sessions(session_db, query, *, limit, exclude_sources, ...)` doing
  FTS → dedup (`dedup_compression_chains`) → rank (`session_rank`/`session_rank_lookup`,
  capped with `"?"` fallback for F11) → root preview (F4-aware) → `last_active`
  sort (F2 helper) → row dicts. Both call sites and the gateway route through it.
- **Automated**: real-DB test — CLI search and gateway search return identical
  ordered results for the same query; >2000-hit DB shows `"?"` instead of crashing.
- **Manual**: `hermes sessions search` and gateway `/sessions search` agree.

#### F15 — Gateway listing semantics

- **Where**: `gateway/slash_commands.py:4350-4353` (`_list_titled_sessions`), 4413
  (`resolve_resume_session_id` call), 4484-4548 (`_handle_sessions_command`);
  `api_server.py:3036`.
- **Fix**: route gateway listing through `query_session_listing` (or the F9
  pipeline) with `exclude_sources=["tool"]`; apply dedup + rank; keep positional
  numbering consistent with the shown list so `/resume <n>` maps to the displayed
  session. Add gateway tests (`tests/gateway/test_resume_command.py`).
- **Automated**: real-DB gateway test — tool sessions absent from `/resume` picker;
  numbered list maps 1:1 to `resolve_resume_session_id`.
- **Manual**: Telegram `/sessions` and `/resume` show a deduped, tool-free, ranked
  list; picking `5` resumes the 5th displayed session.

## Testing Strategy

- Real DB only: `SessionDB(db_path=tmp_path/"state.db")`, seed via direct SQL,
  `TZ=UTC`, no mocks of session-listing/resume functions (per AGENTS.md Testing).
- Run: `scripts/run_tests.sh` on
  `tests/cli/test_cli_resume_command.py`,
  `tests/hermes_cli/test_session_listing.py`,
  `tests/hermes_cli/test_resolve_last_session.py`,
  `tests/gateway/test_resume_command.py`.
- Assert **behavior contracts**, not snapshots (no model lists, no config-version
  literals, no change-detector tests).

## Performance

- Chain-total tokens (F3): one batch `SUM ... GROUP BY` per page — no per-row
  `get_compression_tip` calls.
- `last_active` (F2): single `GROUP BY` per search.
- Rank lookup (F9/F11): capped; unbounded lookups explicitly excluded.

## Migration

None — no schema, config, or env changes.

## References

- Review report: `/tmp/opencode/sessions-resume-improvements-review.md` (360 lines,
  fully read; findings 1-15, priority table at 306-310).
- `hermes_cli/session_listing.py` — parse args:8, `query_session_listing`:45,
  `format_gateway_session_listing`:95, `session_rank_lookup`:124 (cap 2000),
  `session_rank`:141 (walk 156-163), `_compression_root`:171 (cap 184), `root_started_at`:196,
  `dedup_compression_chains`:209, `render_sessions_table`:242 (preview walk 306-331).
- `cli.py` — `_pending_resume_*` init 4538-4545; `_list_recent_sessions` 7591; page
  numbering 7638-7640; `_consume_pending_resume_selection` 8050-8086 (bug 8080);
  reset 9494-9502.
- `hermes_cli/cli_commands_mixin.py` — `_handle_resume_command` 854+ (conversion
  944-949); silent `--limit` swallow 874-878; `_handle_sessions_command` 1081+
  (dead page-parse 1272-1277); search 1144-1263.
- `hermes_cli/main.py` — list 17100-17134 (`_exclude`:17098, workspace filter
  17112-17121 post-offset); search 18003-18094 (sort SQL 18035-18044).
- `hermes_state.py` — `_BRANCH_CHILD_SQL`:210, `_COMPRESSION_CHILD_SQL`:214,
  `_LISTABLE_CHILD_SQL`:222, `_ephemeral_child_sql`:225; `get_compression_tip`:6360
  (walker 6387-6410, cap 100); `list_sessions_rich`:6471 (projection 6743-6756);
  `resolve_resume_session_id` child-follow 7781-7789; `_is_branch_child_row`:9468,
  `_is_compression_child_row`:9478-9483, `get_compression_lineage`:9486.
- `gateway/slash_commands.py` — `_list_titled_sessions` 4350-4353;
  `resolve_resume_session_id` call 4413; `_handle_sessions_command` 4484-4548;
  `api_server.py:3036`.
- Tests — `tests/cli/test_cli_resume_command.py` (MagicMock 234-280),
  `tests/hermes_cli/test_session_listing.py`, `tests/hermes_cli/test_resolve_last_session.py:49-84`,
  `tests/gateway/test_resume_command.py`.
