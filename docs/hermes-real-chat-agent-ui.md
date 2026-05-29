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

## Non-Goals

- Do not auto-start video generation when the user opens chat.
- Do not hardcode prompts, jobs, assets, model decisions, or video briefs.
- Do not route through dashboard plugin job endpoints.
- Do not fake Atlas responses. Model/provider selection remains the normal Hermes runtime configuration path.
