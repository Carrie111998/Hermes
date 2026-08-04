---
title: Workflow Recording
description: Demonstrate a browser workflow once with hermes record, then turn the recording into a replayable skill with /learn.
sidebar_label: Workflow Recording
sidebar_position: 17
---

# Workflow Recording

`hermes record` lets you *demonstrate* a browser workflow instead of describing it. It attaches to your own browser over CDP (the same live-attach as [`/browser connect`](./browser.md)), watches what you do — clicks, typed values, Enter presses, navigations — and saves the demonstration as a recording JSON. `/learn` then recognizes that recording as a source and authors a skill whose Procedure replays the flow with the standard browser tools.

The loop is: **record → learn → replay**.

## Record

1. Make sure Hermes can reach your browser — run `/browser connect` in a chat once (or set `browser.cdp_url` in `config.yaml`).
2. Start recording:

```bash
hermes record --slug checkout-flow
```

3. Perform the workflow in your browser.
4. Press `Ctrl-C` to stop. The recording is written to `~/.hermes/recordings/checkout-flow-<timestamp>.json`.

What gets captured:

| Event | What is stored |
|---|---|
| `click` | CSS selector path, tag, trimmed visible text |
| `input` | Selector and the **final** field value (on `change`, not per keystroke) |
| `enter` | Selector of the focused element when Enter was pressed |
| `navigate` | Top-frame URL changes |

### Secret masking

Password fields (`type=password`, plus `current-password` / `new-password` / `one-time-code` / credit-card autocomplete hints) are masked **at capture time, inside the page**: the recorder never reads the real value — it stores a `{SECRET:<field name>}` placeholder instead. The Python side masks again before writing to disk as defense-in-depth, so a raw credential can never end up in a recording file.

### Recording format

```json
{
  "version": 1,
  "started_at": "2026-07-26T12:00:00+00:00",
  "url": "https://shop.example.com/login",
  "steps": [
    {"t": 0.0, "type": "click", "selector": "button#login", "text": "Sign in"},
    {"t": 1.2, "type": "input", "selector": "input[name=\"user\"]", "value": "alice"},
    {"t": 2.0, "type": "input", "selector": "input[name=\"pw\"]", "value": "{SECRET:pw}"},
    {"t": 2.5, "type": "enter", "selector": "input[name=\"pw\"]"},
    {"t": 3.1, "type": "navigate", "url": "https://shop.example.com/home"}
  ]
}
```

`t` is seconds relative to the first step.

### Other modes

```bash
hermes record --list      # list saved recordings
hermes record --manual    # no CDP: narrate your steps one per line
```

`--manual` is the fallback when no CDP endpoint is available (or you performed the flow in a non-Chromium browser): you type what you did, one step per line, and the same recording format is written with `type: "manual"` steps.

## Learn

Point `/learn` at the recording:

```bash
hermes chat "/learn recording ~/.hermes/recordings/checkout-flow-20260726-120000.json"
```

`/learn` recognizes recording sources (a `.json` under `recordings/`, or the word "recording") and adds replay-specific guidance: the agent reads the JSON, reconstructs the workflow in human terms, and authors a skill whose Procedure replays the flow via `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, and `browser_press`.

For every `{SECRET:*}` placeholder, the agent asks which env var or secret reference should supply the value at replay time (e.g. `$SHOP_PASSWORD` from `.env` or `hermes secrets`). Secrets are never asked for inline and never written into the skill.

## Replay

Once the skill is saved, replay is just using it:

```
you> log into the shop and check my order status
```

The agent loads the learned skill and re-drives the browser through the recorded flow — using the live page snapshot to locate elements, with the recorded selectors and text as hints, and pulling credentials from the secret references you configured.

## Pitfalls

- **Recording captures the active tab.** Switch to the tab you want to demonstrate *before* starting `hermes record`.
- **Selectors are hints, not gospel.** Sites change their DOM; learned skills tell the agent to prefer live `browser_snapshot` refs over recorded selectors when replaying.
- **Values are captured on field change.** If you never blur/commit a field before stopping, its value may be missing — press Tab or Enter before `Ctrl-C`.
- **Non-password secrets aren't auto-masked.** API keys typed into plain text fields are stored as-is; review the recording JSON before sharing it, or use `--manual` for sensitive flows.
