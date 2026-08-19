import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The global-remote share (backend routing case 3): every profile is served
// by the PRIMARY backend over one host, and getConnection() explicitly tags
// the shared descriptor with `sharedPrimary`. Dialing a second WebSocket at it
// used to fail over SSH (per-backend tunnel/ticket) and poison the active
// gateway with a closed socket — "Hermes gateway is not connected" for every
// profile except the primary. Pooled backends (own-remote override, local
// named profile) also carry `profile` for WS URL minting, so `profile` alone
// cannot identify the shared-primary route. These tests pin the fix: only a
// `sharedPrimary` descriptor activates the primary socket; a pooled descriptor
// that also carries `profile` must still dial its own socket.

const gatewayMocks = vi.hoisted(() => ({
  connect: vi.fn(async (_wsUrl: string): Promise<void> => {
    throw new Error('dialed a socket for a shared-primary profile')
  }),
  setConnection: vi.fn()
}))

vi.mock('@/hermes', () => ({
  setApiRequestConnection: vi.fn(),
  HermesGateway: class {
    connectionState = 'closed'
    connect = async (wsUrl: string): Promise<void> => {
      await gatewayMocks.connect(wsUrl)
      this.connectionState = 'open'
    }
    close = vi.fn()
    onEvent = vi.fn(() => () => {})
    onState = vi.fn(() => () => {})
  }
}))
vi.mock('@/store/session', () => ({
  setConnection: gatewayMocks.setConnection,
  setGatewayState: vi.fn()
}))
vi.mock('@/store/notify-baseline', () => ({ markNativeNotifyBaseline: vi.fn() }))

const {
  $gateway,
  closeSecondaryGateways,
  configureGatewayRegistry,
  ensureActiveGatewayOpen,
  ensureGatewayForProfile,
  setPrimaryGateway
} = await import('./gateway')

type DesktopStub = { getConnection: ReturnType<typeof vi.fn> }

function installDesktop(stub: DesktopStub): void {
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = stub
}

function makePrimary(): { connectionState: string } {
  // Only connectionState is consulted by setActive/isOpen for these paths.
  return { connectionState: 'open' }
}

beforeEach(() => {
  configureGatewayRegistry({
    onEvent: vi.fn(),
    primaryProfile: 'default'
  } as never)
})

afterEach(() => {
  closeSecondaryGateways()
  vi.clearAllMocks()
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('ensureGatewayForProfile under a shared global remote', () => {
  it('activates the primary socket for an explicitly shared-primary descriptor', async () => {
    const primary = makePrimary()
    setPrimaryGateway(primary as never, 'default')
    installDesktop({
      // Shared descriptor: primary connection tagged with the profile scope
      // AND the explicit sharedPrimary marker.
      getConnection: vi.fn(async () => ({ port: 4242, profile: 'venture', sharedPrimary: true, token: 't' }))
    })

    await ensureGatewayForProfile('venture')

    expect(gatewayMocks.connect).not.toHaveBeenCalled()
    expect($gateway.get()).toBe(primary)
  })

  it('dials the exact WebSocket URL for a pooled profile descriptor that carries profile', async () => {
    const primary = makePrimary()
    const remoteWsUrl = 'wss://remote.invalid/api/ws?token=fake-test-token'

    setPrimaryGateway(primary as never, 'default')
    installDesktop({
      // Pooled descriptor: carries `profile` for WS URL minting but is NOT
      // shared-primary (no marker) — it must dial its own socket, not reuse
      // the primary. This is the local named / own-remote profile case.
      getConnection: vi.fn(async () => ({
        authMode: 'token',
        baseUrl: 'https://remote.invalid',
        mode: 'remote',
        profile: 'worker',
        token: 'fake-test-token',
        wsUrl: remoteWsUrl
      }))
    })
    gatewayMocks.connect.mockResolvedValueOnce(undefined)

    await ensureGatewayForProfile('worker')

    expect(gatewayMocks.connect).toHaveBeenCalledOnce()
    expect(gatewayMocks.connect).toHaveBeenCalledWith(remoteWsUrl)
    expect($gateway.get()).not.toBe(primary)
  })

  it('declines a pooled profile activation until its socket can connect', async () => {
    const connection = {
      authMode: 'token',
      baseUrl: 'https://worker.invalid',
      mode: 'remote',
      profile: 'worker',
      token: 'fake-test-token',
      wsUrl: 'wss://worker.invalid/api/ws?token=fake-test-token'
    }

    const primary = makePrimary()
    const getConnection = vi.fn(async () => connection)

    setPrimaryGateway(primary as never, 'default')
    await ensureGatewayForProfile('default')
    installDesktop({ getConnection })
    gatewayMocks.connect.mockRejectedValueOnce(new Error('temporarily offline')).mockResolvedValueOnce(undefined)

    const first = await ensureGatewayForProfile('worker')

    expect(first).toBe(false)
    expect($gateway.get()).toBe(primary)
    expect(gatewayMocks.setConnection).not.toHaveBeenCalled()

    const retry = await ensureGatewayForProfile('worker')

    expect(retry).toBe(true)
    expect($gateway.get()).not.toBe(primary)
    expect(gatewayMocks.setConnection).toHaveBeenCalledOnce()
    expect(gatewayMocks.setConnection).toHaveBeenCalledWith(connection)
  })

  it('republishes the active descriptor when a reconnect reopens the socket', async () => {
    // Companion to the decline test above, and the reason this file still
    // imports ensureActiveGatewayOpen. Declining an activation whose socket
    // never opened is only half the contract: once a pooled profile IS
    // active, a socket that drops and is reopened must re-publish its
    // descriptor, or session state keeps pointing at a connection that is no
    // longer the live one. Nothing else in src/store covers that publish --
    // deleting it from reconnectSecondary leaves the whole directory green.
    const connection = {
      authMode: 'token',
      baseUrl: 'https://worker.invalid',
      mode: 'remote',
      profile: 'worker',
      token: 'fake-test-token',
      wsUrl: 'wss://worker.invalid/api/ws?token=fake-test-token'
    }

    const primary = makePrimary()

    setPrimaryGateway(primary as never, 'default')
    installDesktop({ getConnection: vi.fn(async () => connection) })
    gatewayMocks.connect.mockResolvedValue(undefined)

    expect(await ensureGatewayForProfile('worker')).toBe(true)
    expect(gatewayMocks.setConnection).toHaveBeenCalledOnce()

    // Drop the live socket the way a transport failure would, without
    // disturbing wantOpen/retained -- ensureActiveGatewayOpen is the path
    // that notices and redials.
    const active = $gateway.get() as unknown as { connectionState: string }

    active.connectionState = 'closed'

    await ensureActiveGatewayOpen()

    expect(gatewayMocks.setConnection).toHaveBeenCalledTimes(2)
    expect(gatewayMocks.setConnection).toHaveBeenLastCalledWith(connection)
  })
})
