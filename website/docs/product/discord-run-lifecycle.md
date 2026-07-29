---
title: "Discord Run Lifecycle"
description: "Product contract for visible Discord agent run state"
---

# Discord Run Lifecycle

## Product

Hermes exposes a durable, in-thread lifecycle for every Discord agent run that has a real thread destination, so a user can tell whether the agent accepted the request, is still working, and how the run ended without relying on Discord's transient typing indicator.

## User story

As a Discord user working with Hermes in a thread, I can see an immediate start marker and a final bottom-of-thread terminal marker, so I never have to infer run state from typing presence, reactions on the parent message, or an earlier edited progress bubble.

## Required behavior

1. Once a Discord request is accepted for agent execution, Hermes posts a new message in the run's actual destination thread: `⏳ Run started`.
2. Existing typing indicators, processing reactions, streamed output, and tool-progress messages remain available as secondary feedback.
3. The normal answer and generated output are delivered before the terminal marker.
4. Hermes then appends exactly one new terminal message at the bottom of the same thread:
   - `✅ Run complete · <elapsed>` for success;
   - `⏹️ Run stopped · <elapsed>` for user interruption or cancellation;
   - `❌ Run failed · <elapsed>` for an execution failure;
   - `⚠️ Run timed out · <elapsed>` for a timeout.
5. A safe, short reason may follow stopped, failed, or timed-out states. Internal exception details, credentials, and provider payloads must not be exposed.
6. Lifecycle delivery is best-effort and must never replace, suppress, or change the run's actual answer or outcome.
7. This contract applies to Discord only. Other messaging platforms retain their existing behavior unless they adopt an equivalent contract explicitly.

## Boundaries and non-goals

- The typing indicator is not made authoritative; Discord may hide it.
- Reactions remain enabled and retain their existing meaning.
- The start and terminal messages are non-conversational gateway metadata and must not become assistant transcript content or history boundaries.
- This feature does not add job persistence or imply that a run survives gateway process termination.
- This feature does not expose chain-of-thought or private reasoning.

## Acceptance criteria

- A Discord thread receives the start marker before model/tool output.
- Successful output is followed by one fresh completion marker.
- Cancellation/interruption, failure, and timeout each produce the matching fresh terminal marker.
- Lifecycle messages target the actual thread rather than only the parent-channel starter message.
- Duplicate completion callbacks cannot produce duplicate terminal markers.
- Lifecycle send failures do not alter the underlying run outcome.
- Existing Discord reaction, typing, streaming, and response tests remain green.
