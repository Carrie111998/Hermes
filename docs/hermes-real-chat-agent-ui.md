# Hermes Real Chat Agent UI

This document replaces the earlier MVP/plugin demo plan. The dashboard chat page is a real Hermes agent client, not a video demo surface and not a fake job runner.

## Contract

- The user opens the dashboard through `hermes dashboard --tui`.
- `/chat` renders a React chat shell only after the server injects `window.__HERMES_DASHBOARD_EMBEDDED_CHAT__ = true`.
- The browser connects to the existing Hermes gateway WebSocket at `/api/ws` through `GatewayClient`.
- A new chat calls `session.create`; a resumed chat calls `session.resume`.
- User text calls `prompt.submit`; slash commands call `slash.exec`.
- Gateway events render the transcript and side panel: `message.start`, `message.delta`, `message.complete`, `status.update`, `tool.start`, `tool.progress`, `tool.complete`, and pending prompt events.

## Media Attachment Flow

- The paperclip button opens the browser file picker.
- The browser uploads the selected image, video, or audio file to `POST /api/chat/uploads`.
- The dashboard backend stores the file under Hermes home at `dashboard-uploads/<date>/...` and returns the real local path.
- The frontend sends that path into the existing gateway attachment flow:
  - `input.detect_drop` for image or non-image file references
  - `image.attach` only when the path is an image path that needs explicit image attach handling
- The eventual `prompt.submit` text includes the real attachment marker returned by the gateway.

## References and Asset UI

- User attachments are session-level attached references; they are not Skill internal `references/`.
- Persistent media references such as `soul_id`, `element_id`, `media_input`, `image_job`, and `video_job` should render as project assets or side-panel entities, not as raw prompt text only.
- Skill internal `references/` are protected operational content and must not be exposed through the dashboard file browser or export UI.
- Entity prompts from `ask_user_question` need picker UI for `soul_id`, `element`, `voice`, and `language` once those tools are enabled.

## Non-Goals

- Do not auto-start video generation when the user opens chat.
- Do not hardcode prompts, jobs, assets, model decisions, or video briefs.
- Do not route through dashboard plugin job endpoints.
- Do not fake Atlas responses. Model/provider selection remains the normal Hermes runtime configuration path.
