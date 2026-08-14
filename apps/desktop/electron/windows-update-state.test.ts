import assert from 'node:assert/strict'
import { test } from 'vitest'
import { runWindowsUpdatePreflight } from './windows-update-state'

const clear = { kind: 'clear' as const, result: { blocked: false, processes: [] } }
const blocker = { pid: 7, name: 'python.exe', blockerClass: 'desktop-backend', sameInstall: true, }

test('blocked unknown holders abort and reconnect without quiescing them', async () => {
  const phases: string[] = []
  let quiesced = false
  const result = await runWindowsUpdatePreflight({
    scan: async () => ({ kind: 'blocked', result: { blocked: true, processes: [blocker] } }),
    quiesceOwned: async () => { quiesced = true; return true },
    canQuiesceOwned: () => false,
    onTransition: state => phases.push(state.phase)
  })
  assert.equal(result.phase, 'abort')
  assert.equal(result.decision, 'reconnect')
  assert.equal(quiesced, false)
  assert.deepEqual(phases, ['blocked', 'abort'])
})

test('only documented owned holders are gracefully quiesced then rescanned before handoff', async () => {
  const phases: string[] = []
  let calls = 0
  const result = await runWindowsUpdatePreflight({
    scan: async () => ++calls === 1
      ? { kind: 'blocked', result: { blocked: true, processes: [blocker] } }
      : clear,
    quiesceOwned: async processes => { assert.equal(processes.length, 1); return true },
    canQuiesceOwned: process => process.pid === blocker.pid && process.blockerClass === 'desktop-backend',
    onTransition: state => phases.push(state.phase)
  })
  assert.equal(result.decision, 'handoff')
  assert.equal(calls, 2)
  assert.deepEqual(phases, ['blocked', 'quiescing', 'rescan', 'handoff'])
})

test('probe failure aborts rather than allowing handoff', async () => {
  const result = await runWindowsUpdatePreflight({
    scan: async () => ({ kind: 'probe-failure', error: 'scanner unavailable' }),
    quiesceOwned: async () => true,
    canQuiesceOwned: () => false
  })
  assert.deepEqual(result, { phase: 'abort', decision: 'reconnect', error: 'scanner unavailable' })
})

test('a holder that remains after documented quiescence aborts and reconnects', async () => {
  const phases: string[] = []
  let calls = 0
  const result = await runWindowsUpdatePreflight({
    scan: async () => {
      calls += 1
      return {
        kind: 'blocked' as const,
        result: {
          blocked: true,
          processes: [blocker]
        }
      }
    },
    quiesceOwned: async () => true,
    canQuiesceOwned: () => true,
    onTransition: state => phases.push(state.phase)
  })

  assert.equal(calls, 2, 'a clear second scan is required before handoff')
  assert.equal(result.phase, 'abort')
  assert.equal(result.decision, 'reconnect')
  assert.match(result.error || '', /holders remain/i)
  assert.deepEqual(phases, ['blocked', 'quiescing', 'rescan', 'abort'])
})
