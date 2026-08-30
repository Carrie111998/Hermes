import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const secondaryGateways: Array<{
  close: ReturnType<typeof vi.fn>
  connect: ReturnType<typeof vi.fn>
  connectionState: string
  request: ReturnType<typeof vi.fn>
}> = []

vi.mock('@/hermes', () => ({
  HermesGateway: class {
    connectionState = 'closed'
    connect = vi.fn(async () => {
      this.connectionState = 'open'
    })
    request = vi.fn(async (method: string, params: Record<string, unknown>) => {
      if (method === 'slash.exec') {
        return { output: '⊙ Goal (active, 1/20 turns): worker-owned goal' }
      }

      return {
        processes: [
          {
            command: 'worker-owned process',
            session_id: 'worker-process',
            status: 'running'
          }
        ],
        params
      }
    })
    close = vi.fn()
    onEvent = vi.fn(() => () => {})
    onState = vi.fn(() => () => {})

    constructor() {
      secondaryGateways.push(this)
    }
  },
  setApiRequestConnection: vi.fn(),
  setApiRequestProfile: vi.fn()
}))

const { $gateway, closeSecondaryGateways, configureGatewayRegistry, setPrimaryGateway } = await import('./gateway')
const { $goalsBySession, refreshSessionGoal } = await import('./goals')

const {
  $backgroundStatusBySession,
  reconcileBackgroundProcesses,
  refreshBackgroundProcesses,
  resetBackgroundPollingGuard,
  resetSessionBackground,
  stopBackgroundProcess
} = await import('./composer-status')

const { setSessions } = await import('./session')

const OWNER_SESSION_ID = 'session-owned-by-worker'
const LOCAL_SESSION_ID = 'session-on-current-gateway'

type PrimaryGateway = {
  connectionState: string
  request: ReturnType<typeof vi.fn>
}

function installDesktop(): void {
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {
    getConnection: vi.fn(async (profile: null | string) => ({
      port: profile ? 5151 : 4242,
      profile: profile || undefined,
      token: '[REDACTED]'
    })),
    touchBackend: vi.fn(async () => undefined)
  }
}

function makePrimary(): PrimaryGateway {
  return {
    connectionState: 'open',
    request: vi.fn(async (method: string) => {
      if (method === 'slash.exec') {
        return { output: 'No active goal. Set one with /goal <text>.' }
      }

      return { processes: [] }
    })
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(resolvePromise => {
    resolve = resolvePromise
  })

  return { promise, resolve }
}

function resetStores(): void {
  $gateway.set(null)
  $goalsBySession.set({})
  $backgroundStatusBySession.set({})
  setSessions([])
  resetBackgroundPollingGuard()
  closeSecondaryGateways()
  secondaryGateways.length = 0
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
}

beforeEach(() => {
  configureGatewayRegistry({ onEvent: vi.fn() })
  resetStores()
})

afterEach(() => {
  resetStores()
  vi.clearAllMocks()
})

describe('session status refresh routing', () => {
  it('routes goal refreshes to the session owner instead of the active gateway', async () => {
    const primary = makePrimary()
    setPrimaryGateway(primary as never, 'default')
    $gateway.set(primary as never)
    installDesktop()
    setSessions([{ id: OWNER_SESSION_ID, profile: 'worker' } as never])

    await refreshSessionGoal(OWNER_SESSION_ID)

    expect(primary.request).not.toHaveBeenCalled()
    expect(secondaryGateways).toHaveLength(1)
    expect(secondaryGateways[0].request).toHaveBeenCalledWith('slash.exec', {
      command: 'goal status',
      session_id: OWNER_SESSION_ID
    })
    expect($goalsBySession.get()[OWNER_SESSION_ID]).toMatchObject({
      status: 'active',
      title: 'worker-owned goal'
    })
  })

  it('routes background-process refreshes to the session owner instead of the active gateway', async () => {
    const primary = makePrimary()
    setPrimaryGateway(primary as never, 'default')
    $gateway.set(primary as never)
    installDesktop()
    setSessions([{ id: OWNER_SESSION_ID, profile: 'worker' } as never])

    await refreshBackgroundProcesses(OWNER_SESSION_ID)

    expect(primary.request).not.toHaveBeenCalled()
    expect(secondaryGateways).toHaveLength(1)
    expect(secondaryGateways[0].request).toHaveBeenCalledWith('process.list', {
      session_id: OWNER_SESSION_ID
    })
    expect($backgroundStatusBySession.get()[OWNER_SESSION_ID]).toMatchObject([
      { id: 'worker-process', state: 'running', title: 'worker-owned process' }
    ])
  })

  it('routes an interactive process kill to the session owner', async () => {
    const primary = makePrimary()
    setPrimaryGateway(primary as never, 'default')
    $gateway.set(primary as never)
    installDesktop()
    setSessions([{ id: OWNER_SESSION_ID, profile: 'worker' } as never])
    reconcileBackgroundProcesses(OWNER_SESSION_ID, [
      {
        command: 'kill worker process',
        session_id: 'kill-worker-process',
        status: 'running'
      }
    ])

    await stopBackgroundProcess(OWNER_SESSION_ID, 'kill-worker-process')

    expect(primary.request).not.toHaveBeenCalled()
    expect(secondaryGateways).toHaveLength(1)
    expect(secondaryGateways[0].request).toHaveBeenCalledWith('process.kill', {
      process_id: 'kill-worker-process',
      session_id: OWNER_SESSION_ID
    })
    expect($backgroundStatusBySession.get()[OWNER_SESSION_ID]).toBeUndefined()
  })

  it('routes rewind cleanup kills to the session owner', async () => {
    const primary = makePrimary()
    setPrimaryGateway(primary as never, 'default')
    $gateway.set(primary as never)
    installDesktop()
    setSessions([{ id: OWNER_SESSION_ID, profile: 'worker' } as never])
    reconcileBackgroundProcesses(OWNER_SESSION_ID, [
      {
        command: 'rewind worker process',
        session_id: 'rewind-worker-process',
        status: 'running'
      }
    ])

    resetSessionBackground(OWNER_SESSION_ID)
    await vi.waitFor(() => expect(secondaryGateways).toHaveLength(1))

    expect(primary.request).not.toHaveBeenCalled()
    expect(secondaryGateways[0].request).toHaveBeenCalledWith('process.kill', {
      process_id: 'rewind-worker-process',
      session_id: OWNER_SESSION_ID
    })
    expect($backgroundStatusBySession.get()[OWNER_SESSION_ID]).toBeUndefined()
  })
})

describe('session status refresh ordering', () => {
  it('keeps the newest goal refresh when an older response resolves later', async () => {
    const primary = makePrimary()
    const older = deferred<{ output: string }>()
    const newer = deferred<{ output: string }>()
    primary.request.mockImplementationOnce(() => older.promise).mockImplementationOnce(() => newer.promise)
    setPrimaryGateway(primary as never, 'default')
    $gateway.set(primary as never)

    const olderRefresh = refreshSessionGoal(LOCAL_SESSION_ID)
    const newerRefresh = refreshSessionGoal(LOCAL_SESSION_ID)

    newer.resolve({ output: '⊙ Goal (active, 2/20 turns): newest goal' })
    await newerRefresh
    older.resolve({ output: 'No active goal. Set one with /goal <text>.' })
    await olderRefresh

    expect($goalsBySession.get()[LOCAL_SESSION_ID]).toMatchObject({
      status: 'active',
      title: 'newest goal'
    })
  })

  it('keeps the newest background snapshot when an older response resolves later', async () => {
    const primary = makePrimary()
    const older = deferred<{ processes: Array<Record<string, unknown>> }>()
    const newer = deferred<{ processes: Array<Record<string, unknown>> }>()
    primary.request.mockImplementationOnce(() => older.promise).mockImplementationOnce(() => newer.promise)
    setPrimaryGateway(primary as never, 'default')
    $gateway.set(primary as never)

    const olderRefresh = refreshBackgroundProcesses(LOCAL_SESSION_ID)
    const newerRefresh = refreshBackgroundProcesses(LOCAL_SESSION_ID)

    newer.resolve({
      processes: [
        {
          command: 'newest process',
          session_id: 'newest-process',
          status: 'running'
        }
      ]
    })
    await newerRefresh
    older.resolve({ processes: [] })
    await olderRefresh

    expect($backgroundStatusBySession.get()[LOCAL_SESSION_ID]).toMatchObject([
      { id: 'newest-process', state: 'running', title: 'newest process' }
    ])
  })
})
