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

**13 of the 14 are FIXED. Exactly one — the group E load artifact — still fails.**
Every one of the 14 was re-run against `main` @ `eda64dfdc` on 2026-08-11; nothing below is
an inherited claim.

| group | count | status at `eda64dfdc` | evidence |
|-------|-------|-----------------------|----------|
| A | 5 | ✅ fixed by `e467da742` | 32 passed |
| B | 4 | ✅ fixed by `e467da742` | 21 + 114 passed |
| C | 3 | ✅ green | see group C |
| D | 1 | ✅ green — but the "ordering artifact" reading is disputed | see group D |
| E | 1 | ❌ **still fails** — the only open entry | see group E |

Two runs, both from an isolated worktree, both `0 failed` except where noted:
`167 passed in 497.35s` over the three A+B files, and `1 failed, 298 passed, 1 skipped in
727.01s` over the four C/D/E files — that single failure being group E.

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

*(9 of these are now fixed — see the baseline note above. Groups A and B are annotated;
C, D and E are untouched and still need the re-check.)*

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
> **The "ordering artifact" label below is disputed.** `bff71e7ed` edits
> `tests/gateway/test_discord_liveness.py` — a *test-side* change to the very file this
> entry lives in, which is not what you would expect if the cause were a state leak from
> some other module. A separate session records it as the same frozen-fake-clock bug as the
> `latency_non_finite` entry next to it, making the split 4 real + 1 artifact rather than
> 3 + 2. **That reading was not re-derived here** — only the green was measured. Treat
> "passes in isolation" as insufficient grounds for calling something a run artifact.

- `test_discord_liveness::test_liveness_probe_does_not_call_rest_while_websocket_is_healthy`
  — `assert adapter._running is True` → False. **PASSES in isolation** (18 passed).
  State leak / ordering, not a defect in the test's subject.

### E. ❌ STILL OPEN — Load artifact — 1 — the only remaining entry

> **❌ Still fails at `main` @ `eda64dfdc`, reproduced 2026-08-11.** The one failure in
> `1 failed, 298 passed, 1 skipped in 727.01s`, for exactly the cause diagnosed below:
> `subprocess.TimeoutExpired … timed out after 10 seconds` on the cold-interpreter spawn.
> Nothing has landed against it.
>
> Note it reproduced in a **four-file** run, not just the 9,898-test monolithic one — so
> "only fails under the full suite" understates it. Any concurrent load on this box is
> enough. The fix is to raise or remove the hard-coded `timeout=10` at `test_matrix.py:1224`,
> which is a wall-clock deadline in a test, not a property of the code under test.

- `test_matrix::TestMatrixModuleImport::test_module_importable_without_mautrix` —
  `subprocess.TimeoutExpired … timed out after 10 seconds`. Confirmed PASSED when run
  alone (`1 passed in 26.44s`, explicitly PASSED not skipped). Hard-coded `timeout=10` at
  `test_matrix.py:1224` against a cold-interpreter spawn measured at 7.2-8.9s warm — it
  crosses 10s under load. Fires below the pytest cap, so it writes `F` and the run
  continues. Not a product defect.

## Deferred / not done

*(As written, this section describes the state at `e3bfc2ebc`. **Every item has since been
superseded** — annotated inline. Nothing here is a live to-do except the group E timeout.)*

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
   — ⚠️ **Half wrong, and it is now the only open entry.** E reproduced in a four-file,
   300-test run, so it does not need the full suite to fire — "an idle box" is a stronger
   precondition than assumed. The throughput observation stands.
