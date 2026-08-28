---
sidebar_position: 11
sidebar_label: "GitHub PR Reviews via Webhook"
title: "Automated GitHub PR Comments with Webhooks"
description: "Connect Hermes to GitHub so it automatically fetches PR diffs, reviews code changes, and posts comments — triggered by webhooks with no manual prompting"
---

# Automated GitHub PR Comments with Webhooks

This guide walks you through connecting Hermes Agent to GitHub so it automatically fetches a pull request's diff, analyzes the code changes, and posts a comment — triggered by a webhook event with no manual prompting.

When a PR is opened or updated, GitHub sends a webhook POST to your Hermes instance. Hermes runs the agent with a prompt that instructs it to retrieve the diff via the `gh` CLI, and the response is posted back to the PR thread.

:::tip Want a simpler setup without a public endpoint?
If you don't have a public URL or just want to get started quickly, check out [Build a GitHub PR Review Agent](./github-pr-review-agent.md) — uses cron jobs to poll for PRs on a schedule, works behind NAT and firewalls.
:::

:::info Reference docs
For the full webhook platform reference (all config options, delivery types, dynamic subscriptions, security model) see [Webhooks](/user-guide/messaging/webhooks).
:::

:::warning Prompt injection risk
Webhook payloads contain attacker-controlled data — PR titles, commit messages, and descriptions can contain malicious instructions. Hermes does not add an OS sandbox for a route's terminal grant. Isolate the configured terminal backend with Docker, a VM, or an SSH policy, and remember that final `github_comment` delivery runs separately on the gateway host. See the [security section](#security-notes) below.
:::

---

## Prerequisites

- Hermes Agent installed and running (`hermes gateway`)
- [`gh` CLI](https://cli.github.com/) installed and authenticated in the configured terminal backend, where the agent runs `gh pr diff`
- `gh` also installed and authenticated on the gateway host, where the `github_comment` delivery adapter runs `gh pr comment` (this can be the same installation for a local backend)
- A publicly reachable URL for your Hermes instance (see [Local testing with ngrok](#local-testing-with-ngrok) if running locally)
- Admin access to the GitHub repository (required to manage webhooks)

---

## Step 1 — Enable the webhook platform

Add the following to your `~/.hermes/config.yaml`:

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      port: 8644          # default; change if another service occupies this port
      rate_limit: 30      # new identities per profile/route per sliding 60 seconds

      routes:
        github-pr-review:
          provider: github
          signature_mode: github
          secret: "your-webhook-secret-here"   # must match the GitHub webhook secret exactly
          events:
            - pull_request
          toolsets: ["terminal"]                 # explicit grant required for gh

          # The agent is instructed to fetch the actual diff before reviewing.
          # {number} and {repository.full_name} are resolved from the GitHub payload.
          prompt: |
            A pull request event was received (action: {action}).

            PR #{number}: {pull_request.title}
            Author: {pull_request.user.login}
            Branch: {pull_request.head.ref} → {pull_request.base.ref}
            Description: {pull_request.body}
            URL: {pull_request.html_url}

            If the action is "closed" or "labeled", respond with [SILENT].

            Otherwise:
            1. Run: gh pr diff {number} --repo {repository.full_name}
            2. Review the code changes for correctness, security issues, and clarity.
            3. Return a concise, actionable review comment. Do not post it yourself;
               the configured github_comment target posts the final response.

          deliver: github_comment
          deliver_extra:
            repo: "{repository.full_name}"
            pr_number: "{number}"
```

**Key fields:**

| Field | Description |
|---|---|
| `provider` | Binds this route to the GitHub verifier; request headers cannot select a provider. |
| `signature_mode` | Explicitly binds GitHub's `X-Hub-Signature-256` verifier. |
| `secret` (route-level) | HMAC secret for this route. A global `extra.secret` fallback is valid only when exactly one authenticated route uses it; otherwise every route needs unique key material. |
| `events` | At most one authenticated GitHub event class. Supported values are `check_run`, `pull_request`, `push`, `issues`, and `ping`; the header and signed body shape must both match. Empty list accepts without event dispatch authority (`event=unknown`). |
| `toolsets` | Replaces the constrained webhook defaults for this route. `terminal` is required for `gh pr diff`; remove it if you change the prompt to avoid shell access. |
| `prompt` | Template; `{field}` and `{nested.field}` resolve from the GitHub payload. |
| `deliver` | `github_comment` posts via `gh pr comment`. `log` just writes to the gateway log. |
| `deliver_extra.repo` | Resolves to e.g. `org/repo` from the payload. |
| `deliver_extra.pr_number` | Resolves to the PR number from the payload. |

:::note The payload does not contain code
The GitHub webhook payload includes PR metadata (title, description, branch names, URLs) but **not the diff**. The prompt above instructs the agent to run `gh pr diff` to fetch the actual changes. The default `hermes-webhook` toolset is deliberately constrained (web search/extract, vision, clarify — **no terminal**) because webhook payloads can carry untrusted content. This route therefore grants exactly `toolsets: ["terminal"]`; that replaces the platform default for this route. See [Per-route toolsets](/docs/user-guide/messaging/webhooks#per-route-toolsets).
:::

---

## Step 2 — Start the gateway

```bash
hermes gateway
```

You should see:

```
[webhook] Listening on * (all interfaces, IPv4+IPv6):8644 — routes: github-pr-review
```

Verify it's running:

```bash
curl http://localhost:8644/health
# {"status":"ok","platform":"webhook","accepting_webhooks":true}
```

---

## Step 3 — Register the webhook on GitHub

1. Go to your repository → **Settings** → **Webhooks** → **Add webhook**
2. Fill in:
   - **Payload URL:** `https://your-public-url.example.com/webhooks/github-pr-review`
   - **Content type:** `application/json`
   - **Secret:** the same value you set for `secret` in the route config
   - **Which events?** → Select individual events → check **Pull requests**
3. Click **Add webhook**

GitHub will immediately send a signed `ping` event to confirm the connection. Because this route binds exactly `pull_request`, that delivery is rejected with HTTP `401` (`Invalid authenticated webhook metadata`). Both the `X-GitHub-Event` value and the HMAC-covered JSON body shape must match, so relabeling the ping header cannot turn it into a pull-request event. This is expected; a later signed `pull_request` delivery is the functional test.

---

## Step 4 — Open a test PR

Create a branch, push a change, and open a PR. Within 30–90 seconds (depending on PR size and model), Hermes should post a review comment.

To follow the agent's progress in real time:

```bash
tail -f "${HERMES_HOME:-$HOME/.hermes}/logs/gateway.log"
```

---

## Local testing with ngrok

If Hermes is running on your laptop, use [ngrok](https://ngrok.com/) to expose it:

```bash
ngrok http 8644
```

Copy the `https://...ngrok-free.app` URL and use it as your GitHub Payload URL. On the free ngrok tier the URL changes each time ngrok restarts — update your GitHub webhook each session. Paid ngrok accounts get a static domain.

You can smoke-test a static route directly with `curl` — no GitHub account or real PR needed. Add a separate log-only route with its own secret so testing does not mutate the key-bound policy of the production route:

```yaml
# Inside platforms.webhook.extra.routes:
github-pr-review-smoke:
  provider: github
  signature_mode: github
  events: [pull_request]
  secret: "a-distinct-smoke-test-secret"
  prompt: |
    Summarize this test PR payload:
    PR #{number}: {pull_request.title} in {repository.full_name}
  deliver: log
```

:::tip Why use a second route?
The secret is permanently bound to its route name, profile, verifier, prompt, tools, and delivery policy. Reusing the production secret after changing `deliver` from `github_comment` to `log` is rejected; a separate route keeps both authorities unambiguous.
:::

```bash
SECRET="a-distinct-smoke-test-secret"
BODY='{"action":"opened","number":99,"pull_request":{"id":701,"number":99,"state":"open","title":"Test PR","body":"Adds a feature.","user":{"login":"testuser"},"head":{"ref":"feat/x"},"base":{"ref":"main"},"html_url":"https://github.com/org/repo/pull/99"},"repository":{"id":801,"full_name":"org/repo"},"sender":{"id":901,"login":"testuser"}}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk '{print "sha256="$2}')

curl -s -X POST http://localhost:8644/webhooks/github-pr-review-smoke \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: $SIG" \
  -d "$BODY"
# HTTP 202: {"status":"accepted","route":"github-pr-review-smoke","event":"pull_request","delivery_id":"...","deduplication":"authenticated_body_sha256"}
```

Then watch the agent run:
```bash
tail -f "${HERMES_HOME:-$HOME/.hermes}/logs/gateway.log"
```

:::note
`hermes webhook test <name>` only works for **dynamic subscriptions** created with `hermes webhook subscribe`. It does not read routes from `config.yaml`.
:::

---

## Filtering to specific actions

GitHub sends `pull_request` events for many actions: `opened`, `synchronize`, `reopened`, `closed`, `labeled`, etc. The route binds both the `X-GitHub-Event` value and authenticated pull-request body class; route-level `filters` can then narrow by payload fields such as `action`.

The prompt in Step 1 already handles this by returning `[SILENT]` for `closed` and `labeled` events.

:::warning The agent still runs and consumes tokens
The `[SILENT]` instruction suppresses final delivery, but the agent still runs for every `pull_request` event regardless of action. Prefer filtering before the agent wakes. Because filters are part of the key-bound execution policy, set a **fresh route secret** and update the GitHub webhook secret at the same time; reusing the original secret with this edit is rejected.

```yaml
secret: "fresh-secret-for-filtered-policy"
filters:
  - field: "action"
    in: ["opened", "synchronize", "reopened"]
```

For high-volume repositories, you can still filter upstream with a GitHub Actions workflow that calls your webhook URL conditionally.
:::

> There is no Jinja2 or conditional template syntax. `{field}` and `{nested.field}` are the only substitutions supported. Anything else is passed verbatim to the agent.

---

## Using a skill for consistent review style

Load a [Hermes skill](/user-guide/features/skills) to give the agent a consistent review persona. A skill changes the key-bound execution policy, so use a fresh secret in both this route and the GitHub webhook rather than adding it under the old secret:

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      routes:
        github-pr-review:
          provider: github
          signature_mode: github
          secret: "fresh-secret-for-skilled-policy"
          events: [pull_request]
          toolsets: ["terminal"]
          prompt: |
            A pull request event was received (action: {action}).
            PR #{number}: {pull_request.title} by {pull_request.user.login}
            URL: {pull_request.html_url}

            If the action is "closed" or "labeled", respond with [SILENT].

            Otherwise:
            1. Run: gh pr diff {number} --repo {repository.full_name}
            2. Review the diff using your review guidelines.
            3. Return a concise, actionable review comment. Do not post it yourself;
               the configured github_comment target posts the final response.
          skills:
            - review
          deliver: github_comment
          deliver_extra:
            repo: "{repository.full_name}"
            pr_number: "{number}"
```

> **Note:** Only the first skill in the list that is found is loaded. Hermes does not stack multiple skills — subsequent entries are ignored.

---

## Sending responses to Slack or Discord instead

Delivery kind and target fields are part of the key-bound execution policy. To switch an existing route, rotate to a fresh secret in both the route and GitHub, then replace `deliver` and `deliver_extra` with one of these targets:

Slack:

```yaml
# Inside platforms.webhook.extra.routes.<route-name>:
secret: "fresh-secret-for-this-slack-policy"
deliver: slack
deliver_extra:
  chat_id: "C0123456789"   # Slack channel ID (omit to use the configured home channel)
  scope_id: "T0123456789"  # Slack workspace/team ID for this channel
```

Discord:

```yaml
# Inside platforms.webhook.extra.routes.<route-name>:
secret: "fresh-secret-for-this-discord-policy"
deliver: discord
deliver_extra:
  chat_id: "987654321012345678"  # Discord channel ID (omit to use home channel)
```

The target platform must also be enabled and connected in the gateway. If `chat_id` is omitted, the response is sent to that platform's configured home channel. A templated Slack `chat_id` requires an explicit route-bound `scope_id`; for a static channel, the adapter may establish that scope from the connected workspace, but specifying it removes ambiguity.

Valid `deliver` values: `log` · `github_comment` · `telegram` · `discord` · `slack` · `signal` · `sms`

---

## GitLab support

The same adapter works with GitLab, but the route must explicitly bind the GitLab verifier. Request headers never select or auto-detect a provider. GitLab uses `X-Gitlab-Token` for authentication (plain string match, not HMAC), so give it a separate route and secret from GitHub.

For event filtering, GitLab sets `X-GitLab-Event` to values like `Merge Request Hook`, `Push Hook`, and `Pipeline Hook`. A GitLab route may bind at most one of these unsigned header values; use the exact value:

```yaml
# Inside platforms.webhook.extra.routes:
gitlab-mr-review:
  provider: gitlab
  signature_mode: gitlab
  secret: "a-distinct-gitlab-token"
  events:
    - Merge Request Hook
  prompt: |
    Review GitLab MR !{object_attributes.iid}: {object_attributes.title}
    Return a concise review based on the authenticated payload metadata.
  deliver: log
```

GitLab payload fields differ from GitHub's — e.g. `{object_attributes.title}` for the MR title and `{object_attributes.iid}` for the MR number. The easiest way to discover the full payload structure is GitLab's **Test** button in your webhook settings, combined with the **Recent Deliveries** log. Alternatively, omit `prompt` from your route config — Hermes then passes a parseable raw-payload envelope bounded to 4,000 UTF-8 bytes (with an explicit `truncated` marker) to the agent.

---

## Security notes

- **Never use `INSECURE_NO_AUTH`** in production — it disables signature validation entirely. It is only for local development.
- **Rotate your webhook secret** in both GitHub and `config.yaml` whenever the route name, profile, provider, signature mode, or execution policy changes. Key material is permanently bound across profiles to its original route policy and cannot be reassigned.
- **Rate limiting** admits 30 new identities per profile/route in a sliding 60-second window by default (configurable via `extra.rate_limit`). Durable duplicates and already-active identities do not consume another slot; a new identity over quota returns `429`.
- **Duplicate deliveries** are fenced by a durable replay proof derived from the authenticated request body. GitHub's body signature does not authenticate `X-GitHub-Delivery` or `X-Request-ID`, so those observed headers are diagnostic only and never control replay identity. The proof survives process restarts; it is not a one-hour memory cache.
- **Prompt injection and tool authority:** PR titles, descriptions, and commit messages are attacker-controlled. `toolsets: ["terminal"]` grants trusted local-code capability; Hermes does not add an OS sandbox. Isolate the configured terminal backend with Docker, a VM, or SSH policy. That isolation does not cover `github_comment`: its final `gh pr comment` command runs on the gateway host under the adapter's fixed target authority.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| `401 Invalid signature` | Secret in config.yaml doesn't match GitHub webhook secret |
| `404 Unknown route` | Route name in the URL doesn't match the key in `routes:` |
| `429 Rate limit exceeded` | The sliding 60-second quota for new identities was exceeded; wait until the oldest admitted identity leaves the window or raise `extra.rate_limit` |
| Agent cannot fetch the diff | Check `gh` installation and authentication in the configured terminal backend |
| Review succeeds but no comment is posted | Check `gh` installation and authentication on the gateway host; `github_comment` delivery runs there |
| Agent runs but no comment | An intentional autonomous silence response (`[SILENT]`, including that marker followed by explanatory prose) suppresses target delivery by design. If silence was unexpected, inspect the prompt and completed agent output in the gateway log. |
| Port already in use | Change `extra.port` in config.yaml |
| Agent runs but reviews only the PR description | The prompt isn't including the `gh pr diff` instruction — the diff is not in the webhook payload |
| GitHub marks the initial ping failed | Expected for an `events: [pull_request]` route: its header and authenticated body classify as `ping`, not `pull_request`, so the route returns `401`. Confirm a subsequent signed pull-request delivery instead. |

**GitHub's Recent Deliveries tab** (repo → Settings → Webhooks → your webhook) shows the exact request headers, payload, HTTP status, and response body for every delivery. It is the fastest way to diagnose failures without touching your server logs.

---

## Full config reference

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      host: null                # default: all interfaces on IPv4 and IPv6
      port: 8644               # listen port (default: 8644)
      secret: ""               # fallback only when exactly one authenticated route uses it
      rate_limit: 30           # new identities per profile/route per sliding 60 seconds
      max_body_bytes: 1048576  # default and hard maximum: 1 MiB

      routes:
        <route-name>:
          provider: github       # explicit provider/verifier binding
          signature_mode: github
          secret: "required-per-route"
          events: []            # [] = unfiltered/unknown; otherwise one supported event
          prompt: ""            # {field} / {nested.field} resolved from payload
          skills: []            # first matching skill is loaded (only one)
          toolsets: []          # explicit per-route replacement; terminal is not a default
          deliver: "log"        # log | github_comment | telegram | discord | slack | signal | sms
          deliver_extra: {}     # repo + pr_number for github_comment; chat_id for others
```

---

## What's Next?

- **[Cron-Based PR Reviews](./github-pr-review-agent.md)** — poll for PRs on a schedule, no public endpoint needed
- **[Webhook Reference](/user-guide/messaging/webhooks)** — full config reference for the webhook platform
- **[Build a Plugin](/developer-guide/plugins)** — package review logic into a shareable plugin
- **[Profiles](/user-guide/profiles)** — run a dedicated reviewer profile with its own memory and config
