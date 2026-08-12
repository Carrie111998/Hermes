# tests/gateway — first COMPLETED monolithic run — failure inventory

**2026-08-11.** `14 failed, 9829 passed, 55 skipped, 322 warnings in 26004.68s (7:13:24)`

The suite has now finished a single-interpreter run for the first time. All 9,898
tests executed, including the ~57% past index 4257 that had never run.

- Worktree: `.claude/worktrees/eager-kepler-ddb335` @ `e3bfc2ebc` (isolated; shared checkout untouched)
- Command: `python -u -m pytest tests/gateway -p no:cacheprovider -q -rf --tb=short --timeout=900`
- `CLAUDE_CODE_ENTRYPOINT` / `CLAUDECODE` / `HERMES_DISABLE_MESSAGE_TRIGRAM` cleared
- Artifacts: `gw_full2.log` (raw), `gw_full2_failures.txt` (14 tracebacks), `gw_iso.log` (isolation re-run)
- Measured rate 0.40 tests/sec, not the assumed ~1/sec: the box sat at **96.8% commit charge**
  (56 `claude` processes = 29 GB, vmmemWSL 15 GB, chrome 15 GB). This matters — see #10.

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

### A. Stale test scaffolding — 5 — `test_multiplex_adapter_registry.py`
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

### B. Windows POSIX-isms — 4 — can never pass on this host
- `test_slash_access_dispatch.py::test_listed_quick_command_runs_for_non_admin`
- `test_slash_access_dispatch.py::test_admin_runs_quick_command_when_gating_enabled`
  → `'printf' is not recognized as an internal or external command`
- `test_status.py::TestGetProcessStartTime::test_live_process_is_stable_int`
- `test_status.py::TestGetProcessStartTime::test_psutil_fallback_when_no_proc`
  → `subprocess.Popen(["sleep","20"])` → `FileNotFoundError: [WinError 2]`

Same class as the five POSIX-isms previously found in the plugins suite.

### C. Real failures — reproduce in isolation — 3
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

### D. Monolithic-ordering artifact — 1
- `test_discord_liveness::test_liveness_probe_does_not_call_rest_while_websocket_is_healthy`
  — `assert adapter._running is True` → False. **PASSES in isolation** (18 passed).
  State leak / ordering, not a defect in the test's subject.

### E. Load artifact — 1
- `test_matrix::TestMatrixModuleImport::test_module_importable_without_mautrix` —
  `subprocess.TimeoutExpired … timed out after 10 seconds`. Confirmed PASSED when run
  alone (`1 passed in 26.44s`, explicitly PASSED not skipped). Hard-coded `timeout=10` at
  `test_matrix.py:1224` against a cold-interpreter spawn measured at 7.2-8.9s warm — it
  crosses 10s under load. Fires below the pytest cap, so it writes `F` and the run
  continues. Not a product defect.

## Deferred / not done
1. **Nothing was fixed.** No test or production file edited, nothing committed.
2. C-group items (3) are the only ones needing real investigation; two are brand new.
3. A-group (5) and B-group (4) are mechanical fixes — 9 of 14 are test-side.
4. The load artifact (E) would likely vanish on an idle box; the 0.40 tests/sec rate means
   a rerun under low memory pressure should be ~3h, not 7h.
