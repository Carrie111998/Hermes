# Design: Thread-Scope Isolation

**Status:** draft — implementation in progress on `feat/thread-scope-isolation`.
**Severity:** P2 — Hermes reports unrelated work as progress on the conversation the user is actually asking about; this erodes trust in any status/progress answer.

## Root cause

A progress question asked in one Discord thread was answered using evidence
that did not belong to that thread: broad `session_search` summaries and
`tmux`/branch activity from *other* threads that happen to share the same git
repository. The failure has two independent causes that compound:

1. **`session_search` has no notion of "this conversation."**
   `_discover()` / `_list_recent_sessions()` (`tools/session_search_tool.py`)
   query across the *entire profile* database. Nothing scopes the search to
   the session that is asking. A conversation summary from a different
   thread reads, to the model, exactly like a summary of its own past work.

2. **Repository proximity is mistaken for ownership.** `process_registry.py`
   tracks tmux/terminal sessions by `session_key`, and `async_delegations`
   tracks delegations by `origin_session` — both already carry *some*
   identity — but nothing aggregates "what does *this* conversation own" into
   one place the agent can check before answering a status question. So the
   agent falls back to "what's active in this repo right now," which is
   correct for *a* thread, not necessarily *this* thread.

Neither cause is a missing individual identity field — `gateway/session.py`
already resolves precise per-thread session keys, and several subsystems
already stamp their own identity fields. The gap is a missing **aggregation
layer**: nothing durably answers "what artifacts, in flight or completed,
belong to this specific scope of work" in a way that's queried by default and
fails closed when it doesn't know.

## Existing building blocks (do not duplicate)

Confirmed by codebase research before writing this doc — see file:line
references throughout. This design **extends** these rather than growing new
parallel machinery, per `AGENTS.md`'s "Extend, don't duplicate."

| Need | Existing mechanism | Location |
|---|---|---|
| Normalized cross-platform identity | `gateway.session.SessionSource` (`platform`, `chat_id`, `chat_type`, `thread_id`, `parent_chat_id`, `scope_id`, `profile`) | `gateway/session.py:148-309` |
| Per-thread session key | `build_session_key()` | `gateway/session.py:1046-1155` |
| Profile isolation for on-disk state | `get_hermes_home()` / `get_process_hermes_home()` | `hermes_constants.py:114,142` |
| Atomic, mode-aware file writes | `atomic_json_write(path, data, mode=0o600)` → `atomic_replace()` | `utils.py:174,91` |
| Advisory cross-platform file lock | `_FileLock` (`fcntl.flock` / `msvcrt.locking`) | `hermes_cli/active_sessions.py:129` |
| Fail-closed corrupt-state handling | `_read_entries()` — catch, warn, return `[]`, never trust partial data | `hermes_cli/active_sessions.py:182-194` |
| Positive-proof ownership filtering | `process_registry.py` requeue-for-owner logic (`owns_event()` vs. bare key equality) | `tools/process_registry.py:1276-1348` |
| Delegation identity | `async_delegations.origin_session` / `origin_ui_session_id` / `parent_delegation_id` | `tools/async_delegation.py:142-161,220,293,497` |
| tmux/terminal session identity | `ProcessSession.session_key` | `tools/process_registry.py:97,1815,1887-1912` |
| Cron job origin | `create_job(origin=...)`, built from `HERMES_SESSION_*` env vars | `cron/jobs.py:1246,1426`; `tools/cronjob_tools.py:315-338` |
| CLI subcommand family pattern | `register_cli(subs)` + `hermes_cli/main.py` wiring | `hermes_cli/curator.py:680` |
| Schema migration pattern | version-gated `CREATE TABLE IF NOT EXISTS`, `SCHEMA_VERSION` bump | `hermes_state_schema.py:508,536,750-763`; `SCHEMA_VERSION = 23` at `hermes_state_common.py:105` |

Confirmed **absent** (no design conflict, but no shortcut either):
- No scope/project/goal concept anywhere in the session schema.
- Todos (`tools/todo_tool.py`) carry no session/scope identity — in-memory
  per `AIAgent` instance only.
- No durable git branch/worktree registry — git state is queried live.
- No PR-ownership tracking anywhere.
- Discord guild `scope_id` is **deliberately excluded** from
  `build_session_key()` (comment at `gateway/session.py:1062-1064`) — thread
  isolation today rests on `thread_id` alone, with no durable object above it
  representing "the piece of work this thread is for."

## Design

### 1. Scope identity (normalization, not a new ID scheme)

A scope is identified by the same fields `SessionSource`/`build_session_key`
already resolve — **never** by display name (channel name, thread title,
user nickname). The scope identity tuple is:

```
(profile, platform, account_id, guild/workspace scope_id, chat_id, thread_id, topic)
```

`account_id` (the bot/credential identity) and `topic` are included because
the same platform+guild+channel can host more than one active piece of work
(e.g. two threads under one channel, or one thread revisited for two
unrelated goals over time) — `topic` is an explicit, user- or
Hermes-assigned label at `scope create` time, never inferred from message
text. Any field that is missing, unresolvable, or ambiguous (e.g. adapter
didn't supply a `thread_id` where the platform has threads) causes scope
resolution to fail closed — see "Fail-closed rules" below — rather than
guessing.

A `scope_id` is a stable hash of the normalized identity tuple, computed
once at scope creation and stored in the manifest; conversation-side lookups
always re-derive the same tuple from live adapter-supplied identity and hash
it to find the manifest, never trusting a cached/remembered scope_id from
prior conversation text.

### 2. Storage: per-scope manifest file, not a new SQLite table

Per the brief's "Persist a mode-0600 atomic locked scope manifest under the
active Hermes profile," and per the Footprint Ladder (extend existing code
first): reuse the `active_sessions.py` pattern exactly, not
`hermes_state_common.py`'s SQLite schema. A scope manifest is authored and
read far less often than session/message rows, doesn't need FTS, and a
SQLite migration bump (`SCHEMA_VERSION` → 24) is unnecessary machinery for
what's fundamentally a small, infrequently-written JSON document per scope.

```
$HERMES_HOME/scopes/<scope_id>.json       # mode 0600, atomic_json_write
$HERMES_HOME/scopes/<scope_id>.lock       # _FileLock, mirrors active_sessions.py
$HERMES_HOME/scopes/index.json           # identity-tuple-hash -> scope_id, same pattern
```

`$HERMES_HOME` is already profile-scoped (`get_hermes_home()`), so this
gives profile isolation for free — a scope created under one profile is
invisible to another, and Fleet-project isolation follows the same
mechanism once Fleet parity work begins (out of scope for the Mac-host
phase).

Manifest fields (draft; finalized in the implementation PR):

```json
{
  "scope_id": "sha256:...",
  "identity": {"profile": "...", "platform": "discord", "account_id": "...",
               "guild_scope_id": "...", "chat_id": "...", "thread_id": "...", "topic": "..."},
  "goal": "free text set at creation",
  "included_topics": [], "excluded_topics": [],
  "lifecycle": "active | completed | archived",
  "created_at": "...", "updated_at": "...",
  "owned": {
    "session_keys": [],
    "branches": [], "worktrees": [],
    "tmux_session_keys": [],
    "delegation_ids": [],
    "background_task_ids": [],
    "cron_job_ids": [],
    "prs": []
  },
  "external_dependencies": [{"description": "...", "linked_at": "..."}]
}
```

No raw command text, pane contents, credentials, or full unnecessary paths
are ever written into a manifest — only IDs and the identity tuple, per the
brief's privacy requirement. Routing IDs are redacted in ordinary reports
(scope status output shows a short opaque suffix, not the full ID) and only
shown unredacted in `scope audit`.

### 3. Auto-registration vs. manual linking

Auto-register where the artifact's creation path already runs through
Hermes with the current session's identity in hand:

- **tmux/terminal sessions** — `process_registry.py` already stamps
  `session_key` at creation; add a scope-manifest append at the same site,
  resolved from the current session's identity tuple.
- **Delegations** (background and sync) — `async_delegation.py` already
  records `origin_session`; append `delegation_id`/`child_session_id` to the
  owning scope's manifest at creation.
- **Cron jobs** — `cronjob_tools.py::_origin_from_env()` already builds the
  origin dict from `HERMES_SESSION_*`; append the job id to the owning
  scope's manifest at `create_job()` time.

Everything else — **branches, worktrees, PRs** — has no existing
Hermes-owned creation hook (git state is queried live; PRs aren't tracked at
all). Per the brief ("manually created artifacts remain orphaned until
explicitly linked"), these stay manual: `hermes scope link branch <name>`,
`hermes scope link worktree <path>`, `hermes scope link pr <url>`. This is
intentional, not a gap — inventing a git-operation-interception layer to
auto-detect every branch creation is exactly the kind of speculative
infrastructure `AGENTS.md` tells us not to build for a single feature.

### 4. Scoped progress resolution (fail-closed)

The literal root-cause fix: extend `session_search_tool.py` so that, by
default, `_discover()`/`_list_recent_sessions()` filter to the calling
session's resolved scope — computed via the positive-proof pattern already
used in `process_registry.py` (`owns_event()`-style check), not bare
string equality on a remembered ID. An explicit `include_unscoped=True`
argument (documented in the tool schema as "search other conversations too")
opens it back up — this keeps `session_search` as one tool (extend, don't
duplicate) rather than adding a second core tool.

A new `hermes scope status` (and the equivalent slash command) walks the
manifest's `owned.*` id lists and checks *live* state for each — is the
tmux session still running, is the delegation still pending, is the cron job
still scheduled — never trusting the manifest's last-known state as current
truth. `session_search` results, if the agent still consults them, are
always labeled conversation evidence, never presented as proof of live
state or artifact ownership, matching the brief's explicit requirement.

**Fail-closed rules** (apply uniformly across resolution, not just at scope
creation):
- Missing/unresolvable identity field → cannot compute a scope tuple →
  report "scope unknown," do not fall back to unscoped search silently.
- Manifest read fails JSON parse, or lock can't be acquired within a short
  timeout → treat as absent, report uncertainty (mirrors
  `active_sessions.py`'s `_read_entries()` fail-closed catch).
- Cross-profile or cross-scope artifact reference found during a live check
  → excluded from the report, logged, never silently merged in.
- Ambiguous match (identity tuple hashes to more than one manifest — should
  be impossible by construction, but checked) → exclude both, report
  ambiguity rather than picking one.

### 5. Continuity across compaction/resume/restart/`/clear`/handoff/cron

Scope identity is re-derived from adapter-supplied identity on every turn,
not stored in conversation text or carried through compaction summaries —
compaction summaries are exactly the contamination vector in the root-cause
incident, so scope must never be inferable from them. `/new` and `/clear`
start a new session_key under the same thread identity; whether that new
session inherits the existing scope's manifest (same thread → same scope)
or requires a fresh `scope create` is a per-adapter decision resolved by
whether the platform's `thread_id` is stable across the new session — for
Discord, yes (thread identity survives `/clear`); for a DM, there is no
thread so the scope is chat-level. Gateway restart is a non-event for scope,
since the manifest lives on disk under the profile directory, independent of
process lifetime.

**Known gap, not yet closed:** `build_session_key()` already unifies a
Discord channel-initiating message with its later real-thread follow-ups
via `prospective_thread_id` (same `session_key` for both — see
`gateway/session.py:1136-1146`). `hermes_scope`'s own identity normalization
does not yet inherit that continuity: it reads `HERMES_SESSION_THREAD_ID`
fresh each call, which is unset during the initiating-message turn and only
populated once the real thread exists, so a scope touched during that
narrow window and one touched afterward are, today, two different identity
tuples. Documented and exercised by
`tests/gateway/test_discord_scope_isolation.py::TestProspectiveThreadContinuity::test_KNOWN_GAP_scope_identity_does_not_yet_track_prospective_continuity`
so a future fix (bridging `prospective_thread_id` through session env, or
resolving scope from the DB session row's already-unified `thread_id`
instead of live env each time) has a test that flips from documenting the
gap to proving it closed.

### 6. CLI surface

New file `hermes_cli/scope.py`, mirroring `hermes_cli/curator.py`'s
`register_cli(subs)` structure exactly (one `add_parser` per verb: `status`,
`create`, `link`, `unlink`, `dependency`, `audit`, `complete`, `archive`),
wired into `hermes_cli/main.py` the same way curator is. A parallel
`CommandDef` entry in `hermes_cli/commands.py` gives conversation-side
`/scope status` etc. for free across CLI/gateway/Telegram/Slack per the
existing slash-command registry. No new core model tool — this is
Footprint Ladder rung 2 (CLI command + skill), the same rung `hermes cron`
and `hermes curator` already occupy.

## Test plan (TDD — failing reproduction first)

Mirrors `tests/hermes_cli/test_active_sessions.py` / `test_curator_*.py`'s
per-behavior file split rather than one giant file.

1. `tests/hermes_cli/test_scope_repro.py` — failing reproduction of the
   root-cause incident: two Discord threads under one channel, one has
   activity, a progress question asked in the *other* thread must not see
   it. Written and run first, expected to fail against current `main`.
2. `test_scope_identity.py` — normalization, hashing, missing-field
   fail-closed behavior, display-name-never-used-as-identity.
3. `test_scope_manifest.py` — atomic write/lock reuse, mode 0600, corrupt
   JSON fail-closed, concurrent writers.
4. `test_scope_ownership.py` — two scopes sharing one repo; same tmux
   session *name* in different projects; owned vs. orphaned artifacts;
   cross-profile isolation; cross-Fleet-project isolation (manifest path
   only, no Fleet runtime changes yet).
5. `test_scope_search_filter.py` — `session_search` default-scoped
   behavior vs. explicit `include_unscoped=True`; compaction-summary
   contamination must not leak into scope determination.
6. `test_scope_dependencies.py` — external dependencies tracked separately
   from verified progress.
7. `test_scope_lifecycle.py` — restart/resume/`/clear`/handoff continuity;
   `/new` under the same thread; profile separation.
8. `test_scope_privacy.py` — no raw command/pane/env/credential content in
   a manifest; routing IDs redacted in default `scope status` output.
9. `tests/gateway/test_discord_scope_isolation.py` — adapter-level version
   of scenario 1, plus the "prospective thread" continuity case
   (`gateway/session.py:194-204`).

## Explicit non-goals (this phase)

- No Gateway/production Discord routing changes until backup/rollback/canary
  gates exist (per the task's boundaries).
- No Fleet runtime changes until Mac-host E2E is green; Fleet gets a
  separate branch/PR with strict schema, dry-run, rollback, one-project
  canary.
- No new core model tool; `session_search` is extended, not duplicated.
- No auto-detection of git branch/worktree/PR creation — manual linking only.
