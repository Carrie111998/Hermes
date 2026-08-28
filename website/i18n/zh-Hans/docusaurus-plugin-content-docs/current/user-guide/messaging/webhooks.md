---
sidebar_position: 13
title: "Webhooks"
description: "接收来自 GitHub、GitLab 等服务的事件以触发 Hermes agent 运行"
---

# Webhooks

接收来自外部服务（GitHub、GitLab、JIRA、Stripe 等）的事件，并自动触发 Hermes agent 运行。Webhook 适配器运行一个 HTTP 服务器，接受 POST 请求、验证 HMAC 签名、将 payload（载荷）转换为 agent prompt（提示词），并将响应路由回来源或其他已配置的平台。

agent 处理事件后，可通过在 PR 上发布评论、向 Telegram/Discord 发送消息或记录结果来响应。

## 视频教程

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

## 快速开始

1. 通过 `hermes gateway setup` 或环境变量启用
2. 在 `config.yaml` 中定义路由，**或**使用 `hermes webhook subscribe` 动态创建
3. 默认绑定的路由使用 `/webhooks/<route-name>`。显式设置命名 `profile` 的路由始终使用 `/p/<profile>/webhooks/<route-name>`，即使 gateway 运行在单 profile 模式下也是如此。

---

## 设置

有两种方式启用 webhook 适配器。

### 通过设置向导

```bash
hermes gateway setup
```

按照提示启用 webhooks、设置端口和全局 HMAC secret。

### 通过环境变量

添加到 `~/.hermes/.env`：

```bash
WEBHOOK_ENABLED=true
WEBHOOK_PORT=8644        # default
WEBHOOK_SECRET=your-global-secret
```

### 验证服务器

gateway 运行后：

```bash
curl http://localhost:8644/health
```

预期响应：

```json
{"status": "ok", "platform": "webhook", "accepting_webhooks": true}
```

---

## 配置路由 {#configuring-routes}

路由定义了不同 webhook 来源的处理方式。每个路由是 `config.yaml` 中 `platforms.webhook.extra.routes` 下的一个命名条目。

### 路由属性

| 属性 | 是否必填 | 描述 |
|----------|----------|-------------|
| `provider` | **是*** | 显式服务商契约，例如 `github`、`gitlab`、`svix`、`standard_webhooks`、`stripe`、`hermes` 或 `generic`。路由必须声明 `provider` 或 `signature_mode`；Hermes 不会根据攻击者可控请求头猜测。 |
| `signature_mode` | 有时 | 显式验证器模式。通常由 `provider` 推导；服务商没有安全默认值时必填，也可用于选择 `generic_v1` 或推荐的 `generic_v2`。 |
| `events` | 否 | 此路由允许的事件类型。GitHub 最多配置 `check_run`、`pull_request`、`push`、`issues` 或 `ping` 中的一个；Hermes 同时要求未签名的请求头和 HMAC 覆盖的正文结构匹配。GitLab 同样最多配置一个事件，并要求其未签名请求头与路由绑定值完全一致。未配置时不按事件过滤，但解析事件为 `unknown`。其他服务商可从已认证正文解析事件。 |
| `secret` | **是** | 用于签名验证的 HMAC secret。仅当恰好一条已认证路由使用回退值时，才可由全局 `secret` 提供；多条路由必须配置各自唯一的 secret。仅回环测试可设为 `"INSECURE_NO_AUTH"`（跳过验证）。 |
| `profile` | 否 | 有权执行此路由的 profile。省略（或使用 `default`）时绑定 `/webhooks/<route>`；显式名称（例如 `coder`）始终将路由及 secret 绑定到 `/p/coder/webhooks/<route>`。单 profile 模式下，该名称必须是当前运行的 profile；`gateway.multiplex_profiles` 只增加由同一 gateway 服务多个允许 profile 的能力。 |
| `prompt` | 否 | 使用点号表示法访问 payload 字段的模板字符串（例如 `{pull_request.title}`）。若省略，prompt 会包含一个最多 4,000 UTF-8 字节、可解析的原始 payload 信封，而不是无界的完整转储。 |
| `filters` | 否 | 声明式 payload 过滤器，在认证/请求体/事件过滤之后、agent 或直接投递之前求值。不匹配时返回 `{"status":"ignored","reason":"filter"}`（HTTP 200）。 |
| `script` | 否 | 位于活动 profile 的 `$HERMES_HOME/scripts/` 下（通常为 `~/.hermes/scripts/`）的过滤/转换脚本。webhook payload 以 JSON 形式通过 stdin 传入。stdout 为 JSON 对象时会在模板渲染前替换 payload；文本 stdout 以 `script_output` 形式暴露；空 stdout、`[SILENT]` 或 `{"__hermes_ignore__": true}` 会抑制投递。脚本执行开始后，超时或非零退出码会返回 HTTP 500 及 `status=indeterminate`，并持久阻止相同投递标识再次执行。 |
| `skills` | 否 | agent 运行时加载的 skill 名称列表。 |
| `toolsets` | 否 | 仅为此路由替换平台级 webhook toolset 的键列表。只能手动编辑配置，`hermes webhook subscribe` 不可设置，避免 agent 创建订阅时自行提升权限。参见[路由级 toolset](#per-route-toolsets)。 |
| `deliver` | 否 | 响应发送目标：`github_comment`、`telegram`、`discord`、`slack`、`signal`、`sms`、`whatsapp`、`matrix`、`mattermost`、`homeassistant`、`email`、`dingtalk`、`feishu`、`wecom`、`weixin`、`bluebubbles`、`qqbot`，或 `log`（默认）。 |
| `deliver_extra` | 否 | 额外的投递配置——键取决于 `deliver` 类型（例如 `repo`、`pr_number`、`chat_id`）。值支持与 `prompt` 相同的 `{dot.notation}` 模板语法。 |
| `deliver_only` | 否 | 若为 `true`，完全跳过 agent——渲染后的 `prompt` 模板直接作为消息体投递。零 LLM token 消耗，亚秒级投递。参见[直接投递模式](#direct-delivery-mode)了解使用场景。要求 `deliver` 为真实目标（非 `log`）。 |

\* `provider` 与 `signature_mode` 至少声明一个。

### 完整示例

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

### Payload 过滤器 {#payload-filters}

当服务商发送宽泛的事件流、但只有部分 payload 应唤醒 agent 或触发 `deliver_only` 投递时，使用 `filters`。过滤器在签名验证、请求体解析、事件选择和持久化重放准入之后运行，但在脚本、prompt 渲染、agent 调度或目标投递之前运行。因此，被忽略的已认证身份仍会留下永久重放凭证。

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

支持的操作符：

- `exists: true|false`
- `missing: true`
- `equals` / `not_equals`
- `contains` — 适用于字符串、列表和 dict 键
- `in` — 内联列表
- `in_file` — JSON 数组、JSON 对象（使用其键）或按行分隔的文本文件
- `regex`（在 HTTP 事件循环之外的隔离 worker 中求值；超过 4 KiB 的表达式、超过 256 KiB 的输入、无效表达式或超过 100 ms 的匹配均失败关闭）

每条路由最多包含 64 个过滤节点、八层嵌套和八个正则操作符；超过任一限制都会失败关闭。
- `all`、`any` 和 `not` 分组

字段路径使用点号表示法。当存在顶层 `payload` 对象时，`payload.foo` 从中读取；对扁平 payload 则从 webhook 请求体根部读取。`event` / `event_type` 匹配解析出的权威事件类型。`headers.<Name>` 只暴露所选验证器以密码学方式覆盖的请求头；未签名的 GitHub/GitLab 事件或投递请求头仅用于诊断，不能作为过滤输入。

### 脚本过滤与转换 {#script-filters-and-transforms}

当声明式过滤器不够用时，使用 `script`。脚本必须位于活动 profile 的 `$HERMES_HOME/scripts/` 目录下（通常为 `~/.hermes/scripts/`）；相对路径在该目录内解析，且禁止路径穿越到目录之外。Hermes 会在发布路由时捕获精确的脚本字节，执行时只使用这些已捕获字节。`.sh` 和 `.bash` 源码通过带 `--noprofile --norc` 的 bash 运行；其他源码通过当前 Python 解释器的隔离模式运行。

每次调用都在全新、空白的工作目录中运行，并使用已捕获的最小非 secret 环境。它不会从 gateway 进程继承 `BASH_ENV`、`ENV`、`PYTHONPATH`、任意自定义变量或凭据变量。因此，相对导入、相邻 helper 文件、shell 启动钩子和环境中的 secret 都不可用。路由脚本必须是自包含的 JSON 转换：从 stdin 读取所提供的 JSON，并把结果写到 stdout。

路由脚本是受信任的本地代码，并不是操作系统级沙箱。它以 gateway 用户身份运行，能够主动访问该账户可访问的任何资源，也可以脱离 Hermes 对子进程的尽力清理。冻结源码/解释器契约、空白工作目录、最小环境、超时和输出上限用于防止意外的权限漂移，并不能约束恶意脚本。如果不能完全信任路由脚本作者，请把 gateway 本身运行在容器、虚拟机或受限服务账户中。

路由 payload 以 JSON 形式发送到 stdin：

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

脚本结果：

- stdout 为 JSON 对象时，替换 `prompt` 和 `deliver_extra` 使用的 payload。
- 非 JSON 文本 stdout 会以 `script_output` 字段加入 payload。
- 空 stdout、精确的 `[SILENT]` 或 `{"__hermes_ignore__": true}` 会明确抑制投递，并返回 HTTP 200 及 `{"status":"ignored","reason":"script"}`。
- Hermes 会在发布路由之前验证脚本配置。脚本缺失、无法读取或无效时，静态 webhook 监听器不会启动；动态订阅中的同类错误会使候选路由被跳过，或使已发布的动态路由被撤销。它通常不会等到请求到达后才被发现。若防御性检查仍在请求期间发现配置不一致，则会在脚本执行前返回 HTTP 500 及 `status=failed`；修复并重新发布路由后可以重试该投递。
- 脚本执行被持久标记为已开始后，超时、运行时错误、非零退出码或无效输出会返回 HTTP 500 及 `status=indeterminate`。Hermes 会记录不确定结果并阻止相同投递标识再次执行；重试时返回 HTTP 409 及 `status=indeterminate`，而不会再次运行脚本。
- 脚本 stdout 与 stderr 各自最多捕获 1 MiB，两者合计同样最多 1 MiB。超过任一输出上限都会终止脚本，并按上述“脚本已开始”的 `indeterminate` 路径处理。

### Prompt 模板

Prompt 使用点号表示法访问 webhook payload 中的嵌套字段：

- `{pull_request.title}` 解析为 `payload["pull_request"]["title"]`
- `{repository.full_name}` 解析为 `payload["repository"]["full_name"]`
- `{__raw__}` — 包含 `payload`、`truncated` 和 `original_bytes` 字段的可解析 JSON 信封。包括 JSON 转义和元数据在内，整个信封默认最多 4,000 UTF-8 字节。
- 缺失的键保留为字面量 `{key}` 字符串（不报错）
- 嵌套的 dict 和 list 会被 JSON 序列化并截断至 2000 个字符

可以将 `{__raw__}` 与常规模板变量混合使用：

```yaml
prompt: "PR #{pull_request.number} by {pull_request.user.login}: {__raw__}"
```

若路由未配置 `prompt` 模板，Hermes 会把同一个受限的 4,000 字节原始 payload 信封放进生成的 prompt。较大的 payload 会明确标记 `truncated`；省略模板不会产生无界的完整 JSON 转储。

`deliver_extra` 的值中同样支持点号表示法模板。

### 论坛话题投递

向 Telegram 投递 webhook 响应时，可通过在 `deliver_extra` 中包含 `message_thread_id`（或 `thread_id`）来指定特定论坛话题：

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

若 `deliver_extra` 中未提供 `chat_id`，则回退到目标平台配置的主频道。对于命名的 multiplex 路由，如果路由发布时目标适配器尚未活动，Hermes 会从该路由所指 profile 的配置读取主频道，而不是使用进程默认 profile，并将其冻结到路由权限中。

---

## GitHub PR 审查（分步说明） {#github-pr-review}

本演练将为每个 pull request 设置自动代码审查。

### 1. 在 GitHub 中创建 webhook

1. 进入你的仓库 → **Settings** → **Webhooks** → **Add webhook**
2. 将 **Payload URL** 设为 `http://your-server:8644/webhooks/github-pr`
3. 将 **Content type** 设为 `application/json`
4. 将 **Secret** 设为与路由配置匹配的值（例如 `github-webhook-secret`）
5. 在 **Which events?** 下，选择 **Let me select individual events** 并勾选 **Pull requests**
6. 点击 **Add webhook**

### 2. 添加路由配置

按照上方示例，将 `github-pr` 路由添加到 `~/.hermes/config.yaml`。

### 3. 确保 `gh` CLI 已认证

`github_comment` 投递类型使用 GitHub CLI 发布评论：

```bash
gh auth login
```

### 4. 测试

在仓库中打开一个 pull request。webhook 触发后，Hermes 处理事件并在 PR 上发布审查评论。

---

## GitLab Webhook 设置 {#gitlab-webhook-setup}

GitLab webhook 的工作方式类似，但使用不同的认证机制。GitLab 通过 `X-Gitlab-Token` 请求头以明文字符串匹配（非 HMAC）发送 secret。

### 1. 在 GitLab 中创建 webhook

1. 进入你的项目 → **Settings** → **Webhooks**
2. 将 **URL** 设为 `http://your-server:8644/webhooks/gitlab-mr`
3. 输入你的 **Secret token**
4. 选择 **Merge request events**（以及其他你需要的事件）
5. 点击 **Add webhook**

### 2. 添加路由配置

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

## 投递选项 {#delivery-options}

`deliver` 字段控制 agent 处理 webhook 事件后响应的发送目标。

| 投递类型 | 描述 |
|-------------|-------------|
| `log` | 将响应记录到 gateway 日志输出。这是默认值，适合测试使用。 |
| `github_comment` | 通过 `gh pr comment` 将响应作为 pull request 评论发布。需要 `deliver_extra.repo` 和正整数 `deliver_extra.pr_number`。`gh` CLI 必须安装并在 gateway 主机上完成认证（`gh auth login`）。 |
| `telegram` | 将响应路由到 Telegram。使用主频道，或在 `deliver_extra` 中指定 `chat_id`。 |
| `discord` | 将响应路由到 Discord。使用主频道，或在 `deliver_extra` 中指定 `chat_id`。 |
| `slack` | 将响应路由到 Slack。使用已配置的主频道，或在 `deliver_extra` 中指定 `chat_id`。模板化的 `chat_id` 必须显式绑定工作区 `scope_id`；静态频道可由已连接的适配器确立 scope，但显式声明更清楚。 |
| `signal` | 将响应路由到 Signal。使用主频道，或在 `deliver_extra` 中指定 `chat_id`。 |
| `sms` | 通过 Twilio 将响应路由到 SMS。使用主频道，或在 `deliver_extra` 中指定 `chat_id`。 |
| `whatsapp` | 将响应路由到 WhatsApp。使用主频道，或在 `deliver_extra` 中指定 `chat_id`。 |
| `matrix` | 将响应路由到 Matrix。使用主频道，或在 `deliver_extra` 中指定 `chat_id`。 |
| `mattermost` | 将响应路由到 Mattermost。使用主频道，或在 `deliver_extra` 中指定 `chat_id`。 |
| `homeassistant` | 将响应路由到 Home Assistant。使用主频道，或在 `deliver_extra` 中指定 `chat_id`。 |
| `email` | 将响应路由到 Email。使用主频道，或在 `deliver_extra` 中指定 `chat_id`。 |
| `dingtalk` | 将响应路由到 DingTalk。使用主频道，或在 `deliver_extra` 中指定 `chat_id`。 |
| `feishu` | 将响应路由到 Feishu/Lark。使用主频道，或在 `deliver_extra` 中指定 `chat_id`。 |
| `wecom` | 将响应路由到 WeCom。使用主频道，或在 `deliver_extra` 中指定 `chat_id`。 |
| `weixin` | 将响应路由到 Weixin（微信）。使用主频道，或在 `deliver_extra` 中指定 `chat_id`。 |
| `bluebubbles` | 将响应路由到 BlueBubbles（iMessage）。使用主频道，或在 `deliver_extra` 中指定 `chat_id`。 |

跨平台投递时，目标平台也必须在 gateway 中启用并连接。若 `deliver_extra` 中未提供 `chat_id`，响应将发送到该平台配置的主频道。模板化的 Slack `chat_id` 必须提供路由绑定的 `scope_id`，以防已签名事件跨越工作区权限边界。对于静态 Slack 频道，Hermes 可冻结已连接适配器确立的 scope；显式配置 `scope_id` 可消除歧义。

---

## 直接投递模式 {#direct-delivery-mode}

默认情况下，每次 webhook POST 都会触发一次 agent 运行——payload 成为 prompt，agent 处理后投递响应。这会在每次事件时消耗 LLM token。

对于只需**推送纯文本通知**的场景——无需推理、无需 agent 循环，只需投递消息——可在路由上设置 `deliver_only: true`。渲染后的 `prompt` 模板直接作为消息体，适配器将其直接分发到配置的投递目标。

### 何时使用直接投递

- **外部服务推送** — Supabase/Firebase webhook 在数据库变更时触发 → 即时通知 Telegram 用户
- **监控告警** — Datadog/Grafana 告警 webhook → 推送到 Discord 频道
- **agent 间通知** — Agent A 通知 Agent B 的用户某个长时任务已完成
- **后台任务完成** — Cron 任务完成 → 将结果发布到 Slack

优势：

- **零 LLM token** — agent 从不被调用
- **亚秒级投递** — 单次适配器调用，无推理循环
- **与 agent 模式相同的安全性** — HMAC 认证、速率限制、幂等性和请求体大小限制均正常生效
- **同步响应** — 投递成功后 POST 返回 `200 OK`。若确定目标在任何副作用发生前失败，则返回带 `Retry-After: 5` 的 `503`，可以重试；`502` 表示目标结果不确定、需要人工核对，不能自动重试。

### 示例：从 Supabase 推送到 Telegram

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

Supabase edge function 必须把当前 Unix 时间戳放入 `X-Webhook-Timestamp`，用 `antenna-webhook-secret` 对 `<timestamp>.<原始请求正文>` 计算小写 HMAC-SHA256 十六进制摘要，并把摘要放入 `X-Webhook-Signature-V2`，再 POST 到 `https://your-server:8644/webhooks/antenna-matches`。时间戳必须在 gateway 时钟前后 300 秒内。适配器按显式 `generic_v2` 契约验证请求、渲染模板、投递到 Telegram，并返回 `200 OK`。

### 示例：通过 CLI 动态订阅

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

### 响应状态码

| 状态码 | 含义 |
|--------|---------|
| `200 OK` | 投递成功。响应体包含 `status`、`route`、`target`、`delivery_id`，以及持久化目标 `settlement`（`confirmed` 或 `cached`）。 |
| `200 OK`（status=suppressed） | 有意的 autonomous silence 响应在不调用目标的情况下完成结算；`settlement` 为 `suppressed`。 |
| `200 OK`（status=duplicate） | 已认证的重放身份已经完成；永久凭证会阻止重复投递。 |
| `202 Accepted`（status=in_progress） | 同一重放身份正在运行，或已拥有可恢复的暂存投递。 |
| `409 Conflict` | 同一已认证重放身份对应不同正文，或先前结果不确定、需要人工核对。 |
| `401 Unauthorized` | HMAC 签名无效或缺失。 |
| `400 Bad Request` | JSON 请求体格式错误。 |
| `404 Not Found` | 未知路由名称。 |
| `413 Payload Too Large` | 请求体超过 `max_body_bytes`，或副作用开始前的模板扩展超过持久载体限制。 |
| `429 Too Many Requests` | 路由速率限制已超出。 |
| `500 Internal Server Error` | 已启动的路由脚本失败或超时；结果会被持久化标记为不确定并阻止重试。 |
| `502 Bad Gateway` | 目标尝试可能已经产生副作用，因此结果不确定、需要核对。该重放身份会被持久阻止；再次提交同一身份会返回 `409`，不会重新调用目标。 |
| `503 Service Unavailable` | 接收/恢复权限、由所有 webhook 适配器/profile 共享的四个**进程全局**受限路由 worker 之一、目标预检或持久化账本容量不可用。路由 worker 饱和时包含 `Retry-After: 1`；目标在副作用前失败时包含 `Retry-After: 5`；永久重放凭证不会自动淘汰，因此容量耗尽时不提供自动重试间隔。 |

### 配置注意事项

- `deliver_only: true` 要求 `deliver` 为真实目标。`deliver: log`（或省略 `deliver`）在启动时会被拒绝——适配器发现路由配置错误时拒绝启动。
- 直接投递模式下 `skills` 字段被忽略（不运行 agent，无处注入 skill）。
- 模板渲染使用与 agent 模式相同的 `{dot.notation}` 语法，包括 `{__raw__}` token。
- 重放防护只使用已认证的身份材料。Svix/Standard Webhooks 会把消息 ID 纳入签名；Stripe/Hermes 可使用已认证正文中的 ID。GitHub 等仅对正文签名的服务商按已认证正文摘要防护，而不是按未签名的传输 ID。

---

## 动态订阅（CLI） {#dynamic-subscriptions}

除了 `config.yaml` 中的静态路由，还可以使用 `hermes webhook` CLI 命令动态创建 webhook 订阅。当 agent 本身需要设置事件驱动触发器时，这尤为有用。

### 创建订阅

```bash
hermes webhook subscribe github-issues \
  --provider github \
  --events "issues" \
  --prompt "New issue #{issue.number}: {issue.title}\nBy: {issue.user.login}\n\n{issue.body}" \
  --deliver telegram \
  --deliver-chat-id "-100123456789" \
  --description "Triage new GitHub issues"
```

此命令返回 webhook URL 和自动生成的 HMAC secret。将你的服务配置为 POST 到该 URL。

### 列出订阅

```bash
hermes webhook list
```

### 删除订阅

```bash
hermes webhook remove github-issues
```

### 测试订阅

```bash
hermes webhook test github-issues
hermes webhook test github-issues --payload '{"action":"opened","issue":{"number":42,"title":"Test"},"repository":{"id":1,"full_name":"owner/repo"},"sender":{"id":2,"login":"tester"}}'
```

第一条命令会自动生成符合服务商契约的正文。自定义 GitHub payload 仍必须证明配置的事件类别；对于 `issues`，正文至少要包含受支持的 `action` 以及 `issue`、`repository`、`sender` 对象。CLI 会对自定义的精确字节签名并发送路由绑定的 `X-GitHub-Event` 请求头，但不会替你修复不合法的服务商 payload。

### 动态订阅的工作原理

- 订阅存储在活动 profile 的 `${HERMES_HOME:-$HOME/.hermes}/webhook_subscriptions.json`（例如命名 profile `ops` 使用 `~/.hermes/profiles/ops/webhook_subscriptions.json`）
- webhook 适配器在连接时及处理每个传入 webhook 之前检查该文件，因此无需重启。文件身份或元数据变化时会在下一次检查中加载；若这些值没有变化，则受限重读和 SHA-256 内容检查最多大约每秒一次，既能发现元数据未变化的编辑，也避免请求洪泛放大文件读取。
- `config.yaml` 中的静态路由始终优先于同名的动态订阅
- 动态订阅与静态路由使用相同的格式和功能（events、prompt 模板、skills、delivery）
- 无需重启 gateway——订阅后立即生效

### agent 驱动的订阅

agent 可通过 terminal 工具在 `webhook-subscriptions` skill 的引导下创建订阅。向 agent 请求"为 GitHub issues 设置 webhook"，它将运行相应的 `hermes webhook subscribe` 命令。

---

## 路由级 toolset {#per-route-toolsets}

Webhook agent 运行默认使用刻意收紧的 toolset（`web_search`、`web_extract`、`vision_analyze`、`clarify`），因为第三方可控的 webhook payload 可能包含 prompt 注入。公开 PR 标题或 issue 评论不应因此获得 terminal 权限。

对于发送方完全受控的可信路由，例如本机监控守护进程或内部 CI，可以只为该路由配置更宽的 toolset，而不扩大其他 webhook 路由的权限：

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

`hermes webhook subscribe` 不能授予 toolset。若要手动授予，请编辑活动 profile 的 `${HERMES_HOME:-$HOME/.hermes}/webhook_subscriptions.json`：保留 CLI 已保存的完整路由条目，加入 `toolsets`，并把 `secret` 换成从未使用过的新密钥；随后同步更新发送方。Toolset 属于密钥绑定的执行策略，复用旧 secret 会使候选路由被拒绝，并撤销已有动态路由。

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

行为与安全属性：

- 路由列表会**替换**该路由运行时的平台级 webhook toolset 解析结果，而不是与之合并。
- 名称通过与 `platform_toolsets` 相同的路径验证；未知或受平台限制的 toolset 会被移除。
- `hermes webhook subscribe` 有意不提供 toolset 标志。提升权限必须完整地手动编辑配置并轮换新 secret，因此运行时自行创建订阅的 agent 不能自行授予 `terminal`。
- 只应为发送方完全受控且使用真实 HMAC secret 的路由授予提升后的 toolset。任何能向该路由发送有效签名 payload 的主体，实际上都能让 agent 使用这些工具。

---

## 安全性 {#security}

webhook 适配器包含多层安全机制：

### HMAC 签名验证

适配器使用适合各来源的方式验证传入的 webhook 签名：

- **GitHub**：`X-Hub-Signature-256` 请求头——以 `sha256=` 为前缀的 HMAC-SHA256 十六进制摘要
- **GitLab**：`X-Gitlab-Token` 请求头——明文 secret 字符串匹配
- **通用（V2，推荐）**：`X-Webhook-Signature-V2` 与 `X-Webhook-Timestamp` 请求头——对 `<timestamp>.<body>` 计算 HMAC-SHA256；时间戳必须在服务器时钟前后 300 秒内。
- **通用（V1，旧版）**：`X-Webhook-Signature` 请求头——仅对正文计算 HMAC-SHA256，没有签名时间戳或 nonce。持久化账本会永久阻止同一已认证正文再次执行，但这也会把内容完全相同的合法 V1 请求合并为同一身份；请迁移到 V2。

若已配置 secret 但请求中不存在已识别的签名请求头，则请求被拒绝。

### Secret 为必填项

每个路由都必须有 secret。全局 `secret` 只可作为**恰好一条已认证路由**的便利回退值；若两条已认证路由都继承它，就会跨权限域复用密钥材料，因此启动会失败关闭。存在多条已认证路由时，请为每条路由配置唯一 secret。仅用于开发/测试时，可将 secret 设为 `"INSECURE_NO_AUTH"` 以完全跳过验证。

认证密钥材料会在根级持久权限中跨所有 profile 永久绑定。绑定范围包括物理 profile 实例、路由名称、provider、签名模式以及完整的非 secret 执行策略：规范化路由、解析后的 toolset、完整捕获的脚本执行契约（源码字节、解释器与隔离调用方式、最小环境及空白工作目录规则）、快照化的 skill 框架、已捕获的 `in_file` 过滤值，以及冻结的投递目标权限。重命名或移动路由、更换任一依赖或验证器、删除并重建 profile，或启动另一个 profile，都不能重新分配既有密钥。当绑定的文件、profile、授权、skill 或目标不再匹配已发布快照时，Hermes 会撤销该在线路由。任何绑定字段或策略发生变化时，都必须轮换为全新 secret；旧绑定会继续作为重放证据保存，不能复用。

路由的 `profile` 字段在所有 gateway 模式下都会把 secret 绑定到一个执行目标。未设置 `profile` 的路由仅属于默认 profile；显式命名 profile 始终要求匹配的 `/p/<profile>/` 前缀。单 profile gateway 在该前缀中只接受自身名称，而 `gateway.multiplex_profiles` 允许一个 gateway 服务多个获准 profile。即使路由签名有效，URL 前缀与路由绑定不匹配时请求仍会被拒绝。

`INSECURE_NO_AUTH` 仅在 gateway 绑定到回环地址（`127.0.0.1`、`localhost`、`::1`）时被接受。若与非回环绑定（如 `0.0.0.0` 或局域网 IP）组合使用，适配器拒绝启动——这可防止在公共接口上意外暴露未认证的端点。

### 速率限制

每个 profile/路由作用域默认在**滑动 60 秒窗口内最多准入 30 个新身份**。可全局配置该配额：

```yaml
platforms:
  webhook:
    extra:
      rate_limit: 60  # 每个滑动 60 秒窗口的新身份数
```

`rate_limit` 必须是 1 到 10,000 之间的精确整数。`script_timeout_seconds` 在同一个 `extra` 块中配置，必须是 1 到 300 秒之间的精确整数。无效值会使 webhook 以配置错误启动失败，而不会被静默改写。

超过配额的新身份会收到 `429 Too Many Requests`。完全重复、冲突和已处于活动状态的身份会先由持久重放证据解析，不会再次消耗配额，因此重试洪泛无法挤占新任务。

### 幂等性

已认证的服务商投递 ID 会成为永久重放身份。没有已签名投递 ID 的服务商使用已认证的“签名时间戳 + 正文”组合；旧版仅正文 HMAC 则使用已认证正文摘要。已完成和不确定结果的凭证会跨 gateway 重启持久保存，不会为了接收新任务而淘汰。只有显式的本机回环测试绕过具有一小时重放窗口。

重放账本位于稳定 Hermes 根目录的 `state.db`，而不是命名 profile 的本地数据库。标准布局使用 `~/.hermes/state.db`；若活动 `HERMES_HOME` 为 `<root>/profiles/<name>`，Hermes 会把账本解析为 `<root>/state.db`。因此，gateway 在 multiplex 与非 multiplex 模式之间切换时，同一证据仍然有效。在这个根级账本内，操作与重放权限按实际执行路由的有效物理 profile 分区；所以命名单 profile 进程中的字面 `default` 路由属于该命名物理 profile，而不是单独的默认分区。

每个物理 profile 主目录都包含一个持久化随机 `.webhook-profile-incarnation` token。Hermes 从解析后的主目录及该 token 派生路由的 profile generation，把它捕获到持久授权中，并在脚本、agent、目标和恢复副作用开始前再次检查。仅复用名称来重建或替换 profile，不能继承旧任务。失效 owner 恢复同样遵守该边界：适配器只能核对当前由它服务且 generation 仍然有效的物理 profile 行；其他 profile 的行保持不动，等待有权的适配器处理。

账本会在脚本、agent 或目标副作用开始前预留最坏情况存储。`idempotency_max_storage_bytes` 默认为 1 GiB，必须是 5 MiB 至 64 GiB 范围内的精确整数；单个 profile/route/provider 无法占用全局预留空间。`idempotency_max_entries` 默认允许 4096 个活动操作。两个上限都会作为账本权限持久化；用不同值重新打开同一账本时启动失败，而不会静默改变准入语义。这些限制约束共享 `state.db` 内 webhook 账本的逻辑分配，并不限制整个数据库文件或 WAL 的物理大小。永久凭证耗尽预算后，新的唯一身份会以 HTTP 503 失败关闭，而既有身份仍可得到精确的重复/冲突判定。

### 请求体大小限制

`max_body_bytes` 默认为且不能超过 **1 MiB（1,048,576 字节）**。它必须是 1 到 1,048,576 之间的精确整数；布尔值、小数和更大的上限都会在启动时被拒绝。声明的 `Content-Length` 超限会在读取前拒绝；分块传输或其他无长度请求体则会在受限读取过程中一旦越界立即中止。

```yaml
platforms:
  webhook:
    extra:
      max_body_bytes: 1048576  # 默认值及硬上限：1 MiB
```

认证后的载体还有独立上限：渲染 prompt 最多 **512 KiB**，完整持久事件快照最多 **2 MiB**，每个持久目标或 tool grant 权限快照最多 **64 KiB**。若模板在任何已配置脚本或下游副作用开始前扩展超限，Hermes 会返回 HTTP 413 并释放操作声明。若已启动的脚本产生超限载体，Hermes 会改为记录不确定结果并返回 HTTP 500，而不会错误地认为可安全重试。

### Prompt 注入风险

:::warning
Webhook payload 包含攻击者可控的数据——PR 标题、commit 消息、issue 描述等均可能包含恶意指令。在暴露于互联网时，请在沙箱环境（Docker、VM）中运行 gateway。考虑使用 Docker 或 SSH terminal 后端进行隔离。
:::

---

## 故障排查 {#troubleshooting}

### Webhook 未到达

- 验证端口已暴露且可从 webhook 来源访问
- 检查防火墙规则——端口 `8644`（或你配置的端口）必须开放
- 验证 URL 路径是否匹配路由绑定：默认路由使用 `/webhooks/<route-name>`；任何显式命名 profile 在单 profile 和 multiplex 模式下都使用 `/p/<profile>/webhooks/<route-name>`
- 使用 `/health` 端点确认服务器正在运行

### 签名验证失败

- 确保路由配置中的 secret 与 webhook 来源中配置的 secret 完全一致
- 对于 GitHub，secret 基于 HMAC——检查 `X-Hub-Signature-256`
- 对于 GitLab，secret 为明文 token 匹配——检查 `X-Gitlab-Token`
- 检查 gateway 日志中的 `Invalid signature` 警告

### 事件被拒绝或忽略

- 检查事件类型是否与路由的 `events` 条目完全一致。路由绑定的 GitHub/GitLab 事件不匹配时会返回 `401`，而不是被忽略。
- GitHub 路由绑定支持 `check_run`、`pull_request`、`push`、`issues` 和 `ping`。`X-GitHub-Event` 值与已认证 JSON 正文结构必须同时匹配；修改已签名正文旁边的未签名请求头会返回 `401`。
- GitLab 事件使用精确的 `X-GitLab-Event` 请求头值，例如 `Merge Request Hook`、`Push Hook`，而不是 `merge_request` 等 payload 值
- 若 `events` 为空或未设置，请求不按事件过滤。路由绑定的 GitHub/GitLab 请求解析为 `event=unknown`；generic、Stripe 等以已认证正文为准的服务商仍可从 payload 字段解析事件。

### Agent 未响应

- 在前台运行 gateway 以查看日志：`hermes gateway run`
- 检查 prompt 模板是否正确渲染
- 验证投递目标已配置并连接

### 重复响应

- 检查重试是否保持签名实际覆盖的服务商材料，例如 `svix-id`/`webhook-id`、已认证正文 ID，或完全相同的已签名“时间戳 + 正文”组合。未签名的诊断请求头不会控制重放身份。
- 检查 gateway 日志中的持久化账本或结算错误。重放凭证会持久保存，并非一小时的进程内缓存。

### `gh` CLI 错误（GitHub 评论投递）

- 在 gateway 主机上运行 `gh auth login`
- 确保已认证的 GitHub 用户对该仓库有写权限
- 检查 `gh` 是否已安装并在 PATH 中

---

## 环境变量 {#environment-variables}

| 变量 | 描述 | 默认值 |
|----------|-------------|---------|
| `WEBHOOK_ENABLED` | 启用 webhook 平台适配器 | `false` |
| `WEBHOOK_PORT` | 接收 webhook 的 HTTP 服务器端口 | `8644` |
| `WEBHOOK_SECRET` | 仅供恰好一条已认证路由使用的全局 HMAC 回退值；多路由必须各用唯一 secret | _（无）_ |
