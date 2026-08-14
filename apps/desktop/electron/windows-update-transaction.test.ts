import assert from 'node:assert/strict'
import { test } from 'vitest'

import { runWindowsUpdateTransaction } from './windows-update-transaction'

const blocked = {
  phase: 'abort' as const,
  decision: 'reconnect' as const,
  blockers: {
    blocked: true,
    processes: [{ pid: 7, name: 'python.exe', blockerClass: 'desktop-backend', sameInstall: true }]
  },
  error: 'holders require user action'
}

const clear = { phase: 'handoff' as const, decision: 'handoff' as const }

test('caller-level Windows transaction never invokes the updater across two aborted reconnects, then invokes it once after a clean rescan', async () => {
  const preflights = [blocked, blocked, clear]
  const phases: string[] = []
  let updaterSpawns = 0
  let reconnects = 0

  const applyWindowsUpdate = () => runWindowsUpdateTransaction({
    preflight: async () => preflights.shift() || clear,
    spawnUpdater: async () => {
      updaterSpawns += 1
      return { pid: 42 }
    },
    reconnect: async () => { reconnects += 1 },
    onPhase: phase => phases.push(phase)
  })

  assert.equal((await applyWindowsUpdate()).kind, 'aborted')
  assert.equal((await applyWindowsUpdate()).kind, 'aborted')
  assert.equal(updaterSpawns, 0, 'aborted preflights must never reach spawnUpdaterProcess')
  assert.equal(reconnects, 2, 'each aborted preflight reconnects deterministically')
  assert.deepEqual(phases, ['abort', 'result', 'reconnect', 'complete', 'abort', 'result', 'reconnect', 'complete'])

  const clean = await applyWindowsUpdate()
  assert.equal(clean.kind, 'handed-off')
  assert.equal(updaterSpawns, 1, 'exactly one updater spawn follows a clean rescan')
  assert.equal(reconnects, 2)
})

test('caller-level Windows transaction returns a reconnect failure and never spawns the updater', async () => {
  let updaterSpawns = 0
  const result = await runWindowsUpdateTransaction({
    preflight: async () => blocked,
    spawnUpdater: async () => { updaterSpawns += 1 },
    reconnect: async () => { throw new Error('backend socket refused') }
  })

  assert.equal(result.kind, 'reconnect-failed')
  assert.equal(updaterSpawns, 0)
  if (result.kind === 'reconnect-failed') {
    assert.match(String((result.error as Error).message), /socket refused/)
  }
})
