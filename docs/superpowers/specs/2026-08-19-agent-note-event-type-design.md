# AGENT_NOTE — an EventType for arbitrary agent-authored text

**Date:** 2026-08-19
**Status:** approved, implementing
**Repo:** `~/.hermes/agent-src`

## Problem

There is no EventType that carries arbitrary agent- or script-authored prose to
chat. Every semantically-plausible type is special-cased in
`TelegramNotifier._format_payload` (`events/subscribers/telegram_notifier.py:533`),
and each branch renders only the fields *its* type knows. A generic key:value
fallback exists (`:697`) but 64 of the 82 types never reach it.

So a caller with a sentence to send picks the least-wrong existing type and the
sentence is discarded. Concretely, emitting

```
python -m events.emit_external --type boot_summary --source agent-x
```

with `{"headline": ..., "detail": ..., "status": "resolved"}` exits 0 and returns
an event_id, but `boot_summary_body()` (`events/formatting.py:778`) reads only
`boot_id / state / failures / anomalies / done / total / failed / skipped`. The
message renders as:

```
🟢 RECOVERED 🥾 BOOT_SUMMARY — agent-x · 18:50 UTC
───────────────
Boot ? finished ? — ?/? services up.
No failing steps were named.
Full detail: tray Boot panel.
```

### Why this is worse than an ugly render

The render is *identical regardless of payload*. `RepeatGuard`
(`events/noise_guards.py:98`, 1800s window) fingerprints the rendered message
after `normalize_for_fingerprint` collapses digit runs to `N` (`:49`), so two
DIFFERENT messages collapse to one fingerprint and the second is **silently
suppressed**. Verified by reproduction: `A suppressed: False`, `B suppressed: True`.

There is no failure record. `handled_events` shows telegram-notifier handled the
event, `dead_letters` is empty, and the only evidence is the ABSENCE of a
`notification_delivered` line in `~/.hermes/events/audit.jsonl`. A
distinct-but-similar pair of verdicts vanishes entirely.

Because of this, callers were moved off the bus onto the direct Telegram Bot
API — losing the priority dot, icon, source header, and durable-queue delivery.

### Secondary finding (not in the original report)

`evaluate_outcome` reads six payload keys on **every** event type —
`status`, `reason`, `outcome`, `result`, `conclusion`, `message_type`
(`events/outcomes.py:128`). That is why the repro header reads `🟢 RECOVERED`:
a free-form payload can hijack the verdict machinery, not merely render blank.

## Solution

A dedicated `AGENT_NOTE` type whose formatter renders a `headline` plus a
multi-line `detail` **verbatim** — the same contract `AGENT_ITERATION` already
honours for its structured `brief` (`telegram_notifier.py:640-651`).

### Type

```python
AGENT_NOTE = ("agent_note", Priority.NORMAL, "🗒️")
```

Icon U+1F5D2 U+FE0F. Verified free across all 82 members, and disjoint from the
glyphs already used in both topics this type can reach (`agents_memory`:
📚 🚀 🧠 · `watchdog_alerts`: 26 glyphs).

### Payload contract

| Key | Required | Meaning |
|---|---|---|
| `headline` | yes | One line. The subject of the note. |
| `detail` | no | Multi-line free text, rendered **verbatim** (newlines preserved). |
| `attention` | no | `info` (default) · `warn` · `trace`. `act` is accepted but clamped — see below. |

Any other key is ignored by the formatter. Two deliberate anti-bug rules in the
body function:

1. **No `headline` → fall through to the generic key:value dump.** Never render
   something that looks complete when it is not. That failure mode is the whole
   reason this type exists.
2. Body caps at `AGENT_NOTE_MAX_CHARS = 3000` with a **visible**
   `…truncated N chars` note. Silent truncation would recreate the defect in
   miniature. (Precedent: `CRON_SUMMARY_MAX_CHARS = 1500`,
   `telegram_notifier.py:81`. Telegram's own message limit is 4096.)

### Routing

`_POLICY` gains:

```python
_E.AGENT_NOTE: _Spec(Attention.INFO, AGENTS_MEMORY, priority_cap=Priority.HIGH),
```

A conditional hook in `classify()` reads `payload["attention"]` and sets the
class plus its topic:

| `attention` | class | topic |
|---|---|---|
| `info` (default) | INFO | `agents_memory` — 🧠 Agents & Memory (9663) |
| `warn` | WARN | `watchdog_alerts` — 🚨 Alerts (9654) |
| `trace` | TRACE | `cron_firehose` — ⚙ Ops Trace (53609) |
| `act` | clamped to WARN + logged | `watchdog_alerts` |
| unrecognised | INFO (default) | `agents_memory` |

No new topic and no `verbosity.json` key. The three reused topics already pass
these notes: `agents_memory` and `cron_firehose` are `mode:all/min_priority:low`;
`watchdog_alerts` is `significant_only/min_priority:normal` and the WARN class
floor of NORMAL clears it.

**TRACE means batched**, not prompt: `route.batch=True`, flushed hourly or at 20
buffered messages. Callers who want a note to appear promptly should not use it.

#### Clamp placement is load-bearing

`structured_human_gate` promotes **any** event carrying `action_required: true`
plus an `action_kind` in {approval, decision, credential, credits,
manual_intervention} straight to ACT / Action Required
(`events/routing_policy.py:409`, applied at `:548`), and `_derive_wa` documents
that "ACT ALWAYS escalates" — explicit pins may raise a page but never suppress
one (`:588-592`).

Therefore the AGENT_NOTE WARN clamp must run **after** the outcome/human-gate
block, not inside the conditional-hooks block. Placed early it would be silently
overridden and an agent note carrying those two keys would page the phone. This
is pinned by a test.

### Escalation: closed, not merely unlikely

An earlier draft left one valve open: WARN + `--priority critical` yields
`WA_URGENT` (`_derive_wa`, `:599`). Per review decision this is **closed**.

`_Spec` gains a `priority_cap: Optional[Priority] = None` field, mirroring the
existing `priority_floor`, applied in the same block:

```python
priority = _cap(priority, spec.priority_cap)
```

`AGENT_NOTE` sets `priority_cap=Priority.HIGH`. An explicit
`--priority critical` therefore degrades to HIGH — visibly, in the dot — and
`_derive_wa` returns `None` for WARN at HIGH. Combined with the ACT clamp,
**`wa_tier` is unconditionally `None` for every agent note**.

Consequence: no `whatsapp_escalator` branch is needed. `classify_tier()` is a
thin adapter that returns `None` when `route.wa_tier is None`
(`events/subscribers/whatsapp_escalator.py:74`, `:284`), so `should_escalate()`
is naturally False. The unreachability is asserted by a test rather than assumed.

`priority_cap` is added as a general `_Spec` mechanism rather than an AGENT_NOTE
special case, because it is the exact symmetric counterpart of `priority_floor`
and the cap belongs with the routing data, not in the derivation logic.

### RepeatGuard: unchanged

No exemption, no `dedup_key`. Rendering `headline`/`detail` into the message
text is itself the fix: two different notes now produce two different
fingerprints and both deliver. The guard then does only its intended job —
suppressing genuinely verbatim repeats within 30 minutes — which is the
protection against a looping agent machine-gunning a topic (cf. the 2026-07-17
capped-partials bug, 79 near-identical pings).

**Accepted residual:** `normalize_for_fingerprint` collapses digit runs to `N`,
so two notes differing ONLY by a number ("11,241 drawers" then "11,305
drawers") still collide inside the window. That is the documented semantics for
every event type, not an AGENT_NOTE-specific defect.

### Reserved payload keys: documented, not carved out

`status`, `reason`, `outcome`, `result`, `conclusion`, `message_type` are read
by `evaluate_outcome` for every type. Only the recognised vocabulary bites
(`_FAILED_VALUES`, `_RECOVERED_VALUES`, …); an unrecognised string is inert.

This is kept as a **feature**: `status: "failed"` gives a red ❌ FAILED note.
Carving AGENT_NOTE out of the verdict machinery would make it the one type where
`status` does nothing, which is its own surprise. Documented consequence: a
FAILED or DEGRADED verdict promotes an INFO note to WARN on Alerts
(`routing_policy.py:552-557`) — consistent with intent.

`action_required` / `action_kind` are neutralised by the post-gate clamp above.

## Files touched

| File | Change |
|---|---|
| `events/schema.py` | `AGENT_NOTE` member + rationale comment |
| `events/routing_policy.py` | `_Spec.priority_cap` field; `_POLICY` entry; attention hook; post-gate ACT clamp; cap application |
| `events/formatting.py` | new `agent_note_body()` |
| `events/subscribers/telegram_notifier.py` | dispatch branch |
| `tests/events/test_formatting.py` | body rendering |
| `tests/events/test_routing_policy.py` | routing, clamp, cap, escalation |
| `tests/events/test_telegram_notifier.py` | end-to-end delivery + RepeatGuard |

`_POLICY` is `REQUIRED_TOTAL` (`events/coverage.py:155`), so the pre-commit
`python -m events.coverage` hook gates the routing entry. `EVENT_TYPE_EMOJI` is
derived from `EventType.icon`, so the icon table cannot drift.
`events/emit_external.py` needs no change — it resolves via
`EventType.from_string`.

## Tests

1. Two different notes → two different messages, **neither suppressed** (the reported incident).
2. The same note twice inside the window → second suppressed (guard still armed).
3. `attention: "act"` → WARN on `watchdog_alerts`, `wa_tier is None`.
4. `action_required: true` + `action_kind: "approval"` → still WARN, `wa_tier is None` (the escape hatch).
5. `priority=CRITICAL` → effective priority HIGH, `wa_tier is None`, `should_escalate()` False (the closed valve).
6. `detail` rendered verbatim including newlines.
7. Missing `headline` → generic key:value dump, nothing silently dropped.
8. Oversize `detail` → visible truncation note.
9. `attention` info/warn/trace → correct topic; unrecognised value → INFO default.
10. `python -m events.emit_external --type agent_note` round-trip.

## Explicitly not building

- No `dedup_key` / RepeatGuard opt-out.
- No `MEMORY_ROUTING` entry (`memory_writer` is opt-in and deliberately partial).
- No new topic in `topics.json`, no new key in `verbosity.json`.
- No `whatsapp_escalator` branch — unreachable by construction, asserted by test 5.
