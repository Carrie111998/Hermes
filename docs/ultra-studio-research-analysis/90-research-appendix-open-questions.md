# Research Appendix and Open Questions

Status: evidence appendix  
Date: 2026-06-11

## Threads Summary

Four read-only Codex threads were used.

| Thread | Output used |
|---|---|
| docs-audit | Current docs are broad, but not a clean P0 pack. Found endpoint/event drift and P0/P1 conflicts. |
| code-reality | Web chat/upload/Atlas providers exist; media job and asset persistence do not. |
| reference-research | Borrow skills, async job contracts, narrow browser/sandbox ideas; defer cloud infra. |
| synthesis | Recommended six paired md/html docs with P0 separated from cloud references. |

## External Research

| Source | Finding for Ultra Studio |
|---|---|
| Manus Agent Skills | Skills are workflow packages with progressive disclosure and execution, not just prompts. |
| Manus Cloud Browser | Browser automation can use logged-in sessions and human take-over; this is later for Ultra P0. |
| E2B Sandbox | Sandbox lifecycle, filesystem, commands, volumes, and metrics are useful references, not mandatory P0. |
| Browserbase Contexts | Persisted browser contexts are useful when authenticated browser automation becomes real. |
| OpenAI Computer-Using Agent | GUI agents need confirmation for sensitive actions and are not a substitute for APIs. |
| Anthropic computer use | Computer-use is a tool surface with screenshots/actions; fallibility must be explicit. |
| Temporal | Durable execution is valuable for crash/retry/resume, but a DB-backed job table is enough for P0. |
| LangGraph | Good reference for stateful agent workflows and human-in-loop; not required for first media loop. |
| async media APIs | Runway/Replicate/fal patterns support a durable `job -> status -> output asset` contract. |

Source links:

- Manus Agent Skills: https://manus.im/features/agent-skills
- Manus Cloud Browser: https://manus.im/docs/features/cloud-browser
- E2B docs: https://e2b.dev/docs
- Browserbase Contexts: https://docs.browserbase.com/platform/browser/core-features/contexts
- OpenAI Computer-Using Agent: https://openai.com/index/computer-using-agent/
- Anthropic computer use: https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
- Temporal docs: https://docs.temporal.io/
- LangGraph overview: https://docs.langchain.com/oss/python/langgraph/overview
- fal Queue API: https://fal.ai/docs/documentation/model-apis/inference/queue

## Local Evidence

| Evidence | Meaning |
|---|---|
| `web/src/pages/ChatPage.tsx` | Current web chat exists. |
| `web/src/hooks/useGatewayChat.ts` | Uses session and prompt submit gateway flow. |
| `hermes_cli/web_server.py` | Has `/api/pty`, `/api/ws`, `/api/pub`, `/api/events`, plugin routes. |
| `hermes_cli/dashboard_uploads.py` | Uploads are real but return file metadata, not typed assets. |
| `plugins/image_gen/atlas/` | Atlas image provider exists. |
| `plugins/video_gen/atlas/` | Atlas video provider exists and polls prediction status. |
| `tools/image_generation_tool.py` | Image tool dispatch path exists. |
| `tools/video_generation_tool.py` | Video tool dispatch path exists. |
| `skills/creative/workflow-router/SKILL.md` | Router exists as skill prompt. |
| `hermes_cli/ultra_studio_skills.py` | Ultra allowlist helper exists but needs startup wiring. |
| `hermes_state.py` | Sessions/messages exist; media job/asset tables are missing. |

## Drift Found

| Drift | Resolution |
|---|---|
| `/api/sessions` vs `/sessions` | Use `/api/...` in final P0 docs. |
| `media_job.*` vs `job.*` | Use `media_job.created`, `media_job.updated`, `asset.ready`. |
| Product/photo/UGC as P0 vs P1 | P0 skill list is only router, infographic, media QA, prompt repair. |
| Element/Character listed in top-level acceptance | Move to P1; P0 may show disabled/planned actions only. |
| Approval protocol variants | P0 only needs confirm for delete/retry/spend; full approval lifecycle later. |
| PTY vs React chat baseline | P0 uses existing React `/api/ws` chat. PTY remains available but not baseline. |

## Open Questions

These block implementation choices, not architecture:

1. Which exact Atlas video family should be the P0 default?
2. Should P0 store `media_jobs` and `media_assets` in existing SQLite `SessionDB`, or a separate local DB module?
3. Should uploads immediately become assets, or only after the user attaches them to a prompt?
4. Should `video_generate` stay blocking while polling for P0, or move directly to a background worker?
5. Which UI route is canonical for Ultra Studio: existing `/chat`, a new `/studio`, or dashboard plugin route?
6. Should HTML docs be generated from Markdown in CI, or checked in as static rendered files?

## Recommended Next Implementation Slice

1. Add tables and data access methods for `media_jobs` and `media_assets`.
2. Upgrade upload response to include `asset_id`.
3. Add job wrapper tools around existing Atlas providers.
4. Emit media events through existing gateway.
5. Render media cards in chat.
6. Add inspector detail view.
7. Add router handoff tests.
8. Add smoke test: real Atlas success or typed missing credential.

