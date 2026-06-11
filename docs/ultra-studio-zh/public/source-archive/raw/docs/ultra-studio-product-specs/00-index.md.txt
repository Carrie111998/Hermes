# Ultra Studio Product Spec Pack

Status: working specification pack  
Date: 2026-06-10  
Scope: Ultra Studio as an Atlas-first creative agent built on the Hermes fork.

## Why This Is Not One PRD

Ultra Studio is not one feature. It is a product surface, agent runtime, media
job system, asset library, memory layer, marketplace, and skill runtime working
together.

A single PRD would hide important boundaries. This pack splits the work by
decision surface so engineering, design, and product can review the right
document without reading everything.

## Document Map

| Doc | Purpose | Primary reader |
|---|---|---|
| [01-product-surface.md](01-product-surface.md) | Product shell, user jobs, left nav, chat, inspector. | Product + design |
| [02-agent-runtime-contract.md](02-agent-runtime-contract.md) | Session, gateway, sandbox, event stream, approval lifecycle. | Backend + agent runtime |
| [03-media-asset-contract.md](03-media-asset-contract.md) | Media jobs, assets, lineage, downloads, QA, characters/elements. | Backend + frontend |
| [04-skill-tool-prompt-contract.md](04-skill-tool-prompt-contract.md) | Workflow router, skills, tools, prompt compiler, clarification rules. | Agent + workflow engineering |
| [05-memory-marketplace-files.md](05-memory-marketplace-files.md) | Memory, Marketplace, Files, task filesystem, skill/templates catalog. | Product + platform |
| [06-delivery-plan.md](06-delivery-plan.md) | Milestones, P0/P1/P2, acceptance checks, launch gates. | Everyone |
| [components/README.md](components/README.md) | Per-component complete functional specs (19 components). | Engineering |
| [../ultra-studio-zh/visual-guide.html](../ultra-studio-zh/visual-guide.html) | 中文可视化导读：P0 闭环、三栏界面、系统分层和阅读路线。 | Everyone |
| [../ultra-studio-docs-zh/README.html](../ultra-studio-docs-zh/README.html) | 中文串联文档站，连接产品规格、组件、调研、基建和架构附录。 | Product + engineering |
| [components-zh/README.html](components-zh/README.html) | 中文可读版组件规格，按状态、缺口、P0 和验收点整理。 | Product + engineering |

## Source References

Use these as the current source material:

- [Ultra Studio architecture diagram](../ultra-studio-agent-architecture.html)
- [Final research analysis pack](../ultra-studio-research-analysis/00-index.md)
- [Infrastructure design research pack](../ultra-studio-infra-design/00-index.md)
- [Full research appendix](../ultra-studio-agent-research-appendix.html)
- [Manus gap research](../ultra-studio-agent-manus-gap-research.md)
- [Skill/tool/prompt specification](../ultra-studio-agent-skill-tool-prompt-design.md)
- [Real chat agent UI contract](../hermes-real-chat-agent-ui.md)
- [Asset library backend design](../hermes-asset-library-backend-design.md)
- [TokenRouter credential flow](../hermes-tokenrouter-credential-flow.md)
- [CometAPI media gateway](../hermes-cometapi-media-gateway.md)

## Product Shape

Ultra Studio is a creative task computer:

```text
left nav shell
  + creative chat
  + inspector/live panel
  + sandbox/task filesystem
  + browser contexts
  + durable media jobs
  + human approvals
  + skill registry
  + provider constraints
  + artifact/provenance ledgers
  + asset library
```

## Top-Level Acceptance

The product is real enough when:

- A user can start a creative session from web chat.
- The system can route the request to the right skill or ask one useful missing
  field question.
- Uploaded media becomes typed assets, not plain prompt text.
- Atlas image/video jobs create real job records and final assets.
- The UI streams thinking/status/tool/media events instead of freezing.
- The inspector shows the current job, selected asset, QA evidence, download,
  element creation, and character creation.
- Memory, Marketplace, Files, and Tasks exist as first-class navigation surfaces.
- The agent cannot claim completion without an artifact, observation, or ledger
  record.

## Out Of Scope For This Pack

- Upstream Hermes contribution strategy.
- Pricing page copy.
- Public marketing site.
- Provider onboarding outside Atlas, unless used as constraints research.
- Fake demo flows.
