# plan-secretary

Turn assistant future-commitments into human-confirmed, session-scoped task plans.

## Problem

Hermes frequently replies with future-looking commitments in ordinary conversation:

> 我接下来会检查 logs/xxx
> I'll fix that after this
> 下一步我会启动 …

There is no durable executor behind these promises. The human reads them as
"work is scheduled", but nothing wakes the agent up, nothing confirms a due
time, and the commitment drifts or gets double-handled.

## Design: capture → confirm → resolve (same-turn)

The plugin registers three official lifecycle hooks:

1. **`promise_gate`** (new core hook, this PR) — fires when the agent is about
   to finish a turn with a textual answer. If the answer contains a future
   commitment, the plugin keeps the **same turn** going by injecting an
   internal user message:

   ```
   [内部-小秘书承诺确认｜必须立即处理] 你刚刚承诺了后续任务：
     - <promise>
   请选择：立即执行 / 登记为定时任务（说明时间节点）/ 取消。
   ```

   The agent must **execute, register, or cancel** the promise *now* — it does
   not wait for the human's next message. Attempts are capped (default 3) and
   duplicates are deduped, so the loop cannot spin.

2. **`post_llm_call`** — captures commitments into a `pending_captures.json`
   (used by the dashboard + as durable state).

3. **`pre_llm_call`** — before the next LLM turn, injects pending-confirm /
   due-plan reminders (session-scoped).

### Precise capture filter

A sentence only becomes a *pending capture* when all three gates pass:

- **actor** — the assistant/agent (小墨 / me / I) is the one committing;
- **action verb** — 检查/启动/修复/验证/写/跑/回灌/设计/check/start/fix/…;
- **object** — a concrete file, script, process, log, plan, seed, direction…

False positives are rejected by construction:

- reported speech ("小墨说：'我会…'") — quoted, not committed;
- examples/explanation ("如/比如/那种…句子");
- test reports ("→ 正常抓 ✅", "RESULT PASS", dict/`text:` echoes);
- Plan Secretary's own injected text — every injected message carries a
  uniform marker `【小秘书】` and the filter skips any text with it;
- switch commands ("小秘书关") — handled as control, never captured.

### Per-session switch

Default is **ON**; the human can turn the secretary off per session:

- first LLM turn injects a one-time prompt ("本会话默认启用小秘书，如不需要回复：小秘书关");
- `小秘书关` / `小秘书开` / `小秘书状态` are natural-language switches
  persisted to `$HERMES_HOME/state/plan_secretary/sessions/<sid>/enabled.json`;
- hooks check the switch before capturing or reminding.

### Self-capture prevention

All Plan Secretary injected text carries the `【小秘书】` marker; the capture
filter returns empty for any text containing it. The secretary never chases
its own reminders.

## State

All state lives under `$HERMES_HOME/state/plan_secretary/` (per-session
isolated files):

```
pending_captures.json   # status: pending|confirmed|ignored|completed
plan_registry.json      # status: active|deferred|blocked|completed|cancelled
plan_status.json        # aggregate summary
sessions/<sid>/enabled.json    # per-session switch (default ON)
sessions/<sid>/last_capture.json
```

## Desktop pane

`dashboard/manifest.json` + `dashboard/plugin_api.py` expose a FastAPI router
(`GET /api/plugins/plan-secretary/pending`) that serves the real pending count
to a desktop pane — the panel shows the same truth as the hook plugin.

## Core hook: `promise_gate`

This PR adds `promise_gate` to `VALID_HOOKS` plus a helper
`get_promise_gate_continue_message()`. A plugin keeps the turn going by
returning `{"action": "continue", "message": "<follow-up>"}` from a
`promise_gate` callback. `agent/conversation_loop.py` fires the gate before
finalization (≤3 attempts), and `agent/turn_finalizer.py` treats the synthetic
message like other verification-loop synthetic turns.

> Relationship to the official `on_output` proposal (#45881): both intercept
> the final output to re-prompt the model, but they are complementary.
> `on_output` is a *rejection* gate (`{"action":"block"}` — discard output,
> retry) for completion/leak/quality enforcement. `promise_gate` is a
> *continuation* gate (`{"action":"continue"}` — keep the emitted answer,
> then execute/register/cancel the promise) for commitment tracking. The two
> can coexist.

## Testing

```
python -m pytest tests/plugins/plan_secretary/ -q
```
