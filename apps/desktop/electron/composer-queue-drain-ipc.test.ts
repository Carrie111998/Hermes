import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { createComposerPersistenceFileStore } from './composer-persistence-file'
import { ComposerQueueDrainArbiter } from './composer-queue-drain-arbiter'
import { ComposerPersistenceCoordinator, registerComposerQueueDrainIpc } from './composer-queue-drain-ipc'

class FakeSender {
  private readonly listeners = new Map<string, Set<() => void>>()

  constructor(readonly id: number) {}

  once(event: string, listener: () => void): void {
    const wrapped = () => {
      this.listeners.get(event)?.delete(wrapped)
      listener()
    }

    const listeners = this.listeners.get(event) ?? new Set<() => void>()

    listeners.add(wrapped)
    this.listeners.set(event, listeners)
  }

  emit(event: string): void {
    for (const listener of [...(this.listeners.get(event) ?? [])]) {
      listener()
    }
  }
}

class FakeIpcMain {
  private readonly handlers = new Map<
    string,
    (event: { returnValue?: unknown; sender: FakeSender }, payload: any) => void
  >()

  on(channel: string, handler: (event: { returnValue?: unknown; sender: FakeSender }, payload: any) => void): void {
    this.handlers.set(channel, handler)
  }

  send(channel: string, sender: FakeSender, payload: unknown): unknown {
    const event = { sender } as { returnValue?: unknown; sender: FakeSender }

    this.handlers.get(channel)?.(event, payload)

    return event.returnValue
  }
}

describe('composer queue drain IPC', () => {
  it('releases renderer claims on render-process-gone and remains idempotent when destroyed follows', () => {
    const ipcMain = new FakeIpcMain()
    const arbiter = new ComposerQueueDrainArbiter()
    const crashed = new FakeSender(11)
    const live = new FakeSender(22)
    const contender = new FakeSender(33)

    registerComposerQueueDrainIpc(ipcMain, arbiter)

    expect(
      ipcMain.send('hermes:composer-queue-drain:begin', crashed, { scopeKey: 'scope-a', entryId: 'entry-a' })
    ).toEqual(expect.any(Number))

    crashed.emit('render-process-gone')

    expect(
      ipcMain.send('hermes:composer-queue-drain:begin', live, { scopeKey: 'scope-a', entryId: 'entry-b' })
    ).toEqual(expect.any(Number))

    crashed.emit('destroyed')

    expect(
      ipcMain.send('hermes:composer-queue-drain:begin', contender, { scopeKey: 'scope-a', entryId: 'entry-c' })
    ).toBeNull()
  })

  it('allows only the renderer that owns a drain token to finish it', () => {
    const ipcMain = new FakeIpcMain()
    const arbiter = new ComposerQueueDrainArbiter()
    const owner = new FakeSender(11)
    const foreign = new FakeSender(22)

    registerComposerQueueDrainIpc(ipcMain, arbiter)

    const token = ipcMain.send('hermes:composer-queue-drain:begin', owner, {
      scopeKey: 'scope-a',
      entryId: 'entry-a'
    })

    expect(ipcMain.send('hermes:composer-queue-drain:finish', foreign, token)).toBeNull()
    expect(arbiter.excluded('scope-a', 'entry-b')).toBe(true)
    expect(ipcMain.send('hermes:composer-queue-drain:finish', owner, token)).toBe('scope-a')
    expect(arbiter.excluded('scope-a', 'entry-b')).toBe(false)
  })

  it('rolls back only remotely handed-off source claims', () => {
    const ipcMain = new FakeIpcMain()
    const sourceOwner = new FakeSender(11)
    const targetOwner = new FakeSender(22)
    const migrator = new FakeSender(33)

    registerComposerQueueDrainIpc(ipcMain)

    const sourceToken = ipcMain.send('hermes:composer-queue-drain:begin', sourceOwner, {
      entryId: 'source-entry',
      scopeKey: 'source'
    })

    const targetToken = ipcMain.send('hermes:composer-queue-drain:begin', targetOwner, {
      entryId: 'target-entry',
      scopeKey: 'target'
    })

    ipcMain.send('hermes:composer-queue-drain:handoff', migrator, {
      fromScopeKey: 'source',
      toScopeKey: 'target',
      transactionId: 'drain-handoff-1'
    })
    ipcMain.send('hermes:composer-queue-drain:rollback-handoff', migrator, 'drain-handoff-1')

    expect(ipcMain.send('hermes:composer-queue-drain:finish', sourceOwner, sourceToken)).toBe('source')
    expect(ipcMain.send('hermes:composer-queue-drain:finish', targetOwner, targetToken)).toBe('target')
  })

  it('reserves both drain scopes while a handoff transaction is prepared', () => {
    const ipcMain = new FakeIpcMain()
    const migrator = new FakeSender(11)
    const contender = new FakeSender(22)

    registerComposerQueueDrainIpc(ipcMain)

    ipcMain.send('hermes:composer-queue-drain:handoff', migrator, {
      fromScopeKey: 'source',
      toScopeKey: 'target',
      transactionId: 'drain-reservation-1'
    })

    expect(
      ipcMain.send('hermes:composer-queue-drain:begin', contender, { entryId: 'source-entry', scopeKey: 'source' })
    ).toBeNull()
    expect(
      ipcMain.send('hermes:composer-queue-drain:begin', contender, { entryId: 'target-entry', scopeKey: 'target' })
    ).toBeNull()

    ipcMain.send('hermes:composer-queue-drain:rollback-handoff', migrator, 'drain-reservation-1')

    expect(
      ipcMain.send('hermes:composer-queue-drain:begin', contender, { entryId: 'source-entry', scopeKey: 'source' })
    ).toEqual(expect.any(Number))
  })

  it('serializes stale enqueue snapshots from two renderer clients', () => {
    const ipcMain = new FakeIpcMain()
    const firstClient = new FakeSender(11)
    const secondClient = new FakeSender(22)
    const staleSeed = { parks: {}, queues: {} }

    registerComposerQueueDrainIpc(ipcMain)

    ipcMain.send('hermes:composer-persistence:mutate', firstClient, {
      operation: {
        entry: { attachments: [], id: 'entry-a', queuedAt: 1, text: 'from first window' },
        scopeKey: 'scope-a',
        type: 'enqueue'
      },
      seed: staleSeed
    })

    const secondResult = ipcMain.send('hermes:composer-persistence:mutate', secondClient, {
      operation: {
        entry: { attachments: [], id: 'entry-b', queuedAt: 2, text: 'from second window' },
        scopeKey: 'scope-a',
        type: 'enqueue'
      },
      seed: staleSeed
    }) as any

    expect(secondResult.queues['scope-a'].map((entry: any) => entry.text)).toEqual([
      'from first window',
      'from second window'
    ])
  })

  it('composes stale two-client edits and removals on the authoritative scope', () => {
    const ipcMain = new FakeIpcMain()
    const firstClient = new FakeSender(11)
    const secondClient = new FakeSender(22)

    const staleSeed = {
      parks: {},
      queues: {
        'scope-a': [
          { attachments: [], id: 'entry-a', queuedAt: 1, text: 'edit me' },
          { attachments: [], id: 'entry-b', queuedAt: 2, text: 'remove me' }
        ]
      }
    }

    registerComposerQueueDrainIpc(ipcMain)

    ipcMain.send('hermes:composer-persistence:mutate', firstClient, {
      operation: { entryId: 'entry-a', scopeKey: 'scope-a', text: 'edited elsewhere', type: 'update' },
      seed: staleSeed
    })

    const secondResult = ipcMain.send('hermes:composer-persistence:mutate', secondClient, {
      operation: { entryId: 'entry-b', scopeKey: 'scope-a', type: 'remove' },
      seed: staleSeed
    }) as any

    expect(secondResult.result).toBe(true)
    expect(secondResult.queues['scope-a'].map((entry: any) => entry.text)).toEqual(['edited elsewhere'])
  })

  it('migrates the latest peer-window park with the authoritative queue', () => {
    const ipcMain = new FakeIpcMain()
    const stopClient = new FakeSender(11)
    const migrateClient = new FakeSender(22)

    const staleSeed = {
      parks: {},
      queues: {
        source: [{ attachments: [], id: 'source-entry', queuedAt: 1, text: 'held by peer Stop' }],
        target: [{ attachments: [], id: 'target-entry', queuedAt: 0, text: 'already at target' }]
      }
    }

    registerComposerQueueDrainIpc(ipcMain)

    ipcMain.send('hermes:composer-persistence:mutate', stopClient, {
      operation: { scopeKey: 'source', type: 'park' },
      seed: staleSeed
    })

    const migrated = ipcMain.send('hermes:composer-persistence:mutate', migrateClient, {
      operation: { fromScopeKey: 'source', toScopeKey: 'target', type: 'migrate' },
      seed: staleSeed
    }) as any

    expect(migrated.queues.source).toBeUndefined()
    expect(migrated.queues.target.map((entry: any) => entry.text)).toEqual(['already at target', 'held by peer Stop'])
    expect(migrated.parks).toEqual({ target: true })
  })

  it('redirects a peer renderer mutation away from a source reserved for handoff', () => {
    const ipcMain = new FakeIpcMain()
    const migrateClient = new FakeSender(11)
    const lateClient = new FakeSender(22)

    const seed = {
      parks: {},
      queues: {
        source: [{ attachments: [], id: 'source-entry', queuedAt: 1, text: 'migrated source entry' }]
      }
    }

    registerComposerQueueDrainIpc(ipcMain)

    ipcMain.send('hermes:composer-persistence:mutate', migrateClient, {
      operation: {
        fromScopeKey: 'source',
        toScopeKey: 'target',
        transactionId: 'reserved-handoff',
        type: 'migrate'
      },
      seed
    })

    const lateResult = ipcMain.send('hermes:composer-persistence:mutate', lateClient, {
      operation: {
        entry: { attachments: [], id: 'late-entry', queuedAt: 2, text: 'late peer entry' },
        scopeKey: 'source',
        type: 'enqueue'
      },
      seed
    }) as any

    expect(lateResult.queues.source).toBeUndefined()
    expect(lateResult.queues.target.map((entry: any) => entry.text)).toEqual([
      'migrated source entry',
      'late peer entry'
    ])
  })

  it('rejects a competing handoff for a reserved source', () => {
    const coordinator = new ComposerPersistenceCoordinator()

    const seed = {
      parks: {},
      queues: {
        source: [{ attachments: [], id: 'source-entry', queuedAt: 1, text: 'reserved source entry' }]
      }
    }

    coordinator.mutate({
      operation: {
        fromScopeKey: 'source',
        toScopeKey: 'first-target',
        transactionId: 'first-handoff',
        type: 'migrate'
      },
      seed
    })

    expect(() =>
      coordinator.mutate({
        operation: {
          fromScopeKey: 'source',
          toScopeKey: 'competing-target',
          transactionId: 'competing-handoff',
          type: 'migrate'
        },
        seed
      })
    ).toThrow('source is reserved by another handoff')
  })

  it('rejects a nested handoff through a reserved destination', () => {
    const coordinator = new ComposerPersistenceCoordinator()

    const seed = {
      parks: {},
      queues: {
        source: [{ attachments: [], id: 'source-entry', queuedAt: 1, text: 'reserved source entry' }]
      }
    }

    coordinator.mutate({
      operation: {
        fromScopeKey: 'source',
        toScopeKey: 'reserved-target',
        transactionId: 'first-handoff',
        type: 'migrate'
      },
      seed
    })

    expect(() =>
      coordinator.mutate({
        operation: {
          fromScopeKey: 'reserved-target',
          toScopeKey: 'nested-target',
          transactionId: 'nested-handoff',
          type: 'migrate'
        },
        seed
      })
    ).toThrow('scope is reserved by another handoff')
  })

  it('rolls back a committed handoff transaction without disturbing either queue', () => {
    const coordinator = new ComposerPersistenceCoordinator()

    const seed = {
      parks: { source: true as const },
      queues: {
        source: [{ attachments: [], id: 'source-entry', queuedAt: 1, text: 'from source' }],
        target: [{ attachments: [], id: 'target-entry', queuedAt: 0, text: 'already at target' }]
      }
    }

    coordinator.mutate({
      operation: {
        fromScopeKey: 'source',
        toScopeKey: 'target',
        transactionId: 'handoff-1',
        type: 'migrate'
      },
      seed
    })

    const rolledBack = coordinator.mutate({
      operation: { transactionId: 'handoff-1', type: 'rollback-migrate' },
      seed
    })

    expect(rolledBack.queues.source?.map((entry: any) => entry.text)).toEqual(['from source'])
    expect(rolledBack.queues.target?.map((entry: any) => entry.text)).toEqual(['already at target'])
    expect(rolledBack.parks).toEqual({ source: true })
  })

  it('refuses rollback rather than clobbering a concurrent target mutation', () => {
    const coordinator = new ComposerPersistenceCoordinator()

    const seed = {
      parks: {},
      queues: {
        source: [{ attachments: [], id: 'source-entry', queuedAt: 1, text: 'from source' }]
      }
    }

    coordinator.mutate({
      operation: {
        fromScopeKey: 'source',
        toScopeKey: 'target',
        transactionId: 'handoff-conflict',
        type: 'migrate'
      },
      seed
    })
    coordinator.mutate({
      operation: {
        entry: { attachments: [], id: 'concurrent-entry', queuedAt: 2, text: 'concurrent target enqueue' },
        scopeKey: 'target',
        type: 'enqueue'
      },
      seed
    })

    expect(() =>
      coordinator.mutate({
        operation: { transactionId: 'handoff-conflict', type: 'rollback-migrate' },
        seed
      })
    ).toThrow('changed after the handoff commit')

    const current = coordinator.mutate({ operation: { type: 'read' }, seed })
    expect(current.queues.target?.map((entry: any) => entry.text)).toEqual(['from source', 'concurrent target enqueue'])
  })

  it('retains an unfinished handoff transaction across coordinator restart', () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-composer-transaction-'))
    const store = createComposerPersistenceFileStore(path.join(directory, 'composer-persistence.v1.json'))

    const seed = {
      parks: { source: true as const },
      queues: {
        source: [{ attachments: [], id: 'source-entry', queuedAt: 1, text: 'restart-safe source' }],
        target: [{ attachments: [], id: 'target-entry', queuedAt: 0, text: 'restart-safe target' }]
      }
    }

    try {
      const firstMain = new ComposerPersistenceCoordinator(store)
      firstMain.mutate({
        operation: {
          fromScopeKey: 'source',
          toScopeKey: 'target',
          transactionId: 'restart-handoff',
          type: 'migrate'
        },
        seed
      })

      const restartedMain = new ComposerPersistenceCoordinator(store)

      const rolledBack = restartedMain.mutate({
        operation: { transactionId: 'restart-handoff', type: 'rollback-migrate' },
        seed: { parks: {}, queues: {} }
      })

      expect(rolledBack.queues.source?.map((entry: any) => entry.text)).toEqual(['restart-safe source'])
      expect(rolledBack.queues.target?.map((entry: any) => entry.text)).toEqual(['restart-safe target'])
      expect(rolledBack.parks).toEqual({ source: true })
    } finally {
      fs.rmSync(directory, { force: true, recursive: true })
    }
  })

  it('rolls back a failed durable migration and restart retains the source', () => {
    const persisted = {
      parks: { source: true as const },
      queues: {
        source: [{ attachments: [], id: 'source-entry', queuedAt: 1, text: 'survives failed save' }]
      }
    }

    const store = {
      load: () => structuredClone(persisted),
      save: () => {
        throw new Error('injected file save failure')
      }
    }

    const coordinator = new ComposerPersistenceCoordinator(store)

    const migration = {
      operation: { fromScopeKey: 'source', toScopeKey: 'target', type: 'migrate' },
      seed: { parks: {}, queues: {} }
    }

    expect(() => coordinator.mutate(migration)).toThrow('injected file save failure')

    const afterFailure = coordinator.mutate({ operation: { type: 'read' }, seed: { parks: {}, queues: {} } })
    expect(afterFailure.queues.source?.map((entry: any) => entry.text)).toEqual(['survives failed save'])
    expect(afterFailure.queues.target).toBeUndefined()
    expect(afterFailure.parks).toEqual({ source: true })

    const restarted = new ComposerPersistenceCoordinator(store)
    const afterRestart = restarted.mutate({ operation: { type: 'read' }, seed: { parks: {}, queues: {} } })
    expect(afterRestart.queues.source?.map((entry: any) => entry.text)).toEqual(['survives failed save'])
    expect(afterRestart.queues.target).toBeUndefined()
    expect(afterRestart.parks).toEqual({ source: true })
  })

  it('restores authoritative queue state after a main-process coordinator restart', () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-composer-persistence-'))
    const store = createComposerPersistenceFileStore(path.join(directory, 'composer-persistence.v1.json'))

    try {
      const firstMain = new ComposerPersistenceCoordinator(store)

      firstMain.mutate({
        operation: {
          entry: { attachments: [], id: 'durable-entry', queuedAt: 1, text: 'survives restart' },
          scopeKey: 'scope-a',
          type: 'enqueue'
        },
        seed: { parks: {}, queues: {} }
      })

      const restartedMain = new ComposerPersistenceCoordinator(store)
      const restored = restartedMain.mutate({ operation: { type: 'read' }, seed: { parks: {}, queues: {} } })

      expect(restored.queues['scope-a'].map((entry: any) => entry.text)).toEqual(['survives restart'])
    } finally {
      fs.rmSync(directory, { force: true, recursive: true })
    }
  })
})
