# Reference Research

Status: research notes  
Scope: external agent infrastructure patterns plus current Hermes local architecture.

## Research Rule

这些参考只用于抽取基建边界，不能复制私有实现或营销说法。每个参考都拆成 `borrow` 和 `do_not_copy`。

## Reference Matrix

| Reference | What it shows | Borrow | Do not copy |
|---|---|---|---|
| Manus Agent Skills | Skills are executable workflow knowledge, not just prompt copy. Manus emphasizes sandbox execution and one-click skill reuse. | Treat Ultra skills as workflow packages with routing, input gates, tools, QA, and artifacts. | Do not claim private Manus internals; do not expose protected skill references. |
| Manus Cloud Browser | A useful agent needs an isolated browser with authenticated contexts for web tasks. | Add browser context lifecycle and permissioned account access as infra, not just UI. | Do not let local user Chrome be the production security boundary. |
| E2B sandboxes | Agent sandboxes need terminal, filesystem, network, and sometimes desktop/computer-use surfaces. | Model sandbox as a first-class task computer with workspace volume and scoped network. | Do not outsource tenant policy, provider secrets, or asset ACL to a generic sandbox vendor. |
| Browserbase contexts | Browser automation becomes reliable when session data can persist across browser sessions. | Separate browser context IDs from chat sessions; support explicit create/reuse/delete. | Do not make browser cookies global or silently reused across tenants/projects. |
| OpenAI Operator / CUA | Browser agents need user control, visual feedback, and takeover/confirmation boundaries. | Add human approval states for high-risk actions and external account operations. | Do not rely on model judgment alone for payment, login, posting, or destructive actions. |
| Anthropic computer use | Computer-use tools expose screenshot, mouse, and keyboard control, and are explicitly fallible. | Treat desktop/browser control as tool execution with observation, timeout, and rollback evidence. | Do not hide uncertainty; failed visual actions must surface as tool errors. |
| Temporal | Durable execution is useful when tasks must survive crashes, wait for humans, and resume from history. | Use Temporal for session/job orchestration, retries, timeouts, compensation, and approvals. | Do not put every token stream into a giant workflow; keep high-frequency events in event bus. |
| LangGraph | Agent workflows need state graphs, persistence, streaming, and human-in-the-loop. | Use it for skill workflow modeling and router execution plans. | Do not treat LangGraph as the sole durable infrastructure for media jobs and billing. |
| Async media queue APIs | Media providers frequently use submit/status/result/cancel/webhook shapes. | Standardize Atlas job tools around `create`, `status`, `result`, `asset`, `cancel`, `webhook`. | Do not expose third-party provider names as the product contract; Atlas remains the user-facing provider. |
| Existing Hermes | Hermes already has AIAgent, CLI/TUI gateway, sessions, tools, skills, and plugins. | Keep the real Hermes agent loop and TUI event catalog as local truth. | Do not carry over unrelated generic skills or local-provider credential shortcuts into Ultra cloud. |

## Agent Infrastructure Lessons

### 1. The agent needs a task computer

The target product is not only a chat UI. It needs a task computer:

- terminal for tool execution.
- browser context for web work.
- workspace files for uploads, generated outputs, and temporary assets.
- media workers for Atlas image/video jobs.
- event stream for progress, tool calls, asset previews, and reconnect.

### 2. Skills need progressive disclosure

The router should expose only public skill metadata until a skill is selected. Internal `SKILL.md`, `references/`, prompts, and tool recipes stay protected. The runtime loads relevant skill instructions only after routing and preflight.

### 3. Durable orchestration and realtime fanout are separate

Temporal owns durable facts:

- session created/resumed.
- job group started.
- media job submitted.
- approval requested/resolved.
- retry/timeout/compensation.

NATS or equivalent event bus owns realtime fanout:

- message deltas.
- tool progress.
- job progress.
- asset preview availability.
- UI reconnect replay.

The database stores projections and ledgers; it is not the realtime bus.

### 4. Browser contexts are assets with risk

Browser sessions can contain cookies and account access. They need:

- tenant/project ownership.
- explicit lifecycle.
- allowed domain policy.
- audit on use.
- revoke/delete operation.
- no silent reuse across users.

### 5. Provider queues require a stable internal job model

Atlas image/video generation should not leak provider-specific job semantics into the agent UI. The internal contract should normalize:

```text
job_id
job_type
provider_route
model
status
progress
input_asset_ids
output_asset_ids
usage_event_id
error_class
created_at
completed_at
```

## Official Source Links

- Manus Agent Skills: https://manus.im/features/agent-skills
- Manus Cloud Browser: https://manus.im/docs/features/cloud-browser
- E2B docs: https://e2b.dev/docs
- E2B coding agents: https://e2b.dev/docs/use-cases/coding-agents
- Browserbase introduction: https://docs.browserbase.com/welcome/introduction
- Browserbase contexts: https://docs.browserbase.com/platform/browser/core-features/contexts
- OpenAI Operator: https://openai.com/index/introducing-operator/
- OpenAI Computer-Using Agent: https://openai.com/index/computer-using-agent/
- Anthropic computer use tool: https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
- Temporal docs: https://docs.temporal.io/
- Temporal for AI: https://temporal.io/solutions/ai
- LangGraph overview: https://docs.langchain.com/oss/python/langgraph/overview
- LangGraph workflows and agents: https://docs.langchain.com/oss/python/langgraph/workflows-agents
- fal async queue docs, research only: https://fal.ai/docs/documentation/model-apis/inference/queue

## Internal Source Links

- [Existing Hermes architecture guide](../../AGENTS.md)
- [Open-source architecture selection](../open-source-architecture/00-index.html)
- [TokenRouter credential flow](../hermes-tokenrouter-credential-flow.md)
- [CometAPI media gateway](../hermes-cometapi-media-gateway.md)
- [Ultra Studio product specs](../ultra-studio-product-specs/00-index.md)

