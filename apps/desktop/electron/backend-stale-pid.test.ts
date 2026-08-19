import assert from 'node:assert/strict'
import fs from 'node:fs'

import { test } from 'vitest'

import { forceStopBackendChild, isLiveProcessRoot, stopBackendChild, stopBackendTreesForUpdate } from './backend-child'

const live = (pid: number, kill: (signal: NodeJS.Signals) => unknown = () => true) => ({
  exitCode: null,
  kill,
  killed: false,
  pid,
  signalCode: null
})
const exited = (pid: number, kill: (signal: NodeJS.Signals) => unknown = () => true, code = 0) => ({
  exitCode: code,
  kill,
  killed: false,
  pid,
  signalCode: null
})
const signalled = (pid: number, kill: (signal: NodeJS.Signals) => unknown = () => true, signalCode = 'SIGTERM') => ({
  exitCode: null,
  kill,
  killed: true,
  pid,
  signalCode
})

test('only an explicit live retained owner is actionable', () => {
  assert.equal(isLiveProcessRoot(live(4242)), true)
  // THE regression: exitCode 0 with a populated pid is the shape Node leaves
  // behind, and it used to pass every `Number.isInteger(pid)` guard.
  assert.equal(isLiveProcessRoot(exited(4242)), false)
  assert.equal(isLiveProcessRoot(signalled(4242)), false)
  // A pid-only legacy record is observation, not authority.
  assert.equal(isLiveProcessRoot({ pid: 4242 }), false, 'a pid-only legacy record is observation, not authority')
  assert.equal(isLiveProcessRoot({ exitCode: null, pid: 0, signalCode: null }), false)
  // A negative pid would be a POSIX process-GROUP id. taskkill has no such
  // notion, so letting one through would pass a nonsense argument to it.
  assert.equal(isLiveProcessRoot({ exitCode: null, pid: -991, signalCode: null }), false)
})

test('a reaped owner with a still-populated pid is never signalled', () => {
  const signals: string[] = []
  const child = exited(4242, signal => signals.push(signal))
  // exited() has killed=false but exitCode=0 so isLiveProcessRoot is false;
  // signalRetainedChild must refuse to call kill() entirely.
  assert.equal(stopBackendChild(child), false)
  assert.deepEqual(signals, [])
})

test('null / undefined child is a no-op', () => {
  assert.equal(stopBackendChild(null), false)
  assert.equal(stopBackendChild(undefined), false)
  assert.equal(forceStopBackendChild(null), false)
  assert.equal(forceStopBackendChild(undefined), false)
})

test('already-killed child is a no-op', () => {
  const signals: string[] = []
  const child = signalled(4242, signal => signals.push(signal))
  assert.equal(stopBackendChild(child), false)
  assert.deepEqual(signals, [])
})

test('graceful stop signals the retained owner and never accepts a pid mutator', () => {
  const signals: string[] = []
  const child = live(1234, signal => signals.push(signal))
  assert.equal(stopBackendChild(child), true)
  assert.deepEqual(signals, ['SIGTERM'])
})

test('forceful stop sends SIGKILL through the retained owner', () => {
  const signals: string[] = []
  const child = live(5678, signal => signals.push(signal))
  assert.equal(forceStopBackendChild(child), true)
  assert.deepEqual(signals, ['SIGKILL'])
})

test('child.kill throwing is swallowed (best-effort retained-owner signal)', () => {
  const child = live(9, () => {
    throw new Error('EPERM')
  })
  assert.equal(stopBackendChild(child), false)
})

test('stopBackendTreesForUpdate delegates to stopBackendChild then pool', async () => {
  const calls: string[] = []
  const poolChild = live(2001, signal => calls.push(`pool:${signal}`))
  const primaryChild = live(2002, signal => calls.push(`primary:${signal}`))
  await stopBackendTreesForUpdate(primaryChild, {
    stopAllPoolBackends: () => {
      calls.push('pool-stopped')
    }
  })
  // primary gets SIGTERM via stopBackendChild, then pool callback fires.
  assert.deepEqual(calls, ['primary:SIGTERM', 'pool-stopped'])
})

test('stopBackendTreesForUpdate with no pool callback still signals primary', async () => {
  const signals: string[] = []
  const primaryChild = live(3003, signal => signals.push(signal))
  await stopBackendTreesForUpdate(primaryChild, {
    stopAllPoolBackends: () => undefined
  })
  assert.deepEqual(signals, ['SIGTERM'])
})

test('pid-only child (no kill fn) is refused', () => {
  const signals: string[] = []
  const pidOnly = { exitCode: null, pid: 9999, signalCode: null }
  assert.equal(stopBackendChild(pidOnly as any), false)
  assert.deepEqual(signals, [])
})
