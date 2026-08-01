import assert from 'node:assert/strict'
import type { SpawnOptions } from 'node:child_process'

import { test } from 'vitest'

import { spawnUpdaterProcess } from './updater-process'

test('spawnUpdaterProcess hides the updater console and detaches the child on Windows', () => {
  const calls: Array<{ args: string[]; command: string; options: SpawnOptions }> = []
  let unrefCalls = 0

  const child = {
    pid: 4242,
    unref: () => {
      unrefCalls += 1
    }
  }

  const result = spawnUpdaterProcess(
    'hermes-setup.exe',
    ['--update', '--branch', 'main'],
    { cwd: 'C:\\Hermes', detached: true, stdio: 'ignore' },
    {
      isWindows: true,
      spawnProcess: (command, args, options) => {
        calls.push({ args, command, options })

        return child
      }
    }
  )

  assert.equal(result, child)
  assert.equal(unrefCalls, 1)
  // .exe updaters must NOT be routed through cmd.exe: applyUpdates() records
  // child.pid in the marker and the Rust updater's self-PID adoption check
  // expects that PID to be its own.
  assert.deepEqual(calls, [
    {
      args: ['--update', '--branch', 'main'],
      command: 'hermes-setup.exe',
      options: { cwd: 'C:\\Hermes', detached: true, stdio: 'ignore', windowsHide: true }
    }
  ])
})

test('spawnUpdaterProcess enables shell:true for .cmd wrappers on Windows', () => {
  const calls: Array<{ args: string[]; command: string; options: SpawnOptions }> = []

  spawnUpdaterProcess(
    'C:\\Users\\r3dp0\\AppData\\Local\\hermes\\hermes-update-wrapper.cmd',
    ['--update', '--branch', 'main'],
    { cwd: 'C:\\Hermes', detached: true, stdio: 'ignore' },
    {
      isWindows: true,
      spawnProcess: (command, args, options) => {
        calls.push({ args, command, options })

        return { unref: () => {} }
      }
    }
  )

  // shell:true so cmd.exe interprets the .cmd — CreateProcessW can't.
  assert.deepEqual(calls, [
    {
      args: ['--update', '--branch', 'main'],
      command: 'C:\\Users\\r3dp0\\AppData\\Local\\hermes\\hermes-update-wrapper.cmd',
      options: { cwd: 'C:\\Hermes', detached: true, stdio: 'ignore', windowsHide: true, shell: true }
    }
  ])
})

test('spawnUpdaterProcess enables shell:true for .bat wrappers on Windows', () => {
  const calls: Array<{ args: string[]; command: string; options: SpawnOptions }> = []

  spawnUpdaterProcess(
    'C:\\Tools\\hermes-update-wrapper.bat',
    ['--update'],
    { detached: true, stdio: 'ignore' },
    {
      isWindows: true,
      spawnProcess: (command, args, options) => {
        calls.push({ args, command, options })

        return { unref: () => {} }
      }
    }
  )

  assert.deepEqual(calls, [
    {
      args: ['--update'],
      command: 'C:\\Tools\\hermes-update-wrapper.bat',
      options: { detached: true, stdio: 'ignore', windowsHide: true, shell: true }
    }
  ])
})

test('spawnUpdaterProcess preserves updater options off Windows', () => {
  let capturedOptions: SpawnOptions | undefined

  spawnUpdaterProcess(
    'hermes-setup',
    ['--update'],
    { detached: true, stdio: 'ignore' },
    {
      isWindows: false,
      spawnProcess: (_command, _args, options) => {
        capturedOptions = options

        return { unref: () => {} }
      }
    }
  )

  assert.deepEqual(capturedOptions, { detached: true, stdio: 'ignore' })
})

test('spawnUpdaterProcess respects explicit shell:false on Windows', () => {
  const calls: Array<{ args: string[]; command: string; options: SpawnOptions }> = []

  spawnUpdaterProcess(
    'hermes-setup.exe',
    ['--update'],
    { detached: true, stdio: 'ignore', shell: false },
    {
      isWindows: true,
      spawnProcess: (command, args, options) => {
        calls.push({ args, command, options })

        return { unref: () => {} }
      }
    }
  )

  assert.deepEqual(calls, [
    {
      args: ['--update'],
      command: 'hermes-setup.exe',
      options: { detached: true, stdio: 'ignore', windowsHide: true, shell: false }
    }
  ])
})
