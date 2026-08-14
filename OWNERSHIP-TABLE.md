# Ownership Table — current-upstream mutating surfaces

Re-inventoried against `origin/main` @ `2ae96939f53b0cc0aa82868fc9a44702f3dd6c09`
by searching current source. The v7 caller list was **not** used as authority.

## The authority, in one paragraph

One durable authority — the `conversation_ownership` table in `state.db` —
answers exactly one question: *may this process mutate this conversation right
now?* Canonical identity is the **conversation root**
(`SessionDB.get_conversation_root`), so a compression rotation or a delegate
subtree keeps one user-facing conversation identity. A real delegate/subagent
lineage boundary and descendants below it are independently written concurrent
transcript segments and never authorize a write to the owned root; every other
covered segment shares one owner. A grant carries a monotonic
`fence_token`, pinned to the root captured at acquire time, and every fenced
mutation validates `(pinned_root, holder, fence_token)` inside the same
transaction as the mutation itself.

### Why the pre-existing locks are not it, and are not replaced

| Mechanism | Scope | Question it answers | Relationship |
|---|---|---|---|
| `gateway/turn_lease.py` | in-process, per resolved `session_id` | which of two routing keys in *this process* runs first | Kept. Its own docstring defers the cross-process case to "a DB-level lease (separate design)" — that is this table. It still does useful work: it serialises the alias-key route without a DB round trip. |
| `gateway/platforms/base.py::_active_sessions`, `gateway/run.py::_running_agents` | in-process, per routing key | is this chat busy | Kept. Routing-key busy guards, blind to `session_id` aliasing. |
| `compression_locks` (`hermes_state.py`) | durable, per `session_id` | which rotation wins *inside* an owned conversation | Kept, strictly narrower, lives inside ownership. Ownership never consults it and it never grants ownership. |
| `hermes_cli/active_sessions.py` | durable JSON, global | how many chats may be open at once (a **cap**) | Kept. A cap, not exclusivity — it never asks who owns a given conversation. |
| `agent/relay_runtime.py::RelaySessionCoordinator` | in-process | relay/telemetry scope for a turn | Kept. Observability, not mutual exclusion. |

**No second lock authority was added.** One question, one table.

## Admission point

`AIAgent.run_conversation` (`run_agent.py`) is the narrow waist every surface
funnels through. Gating it there is what makes one authority cover every
mutating path. Admission happens **before** the transcript is loaded, so a
refused turn leaves the conversation byte-identical — and no surface answers a
conflict by appending a message, which would break role alternation and
invalidate the conversation's prompt cache.

Deliberately excluded from contention (they run *inside* a conversation
somebody else owns): persist-disabled background-review forks, delegate
subagents (`platform == "subagent"` / `_parent_session_id`, whose root resolves
to the parent's by design), and store-less agents. A delegate/subagent may
publish below its actual parented delegate boundary, including its own rotated
children; root-level compression, branch, reset, and root transcripts remain
covered by the parent's authority.

## Surface table

Legend — **Owner**: `core` = covered by the `run_conversation` admission gate;
`fenced` = mutation validated against the caller's grant at the store boundary.

| Surface | Entry point | Canonical identity | Lease acquired | Fenced write / publication | Release & cancellation | Conflict projection | Tests |
|---|---|---|---|---|---|---|---|
| **CLI / core agent** — send | `cli.py:14709`, `:18639`, `:19159` → `AIAgent.run_conversation` | `get_conversation_root(agent.session_id)` | `run_conversation` admission, pre-history-load | turn flush `run_agent.py:2287` → `append_messages_batch` (fenced) | `own_conversation` `finally`; survives raise + `KeyboardInterrupt` | generic CLI turn error today | `tests/run_agent/test_conversation_ownership_admission.py` |
| **CLI** — `/rewind` | `cli.py:9114` → `rewind_to_message` | root of the target session | inherits the turn grant when held | fenced under matching grant; foreign live owner refused; legacy write only when unowned | n/a (synchronous) | generic CLI command error today | `tests/hermes_state/test_conversation_ownership_rewrites.py` |
| **CLI** — one-shot / batch | `hermes_cli/oneshot.py:466`, `batch_runner.py:349` | per-task session root | core | fenced | core | generic process/task error today | core admission tests |
| **CLI** — background agent | `hermes_cli/cli_commands_mixin.py:2069`, `:1261` | its own session root | core | `append_messages_batch` (fenced) | core | generic background-agent error today | core admission tests |
| **Gateway** — send / queued send | `gateway/run.py:21139`, `:5933` → `run_conversation` | root of the resolved `session_id` (post `switch_session`/tip-walk) | core, after `turn_lease` | fenced | core `finally` + gateway `_release_turn_lease` | generic gateway turn error today | core admission tests |
| **Gateway** — replace / reset | `gateway/session.py:3821` (`replace_messages`), `:3904` (`rewind_to_message`), `:2212` (`promote_to_session_reset`) | root of `session_id` | inherits turn grant when held | fenced under matching grant; foreign live owner refused; legacy write only when unowned | n/a | generic gateway command error today | rewrites tests |
| **Gateway** — slash flush | `gateway/slash_commands.py:4875` → `append_messages_batch` | root of `session_id` | inherits turn grant when held | fenced under matching grant; foreign live owner refused; legacy write only when unowned | n/a | generic gateway command error today | rewrites tests |
| **API** — send / stream / retry / rewind / reset / compress / resume | `gateway/platforms/api_server.py:6302`, `:6795` → `run_conversation`; handlers `_handle_session_chat` (:3662), `_handle_session_chat_stream` (:3779), `_handle_chat_completions` (:4102), `_handle_responses` (:5264), `_handle_fork_session` (:3614), `_handle_delete_session` (:3542) | root of `session_id` | core | fenced | core | generic HTTP/SSE error today; typed 409/429 projection remains future adapter work | core admission tests |
| **API** — 429 fast path | in-memory busy map in `api_server` | derived | *derived only* | — | — | must never be authoritative | — |
| **Desktop** | `apps/desktop` → `tui_gateway` JSON-RPC (`prompt.submit`) | root of `session_id` | core, via TUI gateway | fenced | core | generic RPC/turn error today | core admission tests |
| **TUI / dashboard** — prompt submit | `tui_gateway/methods_prompt.py:257` → `run_conversation` (`:1119`, `:1231`), `tui_gateway/server.py:10078`, `compute_host.py:407` | root of `session_id` | core | fenced | core | generic RPC/turn error today | core admission tests |
| **TUI** — truncate / rewind / replace | `tui_gateway/methods_prompt.py:567` (`replace_messages`), `methods_tools.py:876` (`rewind_to_message`), `methods_session.py:2819` + `server.py:2919` (`append_messages_batch`) | root of `session_id` | inherits turn grant when held | fenced under matching grant; foreign live owner refused; legacy write only when unowned | n/a | generic RPC command error today | rewrites tests |
| **ACP** — prompt | `acp_adapter/server.py:1919` → `run_conversation` | root of `session_id` | core, inside `state.runtime_lock` | fenced | core `finally`; ACP `cancel` (`server.py:1540`) unwinds the turn, which releases | generic ACP error result today | core admission tests |
| **ACP** — history replace | `acp_adapter/session.py:492` → `replace_messages` | root of `session_id` | inherits turn grant when held | fenced under matching grant; foreign live owner refused; legacy write only when unowned | n/a | generic ACP error today | rewrites tests |
| **Cron / background** — scheduled prompt | `cron/scheduler.py:4448` → `run_conversation` on a pool thread | root of the job's session | core | fenced | core `finally` on the pool thread | generic job error today | core admission tests |
| **Compression** — publish child | `agent/conversation_compression.py:3363` → `publish_compression_child` | root (unchanged by rotation) | inherits the turn grant | fenced | compression lock released independently | generic turn error today | rewrites tests |
| **Reset promotion** | `hermes_state.py:promote_to_session_reset` | root of `session_id` | inherits turn grant when held | fenced under matching grant; foreign live owner refused; legacy write only when unowned | n/a | ownership error propagates; other legacy failures return `False` | rewrites tests |
| **Delete / prune** | `hermes_state.py:delete_session`, bulk delete, empty cleanup, prune | root of each target | direct single delete refuses every live owner, including the caller's own grant; maintenance bulk paths skip owned roots | lineage-changing delete never uses the delegate-publication exception; bulk maintenance preserves legacy count contract | n/a | generic caller error for direct conflict; bulk cleanup skips owned rows | rewrites tests |

## API 429 is an optimization, never the authority

plan.md item 6. The API server's in-memory busy map may reject an obvious
collision early to save a DB round trip, but it derives identity from — and
defers truth to — the SQLite authority. Its absence, or a process restart that
empties it, cannot weaken correctness: the admission gate inside
`run_conversation` runs regardless and is the only thing that decides.

## Durable authority is mandatory while a conversation is live

A store mutation looks up the calling **thread's** grant for that session's
conversation. A matching grant runs through `execute_fenced_write`. If no grant
is local, the store resolves the canonical root and checks the durable owner in
the same write transaction as the mutation: a foreign live owner is refused.
Legacy direct mutation remains compatible only when no durable owner is live.
The narrow exception applies only to transcript publication at or below an
actual parented delegate lineage boundary. The boundary is recognized from the
real spawn's `_delegate_from` marker (with `delegate`/`subagent` source as a
compatible signal), must reach a real root, and never exempts deletion, reset,
or other lineage mutation. A re-rooted label is not an exemption.
Re-entrancy is thread-scoped, so a nested rewrite inside an owned turn reuses
the grant while a genuinely concurrent thread or process collides.

## Known gaps carried forward

Recorded rather than silently left out — see EVIDENCE-LEDGER.md for status.

1. **Per-surface conflict projection.** The typed conflict is raised
   consistently by the core gate; surfaces that do not yet catch
   `ConversationOwnershipConflict` explicitly will surface it as their generic
   turn error. Dedicated per-surface projection remains future adapter work.
2. **Lineage re-rooting is fail-closed while owned.** Direct single-session
   deletion refuses while a live owner covers the affected root. Bulk, empty,
   and prune maintenance paths inspect each affected root in their write
   transaction and skip owned lineages while continuing with unrelated rows,
   preserving their count/succeed-on-the-rest contracts. This prevents a child
   from becoming a second acquirable ownership key while the old root is pinned.
3. **Cross-host holders.** `holder_process_is_dead` refuses to judge a holder
   recorded on another hostname, so a stale grant from another machine sharing
   a state DB waits out its TTL. Conservative by design.
