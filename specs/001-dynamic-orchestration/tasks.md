# Tasks: Dynamic Multimodel Orchestration

Status: PROPOSED. Dependency-ordered implementation plan; no row below is complete merely because this contract package exists.

## Wave 0 — contract gate
- [ ] T001 Validate every schema/envelope against spec/data-model/contracts. Depends: none. Acceptance: no contradictions, `unknown` semantics present everywhere. Test: contract documentation consistency review.
- [ ] T002 Add contract fixtures for provider/product/surface/account/entitlement/billing/quota/model/endpoint/variant/region. Depends: T001. Acceptance: same model in separate pools produces distinct route_id. Test: unit/property route canonicalization.
- [ ] T003 Add sanitized observation fixtures with missing plan/quota/reset/price. Depends: T001. Acceptance: every datum has source/captured_at/valid_until/freshness/confidence/is_estimated; absence remains unknown. Test: contract serialization/property.
- [ ] T004 Add AC-01 unknown-capacity policy regression. Depends: T001,T003. Acceptance: high-risk unknown fails closed without reservation/dispatch; lower-risk unknown persists a policy disposition plus evidence before continuation; neither branch treats unknown as healthy, zero or unlimited. Tests: unit/property + decision serialization.

## Wave 1 — pure domain model and policy
- [ ] T101 Implement immutable TaskEnvelope/Route/Observation/Decision/Execution DTOs. Depends: T001-T003. Acceptance: model identity absent from task except audited justification; no secret-bearing fields. Tests: unit + contract redaction.
- [ ] T102 Implement six independent state machines and transition validators. Depends: T101. Acceptance: task/attempt/route/credential/reservation/review cannot be conflated; an exhausted route with an eligible alternative produces fallback/replan, while exhaustion with no eligible route produces `WAITING_FOR_CAPACITY` only with a recheck condition. Tests: unit/property invalid transition rejection plus both exhaustion branch invariants.
- [ ] T103 Implement deterministic eligibility-before-score engine. Depends: T101-T102. Acceptance: policy/privacy/capability/context/freshness/budget/breaker/concurrency/reservation filters execute before score; all rejections have stable code. Tests: unit/property permutation determinism.
- [ ] T104 Implement E0–E4 and V0–V4 policy mapping. Depends: T101. Acceptance: E0 produces no model reservation; E3/E4 enforce configured review independence. Tests: unit/contract.

## Wave 2 — pool projection, reservation and resilience
- [ ] T201 Add pure `CredentialPool.snapshot_for_capacity()` seam. Depends: T101. Acceptance: holds the pool lock and returns bounded secret-free immutable data; it never calls `select()`, `_available_entries(refresh=True)`, `_available_entries(clear_expired=True)`, `_persist()`, OAuth refresh or network. A no-argument `_available_entries()`/`has_available()` is permitted only after focused purity tests prove it cannot refresh, clear expiry, persist, call OAuth/network, or mutate. Tests: sentinels for every prohibited call and purity tests for any permitted no-arg read; every sentinel remains uncalled.
- [ ] T202 Build CapacityView from verified observations and pure snapshot. Depends: T201,T003. Acceptance: stable ordering/hash, per-route/pool isolation, stale/unknown not healthy, and `derived_remaining` is populated only from fresh, compatible `metric=remaining` observation value. Tests: unit/property + contract.
- [ ] T203 Implement atomic reservation ledger/lease/reconcile/release. Depends: T102,T202. Acceptance: unit-compatible dispatchable arithmetic, idempotent release, expiry and no oversubscription, including the last-unit race: with exactly one dispatchable unit and concurrent compatible reservation attempts, exactly one hold succeeds and the other is rejected/recomputed. Tests: concurrency tests (last-unit race/duplicate release/expiry).
- [ ] T204 Implement route/quota-pool circuit breakers. Depends: T202,T203. Acceptance: `route_capacity_exhausted` scopes invalidation/cooldown correctly; unrelated pool untouched. Tests: chaos tests for breaker reopen/half-open/probe budget.

## Wave 3 — explicit replanning/fallback
- [ ] T301 Implement RouteDecision persistence, `RuntimeErrorClassificationV1`, and fallback decision builder. Depends: T103,T203,T204. Acceptance: policy status/trigger/candidates/rejections/reason/quality compensation captured before dispatch; `kind=capacity_exhausted` maps to route/pool-scoped `route_capacity_exhausted` only through attempted normalized route identity, never a provider/model label. The observed `FailoverReason` lacks this mapping. **DECISION REQUIRED before wiring:** approved provider/runtime classification table, evidence threshold, retryability and breaker policy. Tests: contract serialization, classification mapping/unmapped-error rejection, and audit-field completeness.
- [ ] T302 Implement capacity-exhaustion state transition. Depends: T301. Acceptance: invalidate -> cooldown/breaker -> reconcile/release -> authorized refresh -> recompute -> explicit fallback -> reserve -> dispatch -> verify. Tests: integration/state-machine.
- [ ] T303 Exact regressions: an attempted Opus route emits `route_capacity_exhausted` while an eligible GPT route `{provider=openai, quota_pool=openai-team-a}` and/or GLM route `{provider=zai, quota_pool=zai-team-a}` remains; and exhaustion leaves no eligible route. Depends: T302. Acceptance: the first branch replans and dispatches an explicit fallback and MUST NOT create/schedule a sole wait job; the second and only the second produces `WAITING_FOR_CAPACITY` with a recheck condition. Tests: unit/property branch invariants plus integration and E2E using temp-HERMES_HOME and fake adapters/no remote calls.
- [ ] T304 Waiting-only regression. Depends: T302. Acceptance: WAITING_FOR_CAPACITY only when eligible set is empty or policy rejects all; state carries recheck condition/evidence. Tests: integration/property.

## Wave 4 — compatibility and shadow
- [ ] T401 Add a single pure resolver that projects to legacy runtime dict once. Depends: T101,T202,T301. Acceptance: no duplicate route calculation; no credential secret in decision. Tests: unit/parity.
- [ ] T402 Shadow-wire CLI/runtime_provider. Depends: T401. Acceptance: compare legacy/proposed decision without changing dispatch or adding provider call. Tests: integration in temp-HERMES_HOME.
- [ ] T403 Shadow-wire gateway, cron and delegation. Depends: T402. Acceptance: same normalized task/view gets equivalent policy result and divergence reason. Tests: entrypoint integration; cron drift guard remains intact.
- [ ] T404 Verify credential lease vs route reservation separation. Depends: T203,T403. Acceptance: existing `delegate_tool.py` credential lease cannot stand in for capacity reservation. Tests: concurrency/integration.

## Wave 5 — execution, verification, rollout
- [ ] T501 Implement adapter attestation requested/resolved/effective. Depends: T401. Acceptance: unsupported/effective-unproven values are unattested, never invented. Tests: contract/integration.
- [ ] T502 Implement V0–V4 verification planner and independence checks. Depends: T104,T501. Acceptance: same route/pool cannot self-approve a required independent review. Tests: unit/chaos.
- [ ] T503 Add opt-in/best-effort sanitized event schema/projection. Depends: T301,T501. Acceptance: telemetry failure does not block; no prompt/secret/raw provider/tool payload. Tests: contract/security.
- [ ] T504 Legacy/shadow/canary/rollback E2E. Depends: T402-T403,T501-T503. Acceptance: feature flag off restores legacy and releases holds; canary is bounded. Tests: E2E temp-HERMES_HOME only.

## Test matrix
| Requirement/invariant | Unit | Contract | Integration | Concurrency/chaos | E2E temp-HERMES_HOME |
|---|---|---|---|---|---|
| Provenance/unknown | T003 | T101 | T402 | — | T504 |
| Route normalization/pool isolation | T002 | T101 | T403 | T203/T204 | T504 |
| Separate state machines | T102 | T301 | T302 | T203 | T303/T304 |
| Policy-before-score | T103 | T301 | T302 | permutation stress | T504 |
| Pure CapacityView / mutable SSOT | T201/T202 | T201 | T403 | T204 | T504 |
| Reservation/concurrency/breaker | T203/T204 | T301 | T302 | T203/T204 | T504 |
| Explicit fallback/replan | T301/T302 | T301 | T303/T304 | breaker chaos | T303 |
| E0–E4/V0–V4/review | T104 | T502 | T501 | T502 | T504 |
| Legacy/shadow/canary/rollback | — | T504 | T402/T403 | T504 | T504 |
| Telemetry privacy | T503 | T503 | T503 | telemetry-failure | T504 |

## Verification discipline
All integration/E2E work uses a temporary `HERMES_HOME`, isolated fake adapters/clock/storage and no remote provider calls. No task is marked integrated until production call-path wiring and the mapped boundary test exist. Feature flags are not proof of integration; enabled/disabled/rollback behavior must be exercised.

## Contract-slice evidence — 2026-07-27

- `tests/agent/test_dynamic_orchestration.py`: 144 passed, 0 failed, inclusive inside the clean broad sweep.
- `tests/agent`: 6,835 passed; 1 failure outside this diff in `test_copilot_acp_client.py::test_run_prompt_preserves_real_home_when_profile_home_available`; `test_context_compressor.py` exceeded 300s on the first attempt and passed 214/214 on retry.
- Ruff, `py_compile`, and `git diff --check`: passed.
- Gitleaks 8.30.1, verified against the official SHA-256 and run against the patch: 0 findings.
- Independent review found incomplete IDNA endpoint canonicalization; the finding was reproduced and fixed together with equivalent IPv6 normalization and `route-v1` fixtures.
- Final independent-context review (`gpt-5.6-terra`): PASS, 144/144, hashes preserved, no HIGH/MEDIUM. Cross-family attestation remains unavailable because no eligible independent cross-family route was available; T505/T506-equivalent rollout gates remain open.
- This slice is pure contract/domain code. It does not implement `CredentialPool.snapshot_for_capacity()`, production `CapacityView`, persistent reservation ledger, circuit breakers, legacy resolver projection, runtime wiring, shadow mode, canary, rollout, or rollback E2E; therefore no task above is marked complete yet.
