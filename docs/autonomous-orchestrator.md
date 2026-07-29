# Autonomous Subscription Orchestrator

Hermes can enable an autonomous orchestration foundation with:

```yaml
orchestrator:
  enabled: true
  billing_policy: subscription_only
  max_total_attempts: 8
  max_route_attempts: 2
```

When disabled, provider fallback behavior is unchanged. When enabled with
`subscription_only`, Hermes filters fallback routes by credential provenance,
not provider name alone.

## Approved Routes

- Builder: `openai-codex` with saved ChatGPT/device-code OAuth provenance.
- Fallback builder: `anthropic` only with verified Claude subscription OAuth
  provenance. If Claude builds, its output remains `UNATTACKED — DO NOT MERGE`
  until an independent Codex reviewer is available.
- Attacker: `anthropic` with Claude Code or Hermes PKCE subscription OAuth
  provenance. This role is read/test/report only.
- Diagnostician: Google Code Assist/Gemini CLI saved OAuth provenance. This
  role is read-only and may be represented as an external saved-subscription
  route when no native provider exists.

OpenAI API keys, Anthropic API/Console keys, Vertex, OpenRouter, Fable, Gemini
API key/AI Studio routes, and unknown paid fallbacks are refused in
subscription-only mode.

## Health Meanings

Startup health is local and no-spend. It reads only saved status metadata,
expiry timestamps, refresh-token presence flags, and local availability hints.
It does not make inference calls.

- `healthy_reusable`: saved credential metadata is reusable.
- `temporary_rate_limit`: route is parked until reset/cooldown.
- `expired_access_refresh_available`: refresh can be attempted without browser login.
- `expired_access_missing_refresh`: fresh interactive auth is required.
- `revoked_dead`: credential is dead/revoked and requires fresh auth.
- `browser_oauth_timeout`: prior browser login timed out and requires user action.
- `unavailable_cli_model`: required local CLI/model is not available.
- `provider_outage_unknown_transport`: transport/provider state is unknown.

Reusable saved credentials suppress browser-login requests. Fresh interactive
login is escalated only for missing, revoked, no-refresh, or browser-timeout
states. State, reprs, logs, and summaries must never contain access or refresh
token values.

## Goal Queue And Recovery

The standing `/goal` state now has a durable FIFO goal queue. If a goal is
active, waiting, or paused, setting another goal appends it instead of replacing
the current one. Exactly one queued goal is promoted after the current goal
reaches a terminal authorized state such as done or cleared. This queue is
separate from `/queue`, which remains a prompt queue.

Recovery decisions are deterministic data: switch route, wait, refresh saved
credential, checkpoint and rotate context, resume from checkpoint, queue, or
escalate. Retry loops are bounded by total and per-route attempt limits.
Escalations are limited to strategic/business information, new spending, fresh
interactive auth, no approved route, or destructive rollback approval.

## Checkpoints

The orchestrator checkpoint stores sanitized handoff data in `state_meta`:
goal text, plan/evidence paths, git dirty path/status summary, genuine
process/session/worker IDs when known, next action, and verification status or
digest. It does not store file contents or command output. Compression session
rotation migrates the active goal, queued goals, and orchestrator checkpoint to
the continuation session.

## Limitations

Gemini Code Assist health is represented as local saved-route metadata only.
Hermes does not guess unsupported Google protocols or call private vendor APIs.
Credentials are not permanent; refresh or re-auth may still be required when
providers expire, revoke, or change subscription access.
