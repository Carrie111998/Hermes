# hermes-update-wrapper — in-house fix for the Windows update loop

## The problem

On Windows, the in-app **Update** button can get stuck in a loop that surfaces as:

> "Another Hermes update is already running."

The full error string is the same one the desktop shows after the Tauri installer bails
during its self-PID adoption check (`update.rs:163-165`).

### Root cause (verified empirically)

The desktop's `applyUpdates()` flow:

1. `spawnUpdaterProcess('hermes-setup.exe', ['--update', '--branch', 'main'], ...)`
2. writes the update marker with `child.pid` (the *Tauri wrapper's* PID)
3. waits 2.5s (`UPDATE_HANDOFF_DWELL_MS`)
4. calls `app.quit()`

`hermes-setup.exe` is a Tauri app. Tauri re-execs its actual updater logic into a
*different* OS process; the lock's PID does not match that inner process's
`std::process::id()`, so the self-PID adoption check in `update.rs:163-165` fails
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
for the desktop to fully exit before exec'ing the real installer:

- `hermes-update-wrapper.cmd` — entry point. Just hands off to PowerShell.
- `hermes-update-wrapper.ps1` — the actual logic (≈150 lines, no deps).

The desktop's `resolveUpdaterBinary()` now prefers this wrapper when it is
staged; the wrapper then:

1. Sanity-checks that the real installer (`hermes-setup-real.exe`) exists
2. Polls for `Hermes.exe` and Hermes-managed `node.exe` backend processes to
   exit (60s timeout, 1s poll, progress log every 5s)
3. Waits an extra 1s "grace" for the venv shim to release its locks
4. Clears any stale `.hermes-update-in-progress` lock left over from the
   aborted apply
5. Execs the real installer with the original args via `Start-Process` and
   propagates its exit code back to the desktop

The wrapper is non-destructive: the real installer is moved aside to
`hermes-setup-real.exe` once and is left untouched. Reverting = delete the
wrapper files and rename the real installer back.

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
event, includes process tree, timing, and the installer's exit code.

## Source patch (companion to this directory)

The companion source changes are in:

- `apps/desktop/electron/main.ts` — `resolveUpdaterBinary()` prefers
  `hermes-update-wrapper.cmd` when present
- `apps/desktop/electron/updater-process.ts` — adds `shell: true` to spawn
  options on Windows so `cmd.exe` interprets the `.cmd` (CreateProcessW can't
  execute `.cmd` directly)
- `apps/desktop/electron/updater-process.test.ts` — updated for the new
  `shell: true` default, plus a new test verifying the caller's `shell: false`
  opt-out is respected

## Long-term

The proper fix is in the Tauri installer (Rust) — either:

- Have the inner process write its own marker immediately (before reading the
  desktop's marker), or
- Drop the self-PID adoption check entirely and trust the desktop's marker, or
- Have the desktop wait for the wrapper's first marker write before quitting

The wrapper is the path of least resistance for users who can't wait for an
upstream fix; the underlying bug is tracked in
[NousResearch/hermes-agent#75556](https://github.com/NousResearch/hermes-agent/issues/75556).
