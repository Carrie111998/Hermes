# Slack Interactive Replies Design

**Date:** 2026-07-26
**Status:** approved for design; implementation pending written-spec review

## Purpose

Render Hermes's existing `[[slack_buttons: Label:action_id, ...]]` reply directive as real Slack Block Kit buttons and safely route a click back to the originating agent conversation. The work fixes a Slack transport boundary only. It does not add a Sorpio integration, change enrollment policy, send outreach, or grant an action click any execution privilege.

## Scope

The feature applies to Slack messages emitted through both supported paths:

1. `SlackAdapter.send`, used for gateway replies and cron deliveries.
2. The direct Slack branch of `send_message`, used for one-shot cross-platform sends.

Other platforms retain their current text-only behavior. Malformed directives remain visible as ordinary text rather than being interpreted.

## Design

### Directive parsing and rendering

Introduce a small pure helper that recognizes exactly one or more well-formed button directives, strips them from the visible reply text, and returns validated button specifications. A specification has a short Slack label and an action name matching a conservative identifier grammar. The renderer limits the number of buttons to Slack's actions-block limit and supplies normal text as Block Kit fallback text.

When valid buttons are present, both send paths post a section block for the formatted message plus an actions block whose buttons all use one Hermes-owned Slack action ID. The action name is never trusted from the Slack callback; it remains in an internal record. When no valid directive is present, the existing send behavior is unchanged.

### Opaque, short-lived action records

Before posting the block, Hermes creates a cryptographically random opaque token in a profile-scoped, atomically updated action store. The record contains only the approved action names, channel, expected message timestamp, thread timestamp, expiration, and consumed state. Once Slack returns the message timestamp, Hermes binds it to that record.

The record expires after a short fixed interval and is single-use. Hermes validates all of the following before accepting a click:

- the token exists and has not expired or been consumed;
- the Slack callback's channel and message timestamp match the stored record;
- the callback's requested action is one of that card's stored actions; and
- the record can be atomically consumed exactly once.

Missing, malformed, expired, replayed, cross-channel, and cross-message callbacks fail closed. A gateway restart with an unavailable record also fails closed. The store uses `get_hermes_home()` and is therefore profile-safe.

### Click routing and authorization

Register a single Slack Block Kit action handler for the Hermes-owned action ID. After record validation, it creates the same kind of user-sourced, non-internal `MessageEvent` that an ordinary Slack thread message creates: the clicker's Slack identity, source channel, thread, channel prompt, and auto-skill resolution are retained. Its normalized text is `Slack button action: <action_id>`.

The event enters the normal gateway message path, so existing user authorization and every agent-level policy remain in force. The handler neither calls tools nor maps an action name to a privileged operation. An agent that receives `sorpio_enroll`, for example, must still apply its own qualification, duplicate-prevention, and enabled-switch checks.

On a valid click, Hermes replaces the action block with an immutable acknowledgement naming the clicker. If the message update fails, the consumed record remains consumed; a repeated click cannot create a second event.

## Error handling

Slack post failures remove the unbound action record. Parsing errors leave the content untouched and are logged without exposing tokens or Slack credentials. Store I/O errors prevent button creation or click acceptance rather than falling back to an unverified action. All logging is token-free and uses profile-safe paths.

## Tests

Tests will prove the following behavior:

- parser acceptance, validation, and literal fallback for malformed directives;
- Block Kit payload construction and directive removal for `SlackAdapter.send` and direct `send_message` delivery;
- profile-scoped persistence, expiry, channel/message binding, and one-time consumption;
- valid click creation of a normal user-sourced event in the original thread;
- rejection of forged, malformed, expired, replayed, cross-channel, and cross-message clicks; and
- no regression to existing approval and slash-confirm buttons.

Tests use the repository's `scripts/run_tests.sh` wrapper. A live Slack smoke will use a harmless test action while Sorpio enrollment remains disabled; no prospect or enrollment mutation is part of this Hermes change.

## Non-goals

- No Sorpio-specific code, configuration, or policy in Hermes.
- No arbitrary tool execution, direct API mutation, or approval bypass from a click.
- No changes to Slack scopes, app manifest, AgentMail, HubSpot, Outreach Log, OpenProject, or other repositories.
