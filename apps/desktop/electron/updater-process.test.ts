import assert from 'node:assert/strict'
import type { SpawnOptions } from 'node:child_process'
import { EventEmitter } from 'node:events'

import { test } from 'vitest'

import { spawnUpdaterProcess, spawnUpdaterProcessChecked } from './updater-process'

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
  assert.deepEqual(calls, [
    {
      args: ['--update', '--branch', 'main'],
      command: 'hermes-setup.exe',
      options: { cwd: 'C:\\Hermes', detached: true, stdio: 'ignore', windowsHide: true }
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

test('spawnUpdaterProcessChecked returns an explicit terminal failure when updater spawn throws', async () => {
  const result = await spawnUpdaterProcessChecked(
    'hermes-setup.exe',
    ['--update'],
    { detached: true, stdio: 'ignore' },
    {
      isWindows: true,
      spawnProcess: () => {
        throw new Error('access denied')
      }
    }
  )

  assert.deepEqual(result, {
    ok: false,
    stage: 'updater-spawn',
    error: 'access denied'
  })
})

test('spawnUpdaterProcessChecked waits for the spawn event before returning success', async () => {
  let unrefCalls = 0

  const child = Object.assign(new EventEmitter(), {
    pid: 4242,
    unref: () => {
      unrefCalls += 1
    }
  })

  const pending = spawnUpdaterProcessChecked(
    'hermes-setup.exe',
    ['--update'],
    { detached: true, stdio: 'ignore' },
    { spawnProcess: () => child }
  )

  let settled = false
  pending.finally(() => {
    settled = true
  })
  await Promise.resolve()
  assert.equal(settled, false)

  child.emit('spawn')
  const result = await pending

  assert.deepEqual(result, { ok: true, stage: 'updater-spawn', child })
  assert.equal(unrefCalls, 1)
})

test('spawnUpdaterProcessChecked catches asynchronous child-process errors', async () => {
  let unrefCalls = 0

  const child = Object.assign(new EventEmitter(), {
    unref: () => {
      unrefCalls += 1
    }
  })

  const pending = spawnUpdaterProcessChecked(
    'missing-hermes-setup.exe',
    ['--update'],
    { detached: true, stdio: 'ignore' },
    { spawnProcess: () => child }
  )

  child.emit('error', new Error('ENOENT'))

  assert.deepEqual(await pending, { ok: false, stage: 'updater-spawn', error: 'ENOENT' })
  assert.equal(unrefCalls, 0)
})
