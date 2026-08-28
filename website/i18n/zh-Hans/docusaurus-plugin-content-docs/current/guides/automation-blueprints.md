---
sidebar_position: 15
title: "自动化蓝图"
description: "开箱即用的自动化蓝图——定时任务、GitHub 事件触发、API webhook 及多技能工作流"
---

# 自动化蓝图

常见自动化模式的复制粘贴蓝图。每个蓝图使用 Hermes 内置的 [cron 调度器](/user-guide/features/cron) 实现基于时间的触发，使用 [webhook 平台](/user-guide/messaging/webhooks) 实现事件驱动触发。

所有蓝图适用于**任意模型**——不绑定单一提供商。

如需带表单的参数化蓝图（无需手写 cron 语法），请参阅[自动化蓝图目录](/reference/automation-blueprints-catalog)。

:::tip 三种触发类型
| 触发方式 | 方式 | 工具 |
|---------|-----|------|
| **定时** | 按周期运行（每小时、每晚、每周） | `cronjob` 工具或 `/cron` 斜杠命令 |
| **GitHub 事件** | PR 开启、推送、issue、CI 结果时触发 | Webhook 平台（`hermes webhook subscribe`） |
| **API 调用** | 外部服务向你的端点 POST JSON | Webhook 平台（config.yaml 路由或 `hermes webhook subscribe`） |

三种方式均支持投递到 Telegram、Discord、Slack、SMS、邮件、GitHub 评论或本地文件。
:::

---

## 开发工作流

### 每晚待办事项分类

每晚自动对新 issue 进行标签分类、优先级排序和摘要汇总，并将摘要投递到团队频道。

**触发方式：** 定时（每晚）

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

### 自动 PR 代码审查

静态路由会在 PR 开启或更新时获取 diff，并把最终审查直接发布到 PR；受限的 CLI 备选方案只把基于元数据的审查清单写入日志。

**触发方式：** GitHub webhook

**方式 A——动态订阅（CLI，仅元数据分类）：**

动态 CLI 有意不能授予 `terminal` toolset，也不能设置 `github_comment` 所需的 `deliver_extra.repo` / `pr_number`。因此这个可直接运行的版本只根据已认证的 PR 元数据起草清单并写入日志。需要获取 diff 并评论 PR 时，请使用下方的完整静态路由。

```bash
hermes webhook subscribe github-pr-review \
  --provider github \
  --signature-mode github \
  --events "pull_request" \
  --prompt "Triage this pull request from its metadata:
Repository: {repository.full_name}
PR #{pull_request.number}: {pull_request.title}
Author: {pull_request.user.login}
Action: {action}
Description: {pull_request.body}

Summarize the likely review areas and draft questions for a human reviewer. Do not claim to have inspected the diff." \
  --skills github-code-review \
  --deliver log
```

**方式 B——静态路由（config.yaml）：**

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      port: 8644
      secret: "your-global-secret"
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
            Run: gh pr diff {pull_request.number} --repo {repository.full_name}
            Review the diff for security, performance, and code quality.
            Return only the concise review text. Do not post it yourself;
            the github_comment delivery target below posts the final response.
          skills: ["github-code-review"]
          deliver: "github_comment"
          deliver_extra:
            repo: "{repository.full_name}"
            pr_number: "{pull_request.number}"
```

这里的 `terminal` 命令在配置的 terminal 后端中运行；`github_comment` 投递使用的 `gh` 则在 gateway 主机上运行。因此，若 terminal 后端是 Docker 或 SSH，两处都必须具备各自所需的 `gh`/认证。该路由的 secret 会绑定 provider、toolset、prompt、skill 和投递目标；以后修改任一字段时必须换用全新 secret，并同步更新 GitHub webhook。

然后在 GitHub 中：**Settings → Webhooks → Add webhook** → Payload URL：`http://your-server:8644/webhooks/github-pr-review`，Content type：`application/json`，Secret：`github-webhook-secret`，Events：**Pull requests**。

### 文档偏差检测

每周扫描已合并的 PR，找出需要更新文档的 API 变更。

**触发方式：** 定时（每周）

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

### 依赖安全审计

每日扫描项目依赖中的已知漏洞。

**触发方式：** 定时（每日）

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

## DevOps 与监控

### 部署验证

每次部署后触发冒烟测试。CI/CD 流水线在部署完成时向 webhook POST 请求。

**触发方式：** API 调用（webhook）

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

在 CI 中使用同一个 `DEPLOY_WEBHOOK_SECRET`。此 prompt 只使用默认 webhook toolset 中的 web 提取能力，不要求动态订阅自行授予 terminal。

你的 CI/CD 流水线触发方式：

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

### 告警分类

将监控告警与近期变更关联，起草响应方案。这个通用蓝图适用于任何能发送 Hermes Generic V2 签名的告警系统，或位于原始告警系统前方的可信中继。

**触发方式：** API 调用（webhook）

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

在已签名 JSON 正文顶层加入 `"event_type": "alert"`，并严格按上一个部署示例计算 `X-Webhook-Timestamp` 与 `X-Webhook-Signature-V2`。服务商请求头不会自动选择验证器；若发送方已有 Hermes 支持的原生契约，应改用对应的 provider 路由。

### 可用性监控

每 30 分钟检查一次端点，仅在服务宕机时发送通知。

**触发方式：** 定时（每 30 分钟）

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

## 研究与情报

### 竞品仓库侦察

监控竞品仓库中有价值的 PR、功能和架构决策。

**触发方式：** 定时（每日）

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

### AI 新闻摘要

每周汇总 AI/ML 领域动态。

**触发方式：** 定时（每周）

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

### 论文摘要与笔记

每日扫描 arXiv 并将摘要保存到笔记系统。

**触发方式：** 定时（每日）

```bash
hermes cron create "0 8 * * *" \
  "Search arXiv for the 3 most interesting papers on 'language model reasoning' OR 'tool-use agents' from the past day. For each paper, create an Obsidian note with the title, authors, abstract summary, key contribution, and potential relevance to Hermes Agent development." \
  --skill arxiv --skill obsidian \
  --name "Paper digest" \
  --deliver local
```

---

## GitHub 事件自动化

### Issue 分类建议

自动为新 issue 起草标签和初始回复，并写入日志供人工审核。

**触发方式：** GitHub webhook

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

此动态蓝图只记录标签建议与回复草稿。当前 CLI 不能绑定 `github_comment` 所需的 `repo` 和 `pr_number`；若结果必须发布到 GitHub，请使用带显式 `deliver_extra` 目标的完整静态 PR 路由。

### CI 失败分析

分析 CI 失败原因并把诊断信息写入日志。一个 check run 可能关联零个或多个 pull request，因此此蓝图不会假定单一 GitHub 评论目标。

**触发方式：** GitHub webhook

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

### 规划跨仓库移植

某仓库 PR 合并后，检查其 diff，并为另一个仓库准备具体移植计划。

**触发方式：** GitHub webhook

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

这里显式授予 `terminal`，因为动态订阅不能增加路由级 toolset。`gh pr diff` 在配置的 terminal 后端中运行；该环境必须安装并认证 `gh`。

---

## 业务运营

### Stripe 支付监控

跟踪支付事件并汇总失败情况。

**触发方式：** API 调用（webhook）

```bash
STRIPE_WEBHOOK_SECRET="whsec_replace_with_your_endpoint_secret"
hermes webhook subscribe stripe-payments \
  --provider stripe \
  --signature-mode stripe \
  --secret "$STRIPE_WEBHOOK_SECRET" \
  --events "payment_intent.succeeded,payment_intent.payment_failed,charge.dispute.created" \
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

`STRIPE_WEBHOOK_SECRET` 必须是 Stripe 为该 endpoint 提供的 `whsec_...` signing secret；不要使用任意生成的 HMAC secret。Stripe 的 `Stripe-Signature` 请求头和正文 `type` 提供认证契约与事件类型。

### 每日营收摘要

每天早晨汇总关键业务指标。

**触发方式：** 定时（每日）

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

## 多技能工作流

### 安全审计流水线

组合多个技能，每周进行全面安全审查。

**触发方式：** 定时（每周）

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

### 内容流水线

按计划研究、起草并准备内容。

**触发方式：** 定时（每周）

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

## 快速参考

### Cron 调度语法

| 表达式 | 含义 |
|-----------|---------|
| `every 30m` | 每 30 分钟 |
| `every 2h` | 每 2 小时 |
| `0 2 * * *` | 每天凌晨 2:00 |
| `0 9 * * 1` | 每周一上午 9:00 |
| `0 9 * * 1-5` | 工作日上午 9:00 |
| `0 3 * * 0` | 每周日凌晨 3:00 |
| `0 */6 * * *` | 每 6 小时 |

### 投递目标

| 目标 | 参数 | 说明 |
|--------|------|-------|
| 当前会话 | `--deliver origin` | 默认——投递到任务创建所在的位置 |
| 本地文件 | `--deliver local` | 保存输出，不发送通知 |
| Telegram | `--deliver telegram` | 主频道，或用 `telegram:CHAT_ID` 指定特定会话 |
| Discord | `--deliver discord` | 主频道，或用 `discord:CHANNEL_ID` 指定 |
| Slack | `--deliver slack` | 主频道 |
| SMS | `--deliver sms:+15551234567` | 直接发送到手机号 |
| 指定话题 | `--deliver telegram:-100123:456` | Telegram 论坛话题 |

### Webhook 模板变量

| 变量 | 说明 |
|----------|-------------|
| `{pull_request.title}` | PR 标题 |
| `{issue.number}` | Issue 编号 |
| `{repository.full_name}` | `owner/repo` |
| `{action}` | 事件动作（opened、closed 等） |
| `{__raw__}` | 最多 4,000 UTF-8 字节、带明确截断元数据的可解析原始 payload 信封 |
| `{sender.login}` | 触发事件的 GitHub 用户 |

### [SILENT] 模式

当 cron 任务的响应包含 `[SILENT]` 时，投递将被抑制。使用此模式可避免在无事发生时产生通知噪音：

```
If nothing noteworthy happened, respond with [SILENT].
```

这样只有当 Agent 有内容需要汇报时，你才会收到通知。
