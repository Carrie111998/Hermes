---
sidebar_position: 13
title: "Webhooks"
description: "Receive events from GitHub, GitLab, and other services to trigger Hermes agent runs"
---

# Webhooks

Receive events from external services (GitHub, GitLab, JIRA, Stripe, etc.) and trigger Hermes agent runs automatically. The webhook adapter runs an HTTP server that accepts POST requests, validates HMAC signatures, transforms payloads into agent prompts, and routes responses back to the source or to another configured platform.

The agent processes the event and can respond by posting comments on PRs, sending messages to Telegram/Discord, or logging the result.

## Video Tutorial

<div style={{position: 'relative', width: '100%', aspectRatio: '16 / 9', marginBottom: '1.5rem'}}>
  <iframe
    src="https://www.youtube.com/embed/WNYe5mD4fY8"
    title="Hermes Agent — Webhooks Tutorial"
    style={{position: 'absolute', top: 0, left: 0, width: '100%', height: '100%', border: 0}}
    allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
    allowFullScreen
  />
</div>

---

## Quick Start

1. Enable via `hermes gateway setup` or environment variables
2. Define routes in `config.yaml` **or** create them dynamically with `hermes webhook subscribe`
3. Point your service at `/webhooks/<route-name>` for a default-bound route. A route with an explicit named `profile` always uses `/p/<profile>/webhooks/<route-name>`, including on a single-profile gateway.

---

## Setup

There are two ways to enable the webhook adapter.

### Via setup wizard

```bash
hermes gateway setup
```

Follow the prompts to enable webhooks, set the port, and set a global HMAC secret.

### Via environment variables

Add to `~/.hermes/.env`:

```bash
WEBHOOK_ENABLED=true
WEBHOOK_PORT=8644        # default
WEBHOOK_SECRET=your-global-secret
```

### Verify the server

Once the gateway is running:

```bash
curl http://localhost:8644/health
```

Expected response:

```json
{"status": "ok", "platform": "webhook", "accepting_webhooks": true}
```

---

## Configuring Routes {#configuring-routes}

Routes define how different webhook sources are handled. Each route is a named entry under `platforms.webhook.extra.routes` in your `config.yaml`.

### Route properties

| Property | Required | Description |
|----------|----------|-------------|
| `provider` | **Yes*** | Explicit provider contract, such as `github`, `gitlab`, `svix`, `standard_webhooks`, `stripe`, `hermes`, or `generic`. A route must declare `provider` or `signature_mode`; Hermes never guesses from attacker-controlled headers. |
| `signature_mode` | Sometimes | Explicit verifier mode. Usually inferred from `provider`; required when that provider has no safe default, and useful to select `generic_v1` versus recommended `generic_v2`. |
| `events` | No | Event types admitted by this route. GitHub permits at most one of `check_run`, `pull_request`, `push`, `issues`, or `ping`; Hermes requires both the unsigned header and the HMAC-covered body shape to match. GitLab also permits at most one event and requires its unsigned header to equal the route-bound value. With no configured event, the request is unfiltered but its resolved event is `unknown`. Other providers may resolve an authenticated body event. |
| `secret` | **Yes** | HMAC secret for signature validation. A global `secret` may supply the value only when exactly one authenticated route uses that fallback; configure unique per-route secrets otherwise. Set to `"INSECURE_NO_AUTH"` for loopback testing only (skips validation). |
| `profile` | No | Profile authorized to execute this route. Omit it (or use `default`) for `/webhooks/<route>`; an explicit name such as `coder` always binds the route and secret to `/p/coder/webhooks/<route>`. In single-profile mode that name must be the running profile itself; `gateway.multiplex_profiles` only adds the ability to serve multiple allowed profiles from one gateway. |
| `prompt` | No | Template string with dot-notation payload access (e.g. `{pull_request.title}`). If omitted, the prompt includes a parseable raw-payload envelope bounded to 4,000 UTF-8 bytes; it does not include an unbounded full dump. Payload fields are untrusted — see [Authenticated does not mean trusted](#authenticated-does-not-mean-trusted). |
| `filters` | No | Declarative payload filters evaluated after auth/body/event filtering and before agent or direct delivery work. Non-matches return `{"status":"ignored","reason":"filter"}` with HTTP 200. |
| `script` | No | Filter/transform script under the active profile's `$HERMES_HOME/scripts/` (normally `~/.hermes/scripts/`). The webhook payload is passed as JSON on stdin. JSON object stdout replaces the payload before templating; text stdout is exposed as `script_output`; empty stdout, `[SILENT]`, or `{"__hermes_ignore__": true}` suppresses delivery. A timeout or nonzero exit after execution starts returns HTTP 500 as indeterminate and durably fences retries for the same delivery identity. |
| `skills` | No | List of skill names to load for the agent run. |
| `toolsets` | No | List of toolset keys (e.g. `["terminal", "file", "web"]`) that **replaces** the platform-level webhook toolset for runs triggered by this route only. Manual config edit only — not settable via `hermes webhook subscribe`, so agent-created subscriptions cannot self-grant elevated tools. Names are validated the same way as `platform_toolsets` entries (unknown or platform-restricted names are dropped). See [Per-route toolsets](#per-route-toolsets). |
| `deliver` | No | Where to send the response: `github_comment`, `telegram`, `discord`, `slack`, `signal`, `sms`, `whatsapp`, `matrix`, `mattermost`, `homeassistant`, `email`, `dingtalk`, `feishu`, `wecom`, `weixin`, `bluebubbles`, `qqbot`, or `log` (default). |
| `deliver_extra` | No | Additional delivery config — keys depend on `deliver` type (e.g. `repo`, `pr_number`, `chat_id`). Values support the same `{dot.notation}` templates as `prompt`. |
| `deliver_only` | No | If `true`, skip the agent entirely — the rendered `prompt` template becomes the literal message that gets delivered. Zero LLM cost, sub-second delivery. See [Direct Delivery Mode](#direct-delivery-mode) for use cases. Requires `deliver` to be a real target (not `log`). |

\* Declare at least one of `provider` or `signature_mode`.

### Full example

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      port: 8644
      secret: "global-fallback-secret"
      routes:
        github-pr:
          provider: "github"
          events: ["pull_request"]
          secret: "github-webhook-secret"
          prompt: |
            Review this pull request:
            Repository: {repository.full_name}
            PR #{number}: {pull_request.title}
            Author: {pull_request.user.login}
            URL: {pull_request.html_url}
            Diff URL: {pull_request.diff_url}
            Action: {action}
          skills: ["github-code-review"]
          deliver: "github_comment"
          deliver_extra:
            repo: "{repository.full_name}"
            pr_number: "{number}"
        deploy-notify:
          provider: "github"
          events: ["push"]
          secret: "deploy-secret"
          prompt: "New push to {repository.full_name} branch {ref}: {head_commit.message}"
          filters:
            - field: "ref"
              equals: "refs/heads/main"
          deliver: "telegram"
```

### Payload Filters

Use `filters` when a provider sends a broad event stream but only some payloads should wake the agent or trigger `deliver_only` delivery. Filters run after signature validation, body parsing, event selection, and durable replay admission, but before scripts, prompt rendering, agent dispatch, or target delivery. An ignored authenticated identity therefore still leaves a permanent replay proof.

```yaml
platforms:
  webhook:
    extra:
      routes:
        todoist:
          provider: "generic"
          signature_mode: "generic_v2"
          events: ["item:updated"]
          secret: "todoist-secret"
          filters:
            - field: "payload.labels"
              contains: "hermes"
            - any:
                - field: "payload.priority"
                  equals: 4
                - field: "payload.project_id"
                  in_file: "~/.hermes/data/todoist/watchlist.json"
          prompt: "Todoist task changed: {payload.content}"
```

Supported operators:

- `exists: true|false`
- `missing: true`
- `equals` / `not_equals`
- `contains` for strings, lists, and dict keys
- `in` for inline lists
- `in_file` for JSON arrays, JSON objects (keys are used), or newline-delimited text files
- `regex` (evaluated off the HTTP event loop in an isolated worker; patterns over 4 KiB, inputs over 256 KiB, invalid expressions, or matches exceeding 100 ms fail closed)

One route is limited to 64 filter nodes, eight levels of nesting, and eight regex operators. Routes that exceed any limit fail closed.
- `all`, `any`, and `not` groups

Field paths use dot notation. `payload.foo` reads from a top-level `payload` object when one exists, or from the root webhook body for flat payloads. `event` / `event_type` match the resolved authoritative event type. `headers.<Name>` exposes only headers cryptographically covered by the selected verifier; unsigned GitHub/GitLab event or delivery headers are diagnostics and are not filter inputs.

### Script Filters and Transforms

Use `script` when declarative filters are not enough. Scripts must live under the active profile's `$HERMES_HOME/scripts/` (normally `~/.hermes/scripts/`); relative paths resolve there, and path traversal outside that directory is blocked. Hermes captures the exact script bytes when it publishes the route and executes only those captured bytes. `.sh` and `.bash` sources run with bash using `--noprofile --norc`; all other sources run with the current Python interpreter in isolated mode.

Every invocation runs in a fresh, empty working directory with a captured minimal non-secret environment. It does not inherit `BASH_ENV`, `ENV`, `PYTHONPATH`, arbitrary custom variables, or credential variables from the gateway process. Consequently, relative imports, neighboring helper files, shell startup hooks, and ambient environment secrets are unavailable. Route scripts must be self-contained JSON transforms: read the supplied JSON from stdin and emit their result on stdout.

Route scripts are trusted local code, not an operating-system sandbox. They run as the gateway user and can deliberately access anything that account can access or detach processes beyond Hermes' best-effort child cleanup. The frozen source/interpreter contract, empty working directory, minimal environment, timeout, and output limits prevent accidental authority drift; they do not contain a malicious script. Run the gateway itself in a container, VM, or restricted service account when route-script authors are not fully trusted.

The route payload is sent to stdin as JSON:

```python
# ~/.hermes/scripts/todoist-hermes-label.py
import json
import sys

payload = json.load(sys.stdin)
labels = payload.get("payload", {}).get("labels", [])
if "hermes" not in labels:
    print("[SILENT]")
    raise SystemExit(0)

payload["body"] = payload["payload"]["content"]
print(json.dumps(payload))
```

Script outcomes:

- JSON object stdout replaces the payload used by `prompt` and `deliver_extra`.
- Non-JSON text stdout is added to the payload as `script_output`.
- Empty stdout, exact `[SILENT]`, or `{"__hermes_ignore__": true}` explicitly suppresses delivery and returns HTTP 200 with `{"status":"ignored","reason":"script"}`.
- Script configuration is validated before the route is published. A missing, unreadable, or invalid script prevents a static webhook listener from starting; the same defect in a dynamic subscription causes that candidate to be skipped or an already-published dynamic route to be withdrawn. It is not normally accepted and rediscovered per request. A defensive request-time configuration inconsistency still fails before script execution with HTTP 500 and `status=failed`, so that delivery can be retried after the route is repaired and republished.
- Once script execution is durably marked as started, a timeout, runtime failure, nonzero exit, or invalid output returns HTTP 500 with `status=indeterminate`. Hermes records an indeterminate outcome and fences the same delivery identity; a retry returns HTTP 409 with `status=indeterminate` instead of running the script again.
- Script stdout and stderr are each bounded to 1 MiB, and their combined captured output is also bounded to 1 MiB. Crossing any output bound terminates the script and follows the started-script `indeterminate` path above.

### Prompt Templates

Prompts use dot-notation to access nested fields in the webhook payload:

- `{pull_request.title}` resolves to `payload["pull_request"]["title"]`
- `{repository.full_name}` resolves to `payload["repository"]["full_name"]`
- `{__raw__}` — a parseable JSON envelope with `payload`, `truncated`, and `original_bytes` fields. The complete envelope, including JSON escaping and metadata, is bounded to 4,000 UTF-8 bytes by default.
- Missing keys are left as the literal `{key}` string (no error)
- Nested dicts and lists are JSON-serialized and truncated at 2000 characters

You can mix `{__raw__}` with regular template variables:

```yaml
prompt: "PR #{pull_request.number} by {pull_request.user.login}: {__raw__}"
```

If no `prompt` template is configured, Hermes places that same bounded 4,000-byte raw-payload envelope inside the generated prompt. Large payloads are explicitly marked `truncated`; an omitted template never creates an unbounded full JSON dump.

The same dot-notation templates work in `deliver_extra` values.

### Forum Topic Delivery

When delivering webhook responses to Telegram, you can target a specific forum topic by including `message_thread_id` (or `thread_id`) in `deliver_extra`:

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      routes:
        alerts:
          provider: "generic"
          signature_mode: "generic_v2"
          secret: "your-generic-v2-secret"
          events: ["alert"]
          prompt: "Alert: {__raw__}"
          deliver: "telegram"
          deliver_extra:
            chat_id: "-1001234567890"
            message_thread_id: "42"
```

If `chat_id` is not provided in `deliver_extra`, the delivery falls back to the home channel configured for the target platform. For a named multiplexed route whose target adapter is not active during route publication, Hermes reads that home channel from the routed profile's configuration—not from the process default profile—and freezes it into the route authority.

---

## GitHub PR Review (Step by Step) {#github-pr-review}

This walkthrough sets up automatic code review on every pull request.

### 1. Create the webhook in GitHub

1. Go to your repository → **Settings** → **Webhooks** → **Add webhook**
2. Set **Payload URL** to `http://your-server:8644/webhooks/github-pr`
3. Set **Content type** to `application/json`
4. Set **Secret** to match your route config (e.g. `github-webhook-secret`)
5. Under **Which events?**, select **Let me select individual events** and check **Pull requests**
6. Click **Add webhook**

### 2. Add the route config

Add the `github-pr` route to your `~/.hermes/config.yaml` as shown in the example above.

### 3. Ensure `gh` CLI is authenticated

The `github_comment` delivery type uses the GitHub CLI to post comments:

```bash
gh auth login
```

### 4. Test it

Open a pull request on the repository. The webhook fires, Hermes processes the event, and posts a review comment on the PR.

---

## GitLab Webhook Setup {#gitlab-webhook-setup}

GitLab webhooks work similarly but use a different authentication mechanism. GitLab sends the secret as a plain `X-Gitlab-Token` header (exact string match, not HMAC).

### 1. Create the webhook in GitLab

1. Go to your project → **Settings** → **Webhooks**
2. Set the **URL** to `http://your-server:8644/webhooks/gitlab-mr`
3. Enter your **Secret token**
4. Select **Merge request events** (and any other events you want)
5. Click **Add webhook**

### 2. Add the route config

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      routes:
        gitlab-mr:
          provider: "gitlab"
          events: ["Merge Request Hook"]
          secret: "your-gitlab-secret-token"
          prompt: |
            Review this merge request:
            Project: {project.path_with_namespace}
            MR !{object_attributes.iid}: {object_attributes.title}
            Author: {object_attributes.last_commit.author.name}
            URL: {object_attributes.url}
            Action: {object_attributes.action}
          deliver: "log"
```

---

## Delivery Options {#delivery-options}

The `deliver` field controls where the agent's response goes after processing the webhook event.

| Deliver Type | Description |
|-------------|-------------|
| `log` | Logs the response to the gateway log output. This is the default and is useful for testing. |
| `github_comment` | Posts the response as a pull-request comment via `gh pr comment`. Requires `deliver_extra.repo` and a positive `deliver_extra.pr_number`. The `gh` CLI must be installed and authenticated on the gateway host (`gh auth login`). |
| `telegram` | Routes the response to Telegram. Uses the home channel, or specify `chat_id` in `deliver_extra`. |
| `discord` | Routes the response to Discord. Uses the home channel, or specify `chat_id` in `deliver_extra`. |
| `slack` | Routes the response to Slack. Uses the configured home channel, or specifies `chat_id` in `deliver_extra`. A templated `chat_id` requires an explicit route-bound workspace `scope_id`; for a static channel the connected adapter can establish the scope, though declaring it explicitly is clearer. |
| `signal` | Routes the response to Signal. Uses the home channel, or specify `chat_id` in `deliver_extra`. |
| `sms` | Routes the response to SMS via Twilio. Uses the home channel, or specify `chat_id` in `deliver_extra`. |
| `whatsapp` | Routes the response to WhatsApp. Uses the home channel, or specify `chat_id` in `deliver_extra`. |
| `matrix` | Routes the response to Matrix. Uses the home channel, or specify `chat_id` in `deliver_extra`. |
| `mattermost` | Routes the response to Mattermost. Uses the home channel, or specify `chat_id` in `deliver_extra`. |
| `homeassistant` | Routes the response to Home Assistant. Uses the home channel, or specify `chat_id` in `deliver_extra`. |
| `email` | Routes the response to Email. Uses the home channel, or specify `chat_id` in `deliver_extra`. |
| `dingtalk` | Routes the response to DingTalk. Uses the home channel, or specify `chat_id` in `deliver_extra`. |
| `feishu` | Routes the response to Feishu/Lark. Uses the home channel, or specify `chat_id` in `deliver_extra`. |
| `wecom` | Routes the response to WeCom. Uses the home channel, or specify `chat_id` in `deliver_extra`. |
| `weixin` | Routes the response to Weixin (WeChat). Uses the home channel, or specify `chat_id` in `deliver_extra`. |
| `bluebubbles` | Routes the response to BlueBubbles (iMessage). Uses the home channel, or specify `chat_id` in `deliver_extra`. |

For cross-platform delivery, the target platform must also be enabled and connected in the gateway. If no `chat_id` is provided in `deliver_extra`, the response is sent to that platform's configured home channel. A templated Slack `chat_id` requires a route-bound `scope_id` so a signed event cannot cross workspace authority. For a static Slack channel, Hermes may freeze the scope established by the connected adapter; specifying `scope_id` explicitly removes ambiguity.

---

## Direct Delivery Mode {#direct-delivery-mode}

By default, every webhook POST triggers an agent run — the payload becomes a prompt, the agent processes it, and the agent's response is delivered. This costs LLM tokens on every event.

For use cases where you just want to **push a plain notification** — no reasoning, no agent loop, just deliver the message — set `deliver_only: true` on the route. The rendered `prompt` template becomes the literal message body, and the adapter dispatches it directly to the configured delivery target.

### When to use direct delivery

- **External service push** — Supabase/Firebase webhook fires on a database change → notify a user in Telegram instantly
- **Monitoring alerts** — Datadog/Grafana alert webhook → push to a Discord channel
- **Inter-agent pings** — Agent A notifies Agent B's user that a long-running task finished
- **Background job completion** — Cron job finishes → post result to Slack

Benefits:

- **Zero LLM tokens** — the agent is never invoked
- **Sub-second delivery** — a single adapter call, no reasoning loop
- **Same security as agent mode** — HMAC auth, rate limits, idempotency, and body-size limits all still apply
- **Synchronous response** — the POST returns `200 OK` once delivery succeeds. A target failure known to occur before any effect returns `503` with `Retry-After: 5` and is retryable; `502` means the target outcome is indeterminate and requires reconciliation, not an automatic retry.

### Example: Telegram push from Supabase

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      port: 8644
      secret: "global-secret"
      routes:
        antenna-matches:
          provider: "generic"
          signature_mode: "generic_v2"
          secret: "antenna-webhook-secret"
          deliver: "telegram"
          deliver_only: true
          prompt: "🎉 New match: {match.user_name} matched with you!"
          deliver_extra:
            chat_id: "{match.telegram_chat_id}"
```

Your Supabase edge function must set `X-Webhook-Timestamp` to the current Unix timestamp, compute the lowercase HMAC-SHA256 hex digest of `<timestamp>.<exact request body>` with `antenna-webhook-secret`, and send that digest in `X-Webhook-Signature-V2` when it POSTs to `https://your-server:8644/webhooks/antenna-matches`. The timestamp must be within 300 seconds of the gateway clock. The adapter validates that explicit `generic_v2` contract, renders the template, delivers to Telegram, and returns `200 OK`.

### Example: Dynamic subscription via CLI

```bash
hermes webhook subscribe antenna-matches \
  --provider generic \
  --signature-mode generic_v2 \
  --deliver telegram \
  --deliver-chat-id "123456789" \
  --deliver-only \
  --prompt "🎉 New match: {match.user_name} matched with you!" \
  --description "Antenna match notifications"
```

### Response codes

| Status | Meaning |
|--------|---------|
| `200 OK` | Delivered successfully. The body includes `status`, `route`, `target`, `delivery_id`, and the durable target `settlement` (`confirmed` or `cached`). |
| `200 OK` (status=suppressed) | An intentional autonomous silence response was settled without invoking the target; `settlement` is `suppressed`. |
| `200 OK` (status=duplicate) | The authenticated replay identity already settled. Its permanent proof prevents re-delivery. |
| `202 Accepted` (status=in_progress) | That replay identity is already running or owns a retryable staged carrier. |
| `409 Conflict` | The authenticated replay identity conflicts with another body, or its prior outcome is indeterminate and requires reconciliation. |
| `401 Unauthorized` | HMAC signature invalid or missing. |
| `400 Bad Request` | Malformed JSON body. |
| `404 Not Found` | Unknown route name. |
| `413 Payload Too Large` | The request body exceeded `max_body_bytes`, or a pre-effect template expansion exceeded a durable carrier limit. |
| `429 Too Many Requests` | Route rate limit exceeded. |
| `500 Internal Server Error` | A started route script failed or timed out; the outcome is durably fenced as indeterminate. |
| `502 Bad Gateway` | The target attempt may already have taken effect, so its outcome is indeterminate and requires reconciliation. The replay identity is durably fenced; repeating that same identity returns `409` instead of invoking the target again. |
| `503 Service Unavailable` | Intake/recovery authority, one of the four **process-global** bounded route workers shared by all webhook adapters/profiles, target preflight, or durable ledger capacity is unavailable. Route-worker saturation includes `Retry-After: 1`; a pre-effect target failure includes `Retry-After: 5`; capacity saturation has no automatic retry interval because permanent replay proofs are never evicted. |

### Configuration gotchas

- `deliver_only: true` requires `deliver` to be a real target. `deliver: log` (or omitting `deliver`) is rejected at startup — the adapter refuses to start if it finds a misconfigured route.
- The `skills` field is ignored in direct delivery mode (no agent runs, so there's nothing to inject skills into).
- Template rendering uses the same `{dot.notation}` syntax as agent mode, including the `{__raw__}` token.
- Replay fencing uses only authenticated identity material. Svix/Standard Webhooks bind their message ID into the signature; Stripe/Hermes can use an authenticated body ID. Body-only providers such as GitHub are fenced by the authenticated body digest, not by an unsigned transport ID.

---

## Dynamic Subscriptions (CLI) {#dynamic-subscriptions}

In addition to static routes in `config.yaml`, you can create webhook subscriptions dynamically using the `hermes webhook` CLI command. This is especially useful when the agent itself needs to set up event-driven triggers.

### Create a subscription

```bash
hermes webhook subscribe github-issues \
  --provider github \
  --events "issues" \
  --prompt "New issue #{issue.number}: {issue.title}\nBy: {issue.user.login}\n\n{issue.body}" \
  --deliver telegram \
  --deliver-chat-id "-100123456789" \
  --description "Triage new GitHub issues"
```

This returns the webhook URL and an auto-generated HMAC secret. Configure your service to POST to that URL.

### List subscriptions

```bash
hermes webhook list
```

### Remove a subscription

```bash
hermes webhook remove github-issues
```

### Test a subscription

```bash
hermes webhook test github-issues
hermes webhook test github-issues --payload '{"action":"opened","issue":{"number":42,"title":"Test"},"repository":{"id":1,"full_name":"owner/repo"},"sender":{"id":2,"login":"tester"}}'
```

The first command generates a provider-valid body automatically. A custom GitHub payload must still prove the configured event class; for `issues`, that includes a supported `action` plus `issue`, `repository`, and `sender` objects. The CLI signs the exact custom bytes and sends the route-bound `X-GitHub-Event` header, but it does not repair an invalid provider payload for you.

### How dynamic subscriptions work

- Subscriptions are stored in the active profile's `${HERMES_HOME:-$HOME/.hermes}/webhook_subscriptions.json` (for example, `~/.hermes/profiles/ops/webhook_subscriptions.json` for a named profile)
- The webhook adapter checks the file at connect time and before each incoming webhook, so no restart is needed. File identity/metadata changes are loaded on that next check; when those values are unchanged, bounded rereads and SHA-256 content checks are rate-limited to about once per second to catch same-metadata edits without letting request floods amplify file reads.
- Static routes from `config.yaml` always take precedence over dynamic ones with the same name
- Dynamic subscriptions use the same route format and capabilities as static routes (events, prompt templates, skills, delivery)
- No gateway restart required — subscribe and it's immediately live

### Agent-driven subscriptions

The agent can create subscriptions via the terminal tool when guided by the `webhook-subscriptions` skill. Ask the agent to "set up a webhook for GitHub issues" and it will run the appropriate `hermes webhook subscribe` command.

---

## Per-route toolsets {#per-route-toolsets}

Webhook agent runs default to a deliberately constrained toolset (`web_search`, `web_extract`, `vision_analyze`, `clarify`) because webhook payloads can carry untrusted third-party content — a public PR title or issue comment should never be able to prompt-inject its way into your terminal.

For **trusted** routes — a localhost monitoring daemon pushing system alerts, an internal CI system — you can grant a wider toolset to that route only, without widening every other webhook route:

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      routes:
        oom-emergency:
          provider: "generic"
          signature_mode: "generic_v2"
          secret: "monitor-secret"
          prompt: "Memory emergency: {detail}. Diagnose with ps/free/py-spy and report."
          toolsets: ["terminal", "file", "code_execution", "web"]
          deliver: "telegram"
```

For dynamic subscriptions, `hermes webhook subscribe` cannot grant toolsets. To grant them manually, edit the active profile's `${HERMES_HOME:-$HOME/.hermes}/webhook_subscriptions.json`, preserve the complete saved route entry, add `toolsets`, and replace its `secret` with fresh key material. Then update the sender to use that new secret. Toolsets are part of the key-bound execution policy, so reusing the old secret causes the candidate route to be rejected and an existing dynamic route to be withdrawn.

```json
{
  "oom-emergency": {
    "description": "Trusted local memory monitor",
    "profile": "default",
    "provider": "generic",
    "signature_mode": "generic_v2",
    "events": [],
    "secret": "NEW-UNUSED-SECRET",
    "prompt": "Memory emergency: {detail}. Diagnose and report.",
    "skills": [],
    "toolsets": ["terminal", "file", "web"],
    "deliver": "telegram",
    "deliver_extra": {"chat_id": "123456789"},
    "created_at": "2026-08-28T00:00:00Z"
  }
}
```

Behavior and safety properties:

- The route list **replaces** the platform-level webhook toolset resolution for that route's runs (it is not merged).
- Names are validated through the same path as `platform_toolsets` config — unknown names and platform-restricted toolsets are dropped.
- `hermes webhook subscribe` deliberately does **not** accept a toolsets flag. Granting elevated tools requires a complete manual config-file edit and fresh secret, so an agent creating its own subscription at runtime cannot self-grant `terminal`.
- Only grant elevated toolsets to routes whose senders you fully control, with a real HMAC secret. Anyone who can POST a validly-signed payload to that route is effectively running an agent with those tools.

---

## Security {#security}

The webhook adapter includes multiple layers of security:

### HMAC signature validation

The adapter validates incoming webhook signatures using the appropriate method for each source:

- **GitHub**: `X-Hub-Signature-256` header — HMAC-SHA256 hex digest prefixed with `sha256=`
- **GitLab**: `X-Gitlab-Token` header — plain secret string match
- **Generic (V2, recommended)**: `X-Webhook-Signature-V2` + `X-Webhook-Timestamp` headers — HMAC-SHA256 hex digest of `<timestamp>.<body>`. The timestamp (Unix seconds) must be within ±300 seconds of the server clock, which prevents captured requests from being replayed later.
- **Generic (V1, legacy)**: `X-Webhook-Signature` header — raw HMAC-SHA256 hex digest of the body only. V1 has no signed timestamp or nonce, so a captured body can still be presented as fresh later. The durable ledger permanently fences a second execution of that identical authenticated body, which also means legitimate identical V1 payloads collapse into one identity. The gateway logs a deprecation warning once per route; switch senders to V2.

If a secret is configured but no recognized signature header is present, the request is rejected.

### Secret is required

Every route must have a secret. The global `secret` is a convenience fallback for **exactly one authenticated route**; two authenticated routes that inherit it would reuse key material across authority scopes, so startup fails closed. Configure a unique secret on every route when more than one authenticated route exists. For development/testing only, you can set the secret to `"INSECURE_NO_AUTH"` to skip validation entirely.

Authentication key material is permanently bound in the root durable authority across all profiles. The binding covers the physical profile incarnation, route name, provider, signature mode, and the complete non-secret execution policy: the canonical route, resolved toolsets, complete captured script execution contract (source bytes, interpreter and isolated invocation, minimal environment, and empty-working-directory rule), snapshotted skill scaffold, captured `in_file` filter values, and frozen delivery target authority. A key cannot later be reassigned by renaming or moving a route, changing any dependency or verifier, deleting and recreating a profile, or starting another profile. Hermes withdraws a live route when a bound file, profile, grant, skill, or target no longer matches its published snapshot. Rotate to fresh secret material whenever any bound field or policy changes; the previous binding remains as replay evidence and is not reusable.

The route's `profile` field binds its secret to one execution target in every
gateway mode. A route without `profile` is default-profile-only; an explicit
named profile always requires the matching `/p/<profile>/` prefix. A
single-profile gateway accepts only its own name there, while
`gateway.multiplex_profiles` permits one gateway to serve multiple allowed
profiles. A request carrying a valid route signature is still rejected if its
URL prefix does not match the route binding.

`INSECURE_NO_AUTH` is only accepted when the gateway is bound to a loopback host (`127.0.0.1`, `localhost`, `::1`). If it is combined with a non-loopback bind such as `0.0.0.0` or a LAN IP, the adapter refuses to start — this prevents accidentally exposing an unauthenticated endpoint on a public interface.

### Rate limiting

Each profile/route scope admits at most **30 new identities in a sliding 60-second window** by default. Configure the quota globally:

```yaml
platforms:
  webhook:
    extra:
      rate_limit: 60  # new identities per sliding 60 seconds
```

`rate_limit` must be an exact integer from 1 through 10,000. `script_timeout_seconds` is configured in the same `extra` block and must be an exact integer from 1 through 300 seconds. Invalid values make webhook startup fail as a configuration error rather than silently changing the limit.

New identities beyond the quota receive `429 Too Many Requests`. Exact duplicates, conflicts, and already-active identities are resolved from durable replay evidence before this quota and do not consume another slot, so retry floods cannot starve fresh work.

### Idempotency

Authenticated provider delivery IDs become permanent replay identities. Providers without a signed delivery ID use the authenticated signed timestamp/body pair, or—under legacy body-only HMAC—the authenticated body digest. Settled and indeterminate evidence is durable across gateway restarts and is never evicted merely to admit new work. Only the explicit loopback test bypass has a bounded one-hour replay window.

The replay ledger is the stable Hermes root's `state.db`, not a named profile's local database. In a normal layout that is `~/.hermes/state.db`; when active `HERMES_HOME` is `<root>/profiles/<name>`, Hermes resolves the ledger to `<root>/state.db`. The same evidence therefore remains authoritative when a gateway changes between multiplexed and non-multiplexed operation. Within that root ledger, operation and replay authority is partitioned by the effective physical profile that executes the route; a syntactic `default` route in a named single-profile process therefore belongs to that named physical profile, not to a separate default partition.

Each physical profile home carries a durable random `.webhook-profile-incarnation` token. Hermes derives the route's profile generation from that resolved home and token, captures it in durable grants, and checks it again before script, agent, target, and recovery effects. Recreating or replacing a profile therefore cannot inherit stale work merely by reusing its name. Dead-owner recovery is scoped the same way: an adapter may reconcile only rows for physical profiles it currently serves and whose generation is still current; it leaves every other profile's rows untouched for an authorized adapter.

The ledger reserves worst-case storage before a script, agent, or target effect can run. `idempotency_max_storage_bytes` defaults to 1 GiB and must be an exact integer from 5 MiB through 64 GiB; one profile/route/provider cannot consume the global reserve. `idempotency_max_entries` defaults to 4096 live operations. Both caps are persisted as ledger authority, so reopening the same ledger with different values fails startup instead of silently changing admission semantics. These limits bound the webhook ledger's logical allocation inside shared `state.db`, not the physical size of that database or its WAL. Once permanent evidence exhausts the configured budget, new unique identities fail closed with HTTP 503 while exact duplicate/conflict decisions remain available.

### Body size limits

`max_body_bytes` defaults to—and cannot exceed—**1 MiB (1,048,576 bytes)**. It must be an exact integer from 1 through 1,048,576; booleans, fractional values, and larger limits are rejected at startup. A declared `Content-Length` over the cap is rejected before reading. Chunked or otherwise lengthless bodies are aborted during the bounded read as soon as they cross the cap.

```yaml
platforms:
  webhook:
    extra:
      max_body_bytes: 1048576  # default and hard maximum: 1 MiB
```

Post-authentication carriers are bounded separately: the rendered prompt is at most **512 KiB**, the complete durable event snapshot at most **2 MiB**, and each durable target or tool-grant authority snapshot at most **64 KiB**. A template expansion that exceeds a limit before any configured script or downstream effect returns HTTP 413 and releases its operation claim. If a started script produces an oversized carrier, Hermes instead records an indeterminate result and returns HTTP 500 rather than pretending it was safe to retry.

### Authenticated does not mean trusted

:::warning
**HMAC validation authenticates the _sender_, not the _content_.** A valid signature only proves the request came from a party holding the route's secret (e.g. GitHub). It says nothing about who wrote the _business fields_ inside the payload — PR titles, commit messages, issue descriptions, and any other upstream text are authored by arbitrary third parties and must be treated as untrusted.

This is the same trust model that applies to everything the agent reads: web pages, files, and tool output are all untrusted input. Hermes does not — and cannot reliably — sanitize untrusted text with a blocklist; phrasing, encoding, and translation make that trivially bypassable. **The trust boundary is the agent's capability surface, not the input channel.** Harden there:

- **Sandbox the runtime.** Run the gateway with the Docker or SSH terminal backend (or in a VM) when exposed to the internet, so a hijacked turn cannot touch the host.
- **Scope the toolset.** Disable `terminal`, `file`, and outbound-action tools on webhook-triggered sessions if the route only needs to read and summarize. Fewer capabilities means a smaller blast radius if a payload field carries injected instructions.
- **Keep approvals on** for any destructive or outbound operation, so an injected instruction cannot act unattended.
- **Template narrowly.** Prefer a specific `prompt` with named fields (`{pull_request.title}`) over `{__raw__}` or an empty template that dumps the whole payload, so only the fields you intend reach the prompt.
:::

---

## Troubleshooting {#troubleshooting}

### Webhook not arriving

- Verify the port is exposed and accessible from the webhook source
- Check firewall rules — port `8644` (or your configured port) must be open
- Verify the URL path matches the route binding: `/webhooks/<route-name>` for default, or `/p/<profile>/webhooks/<route-name>` for every explicit named profile (single-profile and multiplex modes)
- Use the `/health` endpoint to confirm the server is running

### Signature validation failing

- Ensure the secret in your route config exactly matches the secret configured in the webhook source
- For GitHub, the secret is HMAC-based — check `X-Hub-Signature-256`
- For GitLab, the secret is a plain token match — check `X-Gitlab-Token`
- Check gateway logs for `Invalid signature` warnings

### Event rejected or ignored

- Check that the event type exactly matches your route's `events` entry. A mismatched route-bound GitHub/GitLab event is rejected with `401`, not ignored.
- GitHub route binding supports `check_run`, `pull_request`, `push`, `issues`, and `ping`. The `X-GitHub-Event` value and the authenticated JSON body shape must both match; changing the unsigned header on a signed body is rejected with `401`.
- GitLab events use exact `X-GitLab-Event` header values such as `Merge Request Hook` and `Push Hook`, not payload values such as `merge_request`
- If `events` is empty or not set, requests are unfiltered. Route-bound GitHub/GitLab requests resolve as `event=unknown`; generic, Stripe, and other body-authoritative providers can still resolve an event from authenticated payload fields.

### Agent not responding

- Run the gateway in foreground to see logs: `hermes gateway run`
- Check that the prompt template is rendering correctly
- Verify the delivery target is configured and connected

### Duplicate responses

- Check that retries preserve the provider material actually covered by its signature: for example `svix-id`/`webhook-id`, an authenticated body ID, or the exact signed timestamp/body pair. Unsigned diagnostic headers do not control replay identity.
- Inspect gateway logs for durable-ledger or settlement errors. Replay proofs are persistent; they are not a one-hour process-local cache.

### `gh` CLI errors (GitHub comment delivery)

- Run `gh auth login` on the gateway host
- Ensure the authenticated GitHub user has write access to the repository
- Check that `gh` is installed and on the PATH

---

## Environment Variables {#environment-variables}

| Variable | Description | Default |
|----------|-------------|---------|
| `WEBHOOK_ENABLED` | Enable the webhook platform adapter | `false` |
| `WEBHOOK_PORT` | HTTP server port for receiving webhooks | `8644` |
| `WEBHOOK_SECRET` | Global HMAC fallback for exactly one authenticated route; use unique route secrets when multiple routes exist | _(none)_ |
