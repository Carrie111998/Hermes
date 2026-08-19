import assert from 'node:assert/strict'

import { test } from 'vitest'

import { stopBackendChild, stopBackendTreesForUpdate } from './backend-child'
import { hiddenWindowsChildOptions } from './windows-child-options'

test('hiddenWindowsChildOptions adds windowsHide:true on Windows when unset', () => {
  assert.deepEqual(hiddenWindowsChildOptions({}, true), { windowsHide: true })
})

test('hiddenWindowsChildOptions preserves an existing windowsHide:false on Windows', () => {
  assert.deepEqual(hiddenWindowsChildOptions({ windowsHide: false }, true), { windowsHide: false })
})

test('hiddenWindowsChildOptions preserves an existing windowsHide:true on Windows', () => {
  assert.deepEqual(hiddenWindowsChildOptions({ windowsHide: true }, true), { windowsHide: true })
})

test('hiddenWindowsChildOptions leaves options unchanged off Windows', () => {
  assert.deepEqual(hiddenWindowsChildOptions({}, false), {})
  assert.deepEqual(hiddenWindowsChildOptions({ stdio: 'ignore' }, false), { stdio: 'ignore' })
})

test('hiddenWindowsChildOptions merges windowsHide alongside other options on Windows', () => {
  assert.deepEqual(hiddenWindowsChildOptions({ encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }, true), {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'ignore'],
    windowsHide: true
  })
})

test('hiddenWindowsChildOptions defaults isWindows from process.platform when omitted', () => {
  const result = hiddenWindowsChildOptions({})
  const expectedHide = process.platform === 'win32'

  assert.equal(Boolean(result.windowsHide), expectedHide)
})

function makeChild(
  overrides: Partial<{
    exitCode: number | null
    killed: boolean
    pid: number | null
    signalCode: string | null
  }> = {}
) {
  const calls: string[] = []

  return {
    calls,
    child: {
      // A LIVE handle carries both lifecycle fields as null. `isLiveProcessRoot`
      // is fail-closed (#89614), so a fixture that omits them is a stale record
      // rather than a live one -- that case is pinned in
      // backend-stale-pid.test.ts, deliberately not here.
      exitCode: 'exitCode' in overrides ? (overrides.exitCode as number | null) : null,
      kill: (signal: string) => {
        calls.push(signal)
      },
      killed: overrides.killed ?? false,
      pid: 'pid' in overrides ? overrides.pid : 1234,
      signalCode: 'signalCode' in overrides ? (overrides.signalCode as string | null) : null
    }
  }
}

test('stopBackendChild signals through the retained handle and never tree-kills by pid', () => {
  // INVERTED on purpose (#89614). This used to assert taskkill /T /F against
  // child.pid. A pid is not authority: the OS can reap the child and reassign
  // the number, and taskkill would then kill whoever inherited it.
  const { child, calls } = makeChild({ pid: 4242 })

  stopBackendChild(child)

  assert.deepEqual(calls, ['SIGTERM'], 'the retained handle must be the only mutator')
})

test('stopBackendChild issues no pid-based syscall of any kind', () => {
  // Also inverted. The POSIX branch used process.kill(-pid, ...), which is
  // pid-based too -- a recycled number means signalling an unrelated process
  // GROUP. Quieter than a Windows bugcheck, same defect.
  //
  // With dependency injection gone, process.kill is the only remaining way to
  // smuggle a pid mutator back into this path, so watch it directly.
  const { child, calls } = makeChild({ pid: 4242 })
  const pidKills: Array<[number, unknown]> = []
  const realKill = process.kill

  process.kill = ((pid: number, signal?: unknown) => {
    pidKills.push([pid, signal])
  }) as typeof process.kill

  try {
    stopBackendChild(child)
  } finally {
    process.kill = realKill
  }

  assert.deepEqual(pidKills, [], 'no process.kill by pid or pgid may be issued')
  assert.deepEqual(calls, ['SIGTERM'])
})

test('stopBackendChild still signals a child whose pid never materialised', () => {
  const { child, calls } = makeChild({ pid: null })

  stopBackendChild(child)

  assert.deepEqual(calls, ['SIGTERM'], 'a failed spawn is still worth signalling through the handle')
})

test('stopBackendChild is a no-op for an already-killed child', () => {
  const { child, calls } = makeChild({ killed: true })

  stopBackendChild(child)

  assert.deepEqual(calls, [])
})

test('stopBackendChild is a no-op for a null/undefined child', () => {
  assert.doesNotThrow(() => {
    stopBackendChild(null)
    stopBackendChild(undefined)
  })
})

test('stopBackendChild swallows errors thrown by the kill strategy', () => {
  const child = {
    exitCode: null,
    kill: () => {
      throw new Error('ESRCH: no such process')
    },
    killed: false,
    pid: 99,
    signalCode: null
  }

  assert.doesNotThrow(() => {
    stopBackendChild(child)
  })
})

test('Windows update tree-kills captured roots without pre-signalling the primary backend', () => {
  const primary = makeChild({ pid: 101 })
  const pooled = makeChild({ pid: 202 })
  const events: string[] = []

  stopBackendTreesForUpdate(primary.child, {
    forceKillProcessTree: pid => events.push(`tree:${pid}`),
    stopAllPoolBackends: () => {
      events.push('pool-stop')
      // Production stopAllPoolBackends() already tree-kills every pool root.
      events.push(`tree:${pooled.child.pid}`)
    }
  })

  assert.deepEqual(events, ['tree:101', 'pool-stop', 'tree:202'])
  assert.deepEqual(primary.calls, [], 'the primary root must not be signalled before taskkill /T sees it')
  assert.deepEqual(pooled.calls, [])
})
