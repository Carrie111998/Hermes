import { vi } from 'vitest'

interface LoadOptions {
  profile?: string
  request?: (method: string, params: Record<string, any>) => unknown
  openSession?: (...args: any[]) => unknown
}

export function plain<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

export function deferred<T = unknown>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

export async function loadBotSessions({ profile = 'ops', request, openSession }: LoadOptions = {}) {
  vi.resetModules()

  const calls: any[][] = []
  const invalidations: unknown[] = []
  const notifications: unknown[] = []

  const host = {
    state: {
      profile: { get: () => profile, listen: () => undefined },
      gateway: { get: () => 'open', listen: () => undefined }
    },
    request: async (method: string, params: Record<string, any>) => {
      calls.push([method, params])

      return request?.(method, params)
    },
    openSession: async (...args: any[]) => {
      calls.push(['openSession', ...args])

      return openSession?.(...args)
    },
    newChat: (...args: any[]) => calls.push(['newChat', ...args]),
    notify: (value: unknown) => notifications.push(value),
    notifyError: () => undefined
  }

  const queryClient = {
    invalidateQueries: (value: unknown) => invalidations.push(value)
  }

  vi.doMock('@hermes/plugin-sdk', async importOriginal => {
    const actual = await importOriginal<Record<string, unknown>>()

    return { ...actual, Button: 'Button', host, queryClient }
  })

  const modules = import.meta.glob<{ default: { testing?: any } }>('../plugin.js')
  const load = Object.values(modules)[0]

  if (!load) {
    throw new Error('Bot Mode plugin module was not discovered')
  }

  Reflect.set(globalThis, '__HERMES_BOTS_TEST__', true)
  let loaded

  try {
    loaded = await load()
  } finally {
    Reflect.deleteProperty(globalThis, '__HERMES_BOTS_TEST__')
  }

  if (!loaded.default.testing) {
    throw new Error('Bot Mode test runtime was not exposed')
  }

  return {
    __sessions: loaded.default.testing,
    calls,
    invalidations,
    notifications
  }
}
