# tests/gateway — first COMPLETED monolithic run — failure inventory

**2026-08-11.** `14 failed, 9829 passed, 55 skipped, 322 warnings in 26004.68s (7:13:24)`

The suite has now finished a single-interpreter run for the first time. All 9,898
tests executed, including the ~57% past index 4257 that had never run.

- Worktree: `.claude/worktrees/eager-kepler-ddb335` @ `e3bfc2ebc` (isolated; shared checkout untouched)
- Command: `python -u -m pytest tests/gateway -p no:cacheprovider -q -rf --tb=short --timeout=900`
- `CLAUDE_CODE_ENTRYPOINT` / `CLAUDECODE` / `HERMES_DISABLE_MESSAGE_TRIGRAM` cleared
- Artifacts, under `sources/2026-08-11-gateway-monolithic/` beside this file:
  `gw_full2.log` (raw), `gw_full2_failures.txt` (14 tracebacks), `gw_iso.log` (isolation
  re-run), `GW_FULL2_RUN_HANDOFF.md` (the launch note, kept as written at run time).
  All four keep their as-run filenames; they sat at the repo root until 2026-08-11.
- Measured rate 0.40 tests/sec, not the assumed ~1/sec: the box sat at **96.8% commit charge**
  (56 `claude` processes = 29 GB, vmmemWSL 15 GB, chrome 15 GB). This matters — see #10.

## ⚠️ Baseline `e3bfc2ebc` — re-check every entry against current `HEAD` before actioning

Everything below describes the tree **as of `e3bfc2ebc` (2026-08-11)**. That commit is this
document's baseline, not "current". Entries go stale the moment a fix lands, and a stale
entry is indistinguishable from an open one — this inventory has already dispatched three
separate sessions to redo work that was already finished.

**Re-run the entry's file at current `HEAD` and confirm it still fails before touching it:**

```
python.exe -u -m pytest <file> -p no:cacheprovider -q -rf --tb=short --timeout=900
```

from PowerShell, with `CLAUDE_CODE_ENTRYPOINT`, `CLAUDECODE` and
`HERMES_DISABLE_MESSAGE_TRIGRAM` cleared. The repo default `addopts --timeout=30` is too
low on this box and kills the whole file rather than one test.

### Status as of 2026-08-11

**All 14 are FIXED. Zero remain open.** Every one of the 14 was re-run against `main`;
nothing below is an inherited claim.

| group | count | status on `main` | evidence |
|-------|-------|------------------|----------|
| A | 5 | ✅ fixed by `e467da742` | 32 passed |
| B | 4 | ✅ fixed by `e467da742` | 21 + 114 passed |
| C | 3 | ✅ green | see group C |
| D | 1 | ✅ green — and the "ordering artifact" reading was **wrong**; same bug as C | see group D |
| E | 1 | ✅ fixed by `9ffc94018` — and it was **no longer an artifact** | see group E |

Two runs at `eda64dfdc`, both from an isolated worktree, both `0 failed` except where noted:
`167 passed in 497.35s` over the three A+B files, and `1 failed, 298 passed, 1 skipped in
727.01s` over the four C/D/E files — that single failure being group E, now fixed.

`e467da742` *"fix(gateway): repair the standing tests/gateway failures on Windows"* and
`bff71e7ed` *"fix(tests+whatsapp): clear the nightly gate lane's 4 named failures"* are both
ancestors of `main`; `e467da742` is also on the deployed branch
`claude/checkpoint-all-nonsecret-20260808`. Fixed entries are annotated in place below
rather than deleted, because the record of the run is worth keeping.

Caveat on where you verify: neither fix commit is an ancestor of the branch this document
was written on (`claude/optimistic-darwin-0c9c89` @ `9591ec2e4`). Verify against `main`.

## Delta vs the six known failures

| idx | test | now |
|-----|------|-----|
| 1979 | `test_discord_connect::test_connect_does_not_wait_for_slash_sync` | **FIXED** — confirms `e3bfc2ebc` |
| 2120 | `test_discord_liveness::…[health2-latency_non_finite]` | still fails (real) |
| 3138-3140 | `test_feishu_lazy_sdk_import` ×3 | **ALL THREE PASS** |
| 3812 | `test_matrix::test_module_importable_without_mautrix` | fails monolithic, **passes isolated** |

### The feishu three are unexplained — and now unexplainable from this run

They were the ones with no traceback ever printed. This run was built to capture that
traceback. There is none, because they passed. Do **not** record this as fixed:

- `1299e974a` (the only recent commit touching that file) is dated 2026-08-10 21:09,
  which PREDATES the original failing run, and round-2 triage confirmed it was already
  on main at `e3bfc2ebc` while they still failed.
- So no landed fix explains it. The behaviour is **nondeterministic and not currently
  reproducing**. If it recurs, the tracebacks are still the missing evidence.

## The 14, by cause

*(All 14 are now fixed — see the status table above. Every group carries a ✅ banner with
its evidence; the original triage is preserved verbatim underneath each one.)*

**✅ A 15th failure — a flaky test that was never one of the 14 — was found and fixed here
(2026-08-12).** `test_matrix.py::TestMatrixReactions` had **three** tests racing a clock:
each set `_reaction_redaction_delay_seconds = 0.01`, then `await asyncio.sleep(0.03)` before
asserting the background redaction had run. A 3× margin a loaded box loses, reporting
`Expected mock to have been awaited once. Awaited 0 times.`

It surfaced as a single failure in one of three whole-file runs while group E's fix was being
confirmed — ~1900 lines from that fix's only hunks, looking exactly like a regression it had
caused. It was not: an A/B with `git checkout main -- tests/gateway/test_matrix.py` and
nothing else varied had the control fail E and pass Reactions, and the picked tree pass E and
fail Reactions.

Fixed by waiting on the effect instead of the clock. The adapter already tracks every
scheduled task in `_reaction_redaction_tasks` (`adapter.py:974`, populated at `:3301`), so a
`_drain_reaction_redactions()` helper snapshots that set and `asyncio.gather`s it. Test-side
only; no production change. The `assert_not_awaited()` checks are kept — that deferral is the
real property under test, and it does not race.

Proof it is actually fixed rather than merely passing: raising the background delay to 0.5s
(simulating the loaded box) fails **all three** tests on the old code and passes all nine on
the new. Whole file `250 passed, 1 skipped`.

### A. ✅ FIXED — Stale test scaffolding — 5 — `test_multiplex_adapter_registry.py`

> **✅ FIXED by `e467da742` (2026-08-11), ancestor of `main` and of the deployed branch.**
> Verified green 2026-08-11 at HEAD `a1dcfc059`: `test_multiplex_adapter_registry.py` —
> **32 passed, 0 failed** (`167 passed in 497.35s` across the three files of groups A+B).
> All five test names below still exist unrenamed, so the green covers them.
>
> The landed fix is **not** the one proposed below. Rather than seeding the attribute in
> each test, `run.py:9885` now guards the write with `hasattr` to match its two sibling
> sites. The "**NOT a production bug**" verdict below was also too strong: the same commit
> found a *sixth* test in this group that had been failing **silently**, where the missing
> map made `:14319` return `None` and `_check_slash_access` deny the command. That branch
> conflated a multiplex FOREIGN profile whose policy has not loaded (a real gap that must
> fail closed) with a runner having no base config at all (documented as an allow-all
> disabled policy). Only the former denies now — a production change, at `:14342`.
>
> The original triage is preserved verbatim below.

`AttributeError: 'GatewayRunner' object has no attribute '_profile_gateway_configs'` at
`gateway/run.py:9885`, in `test_secondary_non_binding_platform_ok`,
`test_multiplex_secondary_skips_relay_but_starts_direct_adapter`,
`test_non_multiplex_profile_adapter_start_keeps_relay`,
`test_secondary_same_config_token_is_refused`, `test_feishu_websocket_mode_not_rejected`.

**NOT a production bug.** `run.py:3155` initialises it in `__init__`. The tests build the
runner with `GatewayRunner.__new__(GatewayRunner)`, bypassing `__init__`, and hand-set only
`config` and `_profile_adapters`. `test_open_policy_uses_fatal_config_error` uses the same
`__new__` pattern and passes only because it raises before reaching :9885.
Fix: add `runner._profile_gateway_configs = {}` to each, or stop using `__new__`.
Note `run.py:9586` and `:14319` already guard the attribute with `hasattr`/`getattr`.

### B. ✅ FIXED — Windows POSIX-isms — 4 — ~~can never pass on this host~~

> **✅ FIXED by `e467da742` (2026-08-11), ancestor of `main` and of the deployed branch.**
> Verified green 2026-08-11 at HEAD `a1dcfc059`: `test_slash_access_dispatch.py` —
> **21 passed**; `test_status.py` — **114 passed**. All four names still exist unrenamed.
>
> **"can never pass on this host" was wrong.** They were made portable, not skipped:
> `printf` → `echo` (a builtin in both `cmd.exe` and POSIX `sh`, and already on the same
> `approval.py` literal allowlist that `printf` was chosen from), and
> `Popen(["sleep","20"])` → `sys.executable`. `printf` and `sleep` ship in `Git\usr\bin`,
> which is only on PATH under Git-Bash — so these had passed *only* under that shell, never
> under PowerShell or cron. One of the two `printf` assertions was additionally **vacuous**
> on Windows: it asserted the command's output was ABSENT from a denial, trivially true
> when the command cannot run at all.

- `test_slash_access_dispatch.py::test_listed_quick_command_runs_for_non_admin`
- `test_slash_access_dispatch.py::test_admin_runs_quick_command_when_gating_enabled`
  → `'printf' is not recognized as an internal or external command`
- `test_status.py::TestGetProcessStartTime::test_live_process_is_stable_int`
- `test_status.py::TestGetProcessStartTime::test_psutil_fallback_when_no_proc`
  → `subprocess.Popen(["sleep","20"])` → `FileNotFoundError: [WinError 2]`

Same class as the five POSIX-isms previously found in the plugins suite.

### C. ✅ FIXED — Real failures — reproduce in isolation — 3

> **✅ All three green at `main` @ `eda64dfdc`, verified 2026-08-11.** The three files ran
> in one invocation with the four C/D/E files: `1 failed, 298 passed, 1 skipped in 727.01s`,
> and the single failure was group E, not one of these.
>
> Attribution, at two different confidence levels — the green above is measured, this is not:
> `bff71e7ed` *"fix(tests+whatsapp): clear the nightly gate lane's 4 named failures"* is on
> `main` and touches `tests/gateway/test_discord_liveness.py` and
> `plugins/platforms/whatsapp/adapter.py`, which covers the `latency_non_finite` and
> `test_kill_port_spares_client_process` entries. For
> `test_update_streaming::test_recognized_slash_command_bypasses_pending_update_prompt` no
> commit was traced here; a separate session records it as fixed by `e467da742`'s
> `run.py:14353` guard (`if getattr(self, "config", None) is None: return None`) — the same
> fail-closed correction described under group A. **That last attribution is second-hand.
> The passing test is not.**
>
> The original triage is preserved verbatim below.

Verified by re-running each file alone (`gw_iso.log`):
- `test_discord_liveness::…[health2-latency_non_finite]` — 1 failed, 18 passed.
  `assert 'latency_non_finite' in 'Discord Gateway WebSocket health check failed: ack_stale'`.
  Root cause already established in round-2 triage: `_FakeKeepAlive.__init__` stamps
  `_last_ack` at bot construction, and `_register_slash_commands()` costing >1.0s masks the
  latency reason. Production ordering is correct; the fake's clock is wrong.
- `test_update_streaming::test_recognized_slash_command_bypasses_pending_update_prompt` —
  1 failed, 20 passed. Got `⛔ Slash commands are unavailable for this profile until its
  policy loads.` **Newly surfaced — past the old 4257 ceiling, never triaged.**
- `test_whatsapp_bridge_pidfile::TestKillPortProcess::test_kill_port_spares_client_process` —
  1 failed, 7 passed. `_wait_dead(listener, timeout=5.0)` — stale listener not killed.
  **Newly surfaced — never triaged.**

### D. ✅ FIXED — ~~Monolithic-ordering artifact~~ — 1

> **✅ Green at `main` @ `eda64dfdc`, verified 2026-08-11**, in the same run as group C.
>
> **The "ordering artifact" label below is WRONG — this was re-derived, not inherited.**
> This entry is the *same* defect as the `latency_non_finite` entry in group C, so the split
> is **4 real + 1 artifact**, not 3 + 2. `_FakeKeepAlive` froze `_last_ack` at construction
> while the adapter computes `ack_age = perf_counter() - _last_ack` at sample time against
> `max_ack_age=1.0`, and `ack_stale` is checked *ahead* of both the latency and healthy paths
> (`plugins/platforms/discord/adapter.py:1399` before `:1404`). Every second between the
> factory call and the sample was charged to the fake heartbeat. Isolation merely shrank that
> gap below 1.0s — it did not remove the bug.
>
> Proof by A/B (2026-08-11): two copies of the file differing **only** in the fake, both
> carrying the same injected `time.sleep(1.2)` after the bot is built. The pre-`bff71e7ed`
> fake fails **both** tests with the two errors recorded in this inventory verbatim —
> `assert 'latency_non_finite' in '…failed: ack_stale'` and `assert adapter._running is True`
> → False — and the fixed fake passes both. Same box, adjacent runs, 32s and 19s.
>
> **Treat "passes in isolation" as insufficient grounds for calling something a run
> artifact.** A defect whose trigger is elapsed wall-clock time passes alone every time.
> Group E is the same lesson from the other direction.

- `test_discord_liveness::test_liveness_probe_does_not_call_rest_while_websocket_is_healthy`
  — `assert adapter._running is True` → False. **PASSES in isolation** (18 passed).
  State leak / ordering, not a defect in the test's subject.

### E. ✅ FIXED — filed as a load artifact, but it had stopped being one

> **✅ Fixed on `main` by `9ffc94018`** *"fix(tests): size the matrix import probe's two
> budgets to its real cost"* (`tests/gateway/test_matrix.py` only, +55/−20), landed
> 2026-08-11. This supersedes the earlier note pointing at the same change as unlanded
> `3e3ea51b9` on `claude/gifted-jones-0cb48d`; it was rebased onto `main` to land.
>
> **The "load artifact" classification was stale.** The child spawn was re-measured five
> times, cold: **11.59 / 13.66 / 16.61 / 18.50 / 24.71s, mean 17.01s — every one over the
> hard-coded `timeout=10`.** The test therefore failed *in isolation*, deterministically, on
> a quiet box (`1 failed in 33.82s`), not merely under load. The "7.2-8.9s warm, crosses 10s
> under load" reading below was true when written and is not true now. That also explains why
> it reproduced in a **four-file** run, not just the 9,898-test monolithic one.
>
> **Raising `timeout=10` alone would have made it worse**, which is the part worth carrying
> forward. `pyproject`'s global `addopts --timeout=30` plus pytest-timeout's thread method
> **hard-exit the whole pytest process**, so any subprocess budget above 30s is unreachable:
> pytest kills the **file** first and every test in it reports as never having run. The two
> budgets must be set together and ordered —
> `_SUBPROCESS_TIMEOUT_S = 180` with `@pytest.mark.timeout(_SUBPROCESS_TIMEOUT_S + 30)`, the
> marker sized *above* the subprocess budget so `subprocess.TimeoutExpired` wins the race and
> names what hung. `tests/gateway/test_feishu_lazy_sdk_import.py:28-45` already documented
> this pattern **and named `test_matrix.py` as a casualty of it** — the fix existed in a
> sibling file and had simply never been applied here.
>
> Verified: passes under the **default** addopts (`1 passed in 29.81s`) — the case the marker
> exists for — and the whole file is `250 passed, 1 skipped`. Forcing the budget to 2s yields
> an explicit "budget overrun — nothing was proven either way" failure instead of the bare
> `TimeoutExpired` that used to read like a product defect.

- `test_matrix::TestMatrixModuleImport::test_module_importable_without_mautrix` —
  `subprocess.TimeoutExpired … timed out after 10 seconds`. Confirmed PASSED when run
  alone (`1 passed in 26.44s`, explicitly PASSED not skipped). Hard-coded `timeout=10` at
  `test_matrix.py:1224` against a cold-interpreter spawn measured at 7.2-8.9s warm — it
  crosses 10s under load. Fires below the pytest cap, so it writes `F` and the run
  continues. Not a product defect.

## Deferred / not done

*(As written, this section describes the state at `e3bfc2ebc`. **Every item has since been
superseded** — annotated inline. **Nothing here is a live to-do.**)*

1. **Nothing was fixed.** No test or production file edited, nothing committed.
   — ✅ **Superseded.** True of *this run*; `e467da742` (2026-08-11) then fixed the A- and
   B-group 9 and landed on `main`.
2. C-group items (3) are the only ones needing real investigation; two are brand new.
   — ✅ **All three green** at `main` @ `eda64dfdc`. Nothing left to investigate here.
3. A-group (5) and B-group (4) are mechanical fixes — 9 of 14 are test-side.
   — ✅ **Done in `e467da742`**, but the "test-side" read was half wrong: the A-group fix
   was production-side (`gateway/run.py`), and it exposed a real fail-closed defect.
4. The load artifact (E) would likely vanish on an idle box; the 0.40 tests/sec rate means
   a rerun under low memory pressure should be ~3h, not 7h.
   — ❌ **Wrong, and now fixed in `9ffc94018`.** E did not need the full suite to fire: it
   reproduced in a four-file, 300-test run, and then — once the child spawn was actually
   measured at 11.6-24.7s against a 10s budget — *alone on an idle box*. It had stopped being
   a load artifact entirely. The throughput observation stands.
