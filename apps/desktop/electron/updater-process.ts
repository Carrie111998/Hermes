import { spawn, type SpawnOptions } from 'node:child_process'

import { hiddenWindowsChildOptions } from './windows-child-options'

export interface UpdaterChild {
  pid?: number
  unref: () => void
  once?: (event: 'error' | 'spawn', listener: (...args: any[]) => void) => unknown
}

export interface SpawnUpdaterProcessDeps {
  isWindows?: boolean
  spawnProcess?: (command: string, args: string[], options: SpawnOptions) => UpdaterChild
}

export type UpdaterSpawnReceipt =
  | { ok: true; stage: 'updater-spawn'; child: UpdaterChild }
  | { ok: false; stage: 'updater-spawn'; error: string }

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

  const child = deps.spawnProcess
    ? deps.spawnProcess(updater, updaterArgs, spawnOptions)
    : spawn(updater, updaterArgs, spawnOptions)

  child.unref()

  return child
}

/**
 * Wait for Node's lifecycle signal before treating an updater handoff as
 * started. `spawn()` reports common launch failures (including ENOENT) through
 * the asynchronous `error` event rather than by throwing synchronously.
 */
export function spawnUpdaterProcessChecked(
  updater: string,
  updaterArgs: string[],
  options: SpawnOptions,
  deps: SpawnUpdaterProcessDeps = {}
): Promise<UpdaterSpawnReceipt> {
  const isWindows = deps.isWindows ?? process.platform === 'win32'
  const spawnOptions = hiddenWindowsChildOptions(options, isWindows) as SpawnOptions
  let child: UpdaterChild

  try {
    child = deps.spawnProcess
      ? deps.spawnProcess(updater, updaterArgs, spawnOptions)
      : spawn(updater, updaterArgs, spawnOptions)
  } catch (error) {
    return Promise.resolve({
      ok: false,
      stage: 'updater-spawn',
      error: error instanceof Error ? error.message : String(error)
    })
  }

  if (!child.once) {
    return Promise.resolve({
      ok: false,
      stage: 'updater-spawn',
      error: 'Updater process did not expose spawn lifecycle events.'
    })
  }

  return new Promise(resolve => {
    let settled = false

    const settle = (receipt: UpdaterSpawnReceipt) => {
      if (!settled) {
        settled = true
        resolve(receipt)
      }
    }

    child.once?.('error', error => {
      settle({
        ok: false,
        stage: 'updater-spawn',
        error: error instanceof Error ? error.message : String(error)
      })
    })
    child.once?.('spawn', () => {
      child.unref()
      settle({ ok: true, stage: 'updater-spawn', child })
    })
  })
}
