# Disk-pressure severity bands — design

**Date:** 2026-08-14
**Status:** implemented (see "As built" below)
**Author:** Claude Code session (investigation + design), approved by Diego
**Supersedes nothing.** Extends the two-stage disk work (`94ce27b424`, `a462a0d360`) and the
routing move (`19a8dd9abd`).

## Problem

Diego's requirement: *"generally quiet, but keep alerts as disk space gets dangerously low."*

Neither half holds today, and the second is impossible by construction.

### Measured behaviour (2026-08-13T16:03 → 2026-08-14T17:36, 60 real events)

`RepeatGuard` (`events/noise_guards.py`, 1800s **sliding** window, digit-collapsing
fingerprint) plus `DEFAULT_RE_ALERT_COOLDOWN_SECONDS = 900.0` reads as *"exactly one delivered
message per sustained episode, indefinitely."* It delivered **15**. Replaying all 60 events
through the real guard and the real `format_message` reproduced every outcome (15/15
deliveries, 0 mismatches). Attribution:

| Cause | Count |
|---|---|
| Sliding window genuinely expired (consecutive same-fingerprint gap ≥ 1800s) | 11 |
| First sighting of a new `reasons` set | 2 |
| Gateway restart wiped the in-memory guard | 1 |
| First event of all | 1 |

Independent 30-day counts agree on the rate: 1195 emitted → 317 delivered (3.8×).

### Root causes

1. **Severity is invisible to the fingerprint.** `normalize_for_fingerprint` collapses digit
   runs to `N`, so `C: free: 0.0 GB` and `C: free: 56.63 GB` are the *same message*. In the
   replay a single fingerprint covered every `disk_low` event across the full range — 101 of
   them below 5 GiB, 13 at exactly 0.0 GiB. A disk becoming dangerously low therefore produces
   **no new signal at all**; it is suppressed by the same sliding window as a healthy disk.
   This is the primary defect.

2. **The re-ping/window ratio guarantees leakage.** Sampling is every 60s
   (`gateway_integration.py:841`) and the re-alert cooldown is 900s, so a re-ping lands in
   `[900, 960)`s. Two re-ping intervals are therefore ≥ 1800s, and `is_repeat` compares
   `(now - last) < window` — so **two consecutive intervals can never be suppressed**. One
   skipped or fingerprint-diverted tick always re-delivers. This is not a tuning margin to
   widen; it is arithmetic. (Unbroken chains do hold as designed: 16 consecutive suppressions
   over 4h on one message.)

3. **`reasons` is letters, not digits.** `['disk_low']`, `['phys_high']`,
   `['phys_high','disk_low']` are three independent dedup streams, and interleaving lets each
   age past the window without any tick being missed.

4. **Restarts re-arm both sides.** The guard is in-memory per gateway process
   (`telegram_notifier.py:192`) and the producer's `_latched`/`_last_emit` are too, so a restart
   re-fires the rising edge. The tick-0 deferral (`ae36edea9`) moves this by one interval; it
   does not prevent it. Both 08-14 deliveries landed 58–74s after a `gateway_started`.

### Latent defect in the paging lane

`whatsapp_escalator.format_message` has **no `RESOURCE_PRESSURE` branch**. A `disk_critical`
page falls to the terminal scalar fallback, which takes `scalars[:6]` in payload insertion
order — `commit_used_gb`, `commit_limit_gb`, `commit_pct`, `phys_used_pct`,
`phys_available_gb`, `pagefile_allocated_gb` — and stops **before `disk_c_free_gb`**. The
phone alert whose only job is to say the disk is dying would not mention the disk. Never
observed because `disk_critical` has fired **0 times in 1186 events**; the axis is one day old.

## Goals

- A worsening disk produces a new, distinguishable message at each genuine severity step.
- A stable episode produces roughly one message, not one per hour.
- Both delivery lanes (Telegram and WhatsApp) render it readably and dedup it correctly.
- The bus keeps its periodic sampling for audit and forensics.

## Non-goals

- Changing `RepeatGuard` semantics. It is shared by cron, WhatsApp, and every other
  subscriber; disk severity is not its concern. **Unchanged.**
- Changing thresholds (45/25 GiB) or disarm levels (52/40 GiB). **Unchanged.**
- Changing routing. Everything below 25 GiB is already ACT via the existing `disk_critical`
  hook (`routing_policy.py:481`). **Unchanged.**
- A recovery / "all clear" message. No such event exists today; adding one is separate scope.
- Persisting episode state across gateway restarts (see Accepted limitations).

## Design

### 1. Severity bands in the producer

`ResourcePressureMonitor` owns the existing hysteresis/latch state and is the only place where
"worst band seen this episode" is authoritative for both lanes.

| Edge (GiB free) | Band label | Attention (unchanged) |
|---|---|---|
| 45 | `low` | WARN → `security_and_system` |
| 25 | `critical` | ACT → `action_required` + WhatsApp |
| 12 | `severe` | ACT |
| 6 | `emergency` | ACT |
| 3 | `imminent` | ACT |
| 1 | `full` | ACT |

Geometric spacing: coarse where the slide is slow, tight where minutes matter.

**Ratchet, with hysteresis re-arm.** State is a set of already-announced edges,
`_announced_disk_edges: Set[float]`.

- An edge **fires** when `free < edge` and that edge is not already announced. Edges fire on
  **downward crossings only** — an improving disk never announces, it only re-arms.
- When a single sample crosses several edges at once, announce **only the deepest** edge
  crossed, and mark every crossed edge as announced. A 45 → 2 GiB drop is one `imminent`
  message, not five.
- An edge **re-arms** when `free > edge * 1.2` (45→54, 25→30, 12→14.4, 6→7.2, 3→3.6, 1→1.2).
  Hovering at a boundary therefore cannot flap; a genuine 11 → 30 → 11 GiB round trip does
  re-announce. This mirrors the per-axis `comfortably_clear` disarm vocabulary already in the
  file. The 45 edge is the exception: its 54 GiB re-arm is unreachable in practice because the
  `disk_low` axis disarms at 52 GiB, which clears the episode — and the episode clear resets
  the whole set. That is the intended outcome, not a gap.
- The set clears when the episode clears (no `reasons` and every latched axis comfortably
  clear).

The band is computed only when a disk axis is in `reasons`; a pure `phys_high`/`commit_high`
episode has no band.

### 2. Change-reason stamp (the "quiet" half)

Every emission carries `change`, in precedence order:

| Value | Meaning | Delivered? |
|---|---|---|
| `rising_edge` | episode started (or restarted process re-latched) | yes |
| `band_change` | a severity edge fired | yes |
| `reasons_change` | the `reasons` set differs from the last emission | yes |
| `sustained_repeat` | cooldown elapsed, nothing changed | **no — bus only** |

Bus emission cadence is **unchanged** (every 900s), so audit/telemetry and the ability to
reconstruct an episode after the fact are fully preserved — that sampling is what made this
investigation possible. `TelegramNotifier.handle` and `WhatsAppEscalator.handle` drop
`change == "sustained_repeat"` before rendering, alongside the existing `_CRON_BUS_ONLY` style
gate.

A band change is also an **emission trigger**, so a fast fill is not sitting on a stale label
for up to 15 minutes:

```
emit if rising_edge or band_changed or reasons_changed or cooldown_elapsed
```

`RepeatGuard` stays in the path untouched as defence in depth: identical text within 30 min is
still collapsed.

### 3. Shared body helper

Add `resource_pressure_body(payload)` to `events/formatting.py`; call it from **both**
`telegram_notifier._format_payload` and a new `RESOURCE_PRESSURE` branch in
`whatsapp_escalator.format_message`. This is the established pattern
(`watchdog_burst_body`, `container_crash_loop_body`, `boot_summary_body`) and is mandated by
the escalator's own docstring. It closes the paging-lane defect above.

Rendered body leads with the band, because the band is the part the fingerprint can see:

```
⚠ Resource pressure: disk_low, disk_critical
C: free: 2.4 GB — IMMINENT (under 3 GiB)
Commit: 65.5% (83.32/127.2 GB)
Pagefile: 64.0 GB (+0.0 GB/10m)
```

WhatsApp gets the same band sentence in plain language, e.g.
`Disk IMMINENT: C: has 2.4 GB free (under 3 GiB). The box wedges at zero.`

### 4. Payload additions

```json
{
  "disk_band": "imminent",
  "disk_band_edge_gb": 3,
  "change": "band_change"
}
```

(Named `disk_band_edge_gb`, not `..._floor_gb`: the value is the edge that
fired — 3 GiB is the *ceiling* of the `imminent` band, not its floor.)

Additive only. Absent for non-disk episodes (`disk_band: null`). Existing consumers are
unaffected. Note the WhatsApp scalar fallback becomes unreachable for this type once the branch
in §3 lands — which is the fix; adding payload keys alone would **not** have helped, since
`scalars[:6]` truncates before any field appended after `pagefile_allocated_gb`.

## Testing

- **Band table:** edge firing, deepest-edge-only on a multi-edge drop, 1.2× re-arm, no flap
  across a boundary, set cleared on episode end.
- **Change stamp:** each of the four values produced under its own condition, precedence
  respected, `sustained_repeat` dropped by both subscribers and still present on the bus.
- **Regression (the point of the exercise):** replay the real 2026-08-14 timestamps and
  payloads through the guard and assert the new message count. Today's 60-event / 15-message
  sequence is the baseline; the harness that reproduced it 15/15 is in the session scratchpad
  and should be adapted into `tests/events/`.
- **Rendering:** both lanes render a `disk_critical` payload readably and mention free space —
  a direct regression test for the never-fired paging path.
- **Fingerprint:** two payloads differing only in GB numbers within one band share a
  fingerprint; two in different bands do not.

## Rollout

- Subscribers and producers run **inside the gateway process**, so nothing takes effect until a
  gateway restart. State is in-memory: the first sample after restart re-latches and emits one
  `rising_edge` message.
- `disk_critical` has never fired in production. Its ACT + WhatsApp path will be exercised for
  the first time by this change, so the rendering tests above are load-bearing, and a manual
  synthetic-payload check of both lanes is recommended before relying on it.

## Accepted limitations

- **Restart re-fire is not fixed.** A gateway restart during an episode still costs one
  message (~3/day observed). Closing it means persisting episode state across restarts —
  disproportionate for the win, and a restart mid-episode is arguably worth knowing about.
  Flagged, not built.
- Expected steady state: a stable `disk_low` episode drops from ~15 messages/day to ~4
  (1 band + ~3 restarts). A genuine 45 → 0 GiB slide produces 6 escalating messages that each
  say something new.

## As built

Landed as designed, with three notes:

1. **The bus-only gate is `events.noise_guards.is_sustained_resource_repeat`**, called by
   both subscribers. It sits in `noise_guards` (with its siblings) rather than being
   duplicated per lane, and is duck-typed so that module stays dependency-free. Events with
   no `change` key return False, so a pre-band producer keeps delivering.
2. **`rising_edge` outranks `band_change`.** A concurrent change (`newly_breached`) made the
   rising edge per-axis, so a drop from `low` into `critical` necessarily breaches
   `disk_critical` and stamps `rising_edge`. Both deliver; the stamp is explanatory only.
   Only a deepening *within* an already-latched axis stamps `band_change`.
3. **Payload key is `disk_band_edge_gb`** (see above).

### Measured against the real episode

`tests/events/test_disk_pressure_band_replay.py` replays the actual 27 readings from
2026-08-14 03:50:27Z → 11:08:52Z (13.57 GB → 0.0 GB, then ~7h parked at zero), producer →
bus → notifier, with the RepeatGuard window pinned to 0 so only the `change` stamp can
suppress:

- **The descent produces exactly 4 messages** — `critical`, `severe`, `emergency`, `full` —
  one per band actually crossed, in order.
- **The 19-sample, 5.5-hour tail at ≤ 0.21 GB free produces none.** The disk cannot get
  worse than `full`, so it stops talking.
- **A four-hour hover inside one band produces exactly 1 message**, including a skipped
  sample opening an 1812s gap and another opening 2722s — the exact shape that leaked 11 of
  the 15 messages measured on 2026-08-14.

Note the descent count (4) is not lower than the pre-band behaviour for that same window
(~3, from window expiry). The win is not raw volume there — it is that all 4 say something
new and arrive on the way down, where the old 3 were triggered by missed ticks and were
textually identical at 13 GB and at 0.0 GB. The volume win is in the stable case.
