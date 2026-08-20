# HERMES_DURABLE_CONTEXT_COMPILER_P3_IMPLEMENTATION_V1

## Verdict

`HERMES_DURABLE_CONTEXT_COMPILER_P3_CERTIFIED`

## Baseline

- branch: `hermes-durable-action-commit-p2`
- starting SHA / P2 SHA: `c8ff5d5c79758d0660198083ca5b5a82f431ba04`
- worktree: `/tmp/hermes-p2-implementation`
- database migration: none

## Files changed

- `agent/context_compiler.py`
- `agent/turn_context.py`
- `tests/agent/test_context_compiler.py`

## Context compiler

- implementation: deterministic `ContextCompiler` derived view
- insertion boundary: `agent.turn_context.build_turn_context`, after P1 restore/P2 action recovery and before conversation-loop API message assembly
- llm_free: `true`
- deterministic: `true`
- HOT: P1 checkpoint fields, unresolved P2 action status/replay/verification state, external binding references, and active constraints
- WARM: bounded sanitized evidence/plugin references, only when budget permits
- recent: newest bounded messages selected from the tail; old context is dropped first
- COLD: not automatically inserted; no recall system implemented
- non-durable: compiler returns the existing message list semantics and no durable block

## Token budget and metrics

- explicit budget: `true`
- runtime adaptive: uses the active compressor/model context length, with configured fallback
- reserved headroom: at least 20%, 1024 tokens, or configured output max, whichever is larger at the shared turn boundary
- HOT overflow: raises `CONTEXT_BUDGET_INSUFFICIENT` before provider invocation
- context compilation LLM calls: `0`
- representative synthetic measurement: raw `12008`, compiled `270`, reduction `97.75%`, HOT `270`, WARM `0`, recent `0`, reserved headroom `500`
- measurement note: one large synthetic history was intentionally dropped before the mandatory HOT projection; this is an observed test case, not a general performance claim

## Authority and compression

- mission progression: remains P1 checkpoint-owned
- action execution status: remains P2 ledger-owned
- conversation: non-authoritative
- compression summary: non-authoritative
- post-compression/session rotation: next turn restores P1/P2 state and recompiles the projection; compression code was not redesigned
- external authorities: approval, safety, financial, Repo Router, CodeGraph, and Convergence remain external/reference-only

## Tests

- focused P3 + turn-context: `16 passed`
- combined P1/P2/P3/turn-context: `90 passed`
- relevant canonical non-async subset: `479 passed`, `5 skipped`
- TUI direct startup suite: `238 passed`
- gateway API sync tests: `43 passed`
- gateway async tests blocked: `114` because the environment lacks `pytest-asyncio`; exact failure is `async def functions are not natively supported`
- unrelated SQLite WAL probe: failed once, then passed on isolated rerun; no source change made
- real regression proven: `false`

## Security and operations

- secret values exposed: `0`; evidence/plugin/reference text is bounded and redacted for secret-bearing keys/patterns
- provider mutation: `false`
- live provider calls: `false`
- campaign/financial/deployment/approval/destructive operations: `false`
- PLAY executed: `false`
- provider exactly-once claim: `false`; P2 duplicate-replay protection remains distinct from provider guarantees

## Acceptance invariants

```text
CONTEXT_COMPILER_MACHINE_OWNED=true
CONTEXT_COMPILATION_LLM_FREE=true
CONTEXT_BUDGET_EXPLICIT=true
NO_UNBOUNDED_CONTEXT_SECTION=true
HOT_STATE_ALWAYS_PRESERVED=true
NEXT_ACTION_ALWAYS_FROM_CHECKPOINT=true
ACTION_STATUS_ALWAYS_FROM_LEDGER=true
CONVERSATION_NON_AUTHORITATIVE=true
COMPRESSION_SUMMARY_NON_AUTHORITATIVE=true
POST_COMPRESSION_CONTEXT_REBUILT_FROM_DURABLE_STATE=true
MODEL_CONTEXT_WINDOW_ADAPTIVE=true
CONTEXT_BUDGET_INSUFFICIENT_FAILS_CLOSED=true
NON_DURABLE_CONVERSATIONS_UNBROKEN=true
P1_INVARIANTS_PRESERVED=true
P2_INVARIANTS_PRESERVED=true
EXTERNAL_AUTHORITIES_REMAIN_AUTHORITATIVE=true
NO_PROVIDER_MUTATION=true
PLAY_EXECUTED=false
SECRET_VALUES_EXPOSED=0
REGRESSION_TESTS=PASS
```

`P4 not started.`
