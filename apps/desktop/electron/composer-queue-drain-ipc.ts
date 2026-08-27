import { ComposerQueueDrainArbiter } from './composer-queue-drain-arbiter'

interface QueueDrainSender {
  id: number
  once: (event: string, listener: () => void) => unknown
}

interface QueueDrainIpcEvent {
  returnValue: unknown
  sender: QueueDrainSender
}

interface QueueDrainIpcMain {
  on: (...args: any[]) => unknown
}

export interface ComposerPersistenceState {
  migrations?: Record<string, ComposerPersistenceMigration>
  parks: Record<string, true>
  queues: Record<string, any[]>
}

export interface ComposerPersistenceMigration {
  fromScopeKey: string
  sourceParked: boolean
  sourceQueue: any[]
  targetParked: boolean
  targetQueue: any[]
  toScopeKey: string
}

interface ComposerPersistenceResponse extends ComposerPersistenceState {
  result?: unknown
}

export interface ComposerPersistenceStore {
  load: () => ComposerPersistenceState | null
  save: (state: ComposerPersistenceState) => void
}

export class ComposerPersistenceCoordinator {
  private state: ComposerPersistenceState | null = null

  constructor(private readonly store?: ComposerPersistenceStore) {}

  mutate(request: any): ComposerPersistenceResponse {
    if (!this.state) {
      let persisted: ComposerPersistenceState | null = null

      try {
        persisted = this.store?.load() ?? null
      } catch {
        // Fall back to the renderer's legacy localStorage seed.
      }

      this.state = persisted ?? {
        migrations: {},
        parks: { ...(request?.seed?.parks ?? {}) },
        queues: Object.fromEntries(
          Object.entries(request?.seed?.queues ?? {}).map(([scopeKey, queue]) => [
            scopeKey,
            Array.isArray(queue) ? [...queue] : []
          ])
        )
      }
    }

    const currentState = this.state

    const nextState: ComposerPersistenceState = {
      migrations: Object.fromEntries(
        Object.entries(currentState.migrations ?? {}).map(([transactionId, migration]) => [
          transactionId,
          {
            ...migration,
            sourceQueue: [...migration.sourceQueue],
            targetQueue: [...migration.targetQueue]
          }
        ])
      ),
      parks: { ...currentState.parks },
      queues: Object.fromEntries(Object.entries(currentState.queues).map(([key, queue]) => [key, [...queue]]))
    }

    const operation = request?.operation
    const requestedScopeKey = typeof operation?.scopeKey === 'string' ? operation.scopeKey.trim() : ''

    const reservedMigration = Object.values(nextState.migrations ?? {}).find(
      migration => migration.fromScopeKey === requestedScopeKey
    )

    const scopeKey = reservedMigration?.toScopeKey ?? requestedScopeKey
    let result: unknown

    if (operation?.type === 'enqueue' && scopeKey && operation.entry) {
      nextState.queues[scopeKey] = [...(nextState.queues[scopeKey] ?? []), operation.entry]
      delete nextState.parks[scopeKey]
      result = operation.entry
    } else if (operation?.type === 'dequeue' && scopeKey) {
      const [head, ...rest] = nextState.queues[scopeKey] ?? []

      result = head ?? null

      if (head && rest.length) {
        nextState.queues[scopeKey] = rest
      } else if (head) {
        delete nextState.queues[scopeKey]
        delete nextState.parks[scopeKey]
      }
    } else if (operation?.type === 'update' && scopeKey && typeof operation.entryId === 'string') {
      let changed = false

      nextState.queues[scopeKey] = (nextState.queues[scopeKey] ?? []).map(entry => {
        if (entry?.id !== operation.entryId) {
          return entry
        }

        if (entry.text === operation.text && !Array.isArray(operation.attachments)) {
          return entry
        }

        changed = true
        const { displayText: _dropped, ...rest } = entry

        return {
          ...rest,
          text: operation.text,
          ...(Array.isArray(operation.attachments) ? { attachments: operation.attachments } : {})
        }
      })
      result = changed
    } else if (operation?.type === 'remove' && scopeKey && typeof operation.entryId === 'string') {
      const current = nextState.queues[scopeKey] ?? []
      const next = current.filter(entry => entry?.id !== operation.entryId)

      result = next.length !== current.length

      if (next.length) {
        nextState.queues[scopeKey] = next
      } else {
        delete nextState.queues[scopeKey]
        delete nextState.parks[scopeKey]
      }
    } else if (operation?.type === 'promote' && scopeKey && typeof operation.entryId === 'string') {
      const queue = nextState.queues[scopeKey] ?? []
      const index = queue.findIndex(entry => entry?.id === operation.entryId)

      result = index > 0

      if (index > 0) {
        const entry = queue[index]
        nextState.queues[scopeKey] = [entry, ...queue.slice(0, index), ...queue.slice(index + 1)]
      }
    } else if (operation?.type === 'clear' && scopeKey) {
      delete nextState.queues[scopeKey]
      delete nextState.parks[scopeKey]
    } else if (operation?.type === 'park' && scopeKey) {
      result = (nextState.queues[scopeKey]?.length ?? 0) > 0

      if (result) {
        nextState.parks[scopeKey] = true
      }
    } else if (operation?.type === 'unpark' && scopeKey) {
      delete nextState.parks[scopeKey]
    } else if (operation?.type === 'rollback-migrate' && typeof operation.transactionId === 'string') {
      const migration = nextState.migrations?.[operation.transactionId]

      if (migration) {
        const expectedTargetQueue = [...migration.targetQueue, ...migration.sourceQueue]
        const sourceQueueUnchanged = (nextState.queues[migration.fromScopeKey]?.length ?? 0) === 0

        const targetQueueUnchanged =
          JSON.stringify(nextState.queues[migration.toScopeKey] ?? []) === JSON.stringify(expectedTargetQueue)

        const parksUnchanged =
          Boolean(nextState.parks[migration.fromScopeKey]) === false &&
          Boolean(nextState.parks[migration.toScopeKey]) === (migration.sourceParked || migration.targetParked)

        if (!sourceQueueUnchanged || !targetQueueUnchanged || !parksUnchanged) {
          throw new Error('Composer persistence changed after the handoff commit; refusing destructive rollback')
        }

        if (migration.sourceQueue.length) {
          nextState.queues[migration.fromScopeKey] = [...migration.sourceQueue]
        } else {
          delete nextState.queues[migration.fromScopeKey]
        }

        if (migration.targetQueue.length) {
          nextState.queues[migration.toScopeKey] = [...migration.targetQueue]
        } else {
          delete nextState.queues[migration.toScopeKey]
        }

        if (migration.sourceParked) {
          nextState.parks[migration.fromScopeKey] = true
        } else {
          delete nextState.parks[migration.fromScopeKey]
        }

        if (migration.targetParked) {
          nextState.parks[migration.toScopeKey] = true
        } else {
          delete nextState.parks[migration.toScopeKey]
        }

        delete nextState.migrations?.[operation.transactionId]
        result = true
      } else {
        result = false
      }
    } else if (operation?.type === 'finalize-migrate' && typeof operation.transactionId === 'string') {
      result = Boolean(nextState.migrations?.[operation.transactionId])
      delete nextState.migrations?.[operation.transactionId]
    } else if (operation?.type === 'migrate') {
      const from = typeof operation.fromScopeKey === 'string' ? operation.fromScopeKey.trim() : ''
      const to = typeof operation.toScopeKey === 'string' ? operation.toScopeKey.trim() : ''
      const transactionId = typeof operation.transactionId === 'string' ? operation.transactionId.trim() : ''
      const existingMigration = transactionId ? nextState.migrations?.[transactionId] : undefined

      const competingScopeReservation = Object.entries(nextState.migrations ?? {}).find(
        ([reservedTransactionId, migration]) =>
          reservedTransactionId !== transactionId &&
          [migration.fromScopeKey, migration.toScopeKey].some(scopeKey => scopeKey === from || scopeKey === to)
      )

      const pending = from ? (nextState.queues[from] ?? []) : []

      if (
        existingMigration &&
        (existingMigration.fromScopeKey !== from || existingMigration.toScopeKey !== to)
      ) {
        throw new Error('Composer persistence transaction identity collision')
      }

      if (competingScopeReservation) {
        if (competingScopeReservation[1].fromScopeKey === from) {
          throw new Error('Composer persistence source is reserved by another handoff')
        }

        throw new Error('Composer persistence scope is reserved by another handoff')
      }

      result = existingMigration ? existingMigration.sourceQueue.length > 0 : Boolean(from && to && from !== to && pending.length)

      if (!existingMigration && from && to && from !== to && transactionId) {
        nextState.migrations![transactionId] = {
          fromScopeKey: from,
          sourceParked: Boolean(nextState.parks[from]),
          sourceQueue: [...pending],
          targetParked: Boolean(nextState.parks[to]),
          targetQueue: [...(nextState.queues[to] ?? [])],
          toScopeKey: to
        }
      }

      if (!existingMigration && result) {
        nextState.queues[to] = [...(nextState.queues[to] ?? []), ...pending]
        delete nextState.queues[from]

        if (nextState.parks[from]) {
          nextState.parks[to] = true
        }

        delete nextState.parks[from]
      }
    }

    if (operation?.type !== 'read') {
      this.store?.save(nextState)
      this.state = nextState
    }

    return {
      parks: { ...nextState.parks },
      queues: Object.fromEntries(Object.entries(nextState.queues).map(([key, queue]) => [key, [...queue]])),
      result
    }
  }
}

export function registerComposerQueueDrainIpc(
  ipcMain: QueueDrainIpcMain,
  arbiter = new ComposerQueueDrainArbiter(),
  persistence = new ComposerPersistenceCoordinator()
): ComposerQueueDrainArbiter {
  const trackedOwners = new Set<number>()

  ipcMain.on('hermes:composer-persistence:mutate', (event: QueueDrainIpcEvent, request: any) => {
    event.returnValue = persistence.mutate(request)
  })

  ipcMain.on('hermes:composer-queue-drain:begin', (event: QueueDrainIpcEvent, request: any) => {
    const ownerId = event.sender.id

    if (!trackedOwners.has(ownerId)) {
      trackedOwners.add(ownerId)
      let released = false

      const release = () => {
        if (released) {
          return
        }

        released = true
        trackedOwners.delete(ownerId)
        arbiter.releaseOwner(ownerId)
      }

      event.sender.once('render-process-gone', release)
      event.sender.once('destroyed', release)
    }

    event.returnValue = arbiter.begin(
      typeof request?.scopeKey === 'string' ? request.scopeKey : '',
      typeof request?.entryId === 'string' ? request.entryId : '',
      ownerId
    )
  })

  ipcMain.on('hermes:composer-queue-drain:excluded', (event: QueueDrainIpcEvent, request: any) => {
    event.returnValue = arbiter.excluded(
      typeof request?.scopeKey === 'string' ? request.scopeKey : '',
      typeof request?.entryId === 'string' ? request.entryId : ''
    )
  })

  ipcMain.on('hermes:composer-queue-drain:handoff', (event: QueueDrainIpcEvent, request: any) => {
    event.returnValue = arbiter.handoff(
      typeof request?.fromScopeKey === 'string' ? request.fromScopeKey : '',
      typeof request?.toScopeKey === 'string' ? request.toScopeKey : '',
      typeof request?.transactionId === 'string' ? request.transactionId : undefined
    )
  })

  ipcMain.on('hermes:composer-queue-drain:rollback-handoff', (event: QueueDrainIpcEvent, transactionId: unknown) => {
    event.returnValue = arbiter.rollbackHandoff(typeof transactionId === 'string' ? transactionId : '')
  })

  ipcMain.on('hermes:composer-queue-drain:finalize-handoff', (event: QueueDrainIpcEvent, transactionId: unknown) => {
    event.returnValue = arbiter.finalizeHandoff(typeof transactionId === 'string' ? transactionId : '')
  })

  ipcMain.on('hermes:composer-queue-drain:finish', (event: QueueDrainIpcEvent, token: unknown) => {
    event.returnValue = arbiter.finish(typeof token === 'number' ? token : -1, event.sender.id)
  })

  return arbiter
}
