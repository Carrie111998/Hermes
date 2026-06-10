# Ultra Studio Product Surface

Status: product and UX specification  
Date: 2026-06-10

## Product Intent

Ultra Studio is a creative agent UI for making images, videos, motion graphics,
ads, reusable assets, and characters through real Hermes tools and Atlas models.

The product should feel closer to a creative task computer than a chat wrapper.
The chat is only one surface. Users also need files, memory, marketplace, tasks,
assets, and a live inspector.

## Primary Users

1. Solo creator making short videos, product clips, UGC ads, and reels.
2. Brand/operator reusing products, people, references, logos, and style systems.
3. Power user iterating prompts, comparing models, inspecting failures, and
   collecting reusable assets.

## Main Jobs

| Job | User phrasing | Product responsibility |
|---|---|---|
| Generate media | "Make a cat video", "Generate a product photo" | Route, ask missing fields, run Atlas job, show media card. |
| Edit with references | "Use this image as style", "Animate this product" | Upload, classify asset role, compile provider payload. |
| Reuse assets | "Use the same character", "Use this logo" | Picker, asset refs, element/character linkage. |
| Inspect output | "Why did this fail?", "Download this" | Inspector shows job, status, error, QA, download. |
| Build workflow | "Make UGC", "Make infographicMD" | Marketplace/skills expose available workflows. |
| Continue work | "Open the previous cat task" | Tasks and history restore session plus artifacts. |

## Information Architecture

```text
Ultra Studio
├── New task
├── Search
├── My office
├── Marketplace
├── Files
├── Memory
├── Tasks
│   └── recent sessions / projects / jobs
└── Pricing / account
```

### Left Nav Shell

The left nav is not a decorative sidebar. It is the way users access product
state that does not fit inside a single chat transcript.

Required entries:

- `New task`: starts a new creative session.
- `Search`: searches sessions, files, assets, memory, and marketplace items.
- `My office`: workspace home, recent work, shared projects.
- `Marketplace`: skills, templates, workflow packs, model recipes.
- `Files`: uploaded media, task files, generated artifacts.
- `Memory`: persistent project/user memory, brand facts, preferences.
- `Tasks`: sessions and running/completed creative jobs.

### Center: Creative Session

The center area is the conversation and generation workspace.

It must support:

- Streaming assistant text.
- User text input.
- File upload.
- Model picker.
- Tool status.
- Media cards.
- Ask-user-question cards.
- Error cards with actionable recovery.

The session must not auto-generate media on open. User intent drives execution.

### Right: Inspector / Live Panel

The inspector is a context panel for the currently selected job, asset, or tool
run.

It should show:

- Current job status and progress.
- Provider/model and input constraints.
- Selected asset preview.
- Prompt, seed, dimensions, duration, and lineage.
- QA result and observed evidence.
- Download/export actions.
- Convert to Element.
- Create Character.
- Retry/repair plan when generation fails.

The inspector is not a second chat. It is closer to an IDE inspector or Figma
properties panel.

## Required States

| State | Center behavior | Inspector behavior |
|---|---|---|
| Empty | Prompt input and suggested tasks. | Nothing selected. |
| Thinking | Stream route/plan text. | Show current reasoning phase. |
| Waiting for user | Render structured question. | Show missing field context. |
| Creating | Show job card with progress. | Show job details, model, inputs. |
| Complete | Show media card and summary. | Show asset details and actions. |
| Failed | Show typed error and retry option. | Show provider error, logs, repair plan. |

## Non-Goals

- Do not expose raw provider dashboards in the main UI.
- Do not show internal prompt templates by default.
- Do not use a fake run/status panel that is disconnected from Hermes events.
- Do not merge Marketplace, Memory, and Files into one generic "Assets" page.

