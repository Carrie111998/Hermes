import assert from 'node:assert/strict'

import { test } from 'vitest'

import { forceStopBackendChild, isLiveProcessRoot, stopBackendChild, stopBackendTreesForUpdate } from './backend-child'
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

function makeChild(overrides: Partial<{ pid: number | null; killed: boolean }> = {}) {
  const calls: string[] = []

  return {
    calls,
    child: {
      exitCode: null,
      kill: (signal: NodeJS.Signals) => {
        calls.push(signal)
      },
      killed: overrides.killed ?? false,
      pid: 'pid' in overrides ? overrides.pid : 1234,
      signalCode: null
    }
  }
}

test('stopBackendChild signals the retained owner via kill on any platform', () => {
  const { child, calls } = makeChild({ pid: 4242 })

  const result = stopBackendChild(child)

  assert.equal(result, true, 'must signal a live retained owner')
  assert.deepEqual(calls, ['SIGTERM'])
})

test('stopBackendChild never tree-kills by PID (fail-closed: pid is observation only)', () => {
  // The old API accepted a forceKillProcessTree dep; the new fail-closed API
  // has no such parameter — stopBackendChild only calls child.kill.
  const { child } = makeChild({ pid: 4242 })

  assert.equal(stopBackendChild(child), true)
  // No PID-based tree-kill path exists anymore.
})

test('stopBackendChild falls back to direct kill when group signal is unavailable (no-op throw)', () => {
  // With the new API, there is no group-signal path; stopBackendChild simply
  // calls child.kill directly. A throwing kill is swallowed.
  const signals: string[] = []
  const child = {
    exitCode: null,
    kill: () => {
      signals.push('SIGTERM')
    },
    killed: false,
    pid: 99,
    signalCode: null
  }

  assert.equal(stopBackendChild(child), true)
  assert.deepEqual(signals, ['SIGTERM'])
})

test('stopBackendChild no longer accepts a pid-only legacy record', () => {
  // { pid: 99 } without lifecycle fields is observation, not authority.
  assert.equal(stopBackendChild({ pid: 99 } as any), false)
})

test('stopBackendChild is a no-op for an already-killed child', () => {
  const { child, calls } = makeChild({ killed: true })

  assert.equal(stopBackendChild(child), false)
  assert.deepEqual(calls, [])
})

test('stopBackendChild is a no-op for a null/undefined child', () => {
  assert.equal(stopBackendChild(null), false)
  assert.equal(stopBackendChild(undefined), false)
  assert.equal(forceStopBackendChild(null), false)
  assert.equal(forceStopBackendChild(undefined), false)
})

test('forceStopBackendChild sends SIGKILL through the retained owner', () => {
  const { child, calls } = makeChild({ pid: 9999 })

  assert.equal(forceStopBackendChild(child), true)
  assert.deepEqual(calls, ['SIGKILL'])
})

test('stopBackendChild swallows errors thrown by the kill strategy', () => {
  const child = {
    exitCode: null,
    kill: () => {
      throw new Error('ESRCH: no such process')
    },
    killed: false,
    pid: 999,
    signalCode: null
  }

  assert.equal(stopBackendChild(child), false)
})

test('update teardown signals the retained primary owner only, no PID tree-kill', async () => {
  const events: string[] = []
  const primary = makeChild({ pid: 101 })

  await stopBackendTreesForUpdate(primary.child, {
    stopAllPoolBackends: () => {
      events.push('pool-stop')
    }
  })

  // primary gets SIGTERM via stopBackendChild; pool callback fires.
  assert.deepEqual(events, ['pool-stop'])
  assert.deepEqual(primary.calls, ['SIGTERM'])
})
