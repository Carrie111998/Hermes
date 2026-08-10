---
sidebar_position: 3.1
title: "Discord Background Work"
description: "Route non-interactive work and cron runs into durable Discord operations threads"
---

# Discord Background Work

Hermes can publish non-interactive work to a dedicated Discord operations channel. This keeps `/background` tasks and cron runs out of ordinary conversations while retaining a replyable surface when an operator must intervene.

The feature is opt-in. With no `platforms.discord.extra.noninteractive_work` block, Hermes keeps its existing delivery behavior: interactive messages stay in their origin, and background results use the existing origin delivery path.

## Configure the operations channel

Set a numeric Discord channel ID. The configured name is informational only; Hermes routes by ID so renaming the channel does not change the destination.

```yaml
platforms:
  discord:
    extra:
      noninteractive_work:
        enabled: true
        channel_id: "123456789012345678"
        channel_name: background-sessions
        auto_archive_duration: 1440
        cleanup: archive
        retain_failures: true
        fallback_to_origin: true
        include_start_message: true
        include_cron: true
        include_background: true
        include_delegated: true
        chief_user_id: "234567890123456789"
        mention_on: [failure, intervention]
```

`channel_id` and `chief_user_id` must be Discord snowflakes (numeric strings). `auto_archive_duration` uses Discord's supported thread durations. `cleanup` can be `archive`, `delete`, or `retain`; successful work uses this policy. When `retain_failures` is enabled, failed or intervention-required work is retained even if `cleanup: delete` is configured. `fallback_to_origin` controls whether Hermes reports through the originating conversation when operations-channel or thread delivery cannot be created.

The `include_*` flags select producers: cron runs, `/background` work, and delegated work. `include_start_message` controls the initial lifecycle message. `channel_name` is for operator documentation and does not select a channel.

`mention_on` controls actionable event types. By default, only `failure` and `intervention` mention the configured `chief_user_id`; start, progress, and success messages do not. Ordinary interactive Discord messages do not notify Chief through this feature.

## Thread and delivery behavior

- Every cron run gets a fresh operations thread when the feature is enabled, including recurring and otherwise continuable jobs. A recurring schedule therefore creates one thread per run.
- `/background` work uses a dedicated thread when the originating interaction is eligible for operations delivery. The origin receives its acknowledgement as usual.
- A successful run publishes its result and is then cleaned up according to `cleanup` (archived by default).
- Cron failures and intervention-required cron runs are retained by default. They remain visible and replyable so an operator can provide input. Background result failures follow the configured retention policy; a failed background start notification is logged but does not by itself create a durable intervention binding.
- A reply to a retained **cron** work thread is bound to the recorded job/run/session context and creates a bounded follow-up or input event. It does **not** resurrect a process that has already exited. Retained `/background` threads do not currently provide the same durable cron reply binding.
- If the operations channel or thread cannot be used, Hermes follows `fallback_to_origin` rather than silently dropping the result. If fallback is disabled, the delivery failure is recorded in the gateway logs and the operations result is not moved into an unrelated channel.

Interactive conversations that did not originate as non-interactive work continue to use their existing Discord channel or thread. Ordinary messages do not notify Chief through this feature, and they are not automatically copied into the operations channel.

## Discord permissions

Grant the bot these permissions on the configured channel (or its parent category):

- **View Channel**
- **Send Messages**
- **Create Public Threads**
- **Send Messages in Threads**
- **Manage Threads** — required for archive/delete cleanup

The bot also needs the normal Discord gateway intents and message permissions described in [Discord setup](./discord). Restrict access to the operations channel to the operators who should see autonomous prompts and results.

## Data-sharing and security boundary

Operations delivery copies bounded task status, prompts, results, and any delivered media into the configured Discord channel. Anyone who can read that channel may see that data, so choosing the channel is an explicit data-sharing decision. Do not use a broadly visible channel for secrets, credentials, private environment data, or other content that should not leave the Hermes host. `channel_name` does not provide an access-control boundary; Discord permissions and channel membership do.

Actionable alerts mention only the configured numeric `chief_user_id`. Hermes does not resolve a display name to choose a mention target, and routine lifecycle or success messages do not mention Chief.

## Troubleshooting

### No operations thread is created

Check that `enabled: true` is present and `channel_id` is a valid numeric Discord channel ID, not `#channel-name` or a display name. `channel_name` is not used for lookup. Restart or reload the gateway after changing configuration.

If the ID is valid but the bot cannot access it, verify **View Channel**, **Send Messages**, and **Create Public Threads** for that channel. Check the gateway logs for the Discord API error. With `fallback_to_origin: true`, the originating conversation remains the fallback delivery path.

### Threads are created but messages fail

Verify **Send Messages in Threads** and that the thread has not been manually archived or deleted. A missing **Manage Threads** permission prevents archive/delete cleanup; it should not turn a completed task into a failed task. Cleanup errors leave the thread in place and are logged.

### Failures disappear

Use `retain_failures: true` (the default) and avoid deleting the thread manually. This setting overrides `cleanup: delete` for failure and intervention states. A retained cron thread requires its durable job/run binding; replies are bounded follow-ups or input, not process resurrection. Retained `/background` threads follow the background lifecycle policy and do not currently have the cron reply-binding behavior.

### Chief is not mentioned

Set `chief_user_id` to the user's numeric Discord ID and keep `mention_on` set to include `failure` or `intervention`. Display names and usernames are not accepted as mention targets. No mention is expected for ordinary messages, starts, progress, or success.

This documentation describes the configured and unit-tested behavior; it does not constitute a live Discord smoke-test result.
