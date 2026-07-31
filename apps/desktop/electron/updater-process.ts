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

  // On Windows, route .cmd / .bat updater wrappers through cmd.exe. The default
  // Tauri updater is a .exe and works fine without `shell:true`, but our
  // in-house race-condition fix (hermes-update-wrapper.cmd) lives in
  // HERMES_HOME and CreateProcessW will refuse to execute a .cmd directly. We
  // opt in via shell:true so any .cmd / .bat updater in HERMES_HOME works.
  // The caller can opt out by passing shell:false explicitly. safe because the
  // spawn is detached + unref + the wrapper closes its own console window.
  if (isWindows && !Object.prototype.hasOwnProperty.call(spawnOptions, 'shell')) {
    spawnOptions.shell = true
  }

  const child = deps.spawnProcess
    ? deps.spawnProcess(updater, updaterArgs, spawnOptions)
    : spawn(updater, updaterArgs, spawnOptions)

  child.unref()

  return child
}
