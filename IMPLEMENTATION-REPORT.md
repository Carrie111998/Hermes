# Implementation Report — Durable Cross-Process Conversation Ownership

## Prior state and root cause

Hermes already serialized turns inside one gateway process and serialized context-compression rotation, but no authority covered two independent processes mutating the same durable conversation. A CLI, gateway, TUI, ACP, cron, batch, or HTTP process could therefore overlap on one lineage. A stalled writer could also resume after another process took over and publish stale transcript changes.

The installed Hermes checkout was heavily dirty and was not used as source. The prior hardening worktree remained at `22b186d8a3c51b1bf3a93d49d02f0fc609a2a466` and was not cleaned, reset, rebased, or reused.

## Current-upstream baseline

- Fetched base: `2ae96939f53b0cc0aa82868fc9a44702f3dd6c09` (`origin/main` at work start).
- Branch: `fix/current-upstream-session-ownership`.
- Clean worktree: `C:\Users\cwm4t\AppData\Local\Temp\hermes-current-upstream-session-ownership`.
- `plan.md` was copied byte-for-byte before production edits.
- v7 was preserved outside the repository as historical evidence at `C:\Users\cwm4t\AppData\Local\hermes\historical-artifacts\core-session-continuity-v7` and hash-verified at 288,739 bytes / `e7d378fd4c3b0cc55171d63920cfc38013c68433336cbe627675d93557e7b2e7`.

## Inventory and ownership model

`OWNERSHIP-TABLE.md` maps every current-upstream mutation surface and records its canonical identity, existing guard, new authority, denial behavior, and lifecycle. The implementation deliberately keeps a narrow waist:

- `gateway/turn_lease.py`: in-process scheduling optimization;
- `compression_locks`: rotation-specific serialization inside an owned turn;
- `conversation_ownership`: the sole durable cross-process authority deciding who may mutate a conversation.

The authority key is the conversation lineage root, not a mutable session segment. A grant pins the root captured at admission and carries a monotonic fence token.

## Implementation

### SQLite ownership/fencing kernel

A small `conversation_ownership` table stores root, holder, monotonic fence token, surface, session id, acquisition/refresh times, and expiry. Acquisition uses `BEGIN IMMEDIATE`, rejects live holders, reclaims expired or provably dead local processes, and fails closed if the authority cannot be consulted.

Holder identity includes host, PID, process creation time, thread, and nonce. PID creation time prevents PID-reuse false liveness. A bounded refresher keeps long turns alive; holder-and-fence-scoped release cannot free a successor.

Every fenced mutation validates the pinned `(root, holder, fence)` and unexpired lease in the same transaction as the write. A stale writer's callback is never invoked.

### Core admission and caller coverage

`AIAgent.run_conversation()` is the shared admission boundary used by CLI, gateway, HTTP API, TUI, ACP, cron, batch, and oneshot surfaces. It acquires before transcript loading and releases on normal return, exception, interrupt, or cancellation. Persist-disabled review forks, delegate subagents, and store-less agents do not contend because they either cannot persist or execute inside an already-owned conversation.

Fenced write coverage includes:

- single-message and batched turn publication;
- transcript replacement;
- archive-and-compact;
- rewind and rewind restore;
- reset promotion;
- session deletion and empty-session deletion;
- child-segment writes after an ancestor deletion changes the recomputed lineage root.

Conversation transcript and rewrite methods routed through
`_execute_conversation_write` retain legacy behavior only while no durable
owner is live. If another thread or process owns the canonical root, the store
refuses the ungranted mutation inside the same transaction. Observation or
sidecar metadata helpers such as activity touches and display/API-content
backfills remain outside the transcript authority contract.

### Configuration and private-boundary normalization

Current upstream already had no production read of private `workspace/orchestration-sessions.json`. A behavioral acceptance test now plants contradictory private Workspace state and proves delegation still resolves from supported `config.yaml` contracts.

Route choice is proven provider-scoped rather than model-prefix-scoped: the same `claude-x` model selects Anthropic Messages only for the published `opencode-zen` provider, and Chat Completions elsewhere. Existing `claude-*` checks in Anthropic payload shaping, Bedrock model metadata, pricing, and CLI display normalization are not route inference and were intentionally retained.

## RED → GREEN evidence

The authoritative command/result ledger is `EVIDENCE-LEDGER.md`. Key cycles:

- missing kernel: collection failed with `ModuleNotFoundError`; kernel tests became green;
- missing admission surface: collection failed with `ImportError`; lifecycle/admission tests became green;
- review-discovered missing expiry/compact/empty-delete fences: 3 tests failed because mutations ran; corrections made all 3 green;
- review cycle 1 exposed mutable-lineage split authority and pre-transaction
  lease timestamp sampling; both received discriminating RED tests and minimal
  transaction-boundary corrections;
- delayed immutable review reproduced foreign ungranted append and rewind
  bypasses; two RED tests failed, then the transaction-local durable-owner
  check made both GREEN;
- v9 review then caught that a blanket foreign-owner refusal blocked concurrent
  delegate/subagent publication; a delegate RED failed while a compression
  child control stayed GREEN, then a source-discriminated own-segment exception
  made both outcomes GREEN;
- v10 review found that target-row discrimination missed rotated descendants
  below a delegate boundary and over-exempted a re-rooted delegate label; two
  RED tests drove a bounded lineage-boundary predicate that preserves rotated
  subagents without weakening root authority;
- a malformed delegate-cycle RED then made the boundary walk fail closed unless
  it reaches a real root;
- v11 review showed the exception also covered delete-if-empty/reset and that
  real gateway children may inherit a non-subagent source; two lineage-mutation
  REDs restricted the exception to publication-only, while a real
  `delegate_tool._build_child_agent` test drove recognition of its durable
  `_delegate_from` marker;
- the production `_adopt_live_compression_child` path confirms normal-agent
  rotation does not set the delegate-only parent marker or disable admission;
- delayed candidate-v3 review exposed public exception strings containing
  roots, holder process identity, fence values, and raw SQLite errors. A RED
  projection test drove bounded generic exception text while preserving
  structured diagnostic attributes for trusted internal logging;
- final candidate-v15 review showed whole-transcript `replace_messages` still
  shared the delegate publication exemption. A RED delegate-boundary
  replacement test drove removal of that flag; append/batch and compacted
  publication remain supported, while destructive replacement is owner-checked;
- final focused aggregate: 62 passed;
- kernel + rewrite aggregate: 45 passed.

## Verification

- Changed Python/test Ruff gate: pass.
- `git diff --check`: pass.
- Focused ownership/config aggregate: 62 passed.
- SessionDB broad gate after review-cycle hardening: 388 passed, 8 skipped,
  6 failed; all six signatures exactly match untouched-upstream Windows
  baseline, so zero new signatures.
- Phase-5 caller gates were run serially against the immutable candidate and
  compared with exact-base `2ae9693` reruns of every candidate failure:
  `run_agent` 1,537 passed / 28 failed / 3 skipped / 2 deselected with all 28
  signatures reproduced on base; gateway 5,432 passed / 69 failed / 36
  skipped / 2 xfailed, with 65 reproduced on base and the remaining four
  transient Discord/media failures passing immediately on both candidate and
  base rerun; TUI+ACP+cron 996 passed / 13 failed / 19 skipped with all 13
  reproduced; delegation+MOA+compression 421 passed / 1 failed with that
  Windows open-handle cleanup signature reproduced. Zero persistent new
  normalized caller signature remains.
- Post-replacement-hardening caller slice (`gateway`, `tui_gateway`, `acp_adapter`,
  `-k 'replace or rewind or reset'`): 141 passed, 3 skipped; the three failures
  are the already-baselined Windows process-reaping tests selected only because
  their filename contains `replace`. No ownership caller failed.
- Dashboard `npm run check`: exit 0; typecheck/tests pass; lint has 0 errors and 26 existing warnings.
- Desktop typecheck: pass.
- Desktop UI: 3796 passed / 7 current-upstream failures in 3 unrelated fixture files.
- Desktop Electron: 1027 passed / 28 current-upstream Windows/POSIX/native-environment failures in 7 unrelated files; 2 skipped.
- Full Python discovery is not represented as green: upstream collection calls `os.geteuid()` unconditionally on Windows. A serial retry excluding that file was stopped after 20 minutes at 6% after unrelated host failures accumulated.

No Desktop or dashboard source file is changed by this candidate. No new broad failure signature was observed in the owned Python surface.

## Immutable review and commit

This section is finalized by the controller after staging, immutable patch export, detached replay, parallel reviews, and the local commit. The immutable evidence ledger and external deployment attestation record hashes that cannot be embedded in their own hashed commit without circularity.

## Exclusions and limitations

- No comprehensive private Workspace rewrite.
- No Desktop/dashboard feature port.
- No delegation UX rebuild.
- No unrelated cleanup or generated website churn.
- No production deployment.
- No push.
- API 429 remains only a surface projection/optimization; correctness is in SQLite admission/fencing.
- HTTP, TUI/Desktop RPC, and ACP currently project ownership refusal through
  their generic error contracts; typed per-surface status/code projection is
  explicitly not claimed by this candidate.
- A surface that bypasses `AIAgent.run_conversation()` may mutate without a
  grant only when no durable owner is live. A foreign live authority is
  mandatory and causes a typed refusal before mutation.

## Final state

The candidate is a clean current-upstream vertical repair with one durable authority, monotonic fencing, cancellation-safe lifecycle, explicit mutation coverage, behavioral private-boundary tests, and reproducible evidence. Installation and runtime remain untouched pending separate deployment authorization.
