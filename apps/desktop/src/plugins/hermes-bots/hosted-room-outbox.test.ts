import type { PluginContext } from '@hermes/plugin-sdk'
import { afterEach, describe, expect, it, vi } from 'vitest'

interface TestLockManager {
  request<T>(name: string, options: { mode: 'exclusive' }, callback: (lock: object) => Promise<T> | T): Promise<T>
}

function storageFor(values: Map<string, unknown>, { dropWrites = false } = {}) {
  return {
    get: async (key: string) => structuredClone(values.get(key) ?? null),
    set: async (key: string, value: unknown) => {
      if (!dropWrites) {
        values.set(key, structuredClone(value))
      }
    }
  } as unknown as PluginContext['storage']
}

function lockManager(): TestLockManager {
  const tails = new Map<string, Promise<void>>()

  return {
    request(name, _options, callback) {
      const previous = tails.get(name) || Promise.resolve()
      const result = previous.then(() => callback({}))

      tails.set(
        name,
        result.then(
          () => undefined,
          () => undefined
        )
      )

      return result
    }
  }
}

function command(commandId: string) {
  return {
    commandId,
    kind: 'send' as const,
    roomId: 'room-1',
    authorityId: 'install:home',
    connectionId: 'gateway-a',
    payload: { text: commandId }
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.resetModules()
})

describe('hosted Group Chat outbox persistence', () => {
  it('rejects a silent storage failure instead of acknowledging unsafe durability', async () => {
    const values = new Map<string, unknown>()
    const outbox = await import('./hosted-room-outbox')

    await expect(
      outbox.mutateHostedRoomOutbox(storageFor(values, { dropWrites: true }), {
        command: command('send-a'),
        type: 'enqueue'
      })
    ).rejects.toThrow('did not persist')
    await expect(outbox.readHostedRoomOutbox(storageFor(values))).resolves.toMatchObject({ commands: [] })
  })

  it('preserves concurrent writes from separate renderer module instances', async () => {
    const values = new Map<string, unknown>()
    const locks = lockManager()

    vi.stubGlobal('navigator', { locks })
    const first = await import('./hosted-room-outbox')

    vi.resetModules()
    const second = await import('./hosted-room-outbox')
    const storage = storageFor(values)

    await Promise.all([
      first.mutateHostedRoomOutbox(storage, {
        command: command('send-a'),
        type: 'enqueue'
      }),
      second.mutateHostedRoomOutbox(storage, {
        command: command('send-b'),
        type: 'enqueue'
      })
    ])

    await expect(first.readHostedRoomOutbox(storage)).resolves.toMatchObject({
      commands: [{ commandId: 'send-a' }, { commandId: 'send-b' }]
    })
  })

  it('keeps an active dispatch in flight until startup recovery', async () => {
    const values = new Map<string, unknown>()
    const outbox = await import('./hosted-room-outbox')
    const storage = storageFor(values)

    await outbox.mutateHostedRoomOutbox(storage, {
      command: command('send-a'),
      type: 'enqueue'
    })
    await expect(
      outbox.mutateHostedRoomOutbox(storage, {
        commandId: 'send-a',
        type: 'dispatch'
      })
    ).resolves.toMatchObject({ commands: [{ commandId: 'send-a', status: 'in-flight' }] })
    expect(values.get('hosted-room-outbox-v1')).toMatchObject({
      commands: [{ commandId: 'send-a', status: 'in-flight' }]
    })

    await expect(outbox.recoverHostedRoomOutbox(storage)).resolves.toMatchObject({
      commands: [{ commandId: 'send-a', status: 'pending' }]
    })
  })
})
