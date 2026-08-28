---
sidebar_position: 11
sidebar_label: "通过 Webhook 进行 GitHub PR 审查"
title: "使用 Webhook 自动发布 GitHub PR 评论"
description: "将 Hermes 连接到 GitHub，使其自动获取 PR diff、审查代码变更并发布评论——由 webhook 触发，无需手动提示"
---

# 使用 Webhook 自动发布 GitHub PR 评论

本指南介绍如何将 Hermes Agent 连接到 GitHub，使其自动获取 pull request 的 diff、分析代码变更并发布评论——由 webhook 事件触发，无需手动 prompt（提示词）。

当 PR 被打开或更新时，GitHub 会向你的 Hermes 实例发送一个 webhook POST 请求。Hermes 使用一个 prompt 运行 agent，该 prompt 指示其通过 `gh` CLI 获取 diff，并将响应发布回 PR 线程。

:::tip 想要无需公网端点的更简单配置？
如果你没有公网 URL，或只是想快速上手，请查看 [构建 GitHub PR 审查 Agent](./github-pr-review-agent.md) —— 使用 cron 作业按计划轮询 PR，可在 NAT 和防火墙后运行。
:::

:::info 参考文档
完整的 webhook 平台参考（所有配置选项、投递类型、动态订阅、安全模型），请参阅 [Webhooks](/user-guide/messaging/webhooks)。
:::

:::warning Prompt 注入风险
Webhook payload 包含攻击者可控的数据——PR 标题、commit 消息和描述中可能包含恶意指令。当你的 webhook 端点暴露在公网时，请在沙箱环境（Docker、SSH 后端）中运行 gateway。请参阅下方的[安全说明](#security-notes)。
:::

---

## 前提条件

- Hermes Agent 已安装并运行（`hermes gateway`）
- [`gh` CLI](https://cli.github.com/) 已安装并完成认证：`gh pr diff` 在配置的 terminal 后端中运行，而最终的 `github_comment` 投递在 gateway 主机上运行。使用 Docker 或 SSH terminal 后端时，两处环境都必须分别具备所需的 `gh` 与凭证。
- 你的 Hermes 实例有一个可公网访问的 URL（如果在本地运行，请参阅[使用 ngrok 进行本地测试](#local-testing-with-ngrok)）
- 对 GitHub 仓库的管理员权限（管理 webhook 所需）

---

## 第一步——启用 webhook 平台

在你的 `~/.hermes/config.yaml` 中添加以下内容：

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      port: 8644          # 默认值；如果该端口被其他服务占用，请修改
      rate_limit: 30      # 每个 profile/路由滑动 60 秒内的新身份数

      routes:
        github-pr-review:
          provider: github
          signature_mode: github
          secret: "your-webhook-secret-here"   # 必须与 GitHub webhook secret 完全一致
          events:
            - pull_request
          toolsets: ["terminal"]                 # 运行 gh 所需的显式授权

          # agent 被指示在审查前先获取实际的 diff。
          # {number} 和 {repository.full_name} 从 GitHub payload 中解析。
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
            3. Return only a concise, actionable review comment. Do not post it yourself;
               the github_comment delivery target posts the final response exactly once.

          deliver: github_comment
          deliver_extra:
            repo: "{repository.full_name}"
            pr_number: "{number}"
```

**关键字段：**

| 字段 | 说明 |
|---|---|
| `provider` | 将此路由绑定到 GitHub 验证器；请求头不能选择 provider。 |
| `secret`（路由级别） | 该路由的 HMAC secret。全局 `extra.secret` 回退值仅在恰好一条已认证路由使用时有效；否则每条路由都必须使用唯一密钥材料。 |
| `events` | 最多配置一个已认证 GitHub 事件类别。支持 `check_run`、`pull_request`、`push`、`issues`、`ping`；请求头与已签名正文结构必须同时匹配。空列表不按事件过滤，但事件权限解析为 `unknown`。 |
| `toolsets` | 替换此路由受限的 webhook 默认工具集。`gh pr diff` 需要 `terminal`；若 prompt 不再使用 shell，请移除此授权。 |
| `prompt` | 模板；`{field}` 和 `{nested.field}` 从 GitHub payload 中解析。 |
| `deliver` | `github_comment` 通过 `gh pr comment` 发布。`log` 仅写入 gateway 日志。 |
| `deliver_extra.repo` | 从 payload 中解析为例如 `org/repo`。 |
| `deliver_extra.pr_number` | 从 payload 中解析为 PR 编号。 |

:::note Payload 中不包含代码
GitHub webhook payload 包含 PR 元数据（标题、描述、分支名、URL），但**不包含 diff**。上方的 prompt 指示 agent 在配置的 terminal 后端中运行 `gh pr diff` 来获取实际变更。默认 `hermes-webhook` 工具集出于安全考虑不包含 `terminal`；本路由因此显式授予 `toolsets: ["terminal"]`，并用它替换此路由的平台默认工具集。最终 `github_comment` 投递由 gateway 主机上的 `gh` 独立执行；prompt 只应返回评论文本，不能再次发布。
:::

---

## 第二步——启动 gateway

```bash
hermes gateway
```

你应该看到：

```
[webhook] Listening on * (all interfaces, IPv4+IPv6):8644 — routes: github-pr-review
```

验证其是否正在运行：

```bash
curl http://localhost:8644/health
# {"status": "ok", "platform": "webhook", "accepting_webhooks": true}
```

---

## 第三步——在 GitHub 上注册 webhook

1. 进入你的仓库 → **Settings** → **Webhooks** → **Add webhook**
2. 填写：
   - **Payload URL：** `https://your-public-url.example.com/webhooks/github-pr-review`
   - **Content type：** `application/json`
   - **Secret：** 与路由配置中 `secret` 设置的值相同
   - **Which events?** → 选择单个事件 → 勾选 **Pull requests**
3. 点击 **Add webhook**

GitHub 会立即发送一个已签名的 `ping` 事件以确认连接。此路由精确绑定 `pull_request`，因此该投递会返回 HTTP `401`（`Invalid authenticated webhook metadata`）。`X-GitHub-Event` 值与 HMAC 覆盖的 JSON 正文结构必须同时匹配，所以修改 ping 的请求头也不能把它变成 pull-request 事件。这是预期行为；之后已签名的 `pull_request` 投递才是功能测试。

---

## 第四步——打开一个测试 PR

创建一个分支，推送一个变更，并打开一个 PR。在 30–90 秒内（取决于 PR 大小和模型），Hermes 应该会发布一条审查评论。

要实时跟踪 agent 的进度：

```bash
tail -f "${HERMES_HOME:-$HOME/.hermes}/logs/gateway.log"
```

---

## 使用 ngrok 进行本地测试

如果 Hermes 在你的笔记本上运行，使用 [ngrok](https://ngrok.com/) 将其暴露到公网：

```bash
ngrok http 8644
```

复制 `https://...ngrok-free.app` URL 并将其用作你的 GitHub Payload URL。在 ngrok 免费版中，每次 ngrok 重启后 URL 都会变化——每次会话都需要更新你的 GitHub webhook。付费 ngrok 账户可获得静态域名。

你可以直接用 `curl` 对静态路由进行冒烟测试——无需 GitHub 账户或真实 PR。请添加一条使用独立 secret 的 log-only 路由，避免测试时修改生产路由已绑定到密钥的策略：

```yaml
# 位于 platforms.webhook.extra.routes 内：
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

:::tip 为什么使用第二条路由？
Secret 会永久绑定到路由名称、profile、验证器、prompt、toolset 和投递策略。把生产路由的 `deliver` 从 `github_comment` 改为 `log` 后继续复用原 secret 会被拒绝；独立路由能让两套权限保持明确。
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

然后观察 agent 运行：
```bash
tail -f "${HERMES_HOME:-$HOME/.hermes}/logs/gateway.log"
```

:::note
`hermes webhook test <name>` 仅适用于通过 `hermes webhook subscribe` 创建的**动态订阅**。它不读取 `config.yaml` 中的路由。
:::

---

## 过滤特定 action

GitHub 会针对多种 action 发送 `pull_request` 事件：`opened`、`synchronize`、`reopened`、`closed`、`labeled` 等。路由会同时绑定 `X-GitHub-Event` 值与已认证的 pull-request 正文类别；随后可用路由级 `filters` 按 `action` 等 payload 字段进一步筛选。

第一步的 prompt 会为 `closed` 和 `labeled` action 返回 `[SILENT]`。

:::warning Agent 仍会运行并消耗 token（令牌）
`[SILENT]` 会抑制最终投递，但每个 `pull_request` action 仍会运行 agent 并消耗 token。请在 agent 唤醒前使用路由级 `filters`。Filters 属于密钥绑定的执行策略，因此要同时在路由和 GitHub 中换用**全新 secret**；复用旧 secret 进行此编辑会被拒绝：

```yaml
secret: "NEW-FILTERED-WEBHOOK-SECRET"
filters:
  - field: "action"
    in: ["opened", "synchronize", "reopened"]
```

对于高流量仓库，还可通过 GitHub Actions workflow 在上游按条件调用 webhook URL。
:::

> 不支持 Jinja2 或条件模板语法。`{field}` 和 `{nested.field}` 是唯一支持的替换方式。其他内容会原样传递给 agent。

---

## 使用 skill 保持一致的审查风格

加载一个 [Hermes skill](/user-guide/features/skills) 以赋予 agent 一致的审查风格。在 `config.yaml` 的 `platforms.webhook.extra.routes` 中，向你的路由添加 `skills`：

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      routes:
        github-pr-review:
          provider: github
          signature_mode: github
          secret: "NEW-SKILL-BOUND-WEBHOOK-SECRET"
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
            3. Return only a concise, actionable review comment. Do not post it yourself;
               the github_comment target posts the final response exactly once.
          skills:
            - review
          deliver: github_comment
          deliver_extra:
            repo: "{repository.full_name}"
            pr_number: "{number}"
```

添加或修改 skill 会改变密钥绑定策略。请把示例中的值替换为从未使用过的新 secret，并同步更新 GitHub webhook；不能继续使用此前无 skill 路由的 secret。

> **注意：** 列表中只有第一个找到的 skill 会被加载。Hermes 不会叠加多个 skill——后续条目会被忽略。

---

## 将响应发送到 Slack 或 Discord

改变 `deliver` 或 `deliver_extra` 会改变密钥绑定策略。请同时将 `secret` 换成从未使用过的新值，并在 GitHub webhook 设置中同步更新。以下是两个互斥的目标示例：

Slack：

```yaml
# 在 platforms.webhook.extra.routes.<route-name> 内部：
secret: "NEW-SLACK-BOUND-WEBHOOK-SECRET"
deliver: slack
deliver_extra:
  chat_id: "C0123456789"   # Slack 频道 ID（省略则使用配置的默认频道）
  scope_id: "T0123456789"  # 此频道所属的 Slack 工作区/团队 ID
```

Discord：

```yaml
# 在 platforms.webhook.extra.routes.<route-name> 内部：
secret: "NEW-DISCORD-BOUND-WEBHOOK-SECRET"
deliver: discord
deliver_extra:
  chat_id: "987654321012345678"  # Discord 频道 ID（省略则使用默认频道）
```

目标平台也必须在 gateway 中启用并连接。如果省略 `chat_id`，响应将发送到该平台配置的默认频道。模板化的 Slack `chat_id` 必须显式绑定匹配的工作区/团队 `scope_id`；静态频道可由已连接的适配器确立 scope，但显式配置可消除歧义。

有效的 `deliver` 值：`log` · `github_comment` · `telegram` · `discord` · `slack` · `signal` · `sms`

---

## GitLab 支持

同一适配器也适用于 GitLab。GitLab 使用 `X-Gitlab-Token` 进行认证（纯字符串匹配，非 HMAC），但 Hermes 不会从请求头自动猜测 provider；路由必须显式声明 `provider: gitlab`。

对于事件过滤，GitLab 将 `X-GitLab-Event` 设置为 `Merge Request Hook`、`Push Hook`、`Pipeline Hook` 等值。在 `events` 中使用精确的请求头值：

```yaml
provider: gitlab
signature_mode: gitlab
secret: "your-gitlab-secret-token"
events:
  - Merge Request Hook
```

GitLab 的 payload 字段与 GitHub 不同——例如，MR 标题使用 `{object_attributes.title}`，MR 编号使用 `{object_attributes.iid}`。发现完整 payload 结构最简单的方式是使用 GitLab webhook 设置中的 **Test** 按钮，结合 **Recent Deliveries** 日志。或者，在路由配置中省略 `prompt`——Hermes 会向 agent 传递一个最多 4,000 UTF-8 字节、可解析并带有明确 `truncated` 标记的原始 payload 信封。

---

## 安全说明

- **永远不要在生产环境中使用 `INSECURE_NO_AUTH`**——它会完全禁用签名验证。仅用于本地开发。
- **路由名称、profile、provider、签名模式或执行策略发生变化时轮换 webhook secret**，并在 GitHub 与 `config.yaml` 中同步更新。密钥材料会跨 profile 永久绑定到原始路由策略，不能重新分配。
- **速率限制**默认在每个 profile/路由的滑动 60 秒窗口内准入 30 个新身份（可通过 `extra.rate_limit` 配置）。持久化重复或已活动身份不会再次占用配额；新身份超限返回 `429`。
- **重复投递**由基于已认证请求正文生成的持久重放凭证阻止。GitHub 的正文签名不认证 `X-GitHub-Delivery` 或 `X-Request-ID`，因此这些观察到的请求头仅用于诊断，绝不会控制重放身份。该凭证在进程重启后仍有效，并非一小时的内存缓存。
- **Prompt 注入：** PR 标题、描述和 commit 消息均为攻击者可控内容。恶意 PR 可能尝试操纵 agent 的行为。当暴露在公网时，请在沙箱环境（Docker、VM）中运行 gateway。

---

## 故障排查

| 现象 | 检查项 |
|---|---|
| `401 Invalid signature` | config.yaml 中的 secret 与 GitHub webhook secret 不匹配 |
| `404 Unknown route` | URL 中的路由名称与 `routes:` 中的键不匹配 |
| `429 Rate limit exceeded` | 新身份超过滑动 60 秒配额；等待最早准入身份离开窗口，或提高 `extra.rate_limit` |
| 未发布评论 | `gh` 未安装、不在 PATH 中，或未完成认证（`gh auth login`） |
| Agent 运行但无评论 | 有意的 autonomous silence 响应（`[SILENT]`，包括该标记后附带解释文字）会按设计抑制目标投递。若并非预期静默，请在 gateway 日志中检查 prompt 与完整 agent 输出。 |
| 端口已被占用 | 在 config.yaml 中修改 `extra.port` |
| Agent 运行但仅审查了 PR 描述 | prompt 中未包含 `gh pr diff` 指令——diff 不在 webhook payload 中 |
| GitHub 将初始 ping 标记为失败 | 对 `events: [pull_request]` 路由来说这是预期行为：其请求头和已认证正文都属于 `ping` 而非 `pull_request`，因此返回 `401`。请改为确认之后已签名的 pull-request 投递。 |

**GitHub 的 Recent Deliveries 标签页**（仓库 → Settings → Webhooks → 你的 webhook）显示每次投递的精确请求头、payload、HTTP 状态和响应体。这是无需查看服务器日志即可诊断故障的最快方式。

---

## 完整配置参考

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      # 默认省略 host，监听所有 IPv4 + IPv6 接口；如需限制请显式设置地址
      port: 8644               # 监听端口（默认：8644）
      secret: ""               # 仅当恰好一条已认证路由使用时才可作为回退
      rate_limit: 30           # 每个 profile/路由滑动 60 秒内的新身份数
      max_body_bytes: 1048576  # 默认值及硬上限：1 MiB

      routes:
        <route-name>:
          provider: github       # 显式 provider/验证器绑定
          signature_mode: github
          secret: "required-per-route"
          events: []            # [] = 不过滤/unknown；否则配置一个受支持事件
          prompt: ""            # {field} / {nested.field} 从 payload 中解析
          skills: []            # 加载第一个匹配的 skill（仅一个）
          toolsets: []          # 显式的每路由替换；terminal 不是默认工具
          deliver: "log"        # log | github_comment | telegram | discord | slack | signal | sms
          deliver_extra: {}     # github_comment 需要 repo + pr_number；其他平台需要 chat_id
```

---

## 下一步

- **[基于 Cron 的 PR 审查](./github-pr-review-agent.md)** —— 按计划轮询 PR，无需公网端点
- **[Webhook 参考](/user-guide/messaging/webhooks)** —— webhook 平台的完整配置参考
- **[构建 Plugin](/developer-guide/plugins)** —— 将审查逻辑打包为可共享的 plugin
- **[Profiles](/user-guide/profiles)** —— 运行一个拥有独立内存和配置的专属审查者 profile
