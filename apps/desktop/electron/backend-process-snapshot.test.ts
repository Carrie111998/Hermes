import assert from 'node:assert/strict'

import { test } from 'vitest'

import type { BackendOwnershipEntry } from './backend-ownership'
import {
  inspectBackendOwnershipSnapshot,
  parseWindowsProcessSnapshot,
  windowsProcessSnapshotScript
} from './backend-process-snapshot'

function entry(overrides: Partial<BackendOwnershipEntry> = {}): BackendOwnershipEntry {
  return {
    nonce: 'nonce',
    pid: 42,
    profile: 'default',
    startMarker: 'win:638900000000000000',
    ...overrides
  }
}

test('Windows snapshot script uses one shell and matching Get-Process start markers', () => {
  const script = windowsProcessSnapshotScript([42, 43, 42, -1, Number.NaN])

  assert.match(script, /Get-Process -Id @\(42, 43\)/)
  assert.equal((script.match(/Get-Process/g) || []).length, 1)
  assert.match(script, /Get-CimInstance Win32_Process -Filter 'ProcessId = 42 OR ProcessId = 43'/)
  assert.equal((script.match(/Get-CimInstance/g) || []).length, 1)
  assert.match(script, /\.StartTime\.ToUniversalTime\(\)\.Ticks/)
  assert.doesNotMatch(script, /CreationDate/)
  assert.match(script, /ConvertTo-Json -InputObject @\(\$items\) -Compress/)
})

test('Windows snapshot parser preserves both legacy ticks and spawn-ledger millisecond markers', () => {
  const snapshots = parseWindowsProcessSnapshot(
    JSON.stringify([
      {
        command: 'python -m hermes_cli.main serve --port 0',
        milliseconds: '1750000000000',
        pid: 42,
        ticks: '638900000000000000'
      }
    ])
  )

  assert.deepEqual([...snapshots.get(42)!.startMarkers], [
    'win:638900000000000000',
    'winms:1750000000000'
  ])
  assert.equal(snapshots.get(42)!.command, 'python -m hermes_cli.main serve --port 0')
})

test('snapshot inspection distinguishes live parents, stale PIDs, PID reuse, and inaccessible commands', () => {
  const snapshots = parseWindowsProcessSnapshot(
    JSON.stringify([
      {
        command: 'python -m hermes_cli.main serve --port 0',
        milliseconds: '1750000000000',
        pid: 42,
        ticks: '638900000000000000'
      },
      { command: 'Hermes.exe', milliseconds: '1750000000100', pid: 100, ticks: '638900000001000000' },
      { command: '', milliseconds: '1750000000200', pid: 45, ticks: '638900000002000000' }
    ])
  )
  const live = entry({ parentPid: 100, parentStartMarker: 'winms:1750000000100' })
  const dead = entry({ pid: 43 })
  const reused = entry({ pid: 42, startMarker: 'win:638800000000000000' })
  const inaccessible = entry({ pid: 45, startMarker: 'winms:1750000000200' })

  assert.deepEqual(inspectBackendOwnershipSnapshot([live, dead, reused, inaccessible], snapshots), [
    { identityMatches: true, parentMatches: true },
    { identityMatches: false, parentMatches: undefined },
    { identityMatches: false, parentMatches: undefined },
    { identityMatches: undefined, parentMatches: undefined }
  ])
})

test('empty Windows process snapshots parse as a confirmed empty table', () => {
  assert.deepEqual([...parseWindowsProcessSnapshot('[]')], [])
  assert.throws(() => parseWindowsProcessSnapshot(''), /no JSON/)
})
