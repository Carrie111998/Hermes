# Carlos Build Log — t_2806ab3c

Task: Fix Kanban notifier synthetic-message authorization
Branch: `carlos/fix-kanban-notifier-auth-20260826`

## Summary

Fixed the active-session gateway authorization path so trusted internal synthetic wake events (including Kanban notifier wakes) bypass external platform-user authorization before the `user_id=None` rejection path, then fall through to the existing base-adapter behavior that queues internal events silently behind the active session. Real inbound `user_id=None` / unknown external messages still hit the busy-session authorization gate and are dropped.

## Files changed

- `gateway/run.py`
  - Moved the `event.internal` busy-session bypass ahead of `_is_user_authorized()` in `_handle_active_session_busy_message`.
  - Kept the behavior narrow: internal synthetic events return `False` so the base adapter queues them; no global authorization of `user_id=None`.
- `tests/gateway/test_kanban_notifier.py`
  - Added a minimal Telegram-shaped adapter that exercises `BasePlatformAdapter.handle_message()` active-session guard behavior.
  - Added regression coverage for completed and blocked Kanban notifier wakes into an active Telegram DM session.
  - Added duplicate real inbound `user_id=None` active-DM messages test proving they remain blocked and do not queue.

## Verification

Passed:

- `scripts/run_tests.sh tests/gateway/test_kanban_notifier.py -q`
  - 9 tests passed.
- `scripts/run_tests.sh tests/gateway/test_kanban_notifier.py tests/gateway/test_kanban_notifier_apiserver_wake.py tests/gateway/test_internal_event_bypass_pairing.py tests/gateway/test_kanban_watchers_mixin.py -q`
  - 14 tests passed.
- `scripts/run_tests.sh tests/gateway/test_busy_session_auth_bypass.py tests/gateway/test_internal_event_never_interrupts_busy_session.py tests/gateway/test_busy_session_ack.py tests/gateway/test_telegram_auth_check.py tests/gateway/test_unauthorized_dm_behavior.py -q`
  - 33 tests passed.
- `scripts/run_tests.sh tests/gateway/test_kanban_notifier.py tests/gateway/test_kanban_notifier_apiserver_wake.py tests/gateway/test_kanban_notifier_zero_sub_gate.py tests/gateway/test_kanban_notifier_watcher_dispatch_gate.py tests/gateway/test_kanban_watchers_mixin.py tests/hermes_cli/test_kanban_notify.py tests/hermes_cli/test_kanban_count_notify_subs.py -q`
  - 17 tests passed.
- After removing a temporary `uv run` worktree `.venv`, reran the consolidated relevant set:
  - `scripts/run_tests.sh tests/gateway/test_kanban_notifier.py tests/gateway/test_kanban_notifier_apiserver_wake.py tests/gateway/test_internal_event_bypass_pairing.py tests/gateway/test_busy_session_auth_bypass.py tests/gateway/test_internal_event_never_interrupts_busy_session.py tests/gateway/test_busy_session_ack.py tests/gateway/test_telegram_auth_check.py tests/gateway/test_unauthorized_dm_behavior.py tests/hermes_cli/test_kanban_notify.py tests/hermes_cli/test_kanban_count_notify_subs.py -q`
  - 50 tests passed.
- `uvx --with ruff ruff check gateway/run.py tests/gateway/test_kanban_notifier.py && git diff --check`
  - Ruff passed; diff whitespace check passed.
- `python -m compileall -q gateway/run.py tests/gateway/test_kanban_notifier.py`
  - Passed.

Broader suite note:

- `scripts/run_tests.sh tests/gateway -q` ran 570 gateway files; 4437 tests passed and 4 pre-existing/environment-shaped tests failed outside this change area:
  - `tests/gateway/test_api_server.py::TestHealthDetailedEndpoint::test_health_detailed_returns_ok` saw readiness `degraded` instead of `ok`.
  - `tests/gateway/test_readiness.py::test_collect_runtime_readiness_reports_healthy_local_runtime` saw readiness `degraded` instead of `ok`.
  - `tests/gateway/test_shutdown_forensics.py::TestSpawnAsyncDiagnostic::test_spawns_subprocess_and_writes_output` returned no diagnostic subprocess pid.
  - `tests/gateway/test_systemd_notify.py::test_notify_supports_systemd_abstract_socket` failed binding Linux abstract Unix socket on macOS.

## Restart instruction

Safe operator instruction for Tristan after review/merge/deploy: send `/restart` in the active Telegram DM/channel for the gateway. Do not restart the gateway from this worker.

## Docs / skill notes

No docs or skill patch required. Implementation behavior matches existing `gateway.wake` and busy-session comments: internal synthetic events are trusted gateway-generated messages and should queue behind active sessions without interrupting or running external-user authorization.
