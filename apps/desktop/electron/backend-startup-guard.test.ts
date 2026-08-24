import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'

import { test } from 'vitest'

import { createBackendStartupGuard } from './backend-startup-guard'

type FakeChild = EventEmitter & {
  exitCode: number | null
  signalCode: string | null
}

function child(): FakeChild {
  const process = new EventEmitter() as FakeChild
  process.exitCode = null
  process.signalCode = null

  return process
}

test('captures a spawn error while ownership persistence is pending', () => {
  const process = child()
  const guard = createBackendStartupGuard(process)
  const error = new Error('spawn ENOENT')

  process.emit('error', error)

  assert.equal(guard.failure(), error)
  guard.detach()
})

test('captures an exit while ownership persistence is pending', () => {
  const process = child()
  const guard = createBackendStartupGuard(process, {
    describeExit: code => new Error(`profile backend exited before ownership (${code})`)
  })

  process.exitCode = 17
  process.emit('exit', 17, null)

  assert.match(guard.failure()?.message || '', /profile backend exited before ownership \(17\)/)
  guard.detach()
})

test('detects a child that exited before the guard was installed', () => {
  const process = child()
  process.exitCode = 1

  const guard = createBackendStartupGuard(process)

  assert.match(guard.failure()?.message || '', /exited during startup \(1\)/)
  guard.detach()
  assert.equal(process.listenerCount('error'), 0)
  assert.equal(process.listenerCount('exit'), 0)
})

test('keeps the first causal failure when error and exit both arrive', () => {
  const process = child()
  const guard = createBackendStartupGuard(process)
  const error = new Error('permission denied')

  process.emit('error', error)
  process.exitCode = 1
  process.emit('exit', 1, null)

  assert.equal(guard.failure(), error)
  guard.detach()
})

test('detach hands later lifecycle events to the permanent observer', () => {
  const process = child()
  const guard = createBackendStartupGuard(process)

  guard.detach()
  process.exitCode = 0
  process.emit('exit', 0, null)

  assert.equal(guard.failure(), null)
})
