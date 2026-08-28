---
sidebar_position: 15
title: "Automation Blueprints"
description: "Ready-to-use automation blueprints — scheduled tasks, GitHub event triggers, API webhooks, and multi-skill workflows"
---

# Automation Blueprints

Copy-paste blueprints for common automation patterns. Each blueprint uses Hermes's built-in [cron scheduler](/user-guide/features/cron) for time-based triggers and [webhook platform](/user-guide/messaging/webhooks) for event-driven triggers.

Every blueprint works with **any model** — not locked to a single provider.

For parameterized blueprints with forms instead of cron syntax, see the [Automation Blueprints Catalog](/reference/automation-blueprints-catalog).

:::tip Three Trigger Types
| Trigger | How | Tool |
|---------|-----|------|
| **Schedule** | Runs on a cadence (hourly, nightly, weekly) | `cronjob` tool or `/cron` slash command |
| **GitHub Event** | Fires on PR opens, pushes, issues, CI results | Webhook platform (`hermes webhook subscribe`) |
| **API Call** | External service POSTs JSON to your endpoint | Webhook platform (config.yaml routes or `hermes webhook subscribe`) |

Webhook routes can deliver to the gateway log, Telegram, Discord, Slack, Signal, SMS, or a fully bound GitHub PR comment target. Scheduled tasks have their own configured delivery choices.
:::

---

## Development Workflow

### Nightly Backlog Triage

Label, prioritize, and summarize new issues every night. Delivers a digest to your team channel.

**Trigger:** Schedule (nightly)

```bash
hermes cron create "0 2 * * *" \
  "You are a project manager triaging the NousResearch/hermes-agent GitHub repo.

1. Run: gh issue list --repo NousResearch/hermes-agent --state open --json number,title,labels,author,createdAt --limit 30
2. Identify issues opened in the last 24 hours
3. For each new issue:
   - Suggest a priority label (P0-critical, P1-high, P2-medium, P3-low)
   - Suggest a category label (bug, feature, docs, security)
   - Write a one-line triage note
4. Summarize: total open issues, new today, breakdown by priority

Format as a clean digest. If no new issues, respond with [SILENT]." \
  --name "Nightly backlog triage" \
  --deliver telegram
```

### Automatic PR Code Review

Review every pull request automatically when it's opened. The static route posts the final review directly on the PR; the constrained CLI alternative logs a metadata-only review checklist.

**Trigger:** GitHub webhook

**Option A — Dynamic subscription (CLI, metadata-only review to the log):**

```bash
hermes webhook subscribe github-pr-review \
  --provider github \
  --signature-mode github \
  --events "pull_request" \
  --prompt "Review this pull request:
Repository: {repository.full_name}
PR #{pull_request.number}: {pull_request.title}
Author: {pull_request.user.login}
Action: {action}
Diff URL: {pull_request.diff_url}

Based only on the signed webhook metadata, identify the likely review focus and
return a concise checklist for a human reviewer. Do not claim to have inspected
the diff or post anything to GitHub." \
  --skills github-code-review \
  --deliver log
```

The dynamic CLI does not expose per-route `toolsets` or the `repo` and `pr_number` authority required by `github_comment`. Use the static route below when the agent must fetch the diff and post the result to GitHub.

**Option B — Static route (config.yaml):**

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      port: 8644
      routes:
        github-pr-review:
          provider: github
          signature_mode: github
          events: ["pull_request"]
          secret: "github-webhook-secret"
          filters:
            - field: "action"
              in: ["opened", "synchronize", "reopened"]
          toolsets: ["terminal"]
          prompt: |
            Review PR #{pull_request.number}: {pull_request.title}
            Repository: {repository.full_name}
            Author: {pull_request.user.login}
            Fetch the diff with:
            gh pr diff {pull_request.number} --repo {repository.full_name}
            Review for security, performance, and code quality.
            Return only the review text; do not post it yourself.
          skills: ["github-code-review"]
          deliver: "github_comment"
          deliver_extra:
            repo: "{repository.full_name}"
            pr_number: "{pull_request.number}"
```

Then in GitHub: **Settings → Webhooks → Add webhook** → Payload URL: `http://your-server:8644/webhooks/github-pr-review`, Content type: `application/json`, Secret: `github-webhook-secret`, Events: **Pull requests**.

The agent's `gh pr diff` command runs in the configured terminal backend, while the adapter's final `gh pr comment` delivery runs on the gateway host. Install and authenticate `gh` in both places when they are different machines or containers.

### Docs Drift Detection

Weekly scan of merged PRs to find API changes that need documentation updates.

**Trigger:** Schedule (weekly)

```bash
hermes cron create "0 9 * * 1" \
  "Scan the NousResearch/hermes-agent repo for documentation drift.

1. Run: gh pr list --repo NousResearch/hermes-agent --state merged --json number,title,files,mergedAt --limit 30
2. Filter to PRs merged in the last 7 days
3. For each merged PR, check if it modified:
   - Tool schemas (tools/*.py) — may need docs/reference/tools-reference.md update
   - CLI commands (hermes_cli/commands.py, hermes_cli/main.py) — may need docs/reference/cli-commands.md update
   - Config options (hermes_cli/config.py) — may need docs/user-guide/configuration.md update
   - Environment variables — may need docs/reference/environment-variables.md update
4. Cross-reference: for each code change, check if the corresponding docs page was also updated in the same PR

Report any gaps where code changed but docs didn't. If everything is in sync, respond with [SILENT]." \
  --name "Docs drift detection" \
  --deliver telegram
```

### Dependency Security Audit

Daily scan for known vulnerabilities in project dependencies.

**Trigger:** Schedule (daily)

```bash
hermes cron create "0 6 * * *" \
  "Run a dependency security audit on the hermes-agent project.

1. cd ~/.hermes/hermes-agent && source .venv/bin/activate
2. Run: pip audit --format json 2>/dev/null || pip audit 2>&1
3. Run: npm audit --json 2>/dev/null (in website/ directory if it exists)
4. Check for any CVEs with CVSS score >= 7.0

If vulnerabilities found:
- List each one with package name, version, CVE ID, severity
- Check if an upgrade is available
- Note if it's a direct dependency or transitive

If no vulnerabilities, respond with [SILENT]." \
  --name "Dependency audit" \
  --deliver telegram
```

---

## DevOps & Monitoring

### Deploy Verification

Trigger smoke tests after every deployment. Your CI/CD pipeline POSTs to the webhook when a deploy completes.

**Trigger:** API call (webhook)

```bash
DEPLOY_WEBHOOK_SECRET="replace-with-a-random-secret"
hermes webhook subscribe deploy-verify \
  --provider generic \
  --signature-mode generic_v2 \
  --events "deployment" \
  --secret "$DEPLOY_WEBHOOK_SECRET" \
  --prompt "A deployment just completed:
Service: {service}
Environment: {environment}
Version: {version}
Deployed by: {deployer}

Run these verification steps:
1. Use web extraction to check whether {health_url} responds successfully
2. Inspect the deployment payload's error summary: {errors}
3. Use web extraction to verify that {version_url} reports version {version}

Report: deployment status (healthy/degraded/failed), observed versions, and any errors found.
If healthy, keep it brief. If degraded or failed, provide detailed diagnostics." \
  --deliver telegram
```

Your CI/CD pipeline triggers it:

```bash
BODY='{"event_type":"deployment","service":"api","environment":"prod","version":"2.1.0","deployer":"ci","health_url":"https://api.example.com/health","version_url":"https://api.example.com/version","errors":[]}'
TIMESTAMP=$(date +%s)
SIG=$(printf '%s.%s' "$TIMESTAMP" "$BODY" | openssl dgst -sha256 -hmac "$DEPLOY_WEBHOOK_SECRET" -hex | awk '{print $2}')

curl -X POST http://your-server:8644/webhooks/deploy-verify \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Timestamp: $TIMESTAMP" \
  -H "X-Webhook-Signature-V2: $SIG" \
  -d "$BODY"
```

### Alert Triage

Correlate monitoring alerts with recent changes to draft a response. This generic recipe works with any alerting system, or a trusted relay in front of it, that can send Hermes Generic V2 signatures.

**Trigger:** API call (webhook)

```bash
ALERT_WEBHOOK_SECRET="replace-with-a-random-secret"
hermes webhook subscribe alert-triage \
  --provider generic \
  --signature-mode generic_v2 \
  --events "alert" \
  --secret "$ALERT_WEBHOOK_SECRET" \
  --prompt "Monitoring alert received:
Alert: {alert.name}
Severity: {alert.severity}
Service: {alert.service}
Message: {alert.message}
Timestamp: {alert.timestamp}

Investigate:
1. Search the web for known issues with this error pattern
2. Check if this correlates with any recent deployments or config changes
3. Draft a triage summary with:
   - Likely root cause
   - Suggested first response steps
   - Escalation recommendation (P1-P4)

Be concise. This goes to the on-call channel." \
  --deliver slack
```

Send a top-level `"event_type": "alert"` in the signed JSON body and calculate `X-Webhook-Timestamp` and `X-Webhook-Signature-V2` exactly as in the deploy example above. Provider-specific headers never select a verifier automatically; use a provider-specific route instead when the sender has a supported native contract.

### Uptime Monitor

Check endpoints every 30 minutes. Only notify when something is down.

**Trigger:** Schedule (every 30 min)

```python title="~/.hermes/scripts/check-uptime.py"
import urllib.request, json, time

ENDPOINTS = [
    {"name": "API", "url": "https://api.example.com/health"},
    {"name": "Web", "url": "https://www.example.com"},
    {"name": "Docs", "url": "https://docs.example.com"},
]

results = []
for ep in ENDPOINTS:
    try:
        start = time.time()
        req = urllib.request.Request(ep["url"], headers={"User-Agent": "Hermes-Monitor/1.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        elapsed = round((time.time() - start) * 1000)
        results.append({"name": ep["name"], "status": resp.getcode(), "ms": elapsed})
    except Exception as e:
        results.append({"name": ep["name"], "status": "DOWN", "error": str(e)})

down = [r for r in results if r.get("status") == "DOWN" or (isinstance(r.get("status"), int) and r["status"] >= 500)]
if down:
    print("OUTAGE DETECTED")
    for r in down:
        print(f"  {r['name']}: {r.get('error', f'HTTP {r[\"status\"]}')} ")
    print(f"\nAll results: {json.dumps(results, indent=2)}")
else:
    print("NO_ISSUES")
```

```bash
hermes cron create "every 30m" \
  "If the script reports OUTAGE DETECTED, summarize which services are down and suggest likely causes. If NO_ISSUES, respond with [SILENT]." \
  --script ~/.hermes/scripts/check-uptime.py \
  --name "Uptime monitor" \
  --deliver telegram
```

---

## Research & Intelligence

### Competitive Repository Scout

Monitor competitor repos for interesting PRs, features, and architectural decisions.

**Trigger:** Schedule (daily)

```bash
hermes cron create "0 8 * * *" \
  "Scout these AI agent repositories for notable activity in the last 24 hours:

Repos to check:
- anthropics/claude-code
- openai/codex
- All-Hands-AI/OpenHands
- Aider-AI/aider

For each repo:
1. gh pr list --repo <repo> --state all --json number,title,author,createdAt,mergedAt --limit 15
2. gh issue list --repo <repo> --state open --json number,title,labels,createdAt --limit 10

Focus on:
- New features being developed
- Architectural changes
- Integration patterns we could learn from
- Security fixes that might affect us too

Skip routine dependency bumps and CI fixes. If nothing notable, respond with [SILENT].
If there are findings, organize by repo with brief analysis of each item." \
  --skill competitive-pr-scout \
  --name "Competitor scout" \
  --deliver telegram
```

### AI News Digest

Weekly roundup of AI/ML developments.

**Trigger:** Schedule (weekly)

```bash
hermes cron create "0 9 * * 1" \
  "Generate a weekly AI news digest covering the past 7 days:

1. Search the web for major AI announcements, model releases, and research breakthroughs
2. Search for trending ML repositories on GitHub
3. Check arXiv for highly-cited papers on language models and agents

Structure:
## Headlines (3-5 major stories)
## Notable Papers (2-3 papers with one-sentence summaries)
## Open Source (interesting new repos or major releases)
## Industry Moves (funding, acquisitions, launches)

Keep each item to 1-2 sentences. Include links. Total under 600 words." \
  --name "Weekly AI digest" \
  --deliver telegram
```

### Paper Digest with Notes

Daily arXiv scan that saves summaries to your note-taking system.

**Trigger:** Schedule (daily)

```bash
hermes cron create "0 8 * * *" \
  "Search arXiv for the 3 most interesting papers on 'language model reasoning' OR 'tool-use agents' from the past day. For each paper, create an Obsidian note with the title, authors, abstract summary, key contribution, and potential relevance to Hermes Agent development." \
  --skill arxiv --skill obsidian \
  --name "Paper digest" \
  --deliver local
```

---

## GitHub Event Automations

### Issue Triage Drafts

Draft labels and an initial response for new issues, then log them for review.

**Trigger:** GitHub webhook

```bash
hermes webhook subscribe github-issues \
  --provider github \
  --signature-mode github \
  --events "issues" \
  --prompt "New GitHub issue received:
Repository: {repository.full_name}
Issue #{issue.number}: {issue.title}
Author: {issue.user.login}
Action: {action}
Body: {issue.body}
Labels: {issue.labels}

If this is a new issue (action=opened):
1. Read the issue title and body carefully
2. Suggest appropriate labels (bug, feature, docs, security, question)
3. If it's a bug report, check if you can identify the affected component from the description
4. Draft a helpful initial response acknowledging the issue

If this is a label or assignment change, respond with [SILENT]." \
  --deliver log
```

This dynamic recipe logs label suggestions and a response draft. The CLI cannot bind the `repo` and `pr_number` fields required by `github_comment`; use a static route with an explicit `deliver_extra` target if the result must be posted to GitHub.

### CI Failure Analysis

Analyze CI failures and log diagnostics. A check run can reference zero or multiple pull requests, so this recipe does not assume one fixed GitHub comment target.

**Trigger:** GitHub webhook

```yaml
# config.yaml route
platforms:
  webhook:
    enabled: true
    extra:
      routes:
        ci-failure:
          provider: github
          signature_mode: github
          events: ["check_run"]
          secret: "ci-secret"
          toolsets: ["web"]
          prompt: |
            CI check failed:
            Repository: {repository.full_name}
            Check: {check_run.name}
            Status: {check_run.conclusion}
            Associated PRs: {check_run.pull_requests}
            Details URL: {check_run.details_url}

            If conclusion is "failure":
            1. Use web extraction to inspect the details URL if it is publicly accessible
            2. Identify the likely cause of failure
            3. Suggest a fix
            If conclusion is "success", respond with [SILENT].
          deliver: "log"
```

### Plan Cross-Repo Ports

When a PR merges in one repo, inspect its diff and prepare a concrete porting plan for another repository.

**Trigger:** GitHub webhook

```yaml
# config.yaml route
platforms:
  webhook:
    enabled: true
    extra:
      routes:
        auto-port-plan:
          provider: github
          signature_mode: github
          events: ["pull_request"]
          secret: "auto-port-secret"
          filters:
            - field: "action"
              equals: "closed"
            - field: "pull_request.merged"
              equals: true
          toolsets: ["terminal"]
          prompt: |
            A pull request merged in {repository.full_name}:
            PR #{pull_request.number}: {pull_request.title}
            Merge commit: {pull_request.merge_commit_sha}

            Run:
            gh pr diff {pull_request.number} --repo {repository.full_name}

            Analyze the diff and prepare a file-by-file porting plan for org/go-sdk.
            Include a proposed PR title and body that reference the original PR.
            Do not clone, edit, push, or open a PR; return the plan only.
          skills: ["github-pr-workflow"]
          deliver: log
```

The `terminal` grant is explicit because dynamic subscriptions cannot add per-route toolsets. The `gh pr diff` command runs in the configured terminal backend, where `gh` must be installed and authenticated.

---

## Business Operations

### Stripe Payment Monitoring

Track payment events and get summaries of failures.

**Trigger:** API call (webhook)

```bash
STRIPE_WEBHOOK_SECRET="whsec_replace_with_your_endpoint_secret"
hermes webhook subscribe stripe-payments \
  --provider stripe \
  --signature-mode stripe \
  --events "payment_intent.succeeded,payment_intent.payment_failed,charge.dispute.created" \
  --secret "$STRIPE_WEBHOOK_SECRET" \
  --prompt "Stripe event received:
Event type: {type}
Amount: {data.object.amount} cents ({data.object.currency})
Customer: {data.object.customer}
Status: {data.object.status}

For payment_intent.payment_failed:
- Identify the failure reason from {data.object.last_payment_error}
- Suggest whether this is a transient issue (retry) or permanent (contact customer)

For charge.dispute.created:
- Flag as urgent
- Summarize the dispute details

For payment_intent.succeeded:
- Brief confirmation only

Keep responses concise for the ops channel." \
  --deliver slack
```

### Daily Revenue Summary

Compile key business metrics every morning.

**Trigger:** Schedule (daily)

```bash
hermes cron create "0 8 * * *" \
  "Generate a morning business metrics summary.

Search the web for:
1. Current Bitcoin and Ethereum prices
2. S&P 500 status (pre-market or previous close)
3. Any major tech/AI industry news from the last 12 hours

Format as a brief morning briefing, 3-4 bullet points max.
Deliver as a clean, scannable message." \
  --name "Morning briefing" \
  --deliver telegram
```

---

## Multi-Skill Workflows

### Security Audit Pipeline

Combine multiple skills for a comprehensive weekly security review.

**Trigger:** Schedule (weekly)

```bash
hermes cron create "0 3 * * 0" \
  "Run a comprehensive security audit of the hermes-agent codebase.

1. Check for dependency vulnerabilities (pip audit, npm audit)
2. Search the codebase for common security anti-patterns:
   - Hardcoded secrets or API keys
   - SQL injection vectors (string formatting in queries)
   - Path traversal risks (user input in file paths without validation)
   - Unsafe deserialization (pickle.loads, yaml.load without SafeLoader)
3. Review recent commits (last 7 days) for security-relevant changes
4. Check if any new environment variables were added without being documented

Write a security report with findings categorized by severity (Critical, High, Medium, Low).
If nothing found, report a clean bill of health." \
  --skill codebase-security-audit \
  --name "Weekly security audit" \
  --deliver telegram
```

### Content Pipeline

Research, draft, and prepare content on a schedule.

**Trigger:** Schedule (weekly)

```bash
hermes cron create "0 10 * * 3" \
  "Research and draft a technical blog post outline about a trending topic in AI agents.

1. Search the web for the most discussed AI agent topics this week
2. Pick the most interesting one that's relevant to open-source AI agents
3. Create an outline with:
   - Hook/intro angle
   - 3-4 key sections
   - Technical depth appropriate for developers
   - Conclusion with actionable takeaway
4. Save the outline to ~/drafts/blog-$(date +%Y%m%d).md

Keep the outline to ~300 words. This is a starting point, not a finished post." \
  --name "Blog outline" \
  --deliver local
```

---

## Quick Reference

### Cron Schedule Syntax

| Expression | Meaning |
|-----------|---------|
| `every 30m` | Every 30 minutes |
| `every 2h` | Every 2 hours |
| `0 2 * * *` | Daily at 2:00 AM |
| `0 9 * * 1` | Every Monday at 9:00 AM |
| `0 9 * * 1-5` | Weekdays at 9:00 AM |
| `0 3 * * 0` | Every Sunday at 3:00 AM |
| `0 */6 * * *` | Every 6 hours |

### Delivery Targets

| Target | Flag | Notes |
|--------|------|-------|
| Same chat | `--deliver origin` | Default — delivers to where the job was created |
| Local file | `--deliver local` | Saves output, no notification |
| Telegram | `--deliver telegram` | Home channel, or `telegram:CHAT_ID` for specific |
| Discord | `--deliver discord` | Home channel, or `discord:CHANNEL_ID` |
| Slack | `--deliver slack` | Home channel |
| SMS | `--deliver sms:+15551234567` | Direct to phone number |
| Specific thread | `--deliver telegram:-100123:456` | Telegram forum topic |

### Webhook Template Variables

| Variable | Description |
|----------|-------------|
| `{pull_request.title}` | PR title |
| `{issue.number}` | Issue number |
| `{repository.full_name}` | `owner/repo` |
| `{action}` | Event action (opened, closed, etc.) |
| `{__raw__}` | Parseable raw-payload envelope bounded to 4,000 UTF-8 bytes, with explicit truncation metadata |
| `{sender.login}` | GitHub user who triggered the event |

### The [SILENT] Pattern

When a cron job's response contains `[SILENT]`, delivery is suppressed. Use this to avoid notification spam on quiet runs:

```
If nothing noteworthy happened, respond with [SILENT].
```

This means you only get notified when the agent has something to report.
