# Ultra Studio Research Analysis Index

Status: final research synthesis  
Date: 2026-06-11  
Method: Codex threads, local repo inspection, and current official-source check.

## Verdict

Ultra Studio should not start as a cloud supercomputer platform. The P0 should
be a real creative skill-runner:

```text
web chat
  -> upload or text request
  -> workflow-router
  -> Atlas image/video tool
  -> durable media_job
  -> typed asset
  -> streamed UI status
  -> rendered image/video card
```

Everything else is reference or later work until this loop is real.

## Result Documents

Each result has both Markdown and HTML.

| Result | Markdown | HTML | Purpose |
|---|---|---|---|
| Architecture and decision index | [00-index.md](00-index.md) | [00-index.html](00-index.html) | Canonical verdict, source map, and status truth. |
| P0 MVP vertical slice | [01-p0-mvp-vertical-slice.md](01-p0-mvp-vertical-slice.md) | [01-p0-mvp-vertical-slice.html](01-p0-mvp-vertical-slice.html) | What to build first and what not to build. |
| P0 agent, skill, tool, media contracts | [02-p0-agent-skill-tool-media-contracts.md](02-p0-agent-skill-tool-media-contracts.md) | [02-p0-agent-skill-tool-media-contracts.html](02-p0-agent-skill-tool-media-contracts.html) | Canonical endpoints, events, state enums, typed errors, and Atlas tool contract. |
| P0 security and credential boundaries | [03-p0-security-credential-boundaries.md](03-p0-security-credential-boundaries.md) | [03-p0-security-credential-boundaries.html](03-p0-security-credential-boundaries.html) | Minimal security needed for P0 without overbuilding cloud infra. |
| Later cloud infra roadmap | [04-later-cloud-infra-roadmap.md](04-later-cloud-infra-roadmap.md) | [04-later-cloud-infra-roadmap.html](04-later-cloud-infra-roadmap.html) | What moves to P1/P2 and why it is not P0. |
| Research appendix and open questions | [90-research-appendix-open-questions.md](90-research-appendix-open-questions.md) | [90-research-appendix-open-questions.html](90-research-appendix-open-questions.html) | Threads findings, external references, drift list, and open decisions. |

## Authoritative Sources

For implementation, read in this order:

1. [01-p0-mvp-vertical-slice.md](01-p0-mvp-vertical-slice.md)
2. [02-p0-agent-skill-tool-media-contracts.md](02-p0-agent-skill-tool-media-contracts.md)
3. [03-p0-security-credential-boundaries.md](03-p0-security-credential-boundaries.md)
4. [04-later-cloud-infra-roadmap.md](04-later-cloud-infra-roadmap.md)

The earlier documents remain source material, not final P0 authority:

- [Ultra Studio product specs](../ultra-studio-product-specs/00-index.md)
- [Ultra Studio infra design](../ultra-studio-infra-design/00-index.md)
- [Ultra Studio architecture diagram](../ultra-studio-agent-architecture.html)
- [Open-source architecture selection](../open-source-architecture/00-index.html)

## Thread Results Incorporated

| Lane | Finding |
|---|---|
| docs-audit | Docs are broad and honest, but P0/P1/P2 are mixed; endpoint/event names drift. |
| code-reality | Web chat, uploads, streaming, Atlas image/video providers exist; typed media jobs/assets do not. |
| reference-research | Borrow skill progressive disclosure and async media jobs; defer full sandbox/browser/cloud orchestration. |
| synthesis | Final pack should be small, paired md/html, and split P0 from cloud references. |

## Current Truth

| Capability | Status | Current evidence |
|---|---|---|
| React web chat over `/api/ws` | Green / Yellow | `web/src/pages/ChatPage.tsx`, `web/src/hooks/useGatewayChat.ts`, `hermes_cli/web_server.py` |
| PTY endpoint | Green | `/api/pty` exists, but current `/chat` is not PTY/xterm-based. |
| Upload route | Yellow | `hermes_cli/dashboard_uploads.py` stores files, but does not create typed `asset_id`. |
| Chat/tool streaming | Yellow | Generic `message.delta`, `message.complete`, `tool.start`, `tool.complete` exist. |
| Media-specific events | Red | `media_job.created`, `media_job.updated`, `asset.ready` are not implemented. |
| Atlas image provider | Green | `plugins/image_gen/atlas/` and `tools/image_generation_tool.py` dispatch. |
| Atlas video provider | Green | `plugins/video_gen/atlas/` submits and polls Atlas prediction IDs. |
| Workflow router | Yellow | `skills/creative/workflow-router/SKILL.md` exists as prompt-level routing. |
| Durable media jobs/assets | Red | No `media_jobs` or `media_assets` store in `hermes_state.py`. |

## Decision Log

| Decision | Result |
|---|---|
| P0 chat baseline | Use the existing React `/api/ws` web chat. Do not pivot back to PTY as P0. |
| Endpoint namespace | Use `/api/...` consistently for web-facing P0 endpoints. |
| Event vocabulary | Use `media_job.*` and `asset.ready` as the canonical media event names. |
| P0 skills | `workflow-router`, `infographic-md-flow`, `media-qa`, `prompt-repair`. Product/photo/UGC flows are P1 unless implemented. |
| Element/Character | Not P0. They can appear as disabled or planned actions, not acceptance blockers. |
| Cloud infra | TokenRouter/Temporal/NATS/Kata/Kueue/etc. are later/reference until P0 media loop is stable. |

## Completion Standard

Documentation is complete enough when:

- every final result has both `.md` and `.html`;
- P0 scope and P0 non-goals are explicit;
- endpoint names and event names are canonical;
- media job and asset schemas are concrete;
- security boundaries are enough to prevent fake outputs and provider-key leakage;
- P1/P2 infrastructure is clearly separated from P0;
- each acceptance case can become a test or smoke command.

