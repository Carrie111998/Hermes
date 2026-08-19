# HERMES_DURABLE_MISSION_P1_BASELINE_CANONICALIZATION_V1

## Verdict

`P1_BASELINE_BLOCKED_REGRESSION`

## Baseline before

- repository: `/home/deploy/hermes-agent`
- branch: `hermes-dashboard-recovery-v1`
- HEAD: `4b19cce426edd59daf53ff42668527d5520cb3e4`
- worktree: dirty; 5 modified files, 5 untracked files, no staged files, no submodules
- dirty stat: 335 insertions, 1 deletion in tracked files; 2,244 insertions, 1 deletion after including untracked candidates

## Experiment preservation

- mechanism: Git archival branch and commit
- branch: `hermes-p1-experiment-preservation-20260819`
- commit: `a157da9772`
- parent baseline: `4b19cce426edd59daf53ff42668527d5520cb3e4`
- complete: `true`
- candidate evidence: `tests/agent/test_compression_continuity.py`, 30 passed on preservation branch
- rollback: `git show a157da9772` or branch checkout

## Dirty-file classification and disposition

| File | Dirty change / purpose | Runtime wiring | P1 compatibility | Collision risk | Final disposition | Reason |
|---|---|---|---|---|---|---|
| `agent/mission_state.py` | New JSON mission/compression state candidate | Imported by compression instrumentation | Conflicts with future SessionDB authority | High | `SUPERSEDED_BY_TARGET_ARCHITECTURE` | Preserve vocabulary/evidence only; implementation not authoritative |
| `agent/compression_recovery.py` | New recovery detector/gate | Imported by `conversation_loop` and `turn_context` | Recovery authority not fail-closed or canonical | High | `SUPERSEDED_BY_TARGET_ARCHITECTURE` | Future restoration belongs at shared pre-LLM boundary |
| `agent/conversation_compression.py` | Compression-boundary mission-state instrumentation | Imported on compression path | Compression must remain token management | High | `REVERT_TO_HEAD` | Remove experimental authority side effects |
| `agent/conversation_loop.py` | Post-compression recovery hook | Main conversation loop | Conflicts with future restoration gate | High | `REVERT_TO_HEAD` | Keep committed loop behavior |
| `agent/turn_context.py` | Preflight recovery hook | Shared turn prologue | Candidate authority before future checkpoint API | High | `REVERT_TO_HEAD` | Keep clean shared boundary for P1 |
| `hermes_cli/lah_bootstrap.py` | New candidate router/CodeGraph/governor authority | Imported by dirty `model_tools.py` | Duplicates external authorities | High | `SUPERSEDED_BY_TARGET_ARCHITECTURE` | Router, CodeGraph, Governor remain external |
| `model_tools.py` | Runtime CodeGraph/discovery gates | `handle_function_call` | Candidate policy/runtime authority not part of baseline | High | `REVERT_TO_HEAD` | Do not promote experimental enforcement |
| `tools/mcp_tool.py` | Candidate CodeGraph configuration enforcement | MCP tool/config path | Candidate duplicate CodeGraph authority | High | `REVERT_TO_HEAD` | Preserve committed MCP behavior |
| `tests/agent/test_compression_continuity.py` | Candidate continuity evidence | Test-only | Not required for clean baseline | None | `PRESERVE_AS_EVIDENCE` | Archived; 30 passed separately |
| `hermes_cli/MISSION_RECEIPT_codegraph_runtime_enforcement.json` | Candidate runtime receipt | Evidence-only | Not baseline authority | None | `PRESERVE_AS_EVIDENCE` | Archived with experiment commit |

No dirty file classified `UNKNOWN` or `UNRELATED_DIRTY_CHANGE`.

## Baseline after

- repository: `/home/deploy/hermes-agent`
- branch: `hermes-dashboard-recovery-v1`
- canonical source commit before receipt commit: `4b19cce426edd59daf53ff42668527d5520cb3e4`
- worktree: clean before this receipt commit
- no MissionEngine, checkpoint authority, or ActionCommitStore introduced
- no provider mutation, PLAY, or live provider call executed

## Runtime source binding

- launcher: `/home/deploy/.local/bin/hermes`
- resolved launcher: `/home/deploy/hermes-agent/venv/bin/hermes`
- interpreter: `/home/deploy/hermes-agent/venv/bin/python3`
- installed entrypoint: `hermes_cli.main:main`
- imported source: `/home/deploy/hermes-agent/hermes_cli/main.py`, `/home/deploy/hermes-agent/run_agent.py`
- runtime source binding: `CERTIFIED_CANONICAL_CHECKOUT`

## Startup surfaces and common boundary

- CLI: `cli.py` calls `self.agent.run_conversation()`; forwarder enters `agent.conversation_loop.run_conversation()`.
- TUI: `tui_gateway/server.py` calls `agent.run_conversation()`; forwarder enters `agent.conversation_loop.run_conversation()`.
- gateway/server: `gateway/platforms/api_server.py` and `gateway/run.py` call `agent.run_conversation()`; forwarder enters `agent.conversation_loop.run_conversation()`.
- common symbol: `agent.conversation_loop.run_conversation()` at line 371.
- shared pre-LLM prologue: `build_turn_context()` at line 407; it ensures SessionDB at `agent/turn_context.py:90`, then prepares the turn before provider transport.
- provider boundary: provider request occurs later in transport/helper code, e.g. `agent/chat_completion_helpers.py:1710` and `:1766`.
- `COMMON_PRE_LLM_ENFORCEMENT_BOUNDARY`: `agent/turn_context.py:90-150`, invoked once from `agent/conversation_loop.py:407-422`, before provider invocation.
- restoration gate: not implemented in this mission.

## CodeGraph

- binary: `/home/deploy/.local/bin/codegraph`
- version: `1.5.0`
- project: `/home/deploy/hermes-agent`
- refresh: `codegraph sync /home/deploy/hermes-agent`
- fresh after sync: `true`
- stats: 3,223 files; 89,709 nodes; 254,288 edges; 232.30 MB database
- pending changes after sync: none
- project binding: canonical repository

## Tests

- canonical required focused set: 11 files, 463 tests discovered; 449 passed, 9 failed, 5 startup tests passed separately within failed file
- canonical passing files: SessionDB, resume resolution, turn context, compression, compression persistence, tool dispatch, model tools, MCP startup, bootstrap
- gateway startup file: 1 passed, 9 unavailable because hermetic environment lacks `pytest-asyncio`; failures are `async def functions are not natively supported`, before test bodies
- experimental evidence-only: `tests/agent/test_compression_continuity.py`, 30 passed on preservation branch `a157da9772`
- superseded tests: none; continuity test retained only as evidence, not baseline requirement
- regression interpretation: no source regression observed; environment-dependent async test capability remains documented

## Previous blockers

- B1 dirty candidate: `RESOLVED` for canonical baseline; candidate preserved
- B2 stale CodeGraph: `RESOLVED` after sync
- B3 multiple startup surfaces: `RESOLVED`; common boundary certified
- B4 no production MissionState population: `P1_IMPLEMENTATION_REQUIREMENT`
- B5 no ActionCommit ledger: `P2_IMPLEMENTATION_REQUIREMENT`
- B6 recovery not fail-closed: `P1_IMPLEMENTATION_REQUIREMENT`
- B7 approval restart durability unproven: `EXTERNAL_AUTHORITY_REQUIREMENT`
- B8 no checkpoint schema: `P1_IMPLEMENTATION_REQUIREMENT`
- B9 runtime source ambiguity: `RESOLVED`

## P1 readiness

- canonical baseline clean: `true`
- dirty experiment preserved: `true`
- runtime source binding certified: `true`
- common pre-LLM boundary certified: `true`
- CodeGraph fresh: `true`
- baseline tests pass: `false`; 9 required gateway-startup tests cannot execute because hermetic venv lacks `pytest-asyncio`
- authority collisions: `false`
- secret values exposed: `0`
- provider mutation: `false`
- PLAY executed: `false`

## Exact next mission scope

`N/A` until gateway-startup regression certification is restored.

Bounded scope: stable mission identity; dedicated SessionDB mission tables; checkpoint schema/versioning; checkpoint persistence API; restoration before LLM call; deterministic bounded mission projection; fail-closed missing, corrupt, or incompatible checkpoint behavior.

Explicitly out of scope: ActionCommitStore; side-effect replay; provider mutation changes; approval or safety redesign; semantic/vector recall; subagent isolation; full hot/warm/cold context system; compression rewrite.

## Security and execution

- `SECRET_VALUES_EXPOSED=0`
- `PROVIDER_MUTATION=false`
- `PLAY_EXECUTED=false`
