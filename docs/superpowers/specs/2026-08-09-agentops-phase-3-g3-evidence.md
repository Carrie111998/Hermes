# AgentOps Phase 3 G3 Evidence (offline only)

**Branch:** `codex/agentops-phase-3-incident`

Phase 3 implements read-only Incident Ops primitives: stable signal
fingerprints, bounded cross-target/time correlation, incident state transitions,
deterministic model-degraded review, digest/notifier gates, and a dashboard
manifest/proxy with no chat, long-lived token, or Target-write surface.

## Evidence

| Area | Offline evidence |
|---|---|
| Fingerprint/correlation | `test_stable_cross_target_correlation_and_window_split`, `test_signal_id_is_atomic_idempotency_and_payload_is_redacted_and_frozen`, `test_fingerprint_drops_volatile_ids_and_normalizes_uuid` (including UUIDs embedded in error text) |
| State/history controls | `test_signal_and_split_budgets_fail_closed`, `test_split_merge_suppress_keep_public_history_and_rebind_seen`, `test_merge_marks_source_and_excludes_it_from_digest` (hard `_seen`/split caps, merge exclusion, suppression expiry/reopen) |
| Notification | `test_state_merge_split_and_notification_throttle`, `test_notification_period_is_monotonic_under_date_replay` |
| Model fallback/schema | `test_review_degrades_without_model_and_digest_dashboard_are_bounded`, `test_review_schema_rejects_unsafe_types_and_degraded_actions` |
| Dashboard auth | `test_dashboard_auth_expiry_and_direct_reads_fail_closed` (wrong/expired token and maximum TTL) |

## Explicit non-claims

- No real LLM is connected; reviewer uses deterministic degraded rules.
- No Gateway, LaunchAgent, Cron, business data, Target registry, or lifecycle
  write is performed.
- No dashboard chat or durable token is implemented.
- Seven-day online observation, incident SLOs, and production notification
  delivery remain pending; current evidence is offline/simulated only.
- G3 is not approved: these tests are local counterexample/contract evidence,
  not proof of production fleet coverage or a seven-day observation window.
