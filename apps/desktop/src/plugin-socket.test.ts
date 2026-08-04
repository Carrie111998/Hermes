import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { pluginSocket, setApiRequestProfile } from './hermes'

class FakeWebSocket {
  static opened: FakeWebSocket[] = []

  onclose: null | (() => void) = null
  onmessage: null | ((event: { data: unknown }) => void) = null
  onopen: null | (() => void) = null
  closed = false

  constructor(readonly url: string) {
    FakeWebSocket.opened.push(this)
  }

  close(): void {
    this.closed = true
    this.onclose?.()
  }
}

describe('pluginSocket', () => {
  const getConnection = vi.fn(async (profile: null | string) => ({
    authMode: 'token',
    baseUrl: 'eva-managed://customer-one',
    profile,
    token: '',
    wsUrl: 'ws://127.0.0.1:4123/api/ws?ticket=stale'
  }))

  const getGatewayWsUrl = vi.fn(
    async (_profile: null | string, endpointPath: string) =>
      `ws://127.0.0.1:4123${endpointPath}${endpointPath.includes('?') ? '&' : '?'}ticket=fresh`
  )

  beforeEach(() => {
    FakeWebSocket.opened = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = {
      getConnection,
      getGatewayWsUrl
    }
    getConnection.mockReset()
    getConnection.mockImplementation(async profile => ({
      authMode: 'token',
      baseUrl: 'eva-managed://customer-one',
      profile,
      token: '',
      wsUrl: 'ws://127.0.0.1:4123/api/ws?ticket=stale'
    }))
    getGatewayWsUrl.mockReset()
    getGatewayWsUrl.mockImplementation(
      async (_profile, endpointPath) =>
        `ws://127.0.0.1:4123${endpointPath}${endpointPath.includes('?') ? '&' : '?'}ticket=fresh`
    )
  })

  afterEach(() => {
    vi.useRealTimers()
    setApiRequestProfile(null)
    delete (window as { hermesDesktop?: unknown }).hermesDesktop
    vi.unstubAllGlobals()
  })

  it('mints the exact plugin endpoint for the active profile', async () => {
    setApiRequestProfile('research')
    const dispose = pluginSocket('kanban', '/events?mode=live', vi.fn())

    await vi.waitFor(() => expect(FakeWebSocket.opened).toHaveLength(1))
    expect(getConnection).toHaveBeenCalledWith('research')
    expect(getGatewayWsUrl).toHaveBeenCalledWith('research', '/api/plugins/kanban/events?mode=live')
    expect(FakeWebSocket.opened[0].url).toBe('ws://127.0.0.1:4123/api/plugins/kanban/events?mode=live&ticket=fresh')
    dispose()
  })

  it('rejects plugin ids that could escape their namespace', () => {
    expect(() => pluginSocket('../other', '/events', vi.fn())).toThrow(/invalid plugin id/)
    expect(getGatewayWsUrl).not.toHaveBeenCalled()
  })

  it('closes the old socket and reconnects immediately for the new active profile', async () => {
    setApiRequestProfile('research')
    const dispose = pluginSocket('kanban', '/events', vi.fn())
    await vi.waitFor(() => expect(getGatewayWsUrl).toHaveBeenCalledTimes(1))
    await vi.waitFor(() => expect(FakeWebSocket.opened).toHaveLength(1))

    setApiRequestProfile('operations')
    expect(FakeWebSocket.opened[0].closed).toBe(true)
    await vi.waitFor(() => expect(FakeWebSocket.opened).toHaveLength(2))
    expect(getConnection.mock.calls.map(call => call[0])).toEqual(['research', 'operations'])
    expect(getGatewayWsUrl.mock.calls.at(-1)?.[0]).toBe('operations')
    dispose()
  })

  it('keeps retrying when connection or endpoint-ticket minting fails before a socket exists', async () => {
    vi.useFakeTimers()
    getGatewayWsUrl.mockRejectedValueOnce(new Error('ticket broker unavailable'))
    const dispose = pluginSocket('kanban', '/events', vi.fn())
    await vi.advanceTimersByTimeAsync(0)

    expect(getGatewayWsUrl).toHaveBeenCalledTimes(1)
    expect(FakeWebSocket.opened).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(500)

    expect(getGatewayWsUrl).toHaveBeenCalledTimes(2)
    expect(FakeWebSocket.opened).toHaveLength(1)
    dispose()
  })
})
