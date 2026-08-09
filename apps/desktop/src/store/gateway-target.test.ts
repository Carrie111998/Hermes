import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

class FakeHermesGateway {
  connectionState: 'closed' | 'error' | 'open' = 'closed'
  readonly events: Array<{ payload?: unknown; session_id?: string; type: string }> = []
  readonly stateListeners = new Set<(state: 'closed' | 'error' | 'open') => void>()
  readonly eventListeners = new Set<(event: { payload?: unknown; session_id?: string; type: string }) => void>()

  async connect(_url: string): Promise<void> {
    this.connectionState = 'open'
    this.stateListeners.forEach(listener => listener('open'))
  }

  close(): void {
    this.connectionState = 'closed'
    this.stateListeners.forEach(listener => listener('closed'))
  }

  onEvent(listener: (event: { payload?: unknown; session_id?: string; type: string }) => void): () => void {
    this.eventListeners.add(listener)

    return () => this.eventListeners.delete(listener)
  }

  onState(listener: (state: 'closed' | 'error' | 'open') => void): () => void {
    this.stateListeners.add(listener)

    return () => this.stateListeners.delete(listener)
  }

  emitEvent(event: { payload?: unknown; session_id?: string; type: string }): void {
    this.events.push(event)
    this.eventListeners.forEach(listener => listener(event))
  }
}

const constructedGateways: FakeHermesGateway[] = []
const setGatewayState = vi.fn()
const markNativeNotifyBaseline = vi.fn()
const resolveGatewayWsUrl = vi.fn(async () => 'ws://synthetic-gateway.test/ws')
const getConnection = vi.fn(
  async (_profile?: string | null, _options?: { localOnly?: boolean; remoteOnly?: boolean }) =>
    ({ baseUrl: 'https://synthetic-gateway.test', mode: 'remote', profile: 'default' }) as never
)
const touchBackend = vi.fn(async () => ({ ok: true }))

vi.mock('@hermes/shared', () => ({ resolveGatewayWsUrl }))
vi.mock('@/store/session', () => ({ setGatewayState }))
vi.mock('@/store/notify-baseline', () => ({ markNativeNotifyBaseline }))
vi.mock('@/lib/reconnect-backoff', () => ({ reconnectBackoffDelayMs: () => 1 }))
vi.mock('@/hermes', () => ({
  HermesGateway: class extends FakeHermesGateway {
    constructor() {
      super()
      constructedGateways.push(this)
    }
  }
}))

describe('gateway target-aware registry behavior', () => {
  beforeEach(() => {
    getConnection.mockClear()
    touchBackend.mockClear()
    setGatewayState.mockClear()
    markNativeNotifyBaseline.mockClear()
    resolveGatewayWsUrl.mockClear()
    constructedGateways.length = 0
    vi.stubGlobal('window', { hermesDesktop: { getConnection, touchBackend } })
  })

  afterEach(async () => {
    const gateway = await import('./gateway')

    gateway.closeSecondaryGateways()
    gateway.setPrimaryGateway(null)
    gateway.setPrimaryBackendMode('local')
    vi.unstubAllGlobals()
    vi.resetModules()
    vi.clearAllMocks()
  })

  it('keeps local and remote roots distinct when both profiles are default', async () => {
    const { gatewayTargetKey } = await import('./gateway')

    expect(gatewayTargetKey('default', { localOnly: true })).toBe('__local__:default')
    expect(gatewayTargetKey('default', { remoteOnly: true })).toBe('__remote__:default')
    expect(gatewayTargetKey('default')).toBe('default')
  })

  it('collapses the explicit remote default back to the primary when the primary backend is remote', async () => {
    const gateway = await import('./gateway')
    const primary = new FakeHermesGateway()

    primary.connectionState = 'open'
    gateway.setPrimaryGateway(primary as never, 'default')
    gateway.setPrimaryBackendMode('remote')

    await gateway.ensureGatewayForProfile('default', { remoteOnly: true })

    expect(gateway.activeGateway()).toBe(primary)
    expect(getConnection).not.toHaveBeenCalled()
    expect(constructedGateways).toHaveLength(0)
  })

  it('derives explicit retention keys for primary default-root events', async () => {
    const gateway = await import('./gateway')

    gateway.setPrimaryGateway(null, 'default')
    gateway.setPrimaryBackendMode('local')
    expect(gateway.primaryGatewayRetentionKey('default')).toBe('__local__:default')

    gateway.setPrimaryBackendMode('remote')
    expect(gateway.primaryGatewayRetentionKey('default')).toBe('__remote__:default')

    gateway.setPrimaryGateway(null, 'writer')
    expect(gateway.primaryGatewayRetentionKey('writer')).toBe('writer')
  })

  it('recreates an active remote-root secondary with remoteOnly when the cached entry is gone', async () => {
    const gateway = await import('./gateway')
    const primary = new FakeHermesGateway()

    primary.connectionState = 'open'
    gateway.setPrimaryGateway(primary as never, 'writer')
    gateway.setPrimaryBackendMode('local')

    await gateway.ensureGatewayForProfile('default', { remoteOnly: true })

    expect(getConnection).toHaveBeenCalledWith('default', { remoteOnly: true })

    getConnection.mockClear()
    touchBackend.mockClear()
    gateway.closeSecondaryGateways()

    const reopened = await gateway.ensureActiveGatewayOpen()

    expect(getConnection).toHaveBeenCalledWith('default', { remoteOnly: true })
    expect(touchBackend).toHaveBeenCalledWith('default', { remoteOnly: true })
    expect(reopened).toBeInstanceOf(FakeHermesGateway)
    expect(reopened).not.toBe(primary)
  })

  it('touches explicit local and remote root secondaries with their own target options', async () => {
    const gateway = await import('./gateway')
    const primary = new FakeHermesGateway()

    primary.connectionState = 'open'
    gateway.setPrimaryGateway(primary as never, 'writer')
    gateway.setPrimaryBackendMode('local')

    await gateway.ensureGatewayForProfile('default', { localOnly: true })
    await gateway.ensureGatewayForProfile('default', { remoteOnly: true })

    touchBackend.mockClear()
    gateway.touchSecondaryGateways()

    expect(touchBackend.mock.calls).toEqual(
      expect.arrayContaining([
        ['default', { localOnly: true }],
        ['default', { remoteOnly: true }]
      ])
    )
  })

  it('binds live default sessions to their explicit target key for retention', async () => {
    const gateway = await import('./gateway')

    gateway.rememberGatewayRuntimeTarget('runtime-redacted', '__remote__:default')
    gateway.bindStoredSessionGatewayTarget('runtime-redacted', 'stored-redacted')

    expect(gateway.sessionGatewayRetentionKey('default', 'stored-redacted')).toBe('__remote__:default')
    expect(gateway.sessionGatewayRetentionKey('writer', 'stored-redacted')).toBe('writer')
  })

  it('prunes only the explicitly kept default-root target', async () => {
    const gateway = await import('./gateway')
    const primary = new FakeHermesGateway()

    primary.connectionState = 'open'
    gateway.setPrimaryGateway(primary as never, 'writer')
    gateway.setPrimaryBackendMode('local')

    await gateway.ensureGatewayForProfile('default', { localOnly: true })
    await gateway.ensureGatewayForProfile('default', { remoteOnly: true })

    touchBackend.mockClear()
    gateway.pruneSecondaryGateways(new Set(['__remote__:default']))
    gateway.touchSecondaryGateways()

    expect(touchBackend.mock.calls).toEqual([['default', { remoteOnly: true }]])
  })
})
