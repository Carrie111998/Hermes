import fs from 'node:fs'
import path from 'node:path'

import type {
  ComposerPersistenceMigration,
  ComposerPersistenceState,
  ComposerPersistenceStore
} from './composer-queue-drain-ipc'

function parseState(raw: string): ComposerPersistenceState | null {
  try {
    const parsed: unknown = JSON.parse(raw)

    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return null
    }

    const candidate = parsed as { migrations?: unknown; parks?: unknown; queues?: unknown }

    if (
      !candidate.parks ||
      typeof candidate.parks !== 'object' ||
      Array.isArray(candidate.parks) ||
      !candidate.queues ||
      typeof candidate.queues !== 'object' ||
      Array.isArray(candidate.queues)
    ) {
      return null
    }

    const migrations: Record<string, ComposerPersistenceMigration> = {}
    const parks: Record<string, true> = {}
    const queues: Record<string, any[]> = {}

    for (const [scopeKey, parked] of Object.entries(candidate.parks)) {
      if (parked === true) {
        parks[scopeKey] = true
      }
    }

    for (const [scopeKey, queue] of Object.entries(candidate.queues)) {
      if (Array.isArray(queue)) {
        queues[scopeKey] = queue
      }
    }

    if (candidate.migrations && typeof candidate.migrations === 'object' && !Array.isArray(candidate.migrations)) {
      for (const [transactionId, value] of Object.entries(candidate.migrations)) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) {
          continue
        }

        const migration = value as Partial<ComposerPersistenceMigration>

        if (
          typeof migration.fromScopeKey === 'string' &&
          typeof migration.toScopeKey === 'string' &&
          typeof migration.sourceParked === 'boolean' &&
          typeof migration.targetParked === 'boolean' &&
          Array.isArray(migration.sourceQueue) &&
          Array.isArray(migration.targetQueue)
        ) {
          migrations[transactionId] = {
            fromScopeKey: migration.fromScopeKey,
            sourceParked: migration.sourceParked,
            sourceQueue: migration.sourceQueue,
            targetParked: migration.targetParked,
            targetQueue: migration.targetQueue,
            toScopeKey: migration.toScopeKey
          }
        }
      }
    }

    return { migrations, parks, queues }
  } catch {
    return null
  }
}

/** Main-process durable backing for the synchronous composer coordinator. */
export function createComposerPersistenceFileStore(filePath: string): ComposerPersistenceStore {
  return {
    load: () => {
      try {
        return parseState(fs.readFileSync(filePath, 'utf8'))
      } catch {
        return null
      }
    },
    save: state => {
      const directory = path.dirname(filePath)
      const temporary = `${filePath}.${process.pid}.tmp`

      fs.mkdirSync(directory, { recursive: true })
      fs.writeFileSync(temporary, `${JSON.stringify(state)}\n`, 'utf8')
      fs.renameSync(temporary, filePath)
    }
  }
}
