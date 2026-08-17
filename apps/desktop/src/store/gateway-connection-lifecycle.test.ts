import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Connection lifecycle for registry-scoped secondary gateways:
//
//  1. Removing a connection must dispose its secondaries — remote/cloud
//     sources have no local process whose death would drop the socket, so
//     without an explicit dispose the WebSocket stays open and streams ghost
//     events until page reload.
//  2. A materially edited connection re-dials so fresh sockets target the
//     NEW endpoint.
//  3. When the Electron main reports the connection no longer exists
//     (`No connection with id`), the reconnect loop fail-stops and evicts
//     the entry instead of retrying forever.

const gatewayMocks = vi.hoisted(() => {
  const instances: { close: ReturnType<typeof vi.fn>; connectionState: string }[] = []

  return {
    connect: vi.fn(async (_wsUrl: string): Promise<void> => undefined),
    instances
  }
})

const onActiveConnectionInvalidated = vi.fn()

vi.mock('@/hermes', () => ({
  getApiRequestProfile: vi.fn(() => 'default'),
  setApiRequestConnection: vi.fn(),
  HermesGateway: class {
    connectionState = 'closed'
    close = vi.fn(() => {
      this.connectionState = 'closed'
    })
    connect = async (wsUrl: string): Promise<void> => {
      await gatewayMocks.connect(wsUrl)
      this.connectionState = 'open'
    }
    onEvent = vi.fn(() => () => {})
    onState = vi.fn(() => () => {})
    constructor() {
      gatewayMocks.instances.push(this as never)
    }
  }
}))
vi.mock('@/store/session', () => ({
  setConnection: vi.fn(),
  setGatewayState: vi.fn()
}))
vi.mock('@/store/notify-baseline', () => ({ markNativeNotifyBaseline: vi.fn() }))

const {
  $activeGatewayIdentity,
  $gateway,
  closeSecondaryGateways,
  configureGatewayRegistry,
  disposeSecondariesForConnection,
  ensureActiveGatewayOpen,
  ensureGatewayForAgent,
  ensureGatewayForProfile,
  isActivePrimary,
  setPrimaryGateway
} = await import('./gateway')

function installDesktop(stub: Record<string, unknown>): void {
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = stub
}

function descriptorFor(connectionId: string, profile: string) {
  return {
    authMode: 'token',
    baseUrl: `https://${connectionId}.invalid`,
    mode: 'remote',
    profile,
    token: 'fake-test-token',
    wsUrl: `wss://${connectionId}.invalid/api/ws?profile=${profile}`
  }
}

beforeEach(() => {
  onActiveConnectionInvalidated.mockClear()
  configureGatewayRegistry({ onActiveConnectionInvalidated, onEvent: vi.fn() } as never)
  setPrimaryGateway({ connectionState: 'open' } as never, 'default')
})

afterEach(() => {
  closeSecondaryGateways()
  gatewayMocks.instances.length = 0
  vi.clearAllMocks()
  vi.useRealTimers()
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('disposeSecondariesForConnection', () => {
  it('closes and evicts every secondary scoped to the removed connection', async () => {
    const getConnectionFor = vi.fn(async ({ connectionId, profile }: { connectionId: string; profile: string }) =>
      descriptorFor(connectionId, profile)
    )

    installDesktop({ getConnectionFor })

    await ensureGatewayForAgent('office', 'default')
    await ensureGatewayForAgent('homelab', 'default')
    await ensureGatewayForAgent('homelab', 'work')

    expect(gatewayMocks.instances).toHaveLength(3)

    disposeSecondariesForConnection('homelab')

    // Both homelab sockets closed; the office socket untouched.
    expect(gatewayMocks.instances[0].close).not.toHaveBeenCalled()
    expect(gatewayMocks.instances[1].close).toHaveBeenCalledOnce()
    expect(gatewayMocks.instances[2].close).toHaveBeenCalledOnce()

    // No redial for a removal.
    expect(getConnectionFor).toHaveBeenCalledTimes(3)
    expect(isActivePrimary()).toBe(true)
    expect($gateway.get()).not.toBeNull()
    expect($activeGatewayIdentity.get()).toEqual({ connectionId: null, profile: 'default' })
  })

  it('re-dials disposed secondaries when redial is requested (material edit)', async () => {
    const getConnectionFor = vi.fn(async ({ connectionId, profile }: { connectionId: string; profile: string }) =>
      descriptorFor(connectionId, profile)
    )

    installDesktop({ getConnectionFor })

    await ensureGatewayForAgent('homelab', 'default')
    expect(gatewayMocks.connect).toHaveBeenCalledTimes(1)

    disposeSecondariesForConnection('homelab', { redial: true })

    // The redial runs async through the normal open path — flush it.
    await vi.waitFor(() => {
      expect(gatewayMocks.connect).toHaveBeenCalledTimes(2)
    })

    // Old socket closed, fresh descriptor fetched and the active route
    // atomically republishes the replacement socket.
    expect(gatewayMocks.instances[0].close).toHaveBeenCalledOnce()
    expect(getConnectionFor).toHaveBeenCalledTimes(2)
    expect($gateway.get()).toBe(gatewayMocks.instances[1])
    expect($activeGatewayIdentity.get()).toEqual({ connectionId: 'homelab', profile: 'default' })
  })

  it('keeps outbound gateway work unavailable when an active material-edit redial cannot resolve', async () => {
    vi.useFakeTimers()

    const getConnectionFor = vi
      .fn()
      .mockResolvedValueOnce(descriptorFor('homelab', 'default'))
      .mockRejectedValueOnce(new Error('edited descriptor is invalid'))
      .mockResolvedValue(descriptorFor('homelab', 'default'))

    installDesktop({ getConnectionFor })

    await ensureGatewayForAgent('homelab', 'default')

    disposeSecondariesForConnection('homelab', { redial: true })

    expect(isActivePrimary()).toBe(false)
    expect($gateway.get()).toBeNull()
    expect($activeGatewayIdentity.get()).toEqual({ connectionId: 'homelab', profile: 'default' })

    await vi.runAllTimersAsync()

    expect(getConnectionFor).toHaveBeenCalledTimes(3)
    expect($gateway.get()).toBe(gatewayMocks.instances[1])
    expect($activeGatewayIdentity.get()).toEqual({ connectionId: 'homelab', profile: 'default' })
  })

  it('is a no-op for blank or unknown connection ids', async () => {
    installDesktop({
      getConnectionFor: vi.fn(async ({ connectionId, profile }: { connectionId: string; profile: string }) =>
        descriptorFor(connectionId, profile)
      )
    })

    await ensureGatewayForAgent('homelab', 'default')

    disposeSecondariesForConnection('')
    disposeSecondariesForConnection('ghost')

    expect(gatewayMocks.instances[0].close).not.toHaveBeenCalled()
  })
})

describe('reconnect fail-stop on a removed connection', () => {
  it('evicts the entry instead of retrying when the registry no longer knows the id', async () => {
    const getConnectionFor = vi
      .fn()
      .mockResolvedValueOnce(descriptorFor('homelab', 'default'))
      .mockRejectedValue(new Error('No connection with id "homelab".'))

    installDesktop({ getConnectionFor })

    await ensureGatewayForAgent('homelab', 'default')
    expect(gatewayMocks.instances).toHaveLength(1)

    // Simulate the socket dropping after the connection was removed.
    const socket = gatewayMocks.instances[0] as unknown as { connectionState: string }
    socket.connectionState = 'closed'

    // ensureActiveGatewayOpen drives reconnectSecondary for the active scope.
    const result = await ensureActiveGatewayOpen()

    expect(result).toBeNull()
    // Fail-stop: the entry was disposed + evicted, so a second drive finds
    // nothing to retry (no further getConnectionFor calls).
    const callsAfterFailStop = getConnectionFor.mock.calls.length
    await ensureActiveGatewayOpen()
    expect(getConnectionFor.mock.calls.length).toBe(callsAfterFailStop)
    expect(isActivePrimary()).toBe(true)
    expect($gateway.get()).not.toBeNull()
    expect($activeGatewayIdentity.get()).toEqual({ connectionId: null, profile: 'default' })
    expect(onActiveConnectionInvalidated).toHaveBeenCalledWith('default', expect.any(Number))
  })

  it('keeps retrying on ordinary transport failures', async () => {
    const getConnectionFor = vi
      .fn()
      .mockResolvedValueOnce(descriptorFor('homelab', 'default'))
      .mockRejectedValueOnce(new Error('ECONNREFUSED'))
      .mockResolvedValue(descriptorFor('homelab', 'default'))

    installDesktop({ getConnectionFor })

    await ensureGatewayForAgent('homelab', 'default')

    const socket = gatewayMocks.instances[0] as unknown as { connectionState: string }
    socket.connectionState = 'closed'

    // First drive fails with a transport error → entry survives.
    await ensureActiveGatewayOpen()
    // Second drive succeeds against the surviving entry.
    const reopened = await ensureActiveGatewayOpen()

    expect(reopened).not.toBeNull()
  })
})

describe('reconnect activation races', () => {
  it('does not supersede a newer foreground profile activation when the old source reconnects first', async () => {
    let releaseReconnect: () => void = () => undefined

    const reconnectGate = new Promise<void>(resolve => {
      releaseReconnect = resolve
    })

    let releaseWorkerDescriptor: () => void = () => undefined

    const workerDescriptorGate = new Promise<void>(resolve => {
      releaseWorkerDescriptor = resolve
    })

    const getConnectionFor = vi.fn(async ({ connectionId, profile }: { connectionId: string; profile: string }) =>
      descriptorFor(connectionId, profile)
    )

    const getConnection = vi.fn(async (profile: null | string) => {
      if (profile === 'worker') {
        await workerDescriptorGate
      }

      return { port: 5151, profile, token: 'profile-token' }
    })

    installDesktop({ getConnection, getConnectionFor })
    await ensureGatewayForAgent('homelab', 'default')

    const sourceGateway = gatewayMocks.instances[0] as unknown as { connectionState: string }
    sourceGateway.connectionState = 'closed'
    gatewayMocks.connect.mockImplementationOnce(async () => reconnectGate)

    const reconnect = ensureActiveGatewayOpen()
    await vi.waitFor(() => expect(gatewayMocks.connect).toHaveBeenCalledTimes(2))

    const activateWorker = ensureGatewayForProfile('worker')
    await vi.waitFor(() => expect(getConnection).toHaveBeenCalledWith('worker'))

    releaseReconnect()
    await reconnect
    releaseWorkerDescriptor()
    await activateWorker

    expect($gateway.get()).toBe(gatewayMocks.instances[1])
    expect($activeGatewayIdentity.get()).toEqual({ connectionId: null, profile: 'worker' })
  })
})
