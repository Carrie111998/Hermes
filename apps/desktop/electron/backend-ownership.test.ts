import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { test, vi } from 'vitest'

import {
  backendCommandMatches,
  type BackendIdentity,
  type BackendOwnershipEntry,
  type BackendOwnershipStore,
  createBackendOwnership,
  createBackendShutdownCoordinator,
  parseBackendOwnership
} from './backend-ownership'

const mainSource = readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), 'main.ts'), 'utf8')

function sourceSlice(startMarker: string, endMarker: string): string {
  const start = mainSource.indexOf(startMarker)
  assert.notEqual(start, -1, `missing source marker: ${startMarker}`)
  const end = mainSource.indexOf(endMarker, start + startMarker.length)
  return mainSource.slice(start, end === -1 ? undefined : end)
}

test('Electron backend lock cleanup uses a rename tombstone CAS, including malformed recovery', () => {
  const helper = sourceSlice('async function compareAndDeleteBackendOwnershipLock', '\nfunction normalizeBackendOwnershipLockStartMarker')
  const transaction = sourceSlice('async function withBackendOwnershipLock', '\nfunction execText')

  assert.match(helper, /rename\(lockPath, tombstonePath\)/)
  assert.match(helper, /currentContents !== expectedContents/)
  assert.match(helper, /link\(tombstonePath, lockPath\)/)
  assert.match(helper, /unlink\(tombstonePath\)/)
  assert.doesNotMatch(helper, /unlink\(lockPath\)/)
  assert.match(transaction, /compareAndDeleteBackendOwnershipLock\(lockPath, rawOwner\)/)
  assert.equal(transaction.split('compareAndDeleteBackendOwnershipLock(lockPath, `${contents}\\n`)').length - 1, 2)
})

function memoryStore(initial = '') {
  let contents = initial

  return {
    read: () => contents,
    value: () => contents,
    write: (next: string) => {
      contents = next
    }
  }
}

function identity(overrides: Partial<BackendIdentity> = {}): BackendIdentity {
  return {
    nonce: 'nonce-42',
    pid: 42,
    profile: 'default',
    startMarker: 'os-start-123',
    ...overrides
  }
}

function ownershipEntry(overrides: Partial<BackendIdentity> = {}) {
  return { command: 'hermes serve --port 0', ...identity(overrides) }
}

function stored(entries: object[]): string {
  return JSON.stringify({ backends: entries })
}

function deferred() {
  let resolve!: () => void

  const promise = new Promise<void>(done => {
    resolve = done
  })

  return { promise, resolve }
}

function createOwnership(store: BackendOwnershipStore = memoryStore(), overrides: Partial<Parameters<typeof createBackendOwnership>[0]> = {}) {
  return createBackendOwnership({
    matchesIdentity: async () => true,
    // Unknown parent (no record / legacy) preserves the pre-parent behaviour.
    matchesParent: async () => undefined,
    stop: () => {},
    store,
    ...overrides
  })
}

test('claim persists the caller-supplied exact identity before resolving', async () => {
  const store = memoryStore()
  const ownership = createOwnership(store)
  const claim = ownershipEntry()

  assert.deepEqual(await ownership.claim(claim), claim)
  assert.deepEqual(parseBackendOwnership(store.value()), [claim])
})

test('claim persists immediately, then prunes stale ownership in one non-blocking batch', async () => {
  const stale = ownershipEntry({ nonce: 'stale', pid: 40, startMarker: 'old-start' })
  const uncertain = ownershipEntry({ nonce: 'uncertain', pid: 41, startMarker: 'unknown-start' })
  const claim = ownershipEntry({ nonce: 'new', pid: 42, startMarker: 'new-start' })
  const store = memoryStore(stored([stale, uncertain]))
  const gate = deferred()
  const inspect = vi.fn(async (entries: readonly BackendOwnershipEntry[]) => {
    await gate.promise

    return entries.map(entry => ({
      identityMatches: entry.nonce === stale.nonce ? false : entry.nonce === uncertain.nonce ? undefined : true,
      parentMatches: undefined
    }))
  })
  const ownership = createOwnership(store, { inspect })

  await ownership.claim(claim)

  // Persistence is complete while the cold snapshot remains blocked.
  assert.deepEqual(parseBackendOwnership(store.value()), [stale, uncertain, claim])
  assert.equal(inspect.mock.calls.length, 1)

  const compacted = ownership.compactStale()
  gate.resolve()

  assert.equal(await compacted, 1)
  assert.deepEqual(parseBackendOwnership(store.value()), [uncertain, claim])
  assert.deepEqual(inspect.mock.calls, [[[stale, uncertain, claim]]])
})

test('claim batch-compacts a large roster after persisting without blocking startup', async () => {
  const old = Array.from({ length: 7 }, (_, index) =>
    ownershipEntry({
      nonce: `old-${index}`,
      pid: 100 + index,
      startMarker: `old-start-${index}`
    })
  )
  const uncertain = old[old.length - 1]
  const claim = ownershipEntry({ nonce: 'new', pid: 200, startMarker: 'new-start' })
  const store = memoryStore(stored(old))
  const gate = deferred()
  const inspect = vi.fn(async (entries: readonly BackendOwnershipEntry[]) => {
    await gate.promise

    return entries.map(entry => ({
      identityMatches: entry.nonce === uncertain.nonce ? undefined : entry.nonce === claim.nonce,
      parentMatches: undefined
    }))
  })
  const ownership = createOwnership(store, { inspect })

  await ownership.claim(claim)

  // The exact new claim is durable before the cold snapshot finishes.
  assert.deepEqual(parseBackendOwnership(store.value()), [...old, claim])
  assert.equal(inspect.mock.calls.length, 1)

  const compacted = ownership.compactStale()
  gate.resolve()

  assert.equal(await compacted, old.length - 1)
  assert.deepEqual(parseBackendOwnership(store.value()), [uncertain, claim])
  assert.deepEqual(inspect.mock.calls, [[[...old, claim]]])
})

test('concurrent claims merge both children without a read-await-write lost update', async () => {
  const stale = ownershipEntry({ nonce: 'stale', pid: 79, startMarker: 'stale-start' })
  const store = memoryStore(stored([stale]))
  const gate = deferred()
  const inspect = vi.fn(async (entries: readonly BackendOwnershipEntry[]) => {
    await gate.promise

    return entries.map(entry => ({ identityMatches: entry.nonce !== stale.nonce, parentMatches: undefined }))
  })
  const ownership = createOwnership(store, { inspect })
  const first = ownershipEntry({ nonce: 'first', pid: 80, startMarker: 'first-start' })
  const second = ownershipEntry({ nonce: 'second', pid: 81, startMarker: 'second-start' })

  await Promise.all([ownership.claim(first), ownership.claim(second)])

  assert.equal(inspect.mock.calls.length, 1)
  assert.deepEqual(parseBackendOwnership(store.value()), [stale, first, second])

  const compacted = ownership.compactStale()
  gate.resolve()

  assert.equal(await compacted, 1)
  assert.deepEqual(parseBackendOwnership(store.value()), [first, second])
})

test('ownership mutations use the store transaction boundary so separate interpreters cannot clobber rows', async () => {
  let contents = stored([])
  let transactions = 0
  const store = {
    read: () => contents,
    write: (next: string) => {
      contents = next
    },
    transaction: <T>(operation: () => T): T => {
      transactions += 1
      return operation()
    }
  }
  const first = createOwnership(store)
  const second = createOwnership(store)

  await Promise.all([
    first.claim(ownershipEntry({ pid: 80, nonce: 'first' })),
    second.claim(ownershipEntry({ pid: 81, nonce: 'second' }))
  ])

  assert.ok(transactions >= 2)
  assert.deepEqual(parseBackendOwnership(contents), [
    ownershipEntry({ pid: 80, nonce: 'first' }),
    ownershipEntry({ pid: 81, nonce: 'second' })
  ])
})

test('an ownership read error is quarantined and never treated as an empty roster', async () => {
  let quarantined = 0
  let writes = 0
  const ownership = createOwnership({
    read: () => {
      throw Object.assign(new Error('permission denied'), { code: 'EACCES' })
    },
    write: () => {
      writes += 1
    },
    quarantine: () => {
      quarantined += 1
    }
  })

  assert.deepEqual(await ownership.reapOrphans(), [])
  assert.equal(quarantined, 1)
  assert.equal(writes, 0)
})

test('incomplete claims and persisted records are rejected', async () => {
  const store = memoryStore(
    stored([
      ownershipEntry(),
      { ...ownershipEntry({ pid: 43 }), startMarker: '' },
      { ...ownershipEntry({ pid: 44 }), nonce: undefined },
      { ...ownershipEntry({ pid: 45 }), profile: undefined }
    ])
  )

  const ownership = createOwnership(store)

  await assert.rejects(ownership.claim({ ...ownershipEntry(), startMarker: '' }), /complete process identity/)
  assert.deepEqual(parseBackendOwnership(store.value()), [ownershipEntry()])
})

test('failed persistence awaits asynchronous cleanup of the exact identity', async () => {
  const cleanup = deferred()
  const stop = vi.fn(() => cleanup.promise)
  const expected = new Error('disk full')
  const claim = ownershipEntry({ pid: 43 })

  const ownership = createOwnership(memoryStore(), {
    stop,
    store: {
      read: () => null,
      write: () => {
        throw expected
      }
    }
  })

  let rejected = false

  const result = ownership.claim(claim).catch(error => {
    rejected = true
    throw error
  })

  await vi.waitFor(() => assert.deepEqual(stop.mock.calls, [[claim]]))
  assert.equal(rejected, false)

  cleanup.resolve()
  await assert.rejects(result, expected)
  assert.equal(rejected, true)
})

test('startup reap drops a confirmed PID reuse mismatch without stopping it', async () => {
  const entry = ownershipEntry()
  const store = memoryStore(stored([entry]))
  const matchesIdentity = vi.fn(async () => false)
  const stop = vi.fn()
  const ownership = createOwnership(store, { matchesIdentity, stop })

  assert.deepEqual(await ownership.reapOrphans(), [])
  assert.deepEqual(matchesIdentity.mock.calls, [[entry]])
  assert.equal(stop.mock.calls.length, 0)
  assert.deepEqual(parseBackendOwnership(store.value()), [])
})

test('startup reap preserves records when exact identity probing is uncertain or fails', async () => {
  const uncertain = ownershipEntry({ pid: 50, nonce: 'uncertain' })
  const failed = ownershipEntry({ pid: 51, nonce: 'failed' })
  const store = memoryStore(stored([uncertain, failed]))
  const stop = vi.fn()

  const ownership = createOwnership(store, {
    matchesIdentity: async entry => {
      if (entry.pid === failed.pid) {
        throw new Error('process table unavailable')
      }

      return undefined
    },
    stop
  })

  assert.deepEqual(await ownership.reapOrphans(), [])
  assert.equal(stop.mock.calls.length, 0)
  assert.deepEqual(parseBackendOwnership(store.value()), [uncertain, failed])
})

test('startup reap classifies the whole roster with one batch inspection', async () => {
  const liveParent = {
    ...ownershipEntry({ nonce: 'live-parent', pid: 60 }),
    parentPid: 600,
    parentStartMarker: 'parent-start'
  }
  const stale = ownershipEntry({ nonce: 'stale', pid: 61 })
  const orphan = ownershipEntry({ nonce: 'orphan', pid: 62 })
  const store = memoryStore(stored([liveParent, stale, orphan]))
  const inspect = vi.fn(async () => [
    { identityMatches: true, parentMatches: true },
    { identityMatches: false, parentMatches: false },
    { identityMatches: true, parentMatches: false }
  ])
  const stop = vi.fn()
  const matchesIdentity = vi.fn(async () => assert.fail('batch inspection should replace scalar identity probes'))
  const matchesParent = vi.fn(async () => assert.fail('batch inspection should replace scalar parent probes'))
  const ownership = createOwnership(store, { inspect, matchesIdentity, matchesParent, stop })

  assert.deepEqual(await ownership.reapOrphans(), [62])
  assert.deepEqual(inspect.mock.calls, [[[liveParent, stale, orphan]]])
  assert.equal(matchesIdentity.mock.calls.length, 0)
  assert.equal(matchesParent.mock.calls.length, 0)
  assert.deepEqual(stop.mock.calls, [[orphan]])
  assert.deepEqual(parseBackendOwnership(store.value()), [liveParent])
})

test('startup reap overlaps scalar roster probes when a batch inspector is unavailable', async () => {
  const first = ownershipEntry({ nonce: 'first', pid: 70 })
  const second = ownershipEntry({ nonce: 'second', pid: 71 })
  const gate = deferred()
  let started = 0
  const matchesIdentity = vi.fn(async () => {
    started += 1
    await gate.promise
    return false
  })
  const ownership = createOwnership(memoryStore(stored([first, second])), {
    matchesIdentity,
    matchesParent: async () => false
  })
  const result = ownership.reapOrphans()

  await vi.waitFor(() => assert.equal(started, 2))
  gate.resolve()

  assert.deepEqual(await result, [])
})

test('startup reap fresh-merges a backend claimed while inspection is pending', async () => {
  const stale = ownershipEntry({ nonce: 'stale', pid: 72, startMarker: 'stale-start' })
  const claimed = ownershipEntry({ nonce: 'claimed', pid: 73, startMarker: 'claimed-start' })
  const store = memoryStore(stored([stale]))
  const gate = deferred()
  const inspect = vi.fn(async () => {
    await gate.promise
    return [{ identityMatches: false, parentMatches: false }]
  })
  const ownership = createOwnership(store, { inspect })
  const reap = ownership.reapOrphans()

  await vi.waitFor(() => assert.equal(inspect.mock.calls.length, 1))
  await ownership.claim(claimed)
  gate.resolve()

  assert.deepEqual(await reap, [])
  assert.deepEqual(parseBackendOwnership(store.value()), [claimed])
})

test('startup reap does not resurrect an entry released while inspection is pending', async () => {
  const released = ownershipEntry({ nonce: 'released', pid: 74, startMarker: 'released-start' })
  const store = memoryStore(stored([released]))
  const gate = deferred()
  const inspect = vi.fn(async () => {
    await gate.promise
    return [{ identityMatches: undefined, parentMatches: undefined }]
  })
  const ownership = createOwnership(store, { inspect })
  const reap = ownership.reapOrphans()

  await vi.waitFor(() => assert.equal(inspect.mock.calls.length, 1))
  ownership.release(released)
  gate.resolve()

  assert.deepEqual(await reap, [])
  assert.deepEqual(parseBackendOwnership(store.value()), [])
})

test('startup reap passes the full confirmed identity to stop', async () => {
  const entry = ownershipEntry({ pid: 52 })
  const store = memoryStore(stored([entry]))
  const stop = vi.fn()
  const ownership = createOwnership(store, { stop })

  assert.deepEqual(await ownership.reapOrphans(), [52])
  assert.deepEqual(stop.mock.calls, [[entry]])
  assert.deepEqual(parseBackendOwnership(store.value()), [])
})

test('startup reap preserves failed stops for the next launch', async () => {
  const entry = ownershipEntry({ pid: 53 })
  const store = memoryStore(stored([entry]))

  const ownership = createOwnership(store, {
    stop: () => {
      throw new Error('permission denied')
    }
  })

  assert.deepEqual(await ownership.reapOrphans(), [])
  assert.deepEqual(parseBackendOwnership(store.value()), [entry])
})

test('startup reap never stops a backend whose parent Electron is still alive', async () => {
  const entry = { ...ownershipEntry({ pid: 54 }), parentPid: 100, parentStartMarker: 'os-start-parent' }
  const store = memoryStore(stored([entry]))
  const stop = vi.fn()

  const ownership = createOwnership(store, {
    matchesParent: async () => true,
    stop
  })

  assert.deepEqual(await ownership.reapOrphans(), [])
  assert.equal(stop.mock.calls.length, 0)
  assert.deepEqual(parseBackendOwnership(store.value()), [entry])
})

test('startup reap still reaps a backend whose parent is gone or reused', async () => {
  const gone = { ...ownershipEntry({ pid: 55 }), parentPid: 200, parentStartMarker: 'os-start-dead' }
  const reused = { ...ownershipEntry({ pid: 56 }), parentPid: 201, parentStartMarker: 'os-start-old' }
  const store = memoryStore(stored([gone, reused]))
  const stop = vi.fn()

  const ownership = createOwnership(store, {
    matchesParent: async entry => (entry.parentPid === 201 ? true : false),
    stop
  })

  assert.deepEqual(await ownership.reapOrphans(), [55])
  assert.deepEqual(stop.mock.calls, [[gone]])
  assert.deepEqual(parseBackendOwnership(store.value()), [reused])
})

test('startup reap preserves a record when parent liveness probing fails', async () => {
  const entry = { ...ownershipEntry({ pid: 57 }), parentPid: 300, parentStartMarker: 'os-start-parent' }
  const store = memoryStore(stored([entry]))
  const stop = vi.fn()

  const ownership = createOwnership(store, {
    matchesParent: async () => {
      throw new Error('process table unavailable')
    },
    stop
  })

  assert.deepEqual(await ownership.reapOrphans(), [])
  assert.equal(stop.mock.calls.length, 0)
  assert.deepEqual(parseBackendOwnership(store.value()), [entry])
})

test('claim persists the parent identity so a later reap can see it', async () => {
  const store = memoryStore()
  const ownership = createOwnership(store)
  const claim = { ...ownershipEntry(), parentPid: 42, parentStartMarker: 'os-start-parent' }

  const entry = await ownership.claim(claim)

  assert.equal(entry.parentPid, 42)
  assert.equal(entry.parentStartMarker, 'os-start-parent')
  assert.deepEqual(parseBackendOwnership(store.value())[0].parentPid, 42)
  assert.deepEqual(parseBackendOwnership(store.value())[0].parentStartMarker, 'os-start-parent')
})

test('release removes only the exact identity rather than every record for its PID', () => {
  const oldProcess = ownershipEntry({ nonce: 'old', startMarker: 'start-old' })
  const reusedPid = ownershipEntry({ nonce: 'new', startMarker: 'start-new' })
  const store = memoryStore(stored([oldProcess, reusedPid]))
  const ownership = createOwnership(store)

  ownership.release(oldProcess)

  assert.deepEqual(parseBackendOwnership(store.value()), [reusedPid])
})

test('backend identity check matches only serve and dashboard invocation shapes', () => {
  assert.equal(backendCommandMatches('/venv/bin/hermes serve --port 0'), true)
  assert.equal(backendCommandMatches('python -m hermes_cli.main dashboard --no-open'), true)
  assert.equal(backendCommandMatches('/venv/bin/hermes --profile work serve --port 0'), true)
  assert.equal(backendCommandMatches('"C:\\Hermes Runtime\\hermes.exe" dashboard --no-open'), true)
  assert.equal(backendCommandMatches('hermes chat --query serve'), false)
  assert.equal(backendCommandMatches('unrelated dashboard'), false)
})

test('shutdown coordinator returns one promise and awaits teardown exactly once', async () => {
  const completion = deferred()
  const teardown = vi.fn(() => completion.promise)
  const coordinator = createBackendShutdownCoordinator(teardown)

  const first = coordinator.run()
  const second = coordinator.run()

  assert.equal(first, second)
  assert.equal(coordinator.hasStarted(), true)
  await Promise.resolve()
  assert.equal(teardown.mock.calls.length, 1)

  let finished = false
  first.then(() => {
    finished = true
  })
  await Promise.resolve()
  assert.equal(finished, false)

  completion.resolve()
  await second
  assert.equal(finished, true)
  assert.equal(coordinator.run(), first)
})

// #89298: a corrupt ownership file must never be silently rewritten as [] —
// that permanently erases the only record of still-running backends. The reap
// sweep quarantines the file and skips; a later healthy write recreates it.
test('reapOrphans on a corrupt file quarantines and does not rewrite', async () => {
  let contents = '{ this is not json'
  let quarantined = 0
  const writes: string[] = []
  const stopped: number[] = []

  const store = {
    read: () => contents,
    value: () => contents,
    write: (next: string) => {
      writes.push(next)
      contents = next
    },
    quarantine: () => {
      quarantined += 1
    }
  }

  const ownership = createOwnership(store, {
    stop: identityArg => {
      stopped.push(identityArg.pid)
    }
  })

  assert.deepEqual(await ownership.reapOrphans(), [])
  assert.equal(quarantined, 1)
  assert.deepEqual(writes, [])
  assert.deepEqual(stopped, [])
})

test('reapOrphans on a corrupt file without a quarantine hook still skips the rewrite', async () => {
  const writes: string[] = []

  const store = {
    read: () => 'garbage{{{',
    value: () => 'garbage{{{',
    write: (next: string) => {
      writes.push(next)
    }
  }

  const ownership = createOwnership(store)

  assert.deepEqual(await ownership.reapOrphans(), [])
  assert.deepEqual(writes, [])
})

test('an empty or missing ownership file is NOT corrupt — reap sweeps normally', async () => {
  const writes: string[] = []

  const store = {
    read: () => '',
    value: () => '',
    write: (next: string) => {
      writes.push(next)
    },
    quarantine: () => assert.fail('empty file must not be quarantined')
  }

  const ownership = createOwnership(store)

  assert.deepEqual(await ownership.reapOrphans(), [])
  // Empty roster: rewriting [] is harmless and keeps the legacy behavior.
  assert.equal(writes.length, 1)
})
