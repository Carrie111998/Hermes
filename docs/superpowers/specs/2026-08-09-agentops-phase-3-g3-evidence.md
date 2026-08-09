# AgentOps Phase 3 G3 Evidence (offline only)

**Branch:** `codex/agentops-phase-3-incident`

Phase 3 implements read-only Incident Ops primitives: stable signal
fingerprints, bounded cross-target/time correlation, incident state transitions,
deterministic model-degraded review, digest/notifier gates, and a dashboard
manifest/proxy with no chat, long-lived token, or Target-write surface.

## Evidence

| Area | Offline evidence |
|---|---|
| Fingerprint/correlation | `test_stable_cross_target_correlation_and_window_split` |
| State/notification | `test_state_merge_split_and_notification_throttle` |
| Model fallback/digest/dashboard | `test_review_degrades_without_model_and_digest_dashboard_are_bounded` |

## Explicit non-claims

- No real LLM is connected; reviewer uses deterministic degraded rules.
- No Gateway, LaunchAgent, Cron, business data, Target registry, or lifecycle
  write is performed.
- No dashboard chat or durable token is implemented.
- Seven-day online observation, incident SLOs, and production notification
  delivery remain pending; current evidence is offline/simulated only.
