# Christopher session continuity — root cause and held fix

WB: `a9ab2ff5-4cd9-42c3-9f29-19ba9ad752c7`

## Root cause

Two independent defects produced the opposite-looking symptoms.

1. **A site chat received a fresh replay namespace on every live-drain run.** In the deployed consumer, `/home/pclaw/apps/hermes-pcl/gateway/durable_jsonl_consumer.py:2205-2219` defaults `TGG_PERSISTENT_CHAT_SESSION_SCOPE` to `management`. Any non-management batch therefore passes `replay_namespace=None`. `gateway/replay.py:629-635` converts that to `agent:replay:<random live-drain run id>`, and `gateway/session.py:744-765` includes that namespace in the session key. The session store correctly reuses a key, but the consumer changes the key every run.

2. **The configured 04:00 daily boundary ran in the host timezone, not TGG's timezone.** The deployed `/home/pclaw/apps/hermes-pcl/gateway/session.py:25-27` returns naive `datetime.now()`. Both the request path (`_should_reset`, deployed lines 805-847) and the background expiry watcher (`_is_session_expired`, deployed lines 767-803) consume that same clock. `tgg-app-1` is UTC and Christopher's config had no `timezone` key, so `at_hour: 4` meant 04:00 UTC / 12:00 SGT. A session was therefore still valid at 11:46 SGT and reset before the next 15:19 SGT turn.

## Live-state reconciliation

Read-only inspection on `tgg-app-1` used `python3 -B` from the standard library and `sqlite3 -readonly`; no deployed Python module was imported.

- The systemd unit runs `gateway.durable_jsonl_consumer` with `HERMES_HOME=/home/pclaw/.hermes-christopher-tgg`.
- The unit environment had no `TGG_PERSISTENT_CHAT_SESSION_SCOPE` and no `HERMES_TIMEZONE`.
- `sessions.json` contained the management key under `agent:live-drain:persistent-chat:...`, but site-chat keys were under multiple `agent:replay:live-drain-<run id>:...` prefixes.
- In the inspected `pa_turns` population for chat `120363421424519051@g.us`, adjacent drain runs at 07:19, 07:20, and 07:22 UTC carried different session ids. Rows within one bundled run could share an id, confirming the namespace changed per drain run rather than SessionStore randomly failing to reuse a stable key.
- Management chat `120363426509183563@g.us` used `20260728_060405_5b393180` at 03:46 UTC (11:46 SGT), then `20260729_071927_95942df6` at 07:19 UTC (15:19 SGT), matching a 04:00 UTC boundary.

## Worktree reproduction

Before the fix, the new regression tests failed in the worktree:

- site-chat plans received `agent:replay:live-drain-<random>` instead of the stable live-drain namespace;
- `gateway.session` exposed no configured-timezone clock for the reset path.

## Held fix

- The ordinary durable live-drain call site explicitly uses `agent:live-drain:persistent-chat`; `SessionStore` continues to append the platform/chat suffix, preserving one conversation per chat without merging chats. Bounded backplay explicitly opts out and retains a fresh isolated replay namespace on every run.
- The session clock now sources timezone-aware `hermes_time.now()`. New sessions retain their offset in `sessions.json`; legacy naive entries are interpreted as server-local wall time and converted before comparison. A timezone cutover therefore does not silently age or reset a current legacy session.
- Christopher's generated root and all engine-slot configs declare `timezone: Asia/Singapore`; the slot builder, apply validator, deployment-spec validator, live runtime verifier, checksums, and deployment acceptance contract enforce it.
- Regression coverage exercises two consecutive site-chat drain plans through the real replay namespace + `SessionStore`, proves a second chat remains isolated, proves bounded backplay remains isolated, and exercises both the expiry watcher and request reset path across 04:00 Asia/Singapore.
- Startup auto-resume normalizes aware and legacy-naive markers against the same session clock before freshness arithmetic; a regression test covers an aware Singapore marker.

The timezone setting also makes Christopher's model-visible wall clock Singapore time, which is intentional for this Singapore client. This patch does not change business-event timestamp writers.

Bounded backplay remains a diagnostic/recovery replay: every batch is isolated from the ongoing client-chat transcript, including management batches. It may execute live business writes under its existing bounded authorization, but it does not splice historical prompt context into the live conversation.

## Rollback note

This commit begins writing offset-bearing session timestamps. A source rollback to a pre-fix runtime must not feed those entries to the old naive clock. Before restarting the old runtime, preserve `sessions.json`, then convert each offset-bearing `created_at`, `updated_at`, and `last_resume_marked_at` value to naive UTC (legacy host-local format). The normal forward deploy only restarts the service; `hermes_time` caches timezone resolution per process, so restart is required and is part of the existing deploy path.

No deployment or client-host mutation is included. This commit is held for Edna's pa-agent review/deploy path.

## Ruling compliance (2026-07-30, WB a9ab2ff5)

Teren ruling (2026-07-29 15:52): Christopher runs ONE PERSISTENT SESSION PER
CHAT THAT AUTOCOMPACTS — no daily reset, no idle reset. The fix above retained
the 04:00 daily reset; this follow-on closes it.

**Final behavior:** `session_reset: mode: none` is now the committed policy in
Christopher's generated root config and all three engine-slot configs.
`SessionResetPolicy` mode "none" never resets on daily boundaries or idle
gaps; sessions end only through compression-chain rotation, which preserves
the conversation (compressed history + summary checkpoint) under the same
session key.

**Compaction reality check (the flip precondition) — proven by test, not
code-read**, in `tests/gateway/test_mode_none_compaction.py`:

- The real `AIAgent._compress_context` → `ContextCompressor` machinery (only
  the summariser's LLM network call mocked) compresses an oversized transcript
  (40 → 6 messages, ~40K → ~18K estimated tokens), inserts a summary
  checkpoint, and records the compression session chain in SQLite
  (`end_reason='compression'`, parent link, `get_compression_tip` walk).
- When summary generation fails outright (aux model down), compression still
  bounds context via the static fallback marker and surfaces the failure —
  the last line of defence against unbounded prompt growth under mode "none".
- Repeated grow → compress cycles stay bounded; the chain remains walkable
  from root to live tip.
- On the gateway consumer path (`_handle_message`) with mode "none", a
  transcript past the hygiene threshold (85% of model context, actual/estimated
  tokens, plus the 400-message hard valve) is compressed through the real
  `ContextCompressor`; the turn still completes, the transcript is rewritten
  smaller with a summary checkpoint, and the session entry follows the
  compression chain instead of resetting.
- Cross-04:00-SGT-boundary and multi-day-idle turns REUSE the session under
  mode "none", with a positive control proving the old "both" policy resets
  under the same clock jump (the test can detect a reset).
- Every committed Christopher config (root + 3 slots) carries
  `session_reset: mode: none` and parses through the real yaml →
  `default_reset_policy` plumbing to a never-reset policy.

**Enforcement surface** (same as the timezone key): slot builder
(`build_runtime_slots.py` renders + validates the block), `apply_engine_slot.py`,
`validate_deployment_spec.py`, `verify_runtime.sh`, `SHA256SUMS` regenerated,
and the deployment acceptance contract in `client-agent-deployment.yaml`.

The reset-machinery regression tests from the prior commit are retained
unchanged — they guard the daily/idle machinery for other tenants and the
timezone-aware clock itself.

No deployment or client-host mutation. Held on `worker/a9ab2ff5-9abdeced` for
Edna's pa-agent review/deploy path.
