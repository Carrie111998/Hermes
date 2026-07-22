# CODE_DRIFT Event Producer — Design (Option B)

Date: 2026-07-21
Status: Approved by Diego (this session)
Prior art: laptop-monitor tray row `Hermes agent-src code drift (HEAD vs main)`
(2026-07-21) and `hermes_cli/events_doctor.py::check_code_drift` (a0759ea8b).

## Problem

The gateway's editable install imports the working tree of the shared checkout
at `~/.hermes/agent-src`, which is deliberately kept on a **detached HEAD** so
worktree agents can land commits onto the `main` ref via `git branch -f`. A
commit landed on main therefore does NOT run until the detached checkout is
fast-forwarded and the gateway restarted. On 2026-07-20/21 three restart
cycles ran stale code while every session believed the fix was live.

Two visibility layers already exist, both **local**: the laptop-monitor tray
row (periodic) and `events_doctor` (on-demand). Option B adds the third layer:
drift as a first-class **event-bus event**, so it reaches Telegram routing and
Diego's phone when he is away from the machine.

## Decisions (settled with Diego 2026-07-21)

1. **Build it** — the away-from-keyboard push closes the "VERIFIED LIVE" loop.
2. **Placement**: new producer module `events/producers/code_drift_monitor.py`
   (`CodeDriftMonitor`), NOT a method inside `GatewayHealthMonitor`. Follows
   the `ResourcePressureMonitor` sibling-producer convention.
3. **Noise policy**: rising edge + shape-change immediate + **6h** re-ping for
   a sustained episode. Probe git every **15 min**, not every 60s.
4. **Falling edge**: emit a `status: "resolved"` event when drift clears, but
   only if the episode actually alerted.

## Design

### Event type (`events/schema.py`)

```python
CODE_DRIFT = ("code_drift", Priority.HIGH)
```

HIGH so it survives `significant_only` / `digest_only` verbosity and reaches
Telegram — same rationale as `RESOURCE_PRESSURE`. Doc comment records the
2026-07-20/21 incident and the payload schema below.

Payload (drift edge):

- `status` (str) — `"drifting"`
- `state` (str) — `behind | ahead | diverged`
- `head` / `main` (str) — short SHAs
- `behind_count` / `ahead_count` (int)
- `dirty` (bool) — working tree has uncommitted changes
- `missed_subjects` (list[str]) — up to 5 `<sha> <subject>` lines for
  `HEAD..main` when behind
- `repo` (str) — checkout path probed

Payload (resolved edge): `status: "resolved"`, `head`/`main` (now equal),
plus `repo`.

### Producer (`events/producers/code_drift_monitor.py`)

`CodeDriftMonitor(bus, *, repo_path=None, sampler=None, clock=None,
state_path=None, check_interval_seconds=900, re_alert_cooldown_seconds=21600)`
— everything injectable for hermetic tests.

- **Sampler** `sample_code_drift(repo)`: read-only git via bounded
  `subprocess.run` (15s timeout, `capture_output`), same plumbing proven in
  `events_doctor.check_code_drift`: `rev-parse --verify HEAD/refs/heads/main`,
  `merge-base --is-ancestor` both directions, `rev-list --count`,
  `log --format` for missed subjects, `status --porcelain` for dirty. Returns
  a frozen `DriftSample(state, head, main, behind_count, ahead_count, dirty,
  missed_subjects)` with state ∈ `in_sync | behind | ahead | diverged`.
  Missing repo / unresolvable refs / git failure → `None` ("nothing to
  evaluate", mirrors `sample_resources`). `events_doctor` keeps its own
  already-landed logic untouched — no shared-module refactor.
- **Default repo**: `os.getenv("HERMES_AGENT_SRC") or ~/.hermes/agent-src`
  (same resolution as events_doctor).
- **`check()`**: internally gates on `check_interval_seconds` (15 min) using
  the injected clock, so the 5s gateway poll loop can call it cheaply every
  cycle; swallows sampler exceptions (never crash the poll loop).
- **`evaluate(sample, now)`** — pure edge core:
  - `None` sample → no-op (does not reset the episode).
  - Drift present: emit if (a) rising edge (was in sync / first-ever), OR
    (b) **shape change** — the `(state, behind_count, ahead_count)` tuple
    differs from the last emitted shape (bypasses cooldown), OR (c) sustained
    episode and ≥ 6h (wall clock) since last emit.
  - In sync: if the episode had alerted, emit `status: "resolved"` once and
    clear the episode; otherwise no-op.

### Restart-surviving episode state

The common remediation is "FF the checkout, then restart the gateway" — the
fresh process must still know it was alerting so the resolved ping fires.
Episode state persists via `events.state.load_state/save_state` (atomic
tmp+rename, WinError-5 retry) to a new `events.paths.code_drift_state_path()`
→ `~/.hermes/notifications/code_drift_state.json`:

```json
{"alerting": true, "last_emit_wall": 1784600000.0,
 "last_shape": ["behind", 3, 0]}
```

Wall-clock (`time.time()`) timestamps, not monotonic — same lesson as the
notifier batch-age persistence (18bbbf5cc). Missing/corrupt file degrades to
"no episode" (self-healing default).

### Routing + formatting

- `events/routing_policy.py`: `_E.CODE_DRIFT: _Spec(Attention.WARN, ALERTS)`
  plus a payload hook mirroring `GATEWAY_HEALTH`'s "up → INFO":
  `payload.status == "resolved"` → `Attention.INFO`.
- `events/formatting.py`: `EVENT_TYPE_EMOJI[EventType.CODE_DRIFT] = "🔀"` and
  title `"CODE DRIFT"`. **Eyeball the emoji dict for duplicate `EventType.X`
  keys** — ruff cannot catch attribute-access dup keys (memory:
  events-emoji-dict-dup-key-lint-gap).
- `events/subscribers/telegram_notifier.py`: body branch rendering plain
  language — drifting: state + counts + up to 5 missed subjects + the
  `git -C <repo> merge --ff-only main` remediation line + "restart the
  gateway"; resolved: single "back in sync @ <sha>" line. Body helper lives
  in `events/formatting.py` (`code_drift_body`) like the watchdog bodies.

### Registration (`events/gateway_integration.py`)

`startup()` constructs `_code_drift_monitor = CodeDriftMonitor(_bus)` (module
global + `get_code_drift_monitor()` getter, matching siblings). The
subscriber poll loop calls `_code_drift_monitor.check()` exception-wrapped;
the monitor's internal 15-min gate makes a dedicated loop interval variable
unnecessary, but the call site sits with the other periodic checks.

### Tests (`tests/events/test_code_drift_monitor.py`)

Hermetic per the events-subsystem invariants: fake sampler + fake clock +
`tmp_path` state file, no real git, no sleeps, no live `~/.hermes` I/O.

1. Rising edge emits once with full payload; second identical sample within
   cooldown is silent.
2. Sustained episode re-pings only after 6h.
3. Shape change (behind 3 → behind 5) emits immediately, ignoring cooldown.
4. Falling edge emits `resolved` exactly once; a never-alerted in-sync run
   emits nothing.
5. Restart survival: new monitor instance over a persisted
   `{"alerting": true}` state file + in-sync sample → emits `resolved`.
6. `None` sample is a total no-op (no emit, episode preserved).
7. `dirty` flag carried through payload.
8. `sample_code_drift` unit tests against a throwaway `tmp_path` git repo
   (init → commit → detach → land ahead commit on main): asserts
   `in_sync` / `behind` / missing-repo→`None`. Real git, but local, tiny,
   and read-only. The edge core itself never shells out.
9. Routing: WARN/ALERTS for drifting, INFO for resolved (hook test).
10. Notifier body renders drifting + resolved shapes without splatting raw
    dicts. (Existing pairing tests pick up the emoji/title/routing entries
    automatically.)

## Known limitation (accepted)

The monitor runs inside the gateway, so it detects drift with the currently
running — possibly stale — code: the first deploy cannot announce itself, and
a drifted gateway alerts with old monitor code. The tray row remains the
belt-and-braces layer. Read-only git only; the monitor never fast-forwards.

## Landing protocol

Work in this dedicated worktree (`claude/agitated-jemison-43afe5`), then land
on local main: `git merge-base --is-ancestor main <sha> && git branch -f main
<sha>`. Never `git pull`, never push. The shared detached checkout picks the
change up at the next deliberate FF + gateway restart — at which point this
very producer starts guarding future drift.
