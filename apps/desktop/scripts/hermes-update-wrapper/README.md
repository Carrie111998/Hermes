# hermes-update-wrapper — in-house fix for the Windows update loop

## The problem

On Windows, the in-app **Update** button can get stuck in a loop that surfaces as:

> "Another Hermes update is already running."

The full error string is the same one the desktop shows after the Tauri installer bails
during its self-PID adoption check (`update.rs:161-165`).

### Root cause (verified empirically)

The desktop's `applyUpdates()` flow:

1. `spawnUpdaterProcess('hermes-setup.exe', ['--update', '--branch', 'main'], ...)`
2. writes the update marker with `child.pid` (the *Tauri wrapper's* PID)
3. waits 2.5s (`UPDATE_HANDOFF_DWELL_MS`)
4. calls `app.quit()`

`hermes-setup.exe` is a Tauri app. Tauri re-execs its actual updater logic into a
*different* OS process; the lock's PID does not match that inner process's
`std::process::id()`, so the self-PID adoption check in `update.rs:161-165` fails
and the wrapper aborts before it can write its own marker.

The race window is the time between "desktop writes the marker" and "desktop
process actually exits." On a normal machine the desktop closes in <1 second, and
the loop is rare. On slower machines (or with antivirus / Defender real-time
scans of the staged binary), the desktop takes 2-3+ seconds to exit — exactly
long enough for the Tauri inner process to read the marker and decide "this
isn't me, bail."

Verified: closing Hermes *manually* before running `Hermes-Setup.exe --update
--branch main` from a terminal removes the race and the install succeeds.

## The fix

A tiny PowerShell wrapper that sits in front of the real installer and waits
for the desktop GUI to exit before exec'ing the real installer:

- `hermes-update-wrapper.cmd` — entry point. Just hands off to PowerShell.
- `hermes-update-wrapper.ps1` — the actual logic (~250 lines, no deps, no modules).

The desktop's `resolveUpdaterBinary()` now prefers this wrapper when it is
staged; the wrapper then:

1. Sanity-checks that the real installer (`hermes-setup-real.exe`) exists
2. Polls for `Hermes.exe` to exit (30s timeout, 500ms poll, one log line per
   second)
3. Clears any stale `.hermes-update-in-progress` lock left over from the
   aborted apply (only when the lock is ours, dead, or unreadable)
4. Execs the real installer with the original args via `Start-Process` and
   propagates its exit code back to the desktop

The wrapper is non-destructive: the real installer is moved aside to
`hermes-setup-real.exe` once and is left untouched. Reverting = delete the
wrapper files and rename the real installer back.

### Why we only wait for `Hermes.exe`

A previous version of the wrapper also waited for Hermes-managed Python gateway
backends (`node.exe` processes under `C:\Users\r3dp0\AppData\Local\hermes\venv`)
to exit. The backends sometimes orphan after the desktop GUI quits — that's a
separate Hermes bug (the desktop doesn't reap its child processes on quit).
Waiting for them made the wrapper hang for up to 60s on machines where the
backends don't get reaped.

The Tauri installer handles orphaned backends itself; the only process that
blocks the file replacement is the desktop GUI. So we wait only on `Hermes.exe`,
which normally takes 1-5 seconds to close (covers the slow close case from the
race-condition root cause above), and 30 seconds as a safety net.

## Exit codes

| code | meaning |
| ---- | ------- |
| `0`  | real installer succeeded |
| `2`  | real installer missing (`hermes-setup-real.exe` not found) |
| `3`  | timeout (30s) waiting for `Hermes.exe` to exit |
| `4`  | refused — live foreign update-in-progress lock held by another process |

## Manual one-time install

Until the Tauri installer is updated to stage the wrapper, the user runs:

```powershell
# 1. Build the patched desktop (see top-level patch)
cd $env:LOCALAPPDATA\hermes\hermes-agent
npm run build
npm run pack
# deploys to apps/desktop/release/win-unpacked/

# 2. Install the wrapper
$HERMES_HOME = Join-Path $env:LOCALAPPDATA 'hermes'
Copy-Item "$HERMES_HOME\hermes-agent\apps\desktop\scripts\hermes-update-wrapper\*" $HERMES_HOME -Force

# 3. Move the real installer aside
Move-Item "$HERMES_HOME\hermes-setup.exe" "$HERMES_HOME\hermes-setup-real.exe" -Force
```

## Rollback

```powershell
$HERMES_HOME = Join-Path $env:LOCALAPPDATA 'hermes'
Remove-Item "$HERMES_HOME\hermes-update-wrapper.cmd" -Force
Remove-Item "$HERMES_HOME\hermes-update-wrapper.ps1" -Force
Move-Item "$HERMES_HOME\hermes-setup-real.exe" "$HERMES_HOME\hermes-setup.exe" -Force
```

## Logs

`%LOCALAPPDATA%\hermes\logs\update-wrapper.log` — append-only, one line per
event, includes process tree, timing, and the installer's exit code. Levels
are tagged in `[brackets]` (`info`, `ok`, `warn`, `error`) so the file is
greppable in editors and CI.

Example log from a successful run (desktop closed in 2.1s, installer took
~14s, total ~17s):

```
2026-07-31T17:55:01.034-04:00  [info ] ========================================================================
2026-07-31T17:55:01.034-04:00  [info ] Hermes update wrapper
2026-07-31T17:55:01.034-04:00  [info ] ========================================================================
2026-07-31T17:55:01.034-04:00  [info ] args:     --update --branch main
2026-07-31T17:55:01.034-04:00  [info ] pid:      21364
2026-07-31T17:55:01.034-04:00  [info ] user:     r3dp0
2026-07-31T17:55:01.034-04:00  [info ] home:     C:\Users\r3dp0\AppData\Local\hermes
2026-07-31T17:55:01.034-04:00  [info ] timeout:  30s  poll: 500ms
2026-07-31T17:55:01.034-04:00  [info ] installer: hermes-setup-real.exe
2026-07-31T17:55:01.034-04:00  [info ] waiting for Hermes.exe to exit (timeout: 30s, poll: 500ms)
2026-07-31T17:55:01.050-04:00  [info ]   t= 0s  waiting on Hermes.exe
2026-07-31T17:55:02.052-04:00  [info ]   t= 1s  waiting on Hermes.exe
2026-07-31T17:55:03.053-04:00  [info ]   t= 2s  waiting on Hermes.exe
2026-07-31T17:55:03.140-04:00  [ok  ] Hermes.exe exited after 2.1s
2026-07-31T17:55:03.140-04:00  [ok  ] cleared lock: ...\.hermes-update-in-progress (lock is owned by this handoff)
2026-07-31T17:55:03.140-04:00  [info ] launching real installer: ...\hermes-setup-real.exe --update --branch main
2026-07-31T17:55:17.300-04:00  [ok  ] real installer completed in 14.2s (exit 0)
2026-07-31T17:55:17.300-04:00  [info ] ========================================================================
2026-07-31T17:55:17.300-04:00  [info ] done
2026-07-31T17:55:17.300-04:00  [info ] ========================================================================
```

## Source patch (companion to this directory)

The companion source changes are in:

- `apps/desktop/electron/main.ts` — `resolveUpdaterBinary()` prefers
  `hermes-update-wrapper.cmd` when present
- `apps/desktop/electron/updater-process.ts` — adds `shell: true` to spawn
  options **only for `.cmd`/`.bat` updater paths** on Windows so `cmd.exe`
  interprets the wrapper. The default Tauri `.exe` updater continues to
  spawn directly because `applyUpdates()` records `child.pid` in the
  update marker and the Rust updater's self-PID adoption check expects
  that PID to match its own.
- `apps/desktop/electron/updater-process.test.ts` — updated for the new
  gating plus new tests for `.cmd` and `.bat` paths

## Lock handling

`hermes-update-wrapper.ps1` only deletes `.hermes-update-in-progress` when:

- the lock's owner PID is part of this handoff (this powershell.exe or its
  parent cmd.exe that the desktop spawned), or
- the owner PID is no longer alive (stale leftover), or
- the lock file is unreadable / has no PID.

A live foreign lock (a PID that is alive and not us) is treated as another
updater (dashboard or terminal `hermes update`) and the wrapper refuses to
delete it — the script exits with code 4 so the live update can complete
without being clobbered.

## Long-term

The proper fix is in the Tauri installer (Rust) — either:

- Have the inner process write its own marker immediately (before reading the
  desktop's marker), or
- Drop the self-PID adoption check entirely and trust the desktop's marker, or
- Have the desktop wait for the wrapper's first marker write before quitting

The wrapper is the path of least resistance for users who can't wait for an
upstream fix; the underlying bug is tracked in
[NousResearch/hermes-agent#75556](https://github.com/NousResearch/hermes-agent/issues/75556).
