# Local agents cron UI driver plan

## Goal
Build the local Agents overlay into a cohesive profile/job board: all local Hermes profiles appear automatically, selecting a profile opens an agent detail view, and each detail view lists that profile's detected cron jobs with useful default metadata-derived cards until a custom job-specific UI exists.

## Source truth inspected
- Desktop overlay routes: `apps/desktop/src/app/contrib/wiring.tsx`, `apps/desktop/src/app/routes.ts`, `apps/desktop/src/app/agents/index.tsx`.
- Desktop API client/types: `apps/desktop/src/hermes.ts`, `apps/desktop/src/types/hermes.ts`.
- Existing cron UI helpers: `apps/desktop/src/app/cron/index.tsx`, `apps/desktop/src/app/cron/job-state.ts`.
- Backend endpoints already available: `GET /api/profiles` in `hermes_cli/web_routers/profiles.py`, `GET /api/cron/jobs?profile=<name|all>` in `hermes_cli/web_routers/cron.py`.

## Architecture
Use the existing driver-shaped separation: backend owns durable profile/cron state; desktop renderer runs bounded read activities (`getProfiles`, `getCronJobs(profile)`) and derives default UI summaries locally. Do not add fuzzy `SELF` routes or hidden state. New jobs are auto-detected because each profile detail fetches from `cron/jobs.json` via the existing endpoint.

## Slice 1: local-agent discovery + default cron/job view
Files:
- Modify `apps/desktop/src/app/agents/index.tsx`.
- Add focused tests in `apps/desktop/src/app/agents/index.test.tsx`.

Tasks:
1. Keep existing subagent tree visible as the live-work section.
2. Add a local profile board section populated by `getProfiles()`.
3. Selecting a profile opens an in-overlay detail page/card for that profile.
4. Detail page calls `getCronJobs(profile.name)` and lists jobs.
5. Add default metadata-derived job cards: title, state, schedule, next/last run, delivery, model/provider, script/no-agent flags, prompt preview, last error.
6. Empty/error/loading states must be explicit.

Validation:
- `npm run test:ui -- apps/desktop/src/app/agents/index.test.tsx` if Vitest filtering works; otherwise `npm run test:ui -- --runInBand` is not expected in Vitest, so run `npx vitest run --project ui apps/desktop/src/app/agents/index.test.tsx`.
- `npm run typecheck` or at least `npx tsc -p tsconfig.json --noEmit` for renderer type safety.

## Slice 2: route/deep-link polish
Files likely:
- `apps/desktop/src/app/agents/index.tsx`
- optional route helpers if maintainers want `/agents/<profile>` instead of in-overlay state.

Tasks:
- Preserve selected profile in query/hash if accepted by reviewer.
- Add sidebar/nav link semantics if needed.

## Slice 3: custom views / cron optimizations
Files likely:
- `apps/desktop/src/app/agents/cron-job-views.tsx` (new)
- `cron/suggestion_catalog.py` or job metadata producers only if needed.

Tasks:
- Detect known local cron patterns by stable metadata (`script`, `skills`, `name`, `schedule.kind`, `no_agent`).
- Add custom renderers for known built-ins such as curator/skill maintenance, gateway health, inbox urgency classifiers, and software durable-goal loops when metadata is present.
- If metadata is insufficient, add non-breaking backend fields that make custom views stable without parsing prompts.

## Review gate
Open a GitHub PR per slice when repository permission allows, request el-micaiah review, fix requested changes, merge only after approval/mergeability/check verification.

## Risks
- `origin` is `NousResearch/hermes-agent`; push/PR permission may be blocked. If so, push a branch to an accessible fork or report exact authority blocker.
- Existing `AgentsView` currently means subagents, not local profiles. Keep old functionality to avoid regression.
- Full desktop build may be too heavy for a cron tick; run focused tests/typechecks first, then broader checks before merge.
