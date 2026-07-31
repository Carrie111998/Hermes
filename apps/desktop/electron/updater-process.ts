import { spawn, type SpawnOptions } from 'node:child_process'

import { hiddenWindowsChildOptions } from './windows-child-options'

export interface UpdaterChild {
  pid?: number
  unref: () => void
}

export interface SpawnUpdaterProcessDeps {
  isWindows?: boolean
  spawnProcess?: (command: string, args: string[], options: SpawnOptions) => UpdaterChild
}

/**
 * Spawn the detached installer used for update and bootstrap-recovery handoffs.
 * The helper owns both hidden-console selection and unref semantics so every
 * updater handoff follows the same behavior and can be tested without Electron.
 */
export function spawnUpdaterProcess(
  updater: string,
  updaterArgs: string[],
  options: SpawnOptions,
  deps: SpawnUpdaterProcessDeps = {}
): UpdaterChild {
  const isWindows = deps.isWindows ?? process.platform === 'win32'
  const spawnOptions = hiddenWindowsChildOptions(options, isWindows) as SpawnOptions

  // On Windows, route `.cmd` / `.bat` updater wrappers through `cmd.exe`.
  // The default Tauri updater is a `.exe` and is spawned directly — `applyUpdates()`
  // records `child.pid` in the update marker and the Rust updater's
  // self-PID adoption check (apps/bootstrap-installer/src-tauri/src/update.rs:161-165)
  // expects that PID to match its own, so we must NOT route `.exe` through `cmd.exe`
  // (that would make `child.pid` the cmd.exe wrapper PID and break adoption).
  // The in-house race-condition fix (hermes-update-wrapper.cmd) is a `.cmd` and
  // CreateProcessW cannot execute it directly, so we opt in to `shell:true`
  // only for `.cmd`/`.bat` paths. The caller can opt out by passing
  // `shell:false` explicitly. Safe because the spawn is detached + unref and
  // the wrapper closes its own console window.
  const isCmdLikeLauncher = isWindows && /\.(cmd|bat)$/i.test(updater)
  if (isCmdLikeLauncher && !Object.prototype.hasOwnProperty.call(spawnOptions, 'shell')) {
    spawnOptions.shell = true
  }

  const child = deps.spawnProcess
    ? deps.spawnProcess(updater, updaterArgs, spawnOptions)
    : spawn(updater, updaterArgs, spawnOptions)

  child.unref()

  return child
}
