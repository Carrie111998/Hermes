# Evidence-gated autonomous dispatch

Hermes can require a short-lived external admission receipt before the Kanban
dispatcher reconciles or claims autonomous work. The feature is opt-in so
existing installations retain their current behavior until their controller
is ready to mint `aos.dispatch_admission.v1` receipts.

```yaml
kanban:
  dispatch_in_gateway: true
  max_spawn: 5
  max_in_progress: 5
  require_admission_receipt: true
  admission_receipt_path: ~/.local/state/aos/dispatch-admission.json
  github_receipt_dir: ~/.local/state/aos/github-action-receipts

telemetry:
  request_attribution:
    enabled: true
    litellm_endpoints:
      - http://127.0.0.1:4001
```

An admission receipt is valid for at most five minutes and must pass every
declared gate: hook health, Hermes canaries, source/installed hashes, router
acceptance, telemetry coverage, GitHub broker read-back, quota state, and the
aggregate worker count. Missing, stale, failed, or malformed receipts cause a
no-claim tick. They do not terminate workers already running.

Every card has two explicit controls:

- `admission_class`: `hold`, `cloud_priority`, or `local_only`. Existing cards
  migrate to `hold`; fresh cards must be classified deliberately.
- `completion_contract`: `standard` or `github_effect_v1`. The latter cannot
  become `done` without a fresh `aos.github_action_receipt.v1` bound to the
  exact task, repository, branch, and workspace HEAD.

If GitHub proof is missing or invalid, Hermes retains the proposed completion,
closes the worker run, moves the card to `blocked/receipt_pending`, and keeps
its workspace. A broker receipt reconciler completes the proposal exactly once
after independent GitHub read-back succeeds. Receipt IDs cannot be reused.

When request attribution is enabled, each physical LiteLLM request receives a
metadata-only `aos.telemetry_envelope.v1` under the
`x-litellm-spend-logs-metadata` header. High-cardinality join fields remain in
LiteLLM spend-log metadata; they are not Prometheus labels unless an operator
separately configures those paths.
