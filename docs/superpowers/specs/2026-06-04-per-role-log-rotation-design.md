# Per-Role Catch-All Log Rotation (Windows-safe true rotation)

**Date:** 2026-06-04
**Component:** `hermes_logging.py`
**Status:** Design approved — proceeding to implementation plan
**Related:** commit `422b5928f` (Windows-safe graceful-defer rollover), memory
`windows_agent_log_rotation_deployed.md`

## Problem

On Windows, `~/.hermes/logs/agent.log` grows without bound (~2.4 MB/day)
instead of staying capped at `max_size_mb` (5 MB). The graceful-defer fix
shipped 2026-06-04 (`_ManagedRotatingFileHandler`) correctly STOPPED the
prior crash (a traceback storm to stderr on every emit once the file hit
`maxBytes`) and PRESERVES the backup chain — but it cannot make rotation
COMPLETE.

Root cause: `setup_logging()` attaches the `agent.log` (and `errors.log`)
catch-all handler to the **root logger in every Hermes process**. Multiple
long-lived daemons each open the *shared* `agent.log` at startup and hold
the handle for their entire lifetime:

- `hermes gateway run`
- `hermes dashboard`
- `hermes proxy start --provider nous`
- `profiles/main/scripts/devflow_bridge_runner.py` (cron wrapper; may be
  transient — handled identically either way)

On Windows an open handle in ANY process blocks the rename (no
`FILE_SHARE_DELETE`), so the `base → .1` rename perpetually defers (the
fix's graceful path). The file therefore never rotates while the fleet is
up.

Key observation: the gateway's own `gateway-forensics.log` is a
**single-holder** file (attached only in `mode="gateway"`) and already
rotates fine. This confirms the bug is specifically the **shared** catch-all
files, not the rollover mechanics.

## Goal / Acceptance

- The catch-all log (or its per-role replacement) demonstrably rotates while
  gateway + dashboard + proxy + devflow-bridge are all running.
- Backups still preserved; no traceback storms.
- All existing tests in `tests/test_hermes_logging.py` (incl.
  `TestWindowsSafeRollover`) still pass.
- agent-src is local-only — commit to `main`, do NOT push. Gateway restart to
  load is a USER action (watchdog respawns a killed gateway in ~1 min).

## Design

### Core idea

In a **daemon** process, route the catch-all files (`agent.log`,
`errors.log`) to a **per-role** name. Each daemon becomes the sole long-lived
holder of its own files, so the existing `_ManagedRotatingFileHandler`
rotates them to completion with no cross-process lock. Transient `cli`/`cron`
processes keep the shared `agent.log` — which now rotates fine because no
daemon pins it.

```
role = None  (transient cli/cron)  ->  agent.log        errors.log
role = "gateway"                    ->  agent-gateway.log        errors-gateway.log
role = "dashboard"                  ->  agent-dashboard.log      errors-dashboard.log
role = "proxy"                      ->  agent-proxy.log          errors-proxy.log
role = "devflow-bridge"             ->  agent-devflow-bridge.log errors-devflow-bridge.log
```

### 1. Role resolution — explicit, not magic

`setup_logging()` gains a `role: Optional[str] = None` parameter:

- `role is None` and `mode != "gateway"` → `agent.log` / `errors.log`
  (unchanged; this is the transient path and keeps EVERY existing test green —
  existing tests call `setup_logging()` with no `role`).
- `role` given → `agent-<role>.log` / `errors-<role>.log`.
- `mode == "gateway"` defaults `role` to `"gateway"` when `role` is None, so
  `gateway/run.py` needs no change.

A new **pure helper** lives alongside but OUTSIDE `setup_logging`:

```python
DAEMON_ROLE_SIGNATURES = (
    # (predicate over argv tokens, role name)
    ("gateway", "gateway"),
    ("dashboard", "dashboard"),
    ("proxy", "proxy"),
)

def infer_daemon_role(argv=None) -> Optional[str]:
    """Best-effort role from process argv. Pure / argv-only so it is
    deterministic and unit-testable. Returns None for transient processes."""
```

Rules:
- `argv[0]` basename starting with `devflow_bridge_runner` → `"devflow-bridge"`.
- token `"dashboard"` / `"proxy"` / `"gateway"` present in `argv[1:]` → that role.
- else → `None`.

Keeping inference out of `setup_logging` means the function has no hidden
dependency on `sys.argv`; tests drive it by passing `role=` explicitly.

### 2. Wiring (production call sites only)

Two import-time call sites compute the role and pass it explicitly:

- `hermes_cli/main.py:347` — `_setup_logging(mode="cli", role=infer_daemon_role())`
- `cli.py:678` — `setup_logging(mode="cli", role=infer_daemon_role())`

`gateway/run.py:18692` is unchanged (`mode="gateway"` → role defaults to
`"gateway"`). For the devflow bridge: if `hermes_to_devflow` initializes
hermes logging, it inherits role detection via argv; if it is purely
transient (cron wrapper exits in seconds) it simply stays on the shared
`agent.log`, which is correct.

### 3. Rotation mechanics — unchanged

`_ManagedRotatingFileHandler` and its retry / graceful-defer / cooldown logic
are NOT modified. They now operate on private per-role files, so the
cross-process lock path effectively never trips for daemons. All
`TestWindowsSafeRollover` tests pass untouched.

### 4. Retention — no reaper needed

Per-role files are singletons reused across watchdog respawns: the same role
appends and rotates in place, capped at `maxBytes * (backupCount + 1)`. No
dead-PID file explosion, so no cleanup sweep is required. Transient cli/cron
keep sharing ONE `agent.log` (no per-PID churn either). This is the decisive
advantage of per-role over per-PID.

### 5. Gateway extras unchanged

`gateway.log` (gateway.* loggers only) and `gateway-forensics.log` (INFO+,
unfiltered, `HERMES_GATEWAY_LOG_FILE`-overridable) stay exactly as they are.
`agent-gateway.log` overlaps `gateway-forensics.log` in content but the two
have distinct level/override semantics; keeping both is harmless and
preserves existing forensics tooling.

### 6. Tooling

`hermes_cli/logs.py` `LOG_FILES` map gains role entries so each daemon
catch-all is tail-able by name:

```
"agent-gateway", "agent-dashboard", "agent-proxy", "agent-devflow-bridge"
```

Default `hermes logs` still reads `agent.log`; gateway activity also remains
in `gateway.log`. `hermes logs` does NOT merge the whole `agent*.log` family
(merge-tail is more code for marginal gain). `hermes debug` snapshot MAY
include the role files (optional, low priority).

## Components & boundaries

| Unit | Responsibility | Depends on |
|------|----------------|------------|
| `infer_daemon_role(argv)` | argv → role name or None (pure) | nothing |
| `setup_logging(role=...)` | pick catch-all filenames from role; attach handlers | `infer` only via callers |
| `_catch_all_paths(log_dir, role)` (internal) | role → (agent path, errors path) | `role` |
| `_ManagedRotatingFileHandler` | Windows-safe rollover (unchanged) | — |
| call sites (`main.py`, `cli.py`) | compute role from argv, pass to setup_logging | `infer_daemon_role` |
| `hermes_cli/logs.py` LOG_FILES | expose role files to `hermes logs` | filenames |

## Testing

Extend `tests/test_hermes_logging.py`:

1. **Role resolution table** (`infer_daemon_role`): gateway / dashboard /
   proxy argv tokens, devflow_bridge_runner argv[0], and a transient argv → None.
2. **Daemon role → per-role catch-all**: `setup_logging(role="dashboard")`
   attaches a handler whose `baseFilename` ends `agent-dashboard.log` (and
   `errors-dashboard.log`), and NO handler at the bare `agent.log`.
3. **`mode="gateway"` defaults role to gateway**: catch-all is
   `agent-gateway.log`.
4. **Transient (no role) unchanged**: `setup_logging()` → `agent.log` /
   `errors.log` (this is what every existing test exercises).
5. **End-to-end isolation** (the acceptance proof): a per-role handler on
   `agent-dashboard.log` rotates to completion (real `os.replace`, backups
   shift) WHILE a second open handle pins the shared `agent.log` — proving the
   private file is immune to the cross-process lock that blocks the shared one.
   On Windows this uses a real second handle (mirrors
   `test_real_windows_lock_is_nonfatal`); cross-platform it asserts the
   rename path on the private file succeeds regardless.
6. **All existing tests pass unchanged** — `TestWindowsSafeRollover`,
   `TestSetupLogging`, `TestGatewayMode`, `TestGatewayForensicsLog`,
   `TestAddRotatingHandler`, etc.

## Non-goals / YAGNI

- No socket/QueueListener single-sink (rejected: highest Windows risk).
- No per-PID files / dead-PID reaper (rejected: file churn).
- No `hermes logs` family-merge tail.
- No change to rollover retry/cooldown constants or backup-chain logic.

## Rollback

Single-file revert of `hermes_logging.py` + the two call sites + `logs.py`
restores the prior shared-`agent.log` behavior (benign perpetual-defer).
No data migration; existing `agent.log` / backups are untouched.
