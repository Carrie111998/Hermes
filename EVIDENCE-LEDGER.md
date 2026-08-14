# Evidence Ledger — current-upstream session ownership

Mutable working ledger. Every focused verification run is recorded here with
the exact command, the expected result, and the observed result.

## Controller / worker boundary

| Item | Owner |
|---|---|
| plan.md Phase 0 (v7 immutable bundle) | parent Hermes session — NOT this worker |
| plan.md Phase 1 (clean worktree/branch) | parent — already done; this worktree is the product |
| plan.md Phases 2–4 (inventory, kernel, callers, decoupling) | **this worker** |
| plan.md Phase 5 items 1–7 (focused gates) | **this worker**, focused subset only |
| plan.md Phase 5 items 8–14 (broad suite, clients, scans) | parent |
| plan.md Phase 6 (freeze, immutable review) | parent |
| plan.md Phase 7 (commit, report, manifest) | parent |

This worker does not stage, commit, push, deploy, touch credentials, inspect
the installed dirty checkout, or alter other worktrees.

## Environment

| Field | Value |
|---|---|
| Worktree | `C:\Users\cwm4t\AppData\Local\Temp\hermes-current-upstream-session-ownership` |
| Branch | `fix/current-upstream-session-ownership` |
| Base commit | `2ae96939f53b0cc0aa82868fc9a44702f3dd6c09` (`origin/main`) |
| Worktree state at start | clean except untracked `plan.md` |
| OS | Windows 11 Home 10.0.26200 |
| Interpreter | `C:\Users\cwm4t\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe` — CPython 3.11.15, pytest 8.4.2 |
| Import resolution verified | `hermes_state.__file__` and `gateway.turn_lease.__file__` both resolve **inside this worktree**, not the installed tree |

### Deviation from `AGENTS.md` testing policy — recorded deliberately

`AGENTS.md` says *"ALWAYS use `scripts/run_tests.sh`"*. This worker runs
`pytest` directly, one process at a time, because the task explicitly forbids
spawning parallel per-file pytest runners on Windows and `run_tests.sh` drives
`scripts/run_tests_parallel.py`, which does exactly that. Hermetic parity is
preserved by the autouse `_hermetic_environment` / `_isolate_hermes_home`
fixtures in `tests/conftest.py`, which run identically under a direct pytest
invocation. Broad-suite / CI-parity confirmation remains a parent
responsibility (Phase 5 item 8).

Command shape used throughout:

```
"C:/Users/cwm4t/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pytest <target> -p no:randomly -q
```

## Premise re-verification against current upstream (plan.md Phase 2)

Do not mechanically apply v7. Each plan bullet was re-checked against the
current tree before any edit.

| plan.md item | Premise on current upstream | Disposition |
|---|---|---|
| 7 — remove private `orchestration-sessions.json` coupling from core | `grep -rn "orchestration-sessions"` over `*.py/*.ts/*.tsx/*.md` matches **only `plan.md` itself**. No core module names, reads, or parses that file. | **Already satisfied on current upstream.** Guarded behaviourally (not by source-regex, which `AGENTS.md` bans) — see Slice E. |
| 9 — no `claude-*` prefix route inference | 4 call sites exist. Assessed individually below. | See Slice E. |
| Canonical conversation identity | `SessionDB.get_conversation_root()` (hermes_state.py:9455) already exists and already walks `parent_session_id` to the lineage root. | **Reused, not reinvented.** |
| "two processes cannot own one conversation" | `gateway/turn_lease.py` is in-process only and its own module docstring names the gap: *"A CLI process sharing the session via CLI-continuity is outside any in-process lock — that pair needs a DB-level lease (separate design)."* | **This is the gap being closed.** |
| "do not add a second lock authority" | `compression_locks` already exists but answers a different question (serialise *rotation* within one conversation). Ownership answers *who may mutate the conversation at all*. | Kept separate, documented in OWNERSHIP-TABLE.md. |

### Canonical-root mutability finding (decides the schema)

`get_conversation_root()` recomputes the root by walking `parent_session_id`.
`parent_session_id` is written in exactly two ways on current upstream:

* set at `create_session(parent_session_id=…)` — compression child, delegate
  child. The root is **stable** across this (a new segment inherits the same
  root).
* set to `NULL` — only in destructive paths: `_delete_delegate_children`
  (hermes_state.py:330), `delete_session` (:9994), `delete_sessions` (:10115),
  `delete_empty_sessions` (:10208), prune (:10550).

So the root **is mutable**: deleting an ancestor re-roots its children. A lease
keyed on a *recomputed* root would silently orphan mid-turn and let a second
process acquire on the new root while the first still believes it owns the old
one. Therefore:

> **The grant pins the root captured at acquire time, and every fenced write
> validates `(pinned_root, holder, fence_token)` — it never recomputes the
> root. Destructive paths that would re-root a child refuse in the same write
> transaction while a live owner covers the old root.**

This is what plan.md Slice D means by "lineage changes preserve the canonical
authority contract".

---

## RED → GREEN cycles

Format: one entry per vertical behaviour. RED is recorded *before* the
production change exists.

### Baseline (untouched `origin/main` @ 2ae9693, this machine)

Established by restoring pristine `hermes_state.py` + `hermes_state_common.py`
with `git checkout --`, running the failing tests, then restoring my copies and
verifying both files byte-identical with `sha256sum -c` (both `OK`).

```
python -m pytest tests/hermes_state/test_live_db_isolation_guard.py \
  "tests/test_hermes_state.py::TestFTS5Search::test_search_projection_skips_context_enrichment_queries" \
  -p no:randomly -q
→ 6 failed, 7 passed
```

Pre-existing failure signatures on this host (NOT caused by this work):

| Test | Signature |
|---|---|
| `test_live_db_isolation_guard.py::TestProductionPathRefused::test_explicit_production_db_path_raises` | `Failed: DID NOT RAISE RuntimeError` |
| `…::test_production_profile_db_path_raises` | `DID NOT RAISE` |
| `…::test_read_only_open_of_production_db_raises` | `DID NOT RAISE` |
| `…::test_unnormalized_production_path_raises` | `DID NOT RAISE` |
| `…::test_default_resolution_to_production_raises` | `DID NOT RAISE` |
| `test_hermes_state.py::TestFTS5Search::test_search_projection_skips_context_enrichment_queries` | `assert 0 == 1` |

The isolation-guard group is a host-environment effect (the guard does not arm
under this shell's ambient `HERMES_HOME`). Both groups reproduce identically
with and without this branch's changes, so "zero new normalized failure
signatures" is measured against this list.

---

### Slice A — canonical identity and the SQLite kernel

**RED** (before any production change existed):

```
python -m pytest tests/hermes_state/test_conversation_ownership_kernel.py -p no:randomly -q
→ ModuleNotFoundError: No module named 'agent.session_ownership'
  1 error in 0.32s (collection error)
```

**Production change**

| Path | Change |
|---|---|
| `hermes_state_common.py` | `conversation_ownership` table + expiry index in `SCHEMA_SQL` (applied to fresh AND existing DBs by `_init_schema`'s `executescript`) |
| `agent/session_ownership.py` | **new** — identity (`new_holder_id`, `holder_process_is_dead`), `OwnershipGrant`, typed conflicts, thread-scoped re-entrancy, lease refresher, `own_conversation()` |
| `hermes_state.py` | `try_acquire_conversation_ownership` / `refresh_conversation_ownership` / `release_conversation_ownership` / `get_conversation_owner` / `execute_fenced_write` |

**GREEN**

```
python -m pytest tests/hermes_state/test_conversation_ownership_kernel.py -p no:randomly -q
→ 11 passed in 5.32s
```

Covered: two-PROCESS mutual exclusion (real `subprocess.Popen` holder, not a
mock); root-keyed identity (a compression child collides with its parent);
grant pins its captured root across ancestor deletion; monotonic fence per
handover; fenced write refuses a stale token **without running the mutation**;
fenced write commits in one transaction; holder+fence-scoped release; TTL
takeover; dead-pid takeover before TTL; refresh only extends our own grant;
**authority failure raises instead of silently granting**.

**No-regression on the store**

```
python -m pytest tests/hermes_state/ tests/test_hermes_state.py \
  tests/test_hermes_state_readonly_preflight.py \
  tests/test_hermes_state_compression_locks.py \
  tests/test_hermes_state_wal_fallback.py -p no:randomly -q
→ 6 failed, 354 passed, 8 skipped in 170.16s
```

The 6 failures are exactly the baseline list above — **zero new signatures**.

---

### Slice B — core agent lifecycle

**RED**

```
python -m pytest tests/run_agent/test_conversation_ownership_admission.py -p no:randomly -q
→ ImportError: cannot import name 'ownership_admission_surface'
  1 error in 3.01s (collection error)
```

**Production change**

| Path | Change |
|---|---|
| `agent/session_ownership.py` | `should_own_conversation()` / `ownership_admission_surface()` |
| `run_agent.py` | `AIAgent.run_conversation` becomes the admission gate; its former body moved verbatim to `_run_owned_conversation` (no re-indent, no logic change) |

`run_conversation` is the narrow waist every surface funnels through (CLI,
gateway, HTTP API, TUI, ACP, cron, batch, oneshot), so one gate there covers
every mutating path instead of eight per-surface locks.

Three populations are deliberately excluded from contention, because they run
*inside* a conversation someone else owns: persist-disabled background-review
forks, delegate subagents (their root resolves to the parent's by design), and
store-less agents.

**GREEN**

```
python -m pytest tests/run_agent/test_conversation_ownership_admission.py -p no:randomly -q
→ 11 passed in 8.40s
```

Covered: eligibility is pure input→output; a foreign holder makes the turn
raise `ConversationOwnershipConflict` with **no session row and no messages
written**; the grant is live for the whole turn body and gone after; release
survives a raising turn AND a `KeyboardInterrupt`; a second THREAD on the same
conversation still collides; nested re-entry in the owning thread reuses the
grant and does not release it early.

---

### Slice C — fenced publications, rewrites, expiry, and lineage mutation

The first implementation review found three missing invariants and each was
turned into a failing test before correction:

```
pytest ...::test_fenced_write_refuses_an_expired_grant_without_handover \
       ...::test_archive_and_compact_is_refused_under_a_lost_grant \
       ...::test_delete_session_if_empty_is_refused_under_a_lost_grant
→ 3 failed (all mutations ran under an invalid grant)
```

Corrections:

- fenced writes now validate unexpired `(root, holder, fence)` in the same
  transaction as the mutation;
- single and batched append, replace, archive/compact, rewind, rewind restore,
  reset promotion, session delete, and empty-session delete use the same fenced
  transaction whenever the current thread holds a grant;
- direct deletion refuses lineage re-rooting while a live owner covers the
  affected root; bulk, empty, and prune maintenance skip owned roots and
  continue deleting unrelated rows, preserving their count contracts;
- acquire and refresh timestamps are sampled after `_execute_write` obtains
  SQLite write authority, so lock patience cannot consume a lease before
  publication.

Delayed candidate-v2 review then reproduced a separate authority bypass: a
different thread/`SessionDB` handle with no process-local grant could append or
rewind while another handle held the durable lease. The discriminating RED was:

```
pytest ...::test_foreign_unowned_append_is_refused_while_an_owner_is_live \
       ...::test_foreign_unowned_rewind_is_refused_while_an_owner_is_live
→ 2 failed (both foreign mutations committed)
```

`_execute_conversation_write` now checks the canonical durable owner inside the
same write transaction even when no grant is local. A foreign live owner is
refused; legacy mutation remains compatible when no owner is live.

Candidate-v9 review found the necessary opposite case: delegate/subagent
segments run concurrently with the parent and intentionally do not acquire its
grant. A threaded delegate append was RED under the blanket refusal while a
compression-child control remained GREEN. The transaction now recognizes only
targets at or below a real parented `delegate`/`subagent` lineage boundary as
independently writable. Candidate-v10 review added two more REDs: a rotated
compression child below a delegate was incorrectly refused, while a re-rooted
delegate-labeled row was incorrectly exempt. The bounded transaction-local
lineage predicate now makes both GREEN; root-level compression, branch, reset,
and root targets remain protected. A malformed delegate-cycle RED additionally
requires the bounded walk to reach a real root before granting the exception.
Candidate-v11 review then proved the exception was too broad for
delete-if-empty/reset and too dependent on a hand-planted source label. Two
lineage-mutation REDs restricted it to explicitly marked transcript
publication. A real `delegate_tool._build_child_agent` test, under an inherited
`tui` source, drove recognition of the durable `_delegate_from` marker emitted
by production child assembly.
An ordinary-agent assertion drives production's
`_adopt_live_compression_child` path and proves `_parent_session_id` remains
unset and ownership admission remains enabled. The real delegate child test
also asserts the in-memory `platform`, `_parent_session_id`, and
`should_own_conversation(child) is False`, not just the persisted row.
Delayed candidate-v3 privacy review then demonstrated that generic client
stringification exposed canonical roots, holder host/PID/nonce, fence values,
and raw storage errors. A discriminating RED now requires bounded public text;
the trusted structured fields remain available without appearing in `str(exc)`.
Candidate-v15 review then identified `replace_messages` as too destructive for
the delegate publication exemption. A RED foreign delegate-boundary replacement
test drove strict owner checking for whole-transcript replacement while leaving
append/batch and compacted publication available to real subagents.

```
pytest tests/hermes_state/test_conversation_ownership_kernel.py \
       tests/hermes_state/test_conversation_ownership_rewrites.py -p no:randomly -q
→ 45 passed (latest review-cycle aggregate)
```

### Slice D — supported configuration/provider boundaries

```
pytest tests/e2e/test_core_config_contract_boundaries.py -p no:randomly -q
→ included in focused aggregate below; all 4 passed
```

The behavioral acceptance test plants a contradictory private
`workspace/orchestration-sessions.json`; delegation still resolves exclusively
from supported `config.yaml`. Provider API mode is resolved from provider
identity: the same `claude-x` model maps to Anthropic Messages only for the
published `opencode-zen` provider and remains Chat Completions elsewhere.

### Slice E — current route-prefix call-site disposition

The four current `claude-*` production checks were assessed individually:

1. `hermes_cli/models.py::opencode_model_api_mode` is provider-gated endpoint
   family mapping from OpenCode's published Zen table. The acceptance test also
   uses a non-Claude `qwen*` family routed to `/v1/messages`, so it would fail
   under a provider-gated Claude-prefix-only implementation.
2. Anthropic payload shaping checks a selected Anthropic API mode after route
   resolution; it does not select the provider or endpoint.
3. Bedrock model metadata identifies a model family inside the already-selected
   Bedrock provider; it does not infer an installation route.
4. Pricing/display normalization classifies model names for metadata/UI only;
   neither call site selects an API transport.

No private Workspace schema is consulted and no installation/provider identity
is inferred from a `claude-*` route name.

## Final verification before freeze

### Focused aggregate and static checks

```
pytest tests/run_agent/test_conversation_ownership_admission.py \
       tests/e2e/test_core_config_contract_boundaries.py \
       tests/hermes_state/test_conversation_ownership_kernel.py \
       tests/hermes_state/test_conversation_ownership_rewrites.py \
       -p no:randomly -q
→ 62 passed (latest focused aggregate)

ruff check <all changed Python and tests>
→ All checks passed

git diff --check
→ clean
```

### Broad SessionDB gate

```
pytest tests/hermes_state/ tests/test_hermes_state.py \
       tests/test_hermes_state_readonly_preflight.py \
       tests/test_hermes_state_compression_locks.py \
       tests/test_hermes_state_wal_fallback.py -p no:randomly -q
→ 6 failed, 388 passed, 8 skipped in 184.13s
```

All six normalized signatures exactly match the untouched-upstream baseline
recorded above: five Windows host live-DB-guard expectations and one FTS trace
callback expectation. Zero new signatures.

### Phase-5 caller and lifecycle surfaces

The first all-in-one caller command exited without a pytest summary after 61%,
so it is not acceptance evidence. The same inventory was rerun serially in
bounded surface groups against the immutable replay. Every candidate failure
was then rerun by exact node ID on an untouched worktree at `2ae9693`:

```text
tests/run_agent/
→ 28 failed, 1537 passed, 3 skipped, 2 deselected in 1442.55s
→ exact-base rerun of all 28 candidate failures: the same 28 failed

tests/gateway/
→ 69 failed, 5432 passed, 36 skipped, 2 xfailed in 2438.52s
→ exact-base rerun: 65 persistent signatures reproduced
→ four candidate-only Discord/media signatures immediately passed on both
  candidate and exact base; classified as transient, not persistent additions

tests/tui_gateway/ tests/acp_adapter/ tests/cron/
→ 13 failed, 996 passed, 19 skipped in 347.64s
→ exact-base rerun of all 13 candidate failures: the same 13 failed

delegation + MOA + compression caller files under tests/tools and tests/agent
→ 1 failed, 421 passed in 199.89s
→ exact-base rerun: the same Windows open-handle cleanup test failed
```

Normalized persistent candidate-only failure set: **empty**. The failures are
Windows path/permission/open-handle, optional integration, provider fixture,
and existing platform-contract signatures; none implicates ownership
admission, fencing, delegation publication, rewind/reset/delete handling, or
maintenance count semantics.

After removing the delegate exemption from `replace_messages`, the affected
caller slice (`gateway`, `tui_gateway`, `acp_adapter`, filtered to replace,
rewind, or reset) returned 141 passed / 3 skipped / 3 failed. All three failures
are the previously baselined Windows gateway process-reaping tests selected by
the word `replace` in their filename; no ownership caller failed.

### Dashboard gate

```
cd web && npm run check
→ exit 0; typecheck passed; tests passed; lint: 0 errors, 26 existing warnings
```

### Desktop gates

```
cd apps/desktop && npm run typecheck
→ exit 0

npm run test:ui
→ 3796 passed, 7 failed (3 files)

npm run test:desktop:platforms
→ 1027 passed, 28 failed, 2 skipped (7 files)
```

No Desktop/TypeScript file is changed by this candidate. The failures are
current-upstream Windows/environment signatures in messaging/settings/skills UI
fixtures and POSIX permission/SSH/native-dependency Electron tests; none imports
or exercises the ownership kernel. They are recorded honestly, not represented
as green.

### Full Python discovery attempt

The first full run stopped during collection on upstream's unconditional
`os.geteuid()` use in `tests/hermes_cli/test_doctor_journal_modes.py` on Windows.
A second run excluding that file was stopped after 20 minutes at 6% because the
serial Windows suite is ~20k tests and had already accumulated unrelated host
failures. No orphan process was left running. Focused and SessionDB broad gates
above are the bounded acceptance evidence.

### Safety and scope

- production/installed Hermes checkout was never edited or used as source;
- no deployment was performed;
- no push was performed;
- no credential path or credential value is included;
- no private Workspace checkout was edited;
- generated `node_modules` stayed ignored and outside the candidate manifest.
